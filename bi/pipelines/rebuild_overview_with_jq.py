"""
JQuants get_fin_summary で取得した確定発表日（DiscDate）を真の発表日として、
yfinanceの株価データから当日・翌日リターンを再計算する。

- 入力月の jq_statements.csv（または jq_statements_{month}.csv）を使う
- 確定発表日（DiscDate）が無い銘柄は no_may_disclosure として除外

使い方:
  python rebuild_overview_with_jq.py                 # 今月
  python rebuild_overview_with_jq.py --month 2026-05

出力:
  research/earnings/overview_table.csv（最新月で上書き）
  research/earnings/overview_table_{month}.csv
  research/earnings/overview_sector_reaction.csv（最新月で上書き）
  research/earnings/overview_sector_reaction_{month}.csv
"""

from __future__ import annotations

import argparse
import calendar
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent.parent
ENRICHED_DIR = ROOT / "research/earnings"
SM = ROOT / "bi/outputs/screening_master.parquet"
COV_CSV = ROOT / "research/earnings/coverage_stocks.csv"


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start = (date(y, m, 1) - timedelta(days=7)).strftime("%Y-%m-%d")
    end = (date(y, m, last_day) + timedelta(days=6)).strftime("%Y-%m-%d")
    return start, end


def fetch_price_panel(codes: list[str], start: str, end: str) -> pd.DataFrame:
    tickers = [f"{c}.T" for c in codes]
    raw = yf.download(
        tickers=" ".join(tickers),
        start=start,
        end=end,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
    )
    rows = []
    for code in codes:
        ticker = f"{code}.T"
        try:
            df = raw[ticker].dropna(how="all")
        except KeyError:
            continue
        if df.empty or "Close" not in df.columns:
            continue
        for idx, r in df.iterrows():
            rows.append({
                "code": code,
                "date": idx.date(),
                "close": float(r["Close"]) if pd.notna(r["Close"]) else None,
                "volume": float(r["Volume"]) if pd.notna(r["Volume"]) else None,
            })
    return pd.DataFrame(rows)


