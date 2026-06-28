"""セクター週末リッチレポートの Markdown を parquet から決定論的に生成する（LLM 非使用）。

PM 2026-06-28: セクターは週末のみ・データ駆動の新フォーマットへ刷新。Deep Research（GHA 失敗の主因）を
撤廃し、本スクリプトが sector_weekly / sector_stock_weekly / screening_master の3 parquet から
「強弱ランキング・8週トレンド帯・資金フロー・52週高値圏・主役Top5/下落主役Bottom5・資金の流れ」を
表組み中心の markdown として出力する。色付け（上昇緑/下落赤）・表装飾は lib/md_to_pdf 側で付与。

出力フォーマットは scratchpad の承認サンプルに準拠。3M/1Y は集計歪みのため出力しない。
構成1銘柄の「その他」業種は強弱から除外。巨大業種の累計が小型株はずれ値で歪む場合は累計を非表示にする。

使い方:
    python make_sector_rich_md.py [--date YYYY-MM-DD] [--out PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "bi" / "outputs"


def _fmt_pct(x: float, plus: bool = True) -> str:
    """小数（0.021）→ '+2.1%'。"""
    v = x * 100
    s = f"{v:+.1f}%" if plus else f"{v:.1f}%"
    return s


def _fmt_mcap(v: float) -> str:
    if pd.isna(v):
        return ""
    if v >= 1e12:
        return f"{v / 1e12:.2f}兆円"
    return f"{v / 1e8:,.0f}億円"


def _arrows(returns: list[float]) -> str:
    """W08→W01（古→新）の順で ▲（>0）/▼（≤0）。"""
    out = []
    for r in returns:
        if pd.isna(r):
            out.append("・")
        elif r > 0:
            out.append("▲")
        else:
            out.append("▼")
    return "".join(out)


def _flow_label(ratio: float) -> str:
    if ratio > 0.10:
        return "流入継続"
    if ratio < -0.10:
        return "流出"
    return "横ばい"


def load_data():
    sw = pd.read_parquet(OUT_DIR / "sector_weekly.parquet")
    ssw = pd.read_parquet(OUT_DIR / "sector_stock_weekly.parquet")
    sm = pd.read_parquet(OUT_DIR / "screening_master.parquet")
    valid = set(sm["Code"].astype(str))
    ssw = ssw[ssw["Code"].astype(str).isin(valid)].copy()  # ETF/REIT 除外（個別株のみ）
    return sw, ssw


def sector_fund_flow(ssw_sec: pd.DataFrame) -> float | None:
    a = ssw_sec["ValAvg5d_BlkSeq01"].sum(min_count=1)
    b = ssw_sec["ValAvg5d_BlkSeq04"].sum(min_count=1)
    if pd.isna(a) or pd.isna(b) or b == 0:
        return None
    return a / b - 1


def high52_ratio(ssw_sec: pd.DataFrame) -> tuple[int, int, float]:
    col = ssw_sec["52W_High_Ratio"].dropna()
    n = len(col)
    m = int((col >= -0.10).sum())
    p = (m / n * 100) if n else 0.0
    return n, m, p


def stock_table(ssw_sec: pd.DataFrame, top: bool) -> list[str]:
    s = ssw_sec.dropna(subset=["Return_W01"]).sort_values("Return_W01", ascending=not top)
    s = s.head(5)
    rows = ["| 順位 | コード | 銘柄 | 週間 | 時価総額 |", "|:--:|:--:|:--|--:|--:|"]
    for i, (_, r) in enumerate(s.iterrows(), 1):
        rows.append(
            f"| {i} | {r['Code']} | {r['CompanyName']} | {_fmt_pct(r['Return_W01'])} | {_fmt_mcap(r['MarketCap'])} |"
        )
    return rows


def sector_block(name: str, swrow: pd.Series, ssw_sec: pd.DataFrame, strong: bool) -> list[str]:
    wk = swrow["Return_W01"]
    out = [f"### {name}　{_fmt_pct(wk)}"]

    # 8週トレンド帯（W08→W01）＋累計（異常値ガード）
    w = [swrow[f"Return_W{i:02d}"] for i in range(8, 0, -1)]
    arrows = _arrows(w)
    cum = sum(x for x in w if pd.notna(x)) * 100
    anomalous = (abs(cum) > 30) or any(pd.notna(x) and abs(x) > 0.5 for x in w)

    flow = sector_fund_flow(ssw_sec)
    n, m, p = high52_ratio(ssw_sec)
    flow_str = f"{flow * 100:+.1f}%（{_flow_label(flow)}）" if flow is not None else "—"

    if anomalous:
        metrics = (
            f"**8週トレンド** {arrows}　｜　**資金フロー** {flow_str}　｜　"
            f"**52週高値圏** {n}銘柄中{m}（{p:.1f}%）"
        )
        out.append(metrics)
        out.append(
            f"\n*※本業種は構成{int(swrow['StockCount'])}銘柄と巨大で、中長期の加重平均が小型株のはずれ値"
            f"（株式分割の権利落ち未調整の疑い）に強く歪むため、累計・中長期数値は非表示とした"
            f"（集計バグは別途修正）。*\n"
        )
    else:
        metrics = (
            f"**8週トレンド** {arrows}（累計 {cum:+.1f}%）　｜　**資金フロー** {flow_str}　｜　"
            f"**52週高値圏** {n}銘柄中{m}（{p:.1f}%）"
        )
        out.append(metrics)

    out.append("")
    out.extend(stock_table(ssw_sec, top=strong))
    out.append("")
    return out


def build(date: str | None, out_path: Path | None) -> Path:
    sw, ssw = load_data()
    as_of = str(sw["AsOf"].iloc[0])
    price_asof = str(sw["PriceDataAsOf"].iloc[0])
    date = date or as_of

    ranked = sw[sw["StockCount"] > 1].sort_values("Return_W01", ascending=False).reset_index(drop=True)
    strong = ranked.head(5)
    weak = ranked.tail(5).iloc[::-1].reset_index(drop=True)

    def ssw_of(sec):
        return ssw[ssw["Sector17CodeName"] == sec]

    # 資金の流れ（全業種・対象個別株ベース）
    flows = []
    for _, r in ranked.iterrows():
        f = sector_fund_flow(ssw_of(r["Sector17CodeName"]))
        if f is not None:
            flows.append((r["Sector17CodeName"], f))
    inflow = sorted([x for x in flows if x[1] > 0.10], key=lambda t: -t[1])
    outflow = sorted([x for x in flows if x[1] < -0.10], key=lambda t: t[1])

    n_pos = int((ranked["Return_W01"] > 0).sum())
    pos_names = list(ranked[ranked["Return_W01"] > 0]["Sector17CodeName"])
    pos_str = "・".join(pos_names) if len(pos_names) <= 5 else f"{len(pos_names)}業種"
    top1, top1w = strong.iloc[0]["Sector17CodeName"], strong.iloc[0]["Return_W01"]
    bot1, bot1w = weak.iloc[0]["Sector17CodeName"], weak.iloc[0]["Return_W01"]
    infl_lead = "、".join(f"{s} {v * 100:+.1f}%" for s, v in inflow[:2]) or "なし"
    outfl_lead = "、".join(f"{s} {v * 100:+.1f}%" for s, v in outflow) or "なし"

    L = []
    L.append(f"# セクターレポート {date}（週末版）")
    L.append("")
    L.append(f"※時刻は日本時間。株価は {price_asof} 終値ベース・集計 {as_of}。")
    L.append("")
    L.append(
        f"> **今日の注目** ── {len(ranked)}業種中、週間プラスは{n_pos}業種（{pos_str}）。"
        f"最強は{top1} {_fmt_pct(top1w)}、最弱は{bot1} {_fmt_pct(bot1w)}。"
        f"資金は4週前比で{infl_lead} など広く流入継続、流出は{outfl_lead}。"
        f"「その他」業種は構成1銘柄のため強弱から除外。"
    )
    L.append("")
    L.append("## セクター強弱（週間騰落率）")
    L.append("")
    L.append("| 順位 | 強いセクター | 週間 | 弱いセクター | 週間 |")
    L.append("|:--:|:--|--:|:--|--:|")
    for i in range(5):
        L.append(
            f"| {i + 1} | {strong.iloc[i]['Sector17CodeName']} | {_fmt_pct(strong.iloc[i]['Return_W01'])} "
            f"| {weak.iloc[i]['Sector17CodeName']} | {_fmt_pct(weak.iloc[i]['Return_W01'])} |"
        )
    L.append("")
    L.append("## 強いセクター 深掘り（Top5）")
    L.append("")
    for _, r in strong.iterrows():
        L.extend(sector_block(r["Sector17CodeName"], r, ssw_of(r["Sector17CodeName"]), strong=True))
    L.append("## 弱いセクター 深掘り（Bottom5）")
    L.append("")
    for _, r in weak.iterrows():
        L.extend(sector_block(r["Sector17CodeName"], r, ssw_of(r["Sector17CodeName"]), strong=False))
    L.append("## 資金の流れ（4週前比）")
    L.append("")
    L.append("**🔥 流入継続**　" + "、".join(f"{s} {v * 100:+.1f}%" for s, v in inflow))
    L.append("")
    L.append("**🧊 流出**　" + ("、".join(f"{s} {v * 100:+.1f}%" for s, v in outflow) or "なし"))
    L.append("")

    md = "\n".join(L)
    out_path = out_path or (REPO_ROOT / "market" / "daily" / "sector" / f"{date}_full.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = build(args.date, Path(args.out) if args.out else None)
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
