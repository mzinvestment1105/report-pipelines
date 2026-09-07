# -*- coding: utf-8 -*-
"""
テーマ点数の答え合わせ台帳（theme_score_history.parquet）を作る。

設計の正本: scratchpad/design/theme_early_detection_v3.md（2026-09-07 PM 承認）

なぜ要るか
----------
現行稼働の theme_radar（みんかぶ辞書ベースの当日テーマ検知）は、初動候補テーマの
判定 E5 を「ティアフォー 1 事例」でチューニングしたまま、点灯したテーマがその後
上がったかの答え合わせが 1 件もない（蓄積 1 営業日）。本スクリプトは
  (a) --backfill: 価格パネル（2022-01-04〜）へ theme_radar と同一の点数式を適用して
      毎営業日のテーマ点数史を作る
  (b) --daily: 当日分を追記する
の 2 モードで、その台帳を作る。

点数式の出所
------------
点数・按分・母集団足切り・非資金テーマ除外・Jaccard 統合は theme_radar.py から
**import して使う**（式をコピーして乖離させることを設計で禁止している）。本スクリプトは
theme_radar の関数を一切書き換えず、誌面出力にも触れない。

  theme_radar.stock_weight          w = log10(1+売買代金[億円]) × 騰落率の正の部分
  theme_radar.score_one_day         テーマへの寄与 = w / その銘柄の所属テーマ数
  theme_radar.load_theme_map        構成銘柄 100 以下・非資金テーマ除外の辞書
  theme_radar.merge_overlapping_themes  点灯銘柄集合の Jaccard >= 0.5 で 1 行へ統合
  theme_radar.stock_weight          プライムの上位 50 件絞り込みにも同じ重みを使う
  theme_radar.RADAR_MIN_TURNOVER_OKU / RADAR_MIN_MCAP_OKU / RADAR_PRIME_TOP_N
  theme_radar.EARLY_TOP_POOL / EARLY_MIN_NUP / EARLY_MOVE_PCT / EARLY_MIN_NUP3
  theme_radar.EARLY_MIN_TURN3_OKU   判定 E5 の 3 閾値
  （2026-09-07 PM 承認で代金閾値 100→200 億円。旧条件は D1_prev100 列に残す）
  theme_radar.HEAT_WINDOW_DAYS / ACCEL_DELTA_RATIO / SUSTAIN_MIN_CODES

後知恵の明記（報告書へ必ず転記すること）
--------------------------------------
1. テーマ辞書は 2026-09 のスナップショットを全期間へ適用する（構成が事後的）。
2. 価格パネルに上場廃止銘柄が無い（生存バイアス）。TOB 退場型・仕手崩れ型の大相場は
   構造的に見えない。
3. 時価総額は「最新の発行済株式数 × 調整後終値」で近似する。パネルの AdjClose は
   当日基準ではなく**現在の調整基準**に揃っているため、当時の発行済株式数を掛けると
   分割銘柄の時価総額が分割倍率だけ狂う。最新株数（同じく現在基準）と掛け合わせるのが
   基準の整合する唯一の組み合わせ。実測: 最新株数 × パネル終値は screening_master の
   MarketCap を誤差 20% 以内で 95.4% 再現する。

Reads : bi/outputs/analysis/theme_radar/price_turnover_panel.parquet
        bi/outputs/theme_master_minkabu.parquet
        bi/outputs/screening_master.parquet（市場区分・発行済株式数）
Writes: bi/outputs/analysis/theme_radar/theme_score_history.parquet
既存の本番ファイルは一切上書きしない。
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import theme_radar as tr  # noqa: E402  点数式の正本

OUTROOT = BASE_DIR / ".." / "outputs"
PANEL_STAR = (OUTROOT / "analysis/star_hunter/adj_close_panel.parquet").resolve()
PANEL_PT = (OUTROOT / "analysis/theme_radar/price_turnover_panel.parquet").resolve()
SCREENING_MASTER = (OUTROOT / "screening_master.parquet").resolve()
FIN_HISTORY = (OUTROOT / "financial_history_master.parquet").resolve()
HISTORY_OUT = (OUTROOT / "analysis/theme_radar/theme_score_history.parquet").resolve()

BACKFILL_START = "2022-01-04"

# D4 検知器（個別株・実証済み）: 出来高でなく売買代金の 20 日平均比で代替する。
# パネルに出来高（株数）が無いため、代金 = 出来高 × 株価 の関係から、株価が
# 当日 +5% 動いた程度では代金比と出来高比はほぼ同じ倍率になる（差は 5〜7%）。
# 出来高列が将来入ったら D4_USE_VOLUME を True にして切り替える。
D4_WINDOW = 20              # 平均を取る営業日数
D4_LOOKBACK = 20            # テーマ側で「直近何営業日の発火を数えるか」
D4_A_MULT, D4_A_RET = 3.0, 5.0   # 出来高 3 倍かつ +5% 高
D4_B_MULT, D4_B_RET = 5.0, 7.0   # 出来高 5 倍かつ +7% 高
D4_MIN_FIRES = 2            # テーマ内で何銘柄発火したら D4 とするか

# D5 / R4: 主役テーマの資金シェアの剥落
R4_LEAD_SHARE = 0.08        # 代金シェア 20 日平均がこれ以上なら「主役テーマ」
R4_BREAK_RATIO = 0.85       # その 5 日平均が 20 日平均のこの倍率以下なら「折れている」

D3_NEW_WINDOW = 10          # D3「新規」= 前 10 営業日の点数が 0

# D1 の旧閾値（2026-09-07 の PM 承認で E5 の代金閾値を 100→200 億円へ変更したため、
# 旧条件の点灯を D1_prev100 列として併記し、変更前後の履歴比較を可能にする）。
D1_PREV_TURN3_OKU = 100     # 旧 E5 の +3% 銘柄の売買代金合計の下限（億円）

# 先行リターン（--fill-forward）
FWD_HORIZONS = (5, 20, 60, 120)   # 何営業日先まで測るか
RALLY_WINDOW = 120                # 大相場ラベルの観測窓（営業日）
RALLY_THRESHOLD = 40.0            # 窓内の最大到達がこの % 以上なら大相場 = 1


# ---------------------------------------------------------------------------
# 価格パネル（銘柄 × 日: 調整後終値・売買代金）
# ---------------------------------------------------------------------------
def build_price_turnover_panel(force: bool = False) -> pd.DataFrame:
    """star_hunter の価格パネルへ市場区分・発行済株式数・時価総額を付けて保存する。

    star_hunter パネルは (Date, Code, AdjClose, Turnover) の 4 列。
    テーマ点数の母集団足切り（v14）には市場区分と時価総額が要るため、
    screening_master（市場区分・最新の発行済株式数）を結合して独立 parquet を作る。
    本番ファイルには一切書かない。
    """
    if PANEL_PT.exists() and not force:
        return pd.read_parquet(PANEL_PT)

    if not PANEL_STAR.exists():
        raise FileNotFoundError(f"価格パネルがありません: {PANEL_STAR}")
    pan = pd.read_parquet(PANEL_STAR)
    pan["Code"] = pan["Code"].astype(str)
    pan["Date"] = pd.to_datetime(pan["Date"])

    sm = pd.read_parquet(
        SCREENING_MASTER,
        columns=[
            "Code", "MarketCodeName", "MarketCap", "Close",
            "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock",
        ],
    )
    sm = sm.rename(columns={
        "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock":
            "SharesOutstanding",
    })
    sm["Code"] = sm["Code"].astype(str)
    sm = sm[["Code", "MarketCodeName", "SharesOutstanding"]].drop_duplicates("Code")

    out = pan.merge(sm, on="Code", how="left")
    # 前日終値から騰落率。分割は AdjClose 側で吸収済みなので単純な pct_change でよい。
    out = out.sort_values(["Code", "Date"]).reset_index(drop=True)
    out["PrevClose"] = out.groupby("Code")["AdjClose"].shift(1)
    out["DailyReturn"] = (out["AdjClose"] / out["PrevClose"] - 1.0) * 100.0
    # 時価総額（億円）= 最新の発行済株式数 × その日の調整後終値。基準の整合は
    # モジュール docstring の 3 を参照。
    out["MarketCapOku"] = out["SharesOutstanding"] * out["AdjClose"] / 1e8

    PANEL_PT.parent.mkdir(parents=True, exist_ok=True)
    tmp = PANEL_PT.with_suffix(".parquet.tmp")
    out.to_parquet(tmp, index=False)
    os.replace(tmp, PANEL_PT)
    return out


# ---------------------------------------------------------------------------
# D4 検知器（個別株の売買代金急増 × 高騰）
# ---------------------------------------------------------------------------
def compute_detector_fires(panel: pd.DataFrame) -> pd.DataFrame:
    """銘柄 × 日の検知器発火フラグ（fire_d4）を返す。

    出来高 3 倍 + 5% 高 / 5 倍 + 7% 高 を、売買代金の 20 日移動平均比で判定する
    （パネルに出来高列が無いため。docstring の D4 節を参照）。
    """
    df = panel[["Date", "Code", "Turnover", "DailyReturn"]].copy()
    df = df.sort_values(["Code", "Date"])
    g = df.groupby("Code")["Turnover"]
    # 当日を含めない 20 日平均（当日の急増そのものが平均を押し上げるのを防ぐ）
    df["turn_ma20"] = g.transform(lambda s: s.shift(1).rolling(D4_WINDOW, min_periods=10).mean())
    ratio = df["Turnover"] / df["turn_ma20"]
    ret = df["DailyReturn"]
    fire = (
        ((ratio >= D4_A_MULT) & (ret >= D4_A_RET))
        | ((ratio >= D4_B_MULT) & (ret >= D4_B_RET))
    )
    df["fire_d4"] = fire.fillna(False)
    return df[["Date", "Code", "fire_d4"]]


def rolling_fire_codes(fires: pd.DataFrame, dates: list) -> dict:
    """日 -> 「直近 D4_LOOKBACK 営業日に発火した銘柄集合」を返す。"""
    fired = fires[fires["fire_d4"]]
    by_day: dict = defaultdict(set)
    for d, c in zip(fired["Date"], fired["Code"]):
        by_day[pd.Timestamp(d)].add(str(c))
    out: dict = {}
    for i, d in enumerate(dates):
        lo = max(0, i - D4_LOOKBACK + 1)
        s: set = set()
        for dd in dates[lo:i + 1]:
            s |= by_day.get(dd, set())
        out[d] = s
    return out


# ---------------------------------------------------------------------------
# 1 営業日分のテーマ点数（theme_radar と同一ロジック）
# ---------------------------------------------------------------------------
def day_universe(day: pd.DataFrame) -> pd.DataFrame:
    """v14 の母集団足切りを適用する（theme_radar.extract_radar_universe と同一定義）。

    extract_radar_universe をそのまま呼ぶと make_mover_report の列名（Turnover /
    MarketCapOku / DailyReturn / MarketCodeName）を要求するので、パネル側の列名を
    その形に揃えてから渡す。閾値・プライム上位 50 の絞り込みも theme_radar の定数と
    関数をそのまま使う。
    """
    d = day.rename(columns={})  # 列名は build_price_turnover_panel で既に合わせてある
    return tr.extract_radar_universe(d)


def score_day(day_univ: pd.DataFrame, code_to_themes: dict, theme_size: dict) -> list[dict]:
    """母集団 DataFrame から theme_radar と同じ統合済みテーマ行のリストを返す。"""
    if day_univ is None or len(day_univ) == 0:
        return []
    records = [
        {
            "code": str(r.Code),
            "name": "",
            "return_pct": float(r.DailyReturn),
            "turnover": float(r.Turnover),
            "market": str(getattr(r, "MarketCodeName", "") or ""),
        }
        for r in day_univ.itertuples()
        if pd.notna(r.DailyReturn) and pd.notna(r.Turnover)
    ]
    scored = tr.score_one_day(records, code_to_themes)
    entries = [
        {"theme": t, "score": v["score"], "codes": v["codes"]}
        for t, v in scored.items()
        if len(v["codes"]) >= tr.MIN_CODES_FOR_ALERT
    ]
    return tr.merge_overlapping_themes(entries, theme_size)


# ---------------------------------------------------------------------------
# R4（主役テーマの資金シェア）
# ---------------------------------------------------------------------------
def theme_turnover_share(day_all: pd.DataFrame, code_to_themes: dict) -> dict:
    """テーマ -> その日の代金シェア（全構成銘柄の代金合計 ÷ 市場全体の代金）。

    点灯銘柄でなく**全構成銘柄**の代金を数える（設計書の指定）。多テーマ所属の
    銘柄は点数と同じく所属テーマ数で按分する（1 銘柄の代金が複数テーマで重複計上され、
    シェアの合計が 1 を大きく超えるのを防ぐ）。
    """
    total = float(pd.to_numeric(day_all["Turnover"], errors="coerce").sum())
    if not total or not np.isfinite(total) or total <= 0:
        return {}
    acc: dict = defaultdict(float)
    for code, turn in zip(day_all["Code"].astype(str),
                          pd.to_numeric(day_all["Turnover"], errors="coerce").fillna(0.0)):
        themes = code_to_themes.get(code)
        if not themes:
            continue
        share = float(turn) / len(themes)
        for t in themes:
            acc[t] += share
    return {t: v / total for t, v in acc.items()}


# ---------------------------------------------------------------------------
# backfill / daily 本体
# ---------------------------------------------------------------------------
def compute_history(panel: pd.DataFrame, dates: list) -> pd.DataFrame:
    """指定営業日リストについてテーマ点数史を計算して返す。"""
    code_to_themes, theme_size, stale, excluded = tr.load_theme_map()
    if not code_to_themes:
        raise RuntimeError("テーマ辞書が空です")
    print(f"[dict] テーマ {len(theme_size):,} 件 / 銘柄 {len(code_to_themes):,} 件 "
          f"/ 非資金テーマ除外 {excluded} 件", flush=True)

    fires = compute_detector_fires(panel)
    fire_by_day = rolling_fire_codes(fires, dates)

    by_date = {d: g for d, g in panel.groupby("Date", sort=True)}

    # --- 第 1 パス: 日ごとの点数とシェアを計算する ---
    score_hist: dict = defaultdict(dict)   # theme -> {date: score}
    share_hist: dict = defaultdict(dict)   # theme -> {date: share}
    rows_raw: list[dict] = []
    t0 = time.time()
    for i, d in enumerate(dates):
        day_all = by_date.get(d)
        if day_all is None or len(day_all) == 0:
            continue
        shares = theme_turnover_share(day_all, code_to_themes)
        for t, v in shares.items():
            share_hist[t][d] = v

        univ = day_universe(day_all)
        merged = score_day(univ, code_to_themes, theme_size)
        merged = sorted(merged, key=lambda e: (-float(e["score"]), str(e["theme"])))

        fired_codes = fire_by_day.get(d, set())
        for rank, e in enumerate(merged, 1):
            codes = e["codes"]
            member_counts = e.get("member_counts") or []
            n_up = max([int(x) for x in member_counts], default=len(codes))
            moved = [c for c in codes
                     if float(c.get("return_pct") or 0) >= tr.EARLY_MOVE_PCT]
            n_up3 = len(moved)
            turnover_up3 = sum(float(c.get("turnover") or 0) for c in moved) / 1e8
            n_members = int(theme_size.get(e["theme"], 0))
            # 統合された他テーマの構成銘柄も母数に入れる（点灯側は和集合なので合わせる）
            for m in (e.get("merged_names") or []):
                n_members = max(n_members, int(theme_size.get(m, 0)))
            breadth = (len(codes) / n_members) if n_members else float("nan")
            group = {e["theme"]} | set(e.get("merged_names") or [])
            n_fired = len({str(c.get("code")) for c in codes} & fired_codes)
            # 統合前の各テーマの全構成銘柄からも数える（点灯していない銘柄の発火も拾う）
            rows_raw.append({
                "date": pd.Timestamp(d),
                "theme": e["theme"],
                "merged_names": "|".join(sorted(e.get("merged_names") or [])),
                "score": float(e["score"]),
                "rank": int(rank),
                "n_up": int(n_up),
                "n_up3": int(n_up3),
                "turnover_up3": float(turnover_up3),
                "n_members": int(n_members),
                "breadth_ratio": float(breadth) if n_members else np.nan,
                "n_fired_d4": int(n_fired),
                "_group": group,
            })
            score_hist[e["theme"]][d] = float(e["score"])
        if (i + 1) % 100 == 0:
            el = time.time() - t0
            print(f"[backfill] {i+1}/{len(dates)} 日 ({dates[i].date()}) "
                  f"経過 {el/60:.1f}分 行 {len(rows_raw):,}", flush=True)

    if not rows_raw:
        return pd.DataFrame()

    hist = pd.DataFrame(rows_raw)

    # --- 第 2 パス: 系列に依存する列（share / phase / D1〜D5）を埋める ---
    dpos = {d: i for i, d in enumerate(dates)}

    # テーマ別のシェア系列（5 日 / 20 日平均）
    share_ser: dict = {}
    for t, dd in share_hist.items():
        s = pd.Series(dd).sort_index()
        s = s.reindex(pd.Index(dates), fill_value=0.0)
        share_ser[t] = pd.DataFrame({
            "share": s,
            "share_5d": s.rolling(5, min_periods=1).mean(),
            "share_20d": s.rolling(20, min_periods=1).mean(),
        })

    # R4 状態（市場全体の状態フラグ・日ごとに 1 つ）
    r4_state: dict = {}
    for d in dates:
        on = False
        for t, sdf in share_ser.items():
            s20 = float(sdf.at[d, "share_20d"])
            s5 = float(sdf.at[d, "share_5d"])
            if s20 >= R4_LEAD_SHARE and s5 <= R4_BREAK_RATIO * s20:
                on = True
                break
        r4_state[d] = on
    print(f"[R4] 主役テーマのシェアが折れている日 {sum(r4_state.values()):,}/{len(dates):,} 日 "
          f"({sum(r4_state.values())/len(dates)*100:.1f}%)", flush=True)

    # テーマ別の点数系列（phase・D2・D3 に使う）
    score_ser: dict = {}
    for t, dd in score_hist.items():
        s = pd.Series(dd).sort_index().reindex(pd.Index(dates), fill_value=0.0)
        score_ser[t] = s

    out_rows = []
    for r in hist.to_dict("records"):
        d = r["date"]
        i = dpos[d]
        group = r.pop("_group")
        t = r["theme"]

        # --- シェア（統合されたテーマ群の最大値を代表とする） ---
        sh = sh5 = sh20 = np.nan
        for g in group:
            sdf = share_ser.get(g)
            if sdf is None:
                continue
            v, v5, v20 = (float(sdf.at[d, "share"]), float(sdf.at[d, "share_5d"]),
                          float(sdf.at[d, "share_20d"]))
            if not np.isfinite(sh) or v > sh:
                sh, sh5, sh20 = v, v5, v20
        r["share"], r["share_5d"], r["share_20d"] = sh, sh5, sh20

        # --- 局面（theme_radar._phase と同一定義。heat の代わりに点数を使う） ---
        ser = score_ser.get(t)
        lo = max(0, i - tr.HEAT_WINDOW_DAYS)
        prev = float(ser.iloc[lo:i].sum()) if ser is not None and i > lo else 0.0
        heat = float(r["score"])
        r["phase"] = tr._phase(heat, prev, lit_today=True)

        # --- D1 = 現行 E5（閾値は theme_radar の定数をそのまま参照するため自動追随） ---
        # 2026-09-07 PM 承認で EARLY_MIN_TURN3_OKU を 100→200 億円へ変更
        # （根拠は backfill 検証。適合率 4.1%・リフト 2.02 倍）。
        # 旧閾値 100 億円の点灯は D1_prev100 として併記し、履歴比較を可能にする。
        _base_gate = (
            r["rank"] <= tr.EARLY_TOP_POOL
            and r["n_up"] >= tr.EARLY_MIN_NUP
            and r["n_up3"] >= tr.EARLY_MIN_NUP3
        )
        d1 = bool(
            _base_gate
            and r["turnover_up3"] >= float(tr.EARLY_MIN_TURN3_OKU)
        )
        d1_prev100 = bool(
            _base_gate
            and r["turnover_up3"] >= float(D1_PREV_TURN3_OKU)
        )
        # --- D2 = E5 かつ 直近 5 営業日で 2 回目以上の点灯 ---
        lit_prev5 = 0
        if ser is not None:
            lo5 = max(0, i - 5)
            lit_prev5 = int((ser.iloc[lo5:i] > 0).sum())
        d2 = bool(d1 and lit_prev5 >= 1)
        # --- D3 = 前 10 営業日の点数が 0 で当日点灯 ---
        prev10 = 0.0
        if ser is not None:
            lo10 = max(0, i - D3_NEW_WINDOW)
            prev10 = float(ser.iloc[lo10:i].sum())
        d3 = bool(prev10 <= 0)
        # --- D4 = 構成銘柄のうち直近 20 日に検知器が発火した銘柄が 2 件以上 ---
        d4 = bool(r["n_fired_d4"] >= D4_MIN_FIRES)
        # --- D5 = E5 かつ R4 状態 on ---
        d5 = bool(d1 and r4_state.get(d, False))

        r["D1"], r["D2"], r["D3"], r["D4"], r["D5"] = bool(d1), d2, d3, d4, d5
        r["D1_prev100"] = d1_prev100
        r["r4_state"] = bool(r4_state.get(d, False))
        r["lit_prev5"] = int(lit_prev5)
        # 先行リターン列は後半担当が埋める（設計書の指定により空のまま）
        for c in ("fwd_5", "fwd_20", "fwd_60", "fwd_120",
                  "excess_5", "excess_20", "excess_60", "excess_120", "rally_120",
                  "fwd_lit_20", "fwd_lit_60"):
            r[c] = np.nan
        out_rows.append(r)

    cols = [
        "date", "theme", "merged_names", "score", "rank", "n_up", "n_up3",
        "turnover_up3", "n_members", "breadth_ratio", "share", "share_5d",
        "share_20d", "phase", "D1", "D1_prev100", "D2", "D3", "D4", "D5",
        "r4_state", "n_fired_d4", "lit_prev5",
        "fwd_5", "fwd_20", "fwd_60", "fwd_120",
        "excess_5", "excess_20", "excess_60", "excess_120", "rally_120",
        "fwd_lit_20", "fwd_lit_60",
    ]
    return pd.DataFrame(out_rows)[cols].sort_values(["date", "rank"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 先行リターンの充填（--fill-forward）
# ---------------------------------------------------------------------------
def _cum_index(ret_block: np.ndarray) -> np.ndarray:
    """日次リターン(%)行列（行=営業日・列=銘柄）から等金額バスケット指数を作る。

    各日について、その日データのある銘柄だけの単純平均を取り（= 等金額バスケットの
    日次リターン）、(1+r) の累積積で指数にする（期首 = 1.0）。構成銘柄が 1 つも
    データを持たない日は 0% とみなす。
    """
    with np.errstate(invalid="ignore"):
        daily = np.nanmean(ret_block, axis=1)
    daily = np.where(np.isfinite(daily), daily, 0.0) / 100.0
    return np.cumprod(1.0 + daily)


def build_market_baseline(panel: pd.DataFrame, dates: list) -> np.ndarray:
    """市場ベースライン = 母集団の等金額平均指数（過去検証と同じ方法）。

    母集団は価格パネル全銘柄（screening_master と一致する 3,714 銘柄）。各営業日で
    その日データのある全銘柄の日次リターンを単純平均し、累積積で指数にする。
    """
    g = panel.groupby("Date")["DailyReturn"].mean()
    g = g.reindex(pd.Index(dates)).fillna(0.0) / 100.0
    return np.cumprod(1.0 + g.to_numpy())


def fill_forward_returns(hist: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """台帳の fwd_* / excess_* / rally_120 / fwd_lit_20 / fwd_lit_60 を埋める。

    - fwd_h    : 日 t から h 営業日後までのテーマ全構成銘柄バスケットの騰落率(%)
    - excess_h : fwd_h から同期間の市場ベースライン騰落率を引いた値（対市場超過）
    - rally_120: t から 120 営業日以内のバスケット最大到達が +40% 以上なら 1・
                 そうでなければ 0（窓が期間末で切れる行は NaN = 判定不可）
    - fwd_lit_20 / fwd_lit_60: その日に点灯していた銘柄だけの等金額バスケット版

    バスケットは統合テーマ群（theme + merged_names）の全構成銘柄の和集合。構成は
    2026-09 の辞書スナップショット（後知恵）。
    """
    code_to_themes, theme_size, _stale, _exc = tr.load_theme_map()
    theme_members: dict = defaultdict(set)
    for code, ts in code_to_themes.items():
        for t in ts:
            theme_members[t].add(str(code))

    dates = sorted(pd.to_datetime(panel["Date"].unique()))
    dpos = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    base_idx = build_market_baseline(panel, dates)

    hist = hist.copy().reset_index(drop=True)
    gkeys = []
    for t, m in zip(hist["theme"], hist["merged_names"]):
        names = {str(t)}
        if isinstance(m, str) and m:
            names |= {s for s in m.split("|") if s}
        gkeys.append("|".join(sorted(names)))
    hist["_gkey"] = gkeys

    ret_wide = panel.pivot_table(index="Date", columns="Code",
                                 values="DailyReturn", aggfunc="mean")
    ret_wide = ret_wide.reindex(pd.Index(dates))
    ret_arr = ret_wide.to_numpy(dtype=float)
    col_pos = {str(c): j for j, c in enumerate(ret_wide.columns)}

    # 統合テーマ群ごとのバスケット指数（1 群 1 回だけ計算する）
    groups: dict = {}
    baskets: dict = {}
    gcols: dict = {}
    for gk in hist["_gkey"].unique():
        codes: set = set()
        for nm in gk.split("|"):
            if nm:
                codes |= theme_members.get(nm, set())
        cols = sorted({col_pos[c] for c in codes if c in col_pos})
        groups[gk] = codes
        gcols[gk] = cols
        baskets[gk] = _cum_index(ret_arr[:, cols]) if cols else None
    print(f"[fwd] バスケット {len(groups):,} 群 / 営業日 {n:,}", flush=True)

    # 点灯銘柄（fwd_lit_*）は「その日の母集団 ∩ テーマ群構成銘柄」で再現する
    univ_by_day: dict = {}
    for d, g in panel.groupby("Date", sort=True):
        u = day_universe(g)
        univ_by_day[pd.Timestamp(d)] = (
            {col_pos[c] for c in u["Code"].astype(str) if c in col_pos} if len(u) else set()
        )

    m = len(hist)
    fwd = {h: np.full(m, np.nan) for h in FWD_HORIZONS}
    exc = {h: np.full(m, np.nan) for h in FWD_HORIZONS}
    rally = np.full(m, np.nan)
    lit = {20: np.full(m, np.nan), 60: np.full(m, np.nan)}

    t0 = time.time()
    for i_row, (d, gk) in enumerate(zip(hist["date"], hist["_gkey"])):
        i = dpos.get(pd.Timestamp(d))
        if i is None:
            continue
        bidx = baskets.get(gk)
        if bidx is None:
            continue
        b0 = float(bidx[i])
        if not np.isfinite(b0) or b0 <= 0:
            continue
        for h in FWD_HORIZONS:
            j = i + h
            if j >= n:
                continue
            r = (float(bidx[j]) / b0 - 1.0) * 100.0
            mk = (float(base_idx[j]) / float(base_idx[i]) - 1.0) * 100.0
            fwd[h][i_row] = r
            exc[h][i_row] = r - mk
        jmax = i + RALLY_WINDOW
        if jmax < n:
            peak = float(np.nanmax(bidx[i + 1:jmax + 1]))
            rally[i_row] = 1.0 if (peak / b0 - 1.0) * 100.0 >= RALLY_THRESHOLD else 0.0
        lit_cols = sorted(set(gcols[gk]) & univ_by_day.get(pd.Timestamp(d), set()))
        if lit_cols:
            for h in (20, 60):
                j = i + h
                if j >= n:
                    continue
                idx = _cum_index(ret_arr[i + 1:j + 1][:, lit_cols])
                lit[h][i_row] = (float(idx[-1]) - 1.0) * 100.0
        if (i_row + 1) % 20000 == 0:
            print(f"[fwd] {i_row+1:,}/{m:,} 行 経過 {(time.time()-t0)/60:.1f}分", flush=True)

    for h in FWD_HORIZONS:
        hist[f"fwd_{h}"] = fwd[h]
        hist[f"excess_{h}"] = exc[h]
    hist["rally_120"] = rally
    hist["fwd_lit_20"] = lit[20]
    hist["fwd_lit_60"] = lit[60]
    hist = hist.drop(columns=["_gkey"])
    print(f"[fwd] 充填完了 経過 {(time.time()-t0)/60:.1f}分 / "
          f"fwd_120 非欠損 {int(hist['fwd_120'].notna().sum()):,} / "
          f"rally_120 非欠損 {int(hist['rally_120'].notna().sum()):,}", flush=True)
    return hist


# ---------------------------------------------------------------------------
# エントリー特徴量（--features）
# ---------------------------------------------------------------------------
# 第 2 段階（2026-09-07 PM 承認「3」）で追加する列。第 1 段階の結論
# 「E5 は大相場の的中率を上げるが対市場超過は中央値ゼロ」を受け、
# 「いつ・何を買うと +20/+60 営業日の超過が出るか」を測るための材料を台帳へ足す。
# 既存列は 1 つも書き換えない（追加のみ）。
EPISODE_GAP_DAYS = 3        # 何営業日空いたら別エピソードとみなすか
LIT_WINDOW = 10             # lit_days_10 の窓（当日を除く直近営業日数）
NEW_ENTRANT_WINDOW = 20     # new_entrants / laggard_ratio の参照窓
BREADTH_ACCEL_WINDOW = 5    # breadth_accel の分母（直近営業日数の n_up 平均）
RANK_IMPROVE_MIN = 10       # rank_new_entry: 順位がこれ以上改善したら 1
THEME_MOMENTUM = (OUTROOT / "theme_momentum.parquet").resolve()
STOCK_CONTEXT = (OUTROOT / "analysis/theme_radar/stock_context_daily.parquet").resolve()

FEATURE_COLS = [
    "lit_days_10", "episode_day", "breadth_accel", "share_accel",
    "new_entrants", "new_entrant_ratio", "laggard_ratio",
    "pullback_from_high", "rank_new_entry", "catalyst_days_10",
]


def _load_rank_history() -> dict:
    """みんかぶランキング（theme_momentum.parquet）を {日付: {テーマ: 最良順位}} で返す。

    履歴は 2026-05-16 以降の 56 スナップショットしかない。取れない日は
    rank_new_entry を NaN にして「判定不可」と「該当なし(0)」を区別する。
    """
    if not THEME_MOMENTUM.exists():
        print(f"[feat] みんかぶランキングが無い: {THEME_MOMENTUM}", flush=True)
        return {}
    df = pd.read_parquet(THEME_MOMENTUM)
    df = df[df["source"].astype(str) == "minkabu"]
    if df.empty:
        return {}
    out: dict = defaultdict(dict)
    for snap, theme, rk in zip(df["snapshot_date"].astype(str),
                               df["theme_name"].astype(str),
                               pd.to_numeric(df["rank"], errors="coerce")):
        if not np.isfinite(rk):
            continue
        d = pd.Timestamp(snap).normalize()
        cur = out[d].get(theme)
        # rank_type が popular / rise の 2 系統あるので、より上位（数字が小さい）を採る
        if cur is None or rk < cur:
            out[d][theme] = float(rk)
    if out:
        print(f"[feat] みんかぶランキング {len(out)} 営業日分 "
              f"({min(out).date()}〜{max(out).date()})", flush=True)
    return dict(out)


def _load_catalyst_days() -> dict:
    """「なぜ動いた」記録（stock_context_daily.parquet）を {日付: {銘柄}} で返す。

    materials 列が空でない銘柄だけを「その日 材料の記録がある銘柄」と数える。
    蓄積は 2026-09-03 以降しかないため、それ以前は NaN（判定不可）にする。
    """
    if not STOCK_CONTEXT.exists():
        print(f"[feat] 銘柄コンテキスト蓄積が無い: {STOCK_CONTEXT}", flush=True)
        return {}
    df = pd.read_parquet(STOCK_CONTEXT)
    if df.empty:
        return {}
    mat = df["materials"].astype(str).str.strip()
    df = df[mat.ne("") & mat.ne("None") & mat.ne("nan")]
    out: dict = defaultdict(set)
    for d, c in zip(df["date"].astype(str), df["code"].astype(str)):
        out[pd.Timestamp(d).normalize()].add(c)
    if out:
        print(f"[feat] 材料記録 {len(out)} 営業日分 "
              f"({min(out).date()}〜{max(out).date()})", flush=True)
    return dict(out)


def add_entry_features(hist: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """台帳へエントリー判定用の特徴量 10 列を追加する（既存列は無改変）。"""
    code_to_themes, _theme_size, _stale, _exc = tr.load_theme_map()
    theme_members: dict = defaultdict(set)
    for code, ts in code_to_themes.items():
        for t in ts:
            theme_members[t].add(str(code))

    dates = sorted(pd.to_datetime(panel["Date"].unique()))
    dpos = {d: i for i, d in enumerate(dates)}

    hist = hist.copy().reset_index(drop=True)
    hist["date"] = pd.to_datetime(hist["date"])

    # 統合テーマ群のキー（fill_forward_returns と同じ作り方）
    gkeys = []
    for t, mg in zip(hist["theme"], hist["merged_names"]):
        names = {str(t)}
        if isinstance(mg, str) and mg:
            names |= {s for s in mg.split("|") if s}
        gkeys.append("|".join(sorted(names)))
    hist["_gkey"] = gkeys

    # --- 銘柄 × 日の行列（バスケット指数・20 日リターン・当日母集団） ---
    ret_wide = panel.pivot_table(index="Date", columns="Code",
                                 values="DailyReturn", aggfunc="mean").reindex(pd.Index(dates))
    ret_arr = ret_wide.to_numpy(dtype=float)
    col_pos = {str(c): j for j, c in enumerate(ret_wide.columns)}

    # 20 営業日リターン（laggard_ratio 用・銘柄 × 日）
    px_wide = panel.pivot_table(index="Date", columns="Code",
                                values="AdjClose", aggfunc="mean").reindex(pd.Index(dates))
    px_wide = px_wide[ret_wide.columns]
    px_arr = px_wide.to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        r20_arr = np.full_like(px_arr, np.nan)
        r20_arr[NEW_ENTRANT_WINDOW:] = (
            px_arr[NEW_ENTRANT_WINDOW:] / px_arr[:-NEW_ENTRANT_WINDOW] - 1.0) * 100.0
        r20_med = np.nanmedian(r20_arr, axis=1)   # 母集団中央値（日ごと）

    # 各群のバスケット指数（pullback_from_high 用）
    gcols: dict = {}
    baskets: dict = {}
    gmembers: dict = {}
    for gk in hist["_gkey"].unique():
        codes: set = set()
        for nm in gk.split("|"):
            if nm:
                codes |= theme_members.get(nm, set())
        gmembers[gk] = codes
        cols = sorted({col_pos[c] for c in codes if c in col_pos})
        gcols[gk] = cols
        baskets[gk] = _cum_index(ret_arr[:, cols]) if cols else None

    # 当日「点灯していた」銘柄集合（= その日の母集団 ∩ 群構成銘柄）
    univ_by_day: dict = {}
    for d, g in panel.groupby("Date", sort=True):
        u = day_universe(g)
        univ_by_day[pd.Timestamp(d)] = (
            set(u["Code"].astype(str)) if len(u) else set())

    rank_hist = _load_rank_history()
    rank_days = sorted(rank_hist.keys())
    catalyst = _load_catalyst_days()
    cat_days = sorted(catalyst.keys())

    # --- 群 × 日の系列（点灯フラグ・n_up・点灯銘柄集合） ---
    lit_map: dict = defaultdict(dict)       # gkey -> {date_idx: True}
    nup_map: dict = defaultdict(dict)       # gkey -> {date_idx: n_up}
    litcodes_map: dict = defaultdict(dict)  # gkey -> {date_idx: set(codes)}
    for gk, d, nu in zip(hist["_gkey"], hist["date"], hist["n_up"]):
        i = dpos.get(pd.Timestamp(d))
        if i is None:
            continue
        lit_map[gk][i] = True
        nup_map[gk][i] = int(nu)
    for gk, idxs in lit_map.items():
        codes = gmembers.get(gk, set())
        for i in idxs:
            litcodes_map[gk][i] = codes & univ_by_day.get(dates[i], set())

    m = len(hist)
    F = {c: np.full(m, np.nan) for c in FEATURE_COLS}

    t0 = time.time()
    for i_row, (gk, d, nu, sh5, sh20) in enumerate(
            zip(hist["_gkey"], hist["date"], hist["n_up"],
                hist["share_5d"], hist["share_20d"])):
        i = dpos.get(pd.Timestamp(d))
        if i is None:
            continue
        lit_idx = lit_map[gk]

        # --- lit_days_10: 当日を除く直近 10 営業日の点灯日数 ---
        lo = max(0, i - LIT_WINDOW)
        F["lit_days_10"][i_row] = float(sum(1 for k in range(lo, i) if k in lit_idx))

        # --- episode_day: 連続点灯エピソードの何日目か（3 営業日以上空いたら新規） ---
        day_no = 1
        k = i
        while True:
            prev = None
            for kk in range(k - 1, max(-1, k - 1 - EPISODE_GAP_DAYS), -1):
                if kk in lit_idx:
                    prev = kk
                    break
            if prev is None:
                break
            day_no += 1
            k = prev
        F["episode_day"][i_row] = float(day_no)
        ep_start = k   # エピソード開始日の位置

        # --- breadth_accel: n_up ÷ 直近 5 営業日の n_up 平均（点灯日のみで平均） ---
        lo5 = max(0, i - BREADTH_ACCEL_WINDOW)
        prev_nups = [nup_map[gk][kk] for kk in range(lo5, i) if kk in nup_map[gk]]
        if prev_nups:
            avg = float(np.mean(prev_nups))
            F["breadth_accel"][i_row] = float(nu) / avg if avg > 0 else np.nan

        # --- share_accel: share_5d ÷ share_20d ---
        s5, s20 = float(sh5), float(sh20)
        if np.isfinite(s5) and np.isfinite(s20) and s20 > 0:
            F["share_accel"][i_row] = s5 / s20

        # --- new_entrants: 当日点灯銘柄のうち直近 20 営業日に一度も点灯していなかった数 ---
        today_codes = litcodes_map[gk].get(i, set())
        if today_codes:
            lo20 = max(0, i - NEW_ENTRANT_WINDOW)
            past: set = set()
            for kk in range(lo20, i):
                past |= litcodes_map[gk].get(kk, set())
            newc = today_codes - past
            F["new_entrants"][i_row] = float(len(newc))
            F["new_entrant_ratio"][i_row] = len(newc) / len(today_codes)
        else:
            F["new_entrants"][i_row] = 0.0

        # --- laggard_ratio: 構成銘柄のうち 20 営業日リターンが母集団中央値以下の比率 ---
        cols = gcols.get(gk) or []
        if cols and np.isfinite(r20_med[i]):
            vals = r20_arr[i, cols]
            ok = np.isfinite(vals)
            if ok.sum() > 0:
                F["laggard_ratio"][i_row] = float((vals[ok] <= r20_med[i]).mean())

        # --- pullback_from_high: エピソード開始以降のバスケット高値からの下落率 ---
        bidx = baskets.get(gk)
        if bidx is not None and np.isfinite(bidx[i]) and bidx[i] > 0:
            seg = bidx[ep_start:i + 1]
            seg = seg[np.isfinite(seg)]
            if len(seg) > 0:
                hi = float(np.max(seg))
                if hi > 0:
                    F["pullback_from_high"][i_row] = (float(bidx[i]) / hi - 1.0) * 100.0

        # --- rank_new_entry: みんかぶランキングに新規参入 or 10 位以上改善 ---
        if rank_days and rank_days[0] <= dates[i] <= rank_days[-1]:
            names = [nm for nm in gk.split("|") if nm]
            cur_ranks = [rank_hist.get(dates[i], {}).get(nm) for nm in names]
            cur_ranks = [x for x in cur_ranks if x is not None]
            # 直前のランキング取得日
            prev_day = None
            for dd in reversed(rank_days):
                if dd < dates[i]:
                    prev_day = dd
                    break
            if cur_ranks:
                cur = min(cur_ranks)
                if prev_day is None:
                    F["rank_new_entry"][i_row] = np.nan
                else:
                    prev_ranks = [rank_hist.get(prev_day, {}).get(nm) for nm in names]
                    prev_ranks = [x for x in prev_ranks if x is not None]
                    if not prev_ranks:
                        F["rank_new_entry"][i_row] = 1.0     # 新規参入
                    else:
                        F["rank_new_entry"][i_row] = (
                            1.0 if (min(prev_ranks) - cur) >= RANK_IMPROVE_MIN else 0.0)
            elif dates[i] in rank_hist:
                F["rank_new_entry"][i_row] = 0.0             # ランキング外

        # --- catalyst_days_10: 構成銘柄に「なぜ動いた」記録がある日数（直近 10 営業日） ---
        if cat_days and cat_days[0] <= dates[i]:
            codes_set = gmembers.get(gk, set())
            lo10 = max(0, i - LIT_WINDOW + 1)
            cnt = 0
            for kk in range(lo10, i + 1):
                if dates[kk] < cat_days[0]:
                    continue
                if catalyst.get(dates[kk], set()) & codes_set:
                    cnt += 1
            F["catalyst_days_10"][i_row] = float(cnt)

        if (i_row + 1) % 20000 == 0:
            print(f"[feat] {i_row+1:,}/{m:,} 行 経過 {(time.time()-t0)/60:.1f}分", flush=True)

    for c in FEATURE_COLS:
        hist[c] = F[c]
    hist = hist.drop(columns=["_gkey"])
    cov = " ".join(f"{c}={hist[c].notna().mean()*100:.1f}%" for c in FEATURE_COLS)
    print(f"[feat] 充填完了 経過 {(time.time()-t0)/60:.1f}分 / 非欠損率 {cov}", flush=True)
    return hist


# ---------------------------------------------------------------------------
# 日次の点数行を月次パーティションへ残す（GHA・価格パネル非依存）
# ---------------------------------------------------------------------------
# theme_radar が誌面生成時に既に計算し終えている当日の全テーマ行を、そのまま
# bi/outputs/analysis/theme_radar/theme_score_daily/YYYY-MM.parquet へ追記する。
# 価格パネル（92MB・未追跡）を必要としないので GHA の runner でも動く。
# 誌面と送信には一切影響しない（呼び出し側が try/except で包む契約）。
DAILY_DIR = (OUTROOT / "analysis/theme_radar/theme_score_daily").resolve()

DAILY_COLS = ["date", "theme", "merged_names", "score", "rank", "n_up", "n_up3",
              "turnover_up3", "n_members", "breadth_ratio", "phase", "E5"]


def append_theme_score_daily(rows, trade_date, out_dir: Path | None = None) -> str | None:
    """theme_radar が当日計算したテーマ行を月次 parquet へ追記する。

    Parameters
    ----------
    rows : list[dict]
        theme_radar.evaluate_early_pool(top_pool=全件) が返す当日の全テーマ行。
        使うキー: theme / score / rank / n_up / n3(=n_up3) / turn3_oku /
        passed_gate(=E5) / phase / merged_names / theme_size。キー名の揺れは吸収する。
        evaluate_early_pool は E5 の 3 閾値を theme_radar の定数から読むため、
        誌面の判定と台帳の E5 列が構造的にずれない。
    trade_date : str | date
        対象営業日。
    out_dir : Path
        省略時は bi/outputs/analysis/theme_radar/theme_score_daily/。

    Returns
    -------
    str | None
        書いたファイルのパス。行が無ければ None。
    """
    if not rows:
        return None
    d = pd.Timestamp(str(trade_date)).normalize()
    out_dir = Path(out_dir) if out_dir else DAILY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    def _g(r, *keys, default=None):
        for k in keys:
            if k in r and r[k] is not None:
                return r[k]
        return default

    recs = []
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            continue
        theme = str(_g(r, "theme", "name", default="") or "").strip()
        if not theme:
            continue
        merged = _g(r, "merged_names", default=None)
        if isinstance(merged, (list, tuple, set)):
            merged = "|".join(sorted(str(x) for x in merged))
        recs.append({
            "date": d,
            "theme": theme,
            "merged_names": str(merged or ""),
            "score": float(_g(r, "score", default=float("nan")) or float("nan")),
            "rank": int(_g(r, "rank", default=i) or i),
            "n_up": int(_g(r, "n_up", "nup", default=0) or 0),
            "n_up3": int(_g(r, "n3", "n_up3", default=0) or 0),
            "turnover_up3": float(_g(r, "turn3_oku", "turnover_up3", default=float("nan"))
                                  or float("nan")),
            "n_members": int(_g(r, "n_members", "theme_size", "size", default=0) or 0),
            "breadth_ratio": float(_g(r, "breadth_ratio", default=float("nan"))
                                   or float("nan")),
            "phase": str(_g(r, "phase", default="") or ""),
            "E5": bool(_g(r, "passed_gate", "early", "E5", default=False)),
        })
    if not recs:
        return None
    new = pd.DataFrame(recs)[DAILY_COLS]

    path = out_dir / f"{d.strftime('%Y-%m')}.parquet"
    if path.exists():
        old = pd.read_parquet(path)
        old = old[pd.to_datetime(old["date"]) != d]          # 同一日を差し替える（冪等）
        new = pd.concat([old, new], ignore_index=True)
    new = new.sort_values(["date", "rank"]).reset_index(drop=True)
    tmp = path.with_suffix(".parquet.tmp")
    new.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return str(path)


def write_history(df: pd.DataFrame, mode: str) -> None:
    """台帳を書く。--daily は同一 date の行を差し替えて追記する（原子的置換）。"""
    HISTORY_OUT.parent.mkdir(parents=True, exist_ok=True)
    if mode == "daily" and HISTORY_OUT.exists():
        old = pd.read_parquet(HISTORY_OUT)
        new_dates = set(pd.to_datetime(df["date"]).unique())
        old = old[~pd.to_datetime(old["date"]).isin(new_dates)]
        # --daily が計算するのは compute_history の列（先行リターンと特徴量を含まない）
        # だけなので、concat すると足りない列がその日の行だけ NaN で埋まる。黙って
        # 欠損を作ると「計算した結果ゼロ件」と「まだ計算していない」の区別が付かなく
        # なるため、どの列が未計算のまま入るかを必ず表示する（欠損は残す方が正しい。
        # 埋めるには --fill-forward と --features を後から流す）。
        pending = [c for c in old.columns if c not in df.columns]
        if pending:
            print(f"[write] --daily は次の列を計算しません（当日行は欠損のまま）: "
                  f"{pending}\n"
                  f"        先行リターンは `--fill-forward`、特徴量は `--features` を"
                  f"後から流して埋めてください。", flush=True)
        df = pd.concat([old, df], ignore_index=True)
    df = df.sort_values(["date", "rank"]).reset_index(drop=True)
    tmp = HISTORY_OUT.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, HISTORY_OUT)
    print(f"[write] {HISTORY_OUT} 行 {len(df):,} / "
          f"期間 {pd.to_datetime(df['date']).min().date()}〜"
          f"{pd.to_datetime(df['date']).max().date()}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="テーマ点数の答え合わせ台帳を作る")
    ap.add_argument("--backfill", action="store_true", help="2022-01-04〜直近日を再計算")
    ap.add_argument("--daily", action="store_true", help="直近 1 営業日を追記")
    ap.add_argument("--start", default=BACKFILL_START, help="backfill の開始日")
    ap.add_argument("--end", default=None, help="backfill の終了日")
    ap.add_argument("--rebuild-panel", action="store_true",
                    help="price_turnover_panel.parquet を作り直す")
    ap.add_argument("--fill-forward", action="store_true",
                    help="既存の台帳へ先行リターン（fwd_*/excess_*/rally_120）を充填する")
    ap.add_argument("--features", action="store_true",
                    help="既存の台帳へエントリー判定用の特徴量 10 列を追加する")
    args = ap.parse_args()
    if not (args.backfill or args.daily or args.fill_forward or args.features):
        ap.error("--backfill / --daily / --fill-forward / --features "
                 "のいずれかを指定してください")

    t0 = time.time()
    panel = build_price_turnover_panel(force=args.rebuild_panel)
    print(f"[panel] 行 {len(panel):,} / 銘柄 {panel['Code'].nunique():,} / "
          f"期間 {panel['Date'].min().date()}〜{panel['Date'].max().date()}", flush=True)

    all_dates = sorted(pd.to_datetime(panel["Date"].unique()))
    if args.fill_forward:
        if not HISTORY_OUT.exists():
            raise FileNotFoundError(f"台帳がありません: {HISTORY_OUT}")
        hist = pd.read_parquet(HISTORY_OUT)
        print(f"[fwd] 台帳 {len(hist):,} 行 を読み込み", flush=True)
        hist = fill_forward_returns(hist, panel)
        write_history(hist, "backfill")
        print(f"[done] 経過 {(time.time()-t0)/60:.1f} 分", flush=True)
        return 0

    if args.features:
        if not HISTORY_OUT.exists():
            raise FileNotFoundError(f"台帳がありません: {HISTORY_OUT}")
        hist = pd.read_parquet(HISTORY_OUT)
        print(f"[feat] 台帳 {len(hist):,} 行 × {len(hist.columns)} 列 を読み込み", flush=True)
        before = list(hist.columns)
        hist = add_entry_features(hist, panel)
        added = [c for c in hist.columns if c not in before]
        # 既存列を 1 つも壊していないことを確認してから書く
        missing = [c for c in before if c not in hist.columns]
        if missing:
            raise RuntimeError(f"既存列が失われた: {missing}")
        print(f"[feat] 追加列 {added}", flush=True)
        write_history(hist, "backfill")
        print(f"[done] 経過 {(time.time()-t0)/60:.1f} 分", flush=True)
        return 0

    if args.daily:
        # 当日分だけを書くが、20 日移動平均・10 日局面判定に過去が要るので
        # 計算自体は直近 60 営業日で回して最終日の行だけ残す。
        tail = all_dates[-60:]
        sub = panel[panel["Date"].isin(tail)]
        hist = compute_history(sub, tail)
        hist = hist[hist["date"] == tail[-1]]
        write_history(hist, "daily")
    else:
        start = pd.Timestamp(args.start)
        end = pd.Timestamp(args.end) if args.end else all_dates[-1]
        dates = [d for d in all_dates if start <= d <= end]
        print(f"[backfill] 対象営業日 {len(dates):,} 日 "
              f"({dates[0].date()}〜{dates[-1].date()})", flush=True)
        hist = compute_history(panel, dates)
        write_history(hist, "backfill")

    print(f"[done] 経過 {(time.time()-t0)/60:.1f} 分", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