def compute_returns_at(price_panel: pd.DataFrame, code: str, announce_date) -> dict:
    df = price_panel[price_panel["code"] == code].copy().sort_values("date").reset_index(drop=True)
    if df.empty:
        return {"peak_day_ret": None, "next_day_ret": None, "next_date": None}
    df["prev_close"] = df["close"].shift(1)
    df["ret"] = df["close"] / df["prev_close"] - 1
    target = pd.Timestamp(announce_date).date()
    idx_at = df.index[df["date"] == target]
    if len(idx_at) == 0:
        future = df[df["date"] > target]
        if future.empty:
            return {"peak_day_ret": None, "next_day_ret": None, "next_date": None}
        idx_at = future.index[:1]
    i = int(idx_at[0])
    peak_day_ret = df.iloc[i]["ret"]
    next_date = None
    next_day_ret = None
    if i + 1 < len(df):
        next_date = df.iloc[i + 1]["date"]
        next_day_ret = df.iloc[i + 1]["ret"]
    return {"peak_day_ret": peak_day_ret, "next_day_ret": next_day_ret, "next_date": next_date}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--month", help="対象月 YYYY-MM（既定: 今月）")
    args = p.parse_args()

    month = args.month or date.today().strftime("%Y-%m")
    start, end = month_bounds(month)
    print(f"対象月: {month}（株価期間 {start} 〜 {end}）")

    jq_path = ENRICHED_DIR / f"jq_statements_{month}.csv"
    if not jq_path.exists():
        jq_path = ENRICHED_DIR / "jq_statements.csv"
    jq = pd.read_csv(jq_path) if jq_path.exists() else pd.DataFrame()

    sm = pd.read_parquet(SM)
    cov = pd.read_csv(COV_CSV)
    cov["code"] = cov["code"].astype(str)
    sm["Code"] = sm["Code"].astype(str)
    if not jq.empty:
        jq["code"] = jq["code"].astype(str)

    base = cov.merge(
        sm[["Code", "Sector17CodeName", "MarketCap", "PER_Trailing", "PBR_Trailing", "ROE_LatestYear"]],
        left_on="code",
        right_on="Code",
        how="left",
    ).drop(columns=["Code"])

    if not jq.empty:
        base = base.merge(jq[["code", "DiscDate", "DiscTime", "DocType"]], on="code", how="left")
        base["announce_date"] = base["DiscDate"]
        base["announce_time"] = base["DiscTime"]
        base["doc_type"] = base["DocType"]
        base["date_source"] = base["DiscDate"].apply(lambda x: "jq_confirmed" if pd.notna(x) else "no_disclosure")
    else:
        base["announce_date"] = None
        base["announce_time"] = None
        base["doc_type"] = None
        base["date_source"] = "no_disclosure"

    codes = base["code"].tolist()
    print(f"yfinance bulk download: {len(codes)} tickers")
    price_panel = fetch_price_panel(codes, start, end)
    print(f"price panel rows: {len(price_panel)}")

    out_rows = []
    for _, r in base.iterrows():
        code = r["code"]
        announce = r.get("announce_date")
        if pd.notna(announce):
            rx = compute_returns_at(price_panel, code, announce)
        else:
            announce = None
            rx = {"peak_day_ret": None, "next_day_ret": None, "next_date": None}
        mc = r.get("MarketCap")
        out_rows.append({
            "code": code,
            "name": r.get("name"),
            "Sector17CodeName": r.get("Sector17CodeName"),
            "mc_cho": round(mc / 1e12, 2) if pd.notna(mc) else None,
            "mc_oku": round(mc / 1e8, 0) if pd.notna(mc) else None,
            "announce_date": announce,
            "announce_time": r.get("announce_time"),
            "doc_type": r.get("doc_type"),
            "date_source": r.get("date_source"),
            "peak_day_ret": round(float(rx["peak_day_ret"]), 4) if rx["peak_day_ret"] is not None and pd.notna(rx["peak_day_ret"]) else None,
            "next_day_ret": round(float(rx["next_day_ret"]), 4) if rx["next_day_ret"] is not None and pd.notna(rx["next_day_ret"]) else None,
            "PER_Trailing": r.get("PER_Trailing"),
            "PBR_Trailing": r.get("PBR_Trailing"),
            "ROE_LatestYear": r.get("ROE_LatestYear"),
        })

    out = pd.DataFrame(out_rows)
    out["announce_date"] = pd.to_datetime(out["announce_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out.sort_values(["announce_date", "mc_cho"], ascending=[True, False], na_position="last")

    out_latest = ENRICHED_DIR / "overview_table.csv"
    out_month_file = ENRICHED_DIR / f"overview_table_{month}.csv"
    out.to_csv(out_latest, encoding="utf-8", index=False)
    out.to_csv(out_month_file, encoding="utf-8", index=False)
    print(f"保存: {out_latest}")
    print(f"保存: {out_month_file}")
    print()
    print("=== date_source ===")
    print(out["date_source"].value_counts().to_string())
    print()
    print("=== 発表日別 件数 ===")
    print(out["announce_date"].astype(str).value_counts().sort_index().to_string())
    print()

    sector_agg = (
        out.groupby("Sector17CodeName")
        .agg(
            count=("code", "count"),
            peak_day_ret_median=("peak_day_ret", "median"),
            peak_day_ret_p10=("peak_day_ret", lambda s: s.quantile(0.10)),
            peak_day_ret_p90=("peak_day_ret", lambda s: s.quantile(0.90)),
            next_day_ret_median=("next_day_ret", "median"),
        )
        .sort_values("peak_day_ret_median", ascending=False)
    )
    print("=== セクター反応中央値 ===")
    for sec, row in sector_agg.iterrows():
        pm = row["peak_day_ret_median"]
        nm = row["next_day_ret_median"]
        pm_s = f"{pm:+.2%}" if pd.notna(pm) else "n/a"
        nm_s = f"{nm:+.2%}" if pd.notna(nm) else "n/a"
        print(f"  {str(sec)[:25]:25s} n={int(row['count']):3d}  当日 {pm_s:7s}  翌日 {nm_s}")

    out_sector_latest = ENRICHED_DIR / "overview_sector_reaction.csv"
    out_sector_month = ENRICHED_DIR / f"overview_sector_reaction_{month}.csv"
    sector_agg.reset_index().to_csv(out_sector_latest, encoding="utf-8", index=False)
    sector_agg.reset_index().to_csv(out_sector_month, encoding="utf-8", index=False)
    print(f"保存: {out_sector_latest}")
    print(f"保存: {out_sector_month}")


if __name__ == "__main__":
    main()
