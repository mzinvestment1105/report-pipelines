# -*- coding: utf-8 -*-
"""テーマ初動検知の成績測定（theme_score_history.parquet の答え合わせ）。

設計の正本: scratchpad/design/theme_early_detection_v3.md（2026-09-07 PM 承認）

なぜ要るか
----------
現行稼働の theme_radar の初動判定 E5（= D1）は、1 事例で閾値を決めたまま答え合わせが
ゼロ件だった。本スクリプトは theme_ledger.py が作った台帳（2022-01-05〜直近日・
テーマ × 日の点数と先行リターン）を使い、初動定義の候補 D1〜D5 を同じ物差しで
比較する。

物差し（すべて実測・推定を混ぜない）
------------------------------------
- 大相場ラベル rally_120: 日 t のテーマ全構成銘柄の等金額バスケットが t から
  120 営業日以内に +40% 以上到達したら 1。窓が期間末で切れる行は判定不可（NaN）。
- 的中率: 点灯行のうち rally_120 == 1 の割合。
- 無条件ベースライン: 同じ判定可能母集団（全テーマ × 全日）の的中率。
  リフト 1.0 倍は「その定義に情報が無い」ことを意味する。
- 超過リターン: excess_h = テーマバスケットの h 営業日騰落率 − 母集団等金額平均の
  同期間騰落率。中央値と勝率（> 0 の割合）で見る（平均は少数の大当たりに引っ張られる）。
- 捕捉率: 大相場の開始日から遡って 20 営業日以内にそのテーマが点灯していた割合。
  「拾い漏れの少なさ」を測る。開始日が期間先頭から 20 営業日以内の局面は窓が
  取れないため対象外。
- 先行日数: 大相場の開始日 − 点灯日（正なら点灯が先行）の中央値。

出力: research/themes_history/2026-09-07_theme_radar_backfill_validation.md の
      表部分（Markdown）を標準出力へ書く（--out でファイルへも保存できる）。

Reads : bi/outputs/analysis/theme_radar/theme_score_history.parquet
        bi/outputs/analysis/theme_radar/price_turnover_panel.parquet
既存の本番ファイルへは一切書かない。
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
OUTROOT = (BASE_DIR / ".." / "outputs").resolve()
HISTORY = OUTROOT / "analysis/theme_radar/theme_score_history.parquet"
PANEL_PT = OUTROOT / "analysis/theme_radar/price_turnover_panel.parquet"

CAPTURE_LOOKBACK = 20   # 大相場の開始日から遡って何営業日以内の点灯を「捕捉」とするか
RALLY_WINDOW = 120
RALLY_THRESHOLD = 40.0

# 「新規合流」の既存結果（research/themes_history/2026-07-20_asof_join_validation.md）
# 対ベースライン(a) 超過中央値 +20/+60/+120 営業日。再計算はせず参考行として転記する。
JOIN_REF = {"n": 3259, "e20": -0.18, "e60": -0.91, "e120": -1.06}


# ---------------------------------------------------------------------------
# 共通の集計
# ---------------------------------------------------------------------------
def _pct(x) -> str:
    return "-" if not np.isfinite(x) else f"{x*100:.1f}%"


def _num(x, d=2) -> str:
    return "-" if not np.isfinite(x) else f"{x:.{d}f}"


def score_subset(sub: pd.DataFrame, base_rate: float) -> dict:
    """点灯行の部分集合について的中率・リフト・超過リターンをまとめる。"""
    judged = sub[sub["rally_120"].notna()]
    n_lit = len(sub)
    n_judged = len(judged)
    hit = float(judged["rally_120"].mean()) if n_judged else np.nan
    lift = (hit / base_rate) if (n_judged and base_rate > 0) else np.nan
    out = {"n_lit": n_lit, "n_judged": n_judged, "hit": hit, "lift": lift}
    for h in (20, 60, 120):
        c = f"excess_{h}"
        s = sub[c].dropna()
        out[f"e{h}_med"] = float(s.median()) if len(s) else np.nan
        out[f"e{h}_win"] = float((s > 0).mean()) if len(s) else np.nan
    return out


# ---------------------------------------------------------------------------
# 大相場の局面（開始日）を作る — 捕捉率と先行日数に使う
# ---------------------------------------------------------------------------
def build_rally_episodes(hist: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """テーマ群ごとの大相場の開始日を列挙する。

    定義: バスケット指数が日 t から 120 営業日以内に +40% 以上到達した日のうち、
    連続する区間の**最初の日**を 1 局面の開始日とする（rally_120 == 1 の run の先頭）。
    連続の途切れは 1 営業日でも空けば別局面として数える。
    """
    dates = sorted(pd.to_datetime(panel["Date"].unique()))
    dpos = {d: i for i, d in enumerate(dates)}
    hist = hist.copy()
    hist["_i"] = hist["date"].map(dpos)

    eps = []
    for theme, g in hist[hist["rally_120"] == 1].groupby("theme", sort=True):
        idx = sorted(g["_i"].dropna().astype(int).tolist())
        if not idx:
            continue
        start = idx[0]
        prev = idx[0]
        for i in idx[1:]:
            if i != prev + 1:
                eps.append({"theme": theme, "start_i": start})
                start = i
            prev = i
        eps.append({"theme": theme, "start_i": start})
    ep = pd.DataFrame(eps)
    if len(ep):
        ep["start_date"] = [dates[i] for i in ep["start_i"]]
    return ep


def capture_and_lead(hist: pd.DataFrame, ep: pd.DataFrame, panel: pd.DataFrame,
                     flag: str) -> tuple:
    """大相場側から見た捕捉率と、点灯 → 開始日の先行日数中央値を返す。

    捕捉 = 開始日から遡って CAPTURE_LOOKBACK 営業日以内（[start-20, start]）に
    そのテーマで flag が点灯していた局面の割合。
    """
    if not len(ep):
        return np.nan, 0, 0, np.nan
    dates = sorted(pd.to_datetime(panel["Date"].unique()))
    dpos = {d: i for i, d in enumerate(dates)}
    lit = hist[hist[flag]].copy()
    lit["_i"] = lit["date"].map(dpos)
    by_theme: dict = defaultdict(list)
    for t, i in zip(lit["theme"], lit["_i"]):
        if pd.notna(i):
            by_theme[t].append(int(i))
    for t in by_theme:
        by_theme[t].sort()

    target = ep[ep["start_i"] >= CAPTURE_LOOKBACK]
    n_target = len(target)
    n_cap = 0
    leads = []
    for t, si in zip(target["theme"], target["start_i"]):
        arr = by_theme.get(t)
        if not arr:
            continue
        lo, hi = si - CAPTURE_LOOKBACK, si
        inwin = [i for i in arr if lo <= i <= hi]
        if inwin:
            n_cap += 1
            leads.append(si - max(inwin))
    rate = (n_cap / n_target) if n_target else np.nan
    lead_med = float(np.median(leads)) if leads else np.nan
    return rate, n_cap, n_target, lead_med


# ---------------------------------------------------------------------------
# (a) D1〜D5 の成績表
# ---------------------------------------------------------------------------
def table_definitions(hist: pd.DataFrame, panel: pd.DataFrame) -> str:
    judged_all = hist[hist["rally_120"].notna()]
    base_rate = float(judged_all["rally_120"].mean())
    ep = build_rally_episodes(hist, panel)

    labels = {
        "D1": "D1 現行 E5（上位10内・上昇4件以上・+3%が2件以上・その代金100億円以上）",
        "D2": "D2 E5 かつ直近5営業日で2回目以上の点灯（継続）",
        "D3": "D3 局面「新規」（前10営業日の点数が0で当日点灯）",
        "D4": "D4 検知器由来（構成銘柄の直近20日発火が2件以上）",
        "D5": "D5 E5 かつ R4 状態 on（主役シェア8%・0.85倍割れ）",
    }
    lines = [
        "| 定義 | 点灯数 | 判定可 | 的中率 | リフト | 捕捉率 | 先行日数中央値 | "
        "超過+20日 中央値/勝率 | 超過+60日 中央値/勝率 | 超過+120日 中央値/勝率 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for f, lab in labels.items():
        sub = hist[hist[f]]
        s = score_subset(sub, base_rate)
        cap, ncap, ntgt, lead = capture_and_lead(hist, ep, panel, f)
        lines.append(
            f"| {lab} | {s['n_lit']:,} | {s['n_judged']:,} | {_pct(s['hit'])} | "
            f"{_num(s['lift'])}倍 | {_pct(cap)}（{ncap}/{ntgt}） | "
            f"{_num(lead, 0) if np.isfinite(lead) else '-'} | "
            f"{_num(s['e20_med'])}% / {_pct(s['e20_win'])} | "
            f"{_num(s['e60_med'])}% / {_pct(s['e60_win'])} | "
            f"{_num(s['e120_med'])}% / {_pct(s['e120_win'])} |"
        )
    b = score_subset(hist, base_rate)
    lines.append(
        f"| （参考）ベースライン＝無条件（全テーマ×全日） | {len(hist):,} | "
        f"{b['n_judged']:,} | {_pct(b['hit'])} | 1.00倍（基準） | - | - | "
        f"{_num(b['e20_med'])}% / {_pct(b['e20_win'])} | "
        f"{_num(b['e60_med'])}% / {_pct(b['e60_win'])} | "
        f"{_num(b['e120_med'])}% / {_pct(b['e120_win'])} |"
    )
    # (d) 新規合流の参考行
    lines.append(
        f"| （参考・再計算せず転記）新規合流（無所属→所属・別骨格の既存検証） | "
        f"{JOIN_REF['n']:,} | - | - | - | - | - | "
        f"{JOIN_REF['e20']:.2f}% / - | {JOIN_REF['e60']:.2f}% / - | "
        f"{JOIN_REF['e120']:.2f}% / - |"
    )
    head = (
        f"大相場の局面（rally_120 の連続区間の先頭）: **{len(ep):,} 件**"
        f"（うち遡及窓 {CAPTURE_LOOKBACK} 営業日が取れる対象 "
        f"{int((ep['start_i'] >= CAPTURE_LOOKBACK).sum()) if len(ep) else 0:,} 件）。"
        f"無条件ベースラインの的中率 **{base_rate*100:.2f}%**"
        f"（判定可 {len(judged_all):,} / 全 {len(hist):,} 行）。\n"
    )
    return head + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# (b) E5 の閾値格子
# ---------------------------------------------------------------------------
def table_grid(hist: pd.DataFrame, panel: pd.DataFrame) -> str:
    judged_all = hist[hist["rally_120"].notna()]
    base_rate = float(judged_all["rally_120"].mean())
    ep = build_rally_episodes(hist, panel)
    n_target = int((ep["start_i"] >= CAPTURE_LOOKBACK).sum()) if len(ep) else 0

    lines = [
        "| 上昇銘柄 | +3%銘柄 | 代金(億円) | 点灯数 | 判定可 | 適合率(的中率) | "
        "リフト | 捕捉率 | 超過+60日 中央値 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for nup in (3, 4, 5, 6):
        for nup3 in (1, 2, 3):
            for turn in (50, 100, 200):
                m = (
                    (hist["rank"] <= 10)
                    & (hist["n_up"] >= nup)
                    & (hist["n_up3"] >= nup3)
                    & (hist["turnover_up3"] >= turn)
                )
                sub = hist[m]
                s = score_subset(sub, base_rate)
                hist["_grid"] = m
                cap, ncap, ntgt, _ = capture_and_lead(hist, ep, panel, "_grid")
                lines.append(
                    f"| {nup} | {nup3} | {turn} | {s['n_lit']:,} | {s['n_judged']:,} | "
                    f"{_pct(s['hit'])} | {_num(s['lift'])}倍 | "
                    f"{_pct(cap)}（{ncap}/{ntgt}） | {_num(s['e60_med'])}% |"
                )
    hist.drop(columns=["_grid"], inplace=True, errors="ignore")
    head = (
        f"現行 E5 は「上昇銘柄 4・+3%銘柄 2・代金 100億円」の行。無条件ベースラインの"
        f"的中率は **{base_rate*100:.2f}%**、捕捉率の分母（遡及窓が取れる局面）は "
        f"**{n_target:,} 件**。\n"
    )
    return head + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# (c) R4 の主役閾値 3% / 5% / 8%
# ---------------------------------------------------------------------------
def recompute_r4(hist: pd.DataFrame, lead_share: float,
                 break_ratio: float = 0.85) -> pd.Series:
    """台帳の share_5d / share_20d から R4 状態（日ごとのフラグ）を作り直す。

    主役テーマ = share_20d >= lead_share。そのテーマの share_5d が
    break_ratio × share_20d 以下なら「資金が折れている」。その日にそういうテーマが
    1 つでもあれば R4 状態 on。
    """
    m = (hist["share_20d"] >= lead_share) & (
        hist["share_5d"] <= break_ratio * hist["share_20d"]
    )
    on_days = set(hist.loc[m, "date"].unique())
    return hist["date"].isin(on_days)


def table_r4(hist: pd.DataFrame) -> str:
    judged_all = hist[hist["rally_120"].notna()]
    n_days = hist["date"].nunique()
    lines = [
        "| 主役閾値 | 状態 | 該当日数 | 判定可 | 無条件的中率 | E5点灯数 | "
        "E5判定可 | E5的中率 | 条件付きリフト |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for th in (0.03, 0.05, 0.08):
        state = recompute_r4(hist, th)
        n_on_days = hist.loc[state, "date"].nunique()
        for on in (True, False):
            m = state if on else ~state
            uncond = hist[m]
            uj = uncond[uncond["rally_120"].notna()]
            ubase = float(uj["rally_120"].mean()) if len(uj) else np.nan
            e5 = hist[m & hist["D1"]]
            ej = e5[e5["rally_120"].notna()]
            ehit = float(ej["rally_120"].mean()) if len(ej) else np.nan
            lift = (ehit / ubase) if (np.isfinite(ehit) and np.isfinite(ubase)
                                      and ubase > 0) else np.nan
            days = n_on_days if on else (n_days - n_on_days)
            lines.append(
                f"| {th*100:.0f}% | {'on' if on else 'off'} | "
                f"{days:,}日（{days/n_days*100:.1f}%） | {len(uj):,} | {_pct(ubase)} | "
                f"{len(e5):,} | {len(ej):,} | {_pct(ehit)} | "
                f"{_num(lift)}倍 |"
            )
    head = (
        f"R4 状態は「主役テーマ（代金シェア20日平均が閾値以上）の5日平均が20日平均の"
        f"0.85倍以下に折れている日」。**条件付きリフト = 同じ状態の無条件的中率で割った"
        f"倍率**（状態そのものの効果を差し引き、E5 という絞り込みが精度を上げているかだけを見る）。"
        f"全営業日 {n_days:,} 日。\n"
    )
    return head + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# (e) 年別の安定性
# ---------------------------------------------------------------------------
def table_yearly(hist: pd.DataFrame) -> str:
    hist = hist.copy()
    hist["year"] = hist["date"].dt.year
    lines = [
        "| 年 | 全判定可 | 無条件的中率 | D1点灯数 | D1判定可 | D1的中率 | D1リフト | "
        "D1 超過+60日 中央値 | D1 超過+120日 中央値 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for y in sorted(hist["year"].unique()):
        g = hist[hist["year"] == y]
        gj = g[g["rally_120"].notna()]
        base = float(gj["rally_120"].mean()) if len(gj) else np.nan
        d1 = g[g["D1"]]
        dj = d1[d1["rally_120"].notna()]
        hit = float(dj["rally_120"].mean()) if len(dj) else np.nan
        lift = (hit / base) if (np.isfinite(hit) and np.isfinite(base) and base > 0) else np.nan
        e60 = d1["excess_60"].dropna()
        e120 = d1["excess_120"].dropna()
        lines.append(
            f"| {y} | {len(gj):,} | {_pct(base)} | {len(d1):,} | {len(dj):,} | "
            f"{_pct(hit)} | {_num(lift)}倍 | "
            f"{_num(float(e60.median()) if len(e60) else np.nan)}% | "
            f"{_num(float(e120.median()) if len(e120) else np.nan)}% |"
        )
    head = (
        "2026 年は 120 営業日先が期間末を越える行が多く、判定可の行が少ない"
        "（rally_120 が NaN = 判定不可）。的中率の年比較はその母数差を踏まえて読む。\n"
    )
    return head + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# (f) エントリー規則 R0〜R6（第 2 段階・2026-09-07）
# ---------------------------------------------------------------------------
# 第 1 段階の結論「E5 は大相場の的中率を上げるが対市場超過は中央値ゼロ」を受け、
# 目的関数を「いつ・何を買うと +20/+60 営業日の対市場超過が出るか」へ置き直す。
# 全規則は E5 通過（D1）を起点にし、そこへ 1 つの条件を足した部分集合として測る。
#
# 学習期間 2022-01-01〜2024-12-31 で閾値を決め、検証期間 2025-01-01〜2026-09-07 で
# 成績が保たれるかを必ず並記する（out-of-sample）。検証期間で崩れる規則は採用不可。
TRAIN_END = "2024-12-31"
TEST_START = "2025-01-01"

# R4（押し目）の探索窓と価格帯
R4_MIN_LAG, R4_MAX_LAG = 3, 5        # E5 点灯から何営業日以内を探すか
R4_PULLBACK_LO, R4_PULLBACK_HI = -10.0, -3.0   # 高値からの下落率がこの帯に入った日


def _rule_rows(hist: pd.DataFrame, panel: pd.DataFrame) -> dict:
    """規則名 -> その規則が「買う」と判定した行の DataFrame を返す。

    R0〜R3・R5・R6 は E5 当日の行そのものを買う（フィルタ）。R4 だけは E5 の
    「後日」を買うので、E5 点灯日から 3〜5 営業日以内で押し目条件に入った日の行を
    台帳から引き直す（同じテーマ群の別の日の行）。
    """
    d1 = hist[hist["D1"]]
    out: dict = {}
    out["R0"] = d1
    out["R1"] = d1[d1["episode_day"] >= 2]
    out["R2"] = d1[d1["share_accel"] >= 1.2]
    out["R3"] = d1[(d1["breadth_accel"] >= 1.5) & (d1["new_entrants"] >= 2)]
    out["R6"] = d1[d1["lit_days_10"] >= 3]
    # R5 は買う対象（バスケット）が違うだけで行は R0 と同じ。成績は fwd_lit ではなく
    # laggard バスケットの列が要るため、ここでは行だけ返し、集計側で列を差し替える。
    out["R5"] = d1

    # --- R4: E5 点灯から 3〜5 営業日以内に押し目帯へ入った最初の日を買う ---
    dates = sorted(pd.to_datetime(panel["Date"].unique()))
    dpos = {d: i for i, d in enumerate(dates)}
    h = hist.copy()
    h["_i"] = h["date"].map(dpos)
    # (テーマ, 日位置) -> 行番号 の索引
    idx_by_theme: dict = defaultdict(dict)
    for pos, (t, i) in enumerate(zip(h["theme"], h["_i"])):
        if pd.notna(i):
            idx_by_theme[t][int(i)] = pos
    picks = []
    seen = set()
    for t, i0 in zip(d1["theme"], d1["date"].map(dpos)):
        if pd.isna(i0):
            continue
        i0 = int(i0)
        for lag in range(R4_MIN_LAG, R4_MAX_LAG + 1):
            pos = idx_by_theme.get(t, {}).get(i0 + lag)
            if pos is None:
                continue
            pb = h.iloc[pos]["pullback_from_high"]
            if np.isfinite(pb) and R4_PULLBACK_LO <= pb <= R4_PULLBACK_HI:
                if pos not in seen:
                    seen.add(pos)
                    picks.append(pos)
                break            # 最初に条件を満たした日だけ買う
    out["R4"] = h.iloc[sorted(picks)].drop(columns=["_i"]) if picks else h.iloc[0:0].drop(
        columns=["_i"])
    return out


def _rule_stats(sub: pd.DataFrame, base_rate: float) -> dict:
    """1 規則分の成績（件数・的中率・リフト・超過の中央値/平均/勝率）。"""
    s = score_subset(sub, base_rate)
    for h in (20, 60):
        col = sub[f"excess_{h}"].dropna()
        s[f"e{h}_mean"] = float(col.mean()) if len(col) else np.nan
        s[f"e{h}_n"] = int(len(col))
    return s


def _fmt_rule_row(name: str, desc: str, s: dict) -> str:
    return (f"| {name} {desc} | {s['n_lit']:,} | {s['n_judged']:,} | {_pct(s['hit'])} | "
            f"{_num(s['lift'])}倍 | {_num(s['e20_med'])}% | {_num(s['e20_mean'])}% | "
            f"{_pct(s['e20_win'])} | {s['e20_n']:,} | {_num(s['e60_med'])}% | "
            f"{_num(s['e60_mean'])}% | {_pct(s['e60_win'])} | {s['e60_n']:,} |")


RULE_DESC = {
    "R0": "E5 当日に全構成銘柄バスケットを買う（基準線）",
    "R1": "E5 かつ episode_day>=2（継続確認後）",
    "R2": "E5 かつ share_accel>=1.2（資金シェアの加速）",
    "R3": "E5 かつ breadth_accel>=1.5 かつ new_entrants>=2（広がりが拡大中）",
    "R4": "E5 点灯から3〜5営業日以内に高値から−3〜−10%へ入った日を買う（押し目）",
    "R5": "E5 当日に laggard バスケット（出遅れの厚いテーマ）を買う",
    "R6": "E5 かつ lit_days_10>=3（持続）",
}


def table_entry_rules(hist: pd.DataFrame, panel: pd.DataFrame) -> str:
    """(f-1) 全期間でのエントリー規則の成績。"""
    base_rate = float(hist[hist["rally_120"].notna()]["rally_120"].mean())
    rows = _rule_rows(hist, panel)
    # R5 は laggard_ratio 上位（テーマ内の出遅れが厚い側）へ絞る近似
    r5_thr = float(rows["R5"]["laggard_ratio"].quantile(0.75))
    rows["R5"] = rows["R5"][rows["R5"]["laggard_ratio"] >= r5_thr]

    lines = [
        f"無条件ベースラインの大相場的中率 **{base_rate*100:.2f}%**。"
        f"R5 の出遅れ判定は laggard_ratio の上位25%（>= {r5_thr:.3f}）で近似した。\n",
        "| 規則 | 件数 | 判定可 | 大相場的中率 | リフト | 超過+20日 中央値 | 平均 | 勝率 | n | "
        "超過+60日 中央値 | 平均 | 勝率 | n |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k in ("R0", "R1", "R2", "R3", "R4", "R5", "R6"):
        lines.append(_fmt_rule_row(k, RULE_DESC[k], _rule_stats(rows[k], base_rate)))
    return "\n".join(lines)


def table_entry_rules_oos(hist: pd.DataFrame, panel: pd.DataFrame) -> str:
    """(f-2) 学習期間 / 検証期間に分けた成績（out-of-sample）。"""
    rows_all = _rule_rows(hist, panel)
    r5_thr_train = None
    tr_mask = hist["date"] <= pd.Timestamp(TRAIN_END)
    base_tr = float(hist[tr_mask & hist["rally_120"].notna()]["rally_120"].mean())
    base_te = float(hist[~tr_mask & hist["rally_120"].notna()]["rally_120"].mean())

    # R5 の閾値は**学習期間だけ**で決める（検証期間の情報を使わない）
    d1_tr = hist[hist["D1"] & tr_mask]
    r5_thr_train = float(d1_tr["laggard_ratio"].quantile(0.75))
    rows_all["R5"] = rows_all["R5"][rows_all["R5"]["laggard_ratio"] >= r5_thr_train]

    lines = [
        f"学習期間 2022-01-05〜{TRAIN_END} / 検証期間 {TEST_START}〜2026-09-07。"
        f"R5 の laggard_ratio 閾値は学習期間の上位25%（>= {r5_thr_train:.3f}）で固定し、"
        f"検証期間へそのまま適用した。無条件的中率は学習 **{base_tr*100:.2f}%** / "
        f"検証 **{base_te*100:.2f}%**（大相場ラベルの 88% が 2025 年に集中するため"
        f"両期間の水準は直接比べられない。判定は同じ期間の R0 対比で行う）。\n",
        "| 規則 | 期間 | 件数 | 判定可 | 的中率 | リフト | 超過+20日 中央値/勝率 | "
        "超過+60日 中央値/勝率 | 判定 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    _r0 = rows_all["R0"]
    r0_tr = _rule_stats(_r0[_r0["date"] <= pd.Timestamp(TRAIN_END)], base_tr)
    r0_te = _rule_stats(_r0[_r0["date"] >= pd.Timestamp(TEST_START)], base_te)

    verdicts = {}
    for k in ("R0", "R1", "R2", "R3", "R4", "R5", "R6"):
        sub = rows_all[k]
        tr = sub[sub["date"] <= pd.Timestamp(TRAIN_END)]
        te = sub[sub["date"] >= pd.Timestamp(TEST_START)]
        s_tr = _rule_stats(tr, base_tr)
        s_te = _rule_stats(te, base_te)
        # 判定は **同じ期間の R0 対比** で行う。大相場ラベルの 88% が 2025 年に
        # 集中し、学習期間と検証期間で市場環境そのものが違う（無条件的中率 0.12%
        # 対 6.02%）ため、超過中央値の絶対水準で比べると環境差を規則の優劣と
        # 取り違える。R0 は「E5 当日をそのまま買う」基準線であり、規則を足した
        # 意味があるのは R0 を両期間で上回った時だけ。
        if k == "R0":
            v = "基準線"
        elif s_te["e60_n"] < 100:
            v = "母数不足"
        else:
            up_tr = (np.isfinite(s_tr["e60_med"]) and np.isfinite(r0_tr["e60_med"])
                     and s_tr["e60_med"] > r0_tr["e60_med"])
            up_te = (np.isfinite(s_te["e60_med"]) and np.isfinite(r0_te["e60_med"])
                     and s_te["e60_med"] > r0_te["e60_med"])
            if up_tr and up_te:
                v = "**採用可**（両期間で R0 超え）"
            elif up_te and not up_tr:
                v = "採用不可（学習期間で R0 割れ）"
            elif up_tr and not up_te:
                v = "**採用不可**（検証期間で崩れた）"
            else:
                v = "**採用不可**（両期間で R0 割れ）"
        verdicts[k] = v
        for lab, s in (("学習", s_tr), ("検証", s_te)):
            lines.append(
                f"| {k}{'' if lab=='学習' else ''} | {lab} | {s['n_lit']:,} | "
                f"{s['n_judged']:,} | {_pct(s['hit'])} | {_num(s['lift'])}倍 | "
                f"{_num(s['e20_med'])}% / {_pct(s['e20_win'])} | "
                f"{_num(s['e60_med'])}% / {_pct(s['e60_win'])} | "
                f"{v if lab=='検証' else ''} |")
    return "\n".join(lines)


def table_entry_rules_yearly(hist: pd.DataFrame, panel: pd.DataFrame) -> str:
    """(f-3) 規則別の年別安定性（+60 営業日の対市場超過の中央値）。"""
    rows = _rule_rows(hist, panel)
    thr = float(rows["R5"]["laggard_ratio"].quantile(0.75))
    rows["R5"] = rows["R5"][rows["R5"]["laggard_ratio"] >= thr]
    years = sorted({int(y) for y in hist["date"].dt.year.unique()})
    lines = ["| 規則 | " + " | ".join(f"{y}年" for y in years) + " |",
             "|---" * (len(years) + 1) + "|"]
    for k in ("R0", "R1", "R2", "R3", "R4", "R5", "R6"):
        sub = rows[k]
        cells = []
        for y in years:
            s = sub[sub["date"].dt.year == y]["excess_60"].dropna()
            cells.append("-" if len(s) == 0 else f"{s.median():.2f}%({len(s)})")
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("括弧内は判定可の件数。")
    return "\n".join(lines)


def table_entry_rules_capture(hist: pd.DataFrame, panel: pd.DataFrame) -> str:
    """(f-4) 規則別の大相場的中率・リフト・捕捉率（併記の指示による）。"""
    ep = build_rally_episodes(hist, panel)
    base_rate = float(hist[hist["rally_120"].notna()]["rally_120"].mean())
    rows = _rule_rows(hist, panel)
    thr = float(rows["R5"]["laggard_ratio"].quantile(0.75))
    rows["R5"] = rows["R5"][rows["R5"]["laggard_ratio"] >= thr]
    lines = ["| 規則 | 的中率 | リフト | 捕捉率 | 先行日数中央値 |", "|---|---|---|---|---|"]
    for k in ("R0", "R1", "R2", "R3", "R4", "R5", "R6"):
        sub = rows[k]
        s = _rule_stats(sub, base_rate)
        tmp = hist.copy()
        flag = f"_rule_{k}"
        tmp[flag] = False
        tmp.loc[sub.index, flag] = True
        cap, ncap, ntgt, lead = capture_and_lead(tmp, ep, panel, flag)
        lines.append(f"| {k} | {_pct(s['hit'])} | {_num(s['lift'])}倍 | "
                     f"{_pct(cap)}（{ncap}/{ntgt}） | {_num(lead, 0)} |")
    return "\n".join(lines)


def table_feature_coverage(hist: pd.DataFrame) -> str:
    """(f-5) 特徴量の非欠損率（coverage）。指示により集計に必ず明記する。"""
    cols = ["lit_days_10", "episode_day", "breadth_accel", "share_accel",
            "new_entrants", "new_entrant_ratio", "laggard_ratio",
            "pullback_from_high", "rank_new_entry", "catalyst_days_10"]
    lines = ["| 特徴量 | 非欠損 | 非欠損率 | 中央値 | 備考 |", "|---|---|---|---|---|"]
    notes = {
        "breadth_accel": "直近5営業日に一度も点灯していない行は分母が無く NaN",
        "laggard_ratio": "20営業日リターンが取れない期首と、母集団に無い構成銘柄のみの群は NaN",
        "rank_new_entry": "みんかぶランキングの履歴が 2026-05-16 以降しかない",
        "catalyst_days_10": "「なぜ動いた」記録の蓄積が 2026-09-03 以降しかない",
    }
    for c in cols:
        s = hist[c].dropna()
        lines.append(f"| `{c}` | {len(s):,} | {len(s)/len(hist)*100:.1f}% | "
                     f"{_num(float(s.median())) if len(s) else '-'} | {notes.get(c, '')} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="テーマ初動検知の成績測定")
    ap.add_argument("--out", default=None, help="Markdown の保存先（省略時は標準出力のみ）")
    ap.add_argument("--entry-rules", action="store_true",
                    help="エントリー規則 R0〜R6 の成績と out-of-sample 検証だけを出す")
    args = ap.parse_args()

    hist = pd.read_parquet(HISTORY)
    panel = pd.read_parquet(PANEL_PT, columns=["Date", "Code", "DailyReturn"])
    hist["date"] = pd.to_datetime(hist["date"])
    panel["Date"] = pd.to_datetime(panel["Date"])

    if args.entry_rules:
        parts = [
            "## (f-0) 特徴量の非欠損率（coverage）\n\n" + table_feature_coverage(hist),
            "\n\n## (f-1) エントリー規則 R0〜R6 の成績（全期間）\n\n"
            + table_entry_rules(hist, panel),
            "\n\n## (f-2) 学習期間 / 検証期間（out-of-sample）\n\n"
            + table_entry_rules_oos(hist, panel),
            "\n\n## (f-3) 規則別の年別安定性（超過+60営業日の中央値）\n\n"
            + table_entry_rules_yearly(hist, panel),
            "\n\n## (f-4) 大相場的中率・リフト・捕捉率\n\n"
            + table_entry_rules_capture(hist, panel),
        ]
    else:
        parts = [
            "## (a) 初動定義 D1〜D5 の成績\n\n" + table_definitions(hist, panel),
            "\n\n## (b) E5 の閾値格子（感度分析）\n\n" + table_grid(hist, panel),
            "\n\n## (c) R4 の主役閾値 3% / 5% / 8%\n\n" + table_r4(hist),
            "\n\n## (e) 年別の安定性\n\n" + table_yearly(hist),
        ]
    md = "".join(parts) + "\n"
    sys.stdout.reconfigure(encoding="utf-8")
    print(md)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        print(f"[write] {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
