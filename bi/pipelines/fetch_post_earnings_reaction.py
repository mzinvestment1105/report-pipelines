"""
yfinance で全カバレッジ銘柄の対象月周辺の日次株価を取得し、決算後の株価反応を計算する。

- 期間: 対象月の月初前1週間 〜 月末+5日（翌月確認用）
- yfinanceで全銘柄を一括取得し、出来高スパイクの最大日を推定発表日として記録
  （JQuantsで確定発表日が取れる前提で rebuild_overview_with_jq.py が使う）

使い方:
  python fetch_post_earnings_reaction.py                 # 今月
  python fetch_post_earnings_reaction.py --month 2026-05

出力:
  research/earnings/post_earnings_reaction.csv（最新月で上書き）
"""

from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent.parent
COVERAGE_CSV = ROOT / "research/earnings/coverage_stocks.csv"
OUT_CSV = ROOT / "research/earnings/post_earnings_reaction.csv"


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start = (date(y, m, 1) - timedelta(days=7)).strftime("%Y-%m-%d")
    end = (date(y, m, last_day) + timedelta(days=6)).strftime("%Y-%m-%d")
    return start, end


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--month", help="対象月 YYYY-MM（既定: 今月）")
    args = p.parse_args()

    month = args.month or date.today().strftime("%Y-%m")
    start, end = month_bounds(month)
    month_start = f"{month}-01"

    cov = pd.read_csv(COVERAGE_CSV)
    cov["code"] = cov["code"].astype(str)
    tickers = [f"{c}.T" for c in cov["code"]]

    print(f"yfinance bulk download: {len(tickers)} tickers ({start} 〜 {end})")
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

    records: list[dict] = []
    for code in cov["code"]:
        ticker = f"{code}.T"
        try:
            df = raw[ticker].dropna(how="all")
        except KeyError:
            records.append({"code": code, "status": "TICKER_MISSING"})
            continue

        if df.empty or "Volume" not in df.columns:
            records.append({"code": code, "status": "EMPTY"})
            continue

        df = df.copy()
        df["prev_close"] = df["Close"].shift(1)
        df["ret"] = df["Close"] / df["prev_close"] - 1
        df["vol_avg5"] = df["Volume"].rolling(5).mean().shift(1)
        df["vol_spike"] = df["Volume"] / df["vol_avg5"]

        df_in = df[df.index >= pd.Timestamp(month_start)].copy()
        if df_in.empty:
            records.append({"code": code, "status": "NO_MONTH"})
            continue

        peak_idx = df_in["vol_spike"].idxmax()
        if pd.isna(peak_idx):
            peak_idx = df_in["Volume"].idxmax()

        peak_row = df_in.loc[peak_idx]
        peak_date = peak_idx.date()
        after_rows = df_in[df_in.index > peak_idx]
        next_ret = after_rows.iloc[0]["ret"] if len(after_rows) > 0 else None
        next_date = after_rows.index[0].date() if len(after_rows) > 0 else None

        records.append({
            "code": code,
            "peak_date": peak_date,
            "peak_vol_spike": round(float(peak_row["vol_spike"]), 2) if pd.notna(peak_row["vol_spike"]) else None,
            "peak_close": round(float(peak_row["Close"]), 2) if pd.notna(peak_row["Close"]) else None,
            "peak_day_ret": round(float(peak_row["ret"]), 4) if pd.notna(peak_row["ret"]) else None,
            "next_date": next_date,
            "next_day_ret": round(float(next_ret), 4) if next_ret is not None and pd.notna(next_ret) else None,
            "status": "OK",
        })

    out = pd.DataFrame(records).merge(cov[["code", "name", "sectors"]], on="code", how="left")
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_CSV, encoding="utf-8", index=False)
    print(f"OK: {(out['status']=='OK').sum()} / {len(out)}")
    print(f"保存: {OUT_CSV}")


if __name__ == "__main__":
    main()
