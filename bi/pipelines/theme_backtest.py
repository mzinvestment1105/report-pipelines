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


def main() -> int:
    ap = argparse.ArgumentParser(description="テーマ初動検知の成績測定")
    ap.add_argument("--out", default=None, help="Markdown の保存先（省略時は標準出力のみ）")
    args = ap.parse_args()

    hist = pd.read_parquet(HISTORY)
    panel = pd.read_parquet(PANEL_PT, columns=["Date", "Code", "DailyReturn"])
    hist["date"] = pd.to_datetime(hist["date"])
    panel["Date"] = pd.to_datetime(panel["Date"])

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
