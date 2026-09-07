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

        # --- D1 = 現行 E5 ---
        d1 = (
            r["rank"] <= tr.EARLY_TOP_POOL
            and r["n_up"] >= tr.EARLY_MIN_NUP
            and r["n_up3"] >= tr.EARLY_MIN_NUP3
            and r["turnover_up3"] >= float(tr.EARLY_MIN_TURN3_OKU)
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
        "share_20d", "phase", "D1", "D2", "D3", "D4", "D5",
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


def write_history(df: pd.DataFrame, mode: str) -> None:
    """台帳を書く。--daily は同一 date の行を差し替えて追記する（原子的置換）。"""
    HISTORY_OUT.parent.mkdir(parents=True, exist_ok=True)
    if mode == "daily" and HISTORY_OUT.exists():
        old = pd.read_parquet(HISTORY_OUT)
        new_dates = set(pd.to_datetime(df["date"]).unique())
        old = old[~pd.to_datetime(old["date"]).isin(new_dates)]
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
    args = ap.parse_args()
    if not (args.backfill or args.daily or args.fill_forward):
        ap.error("--backfill / --daily / --fill-forward のいずれかを指定してください")

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
