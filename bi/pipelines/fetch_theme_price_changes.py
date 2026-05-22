"""テーマ別株価変化率 ETL。

JQuants /equities/bars/daily を 4日分（最新営業日・5営業日前・22営業日前・66営業日前）
取得し、全銘柄の 5日変化率・1ヶ月変化率・3ヶ月変化率を算出する。

出力:
  bi/outputs/price_changes.parquet
  列: code, close_now, close_5d, close_1m, close_3m, change_5d_pct, change_1m_pct, change_3m_pct, baseline_date
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import jquantsapi
import pandas as pd
from dotenv import load_dotenv

from jq_client_utils import (
    fetch_paginated_v2,
    latest_trading_day_date_v2,
    normalize_code_4,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "bi" / "outputs" / "price_changes.parquet"
_ENV_PATH = Path(__file__).resolve().parent / ".env"


def fetch_close_on_date(client: jquantsapi.ClientV2, target_date: date) -> pd.DataFrame:
    """指定日の全銘柄 Close を返す。普通株のみ（5桁コード末尾"0"）。"""
    rows = fetch_paginated_v2(
        client,
        "/equities/bars/daily",
        params={"date": target_date.strftime("%Y-%m-%d")},
        sleep_seconds=1.2,
    )
    if not rows:
        return pd.DataFrame(columns=["Code", "Close"])
    df = pd.DataFrame.from_records(rows)
    if "Code" not in df.columns or "C" not in df.columns:
        return pd.DataFrame(columns=["Code", "Close"])
    df = df[df["Code"].astype(str).str.endswith("0")].copy()
    df["Code"] = df["Code"].map(normalize_code_4).astype(str)
    df = df.rename(columns={"C": "Close"})
    df = df[["Code", "Close"]].copy()
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Close"])
    return df


def find_trading_day_near(client: jquantsapi.ClientV2, target: date, max_back_days: int = 10) -> date:
    """target 以前で daily quotes が取れる最初の日付を返す（API1〜10回で済む）。"""
    cur = target
    for _ in range(max_back_days + 1):
        rows = fetch_paginated_v2(
            client,
            "/equities/bars/daily",
            params={"date": cur.strftime("%Y-%m-%d")},
            sleep_seconds=0.5,
        )
        if rows:
            return cur
        cur = cur - timedelta(days=1)
    raise RuntimeError(f"{max_back_days}日連続で営業日が見つからない: target={target}")


def main() -> int:
    load_dotenv(_ENV_PATH)
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY 未設定")
    client = jquantsapi.ClientV2(api_key=api_key)

    print("=== テーマ株価変化率 ETL ===")
    baseline = latest_trading_day_date_v2(client)
    print(f"baseline (最新営業日): {baseline}")

    # カレンダー日数で逆算してから最寄りの営業日を1回だけ確認（API4-12回で完結）
    d_5d_target = baseline - timedelta(days=7)
    d_1m_target = baseline - timedelta(days=32)
    d_3m_target = baseline - timedelta(days=96)
    d_5d = find_trading_day_near(client, d_5d_target)
    print(f"~5営業日前 (target={d_5d_target}): {d_5d}")
    d_1m = find_trading_day_near(client, d_1m_target)
    print(f"~22営業日前 (target={d_1m_target}): {d_1m}")
    d_3m = find_trading_day_near(client, d_3m_target)
    print(f"~66営業日前 (target={d_3m_target}): {d_3m}")

    print("\n取得中: baseline …")
    df_now = fetch_close_on_date(client, baseline).rename(columns={"Close": "close_now"})
    print(f"  rows: {len(df_now):,}")
    print("取得中: 5d …")
    df_5d = fetch_close_on_date(client, d_5d).rename(columns={"Close": "close_5d"})
    print(f"  rows: {len(df_5d):,}")
    print("取得中: 1m …")
    df_1m = fetch_close_on_date(client, d_1m).rename(columns={"Close": "close_1m"})
    print(f"  rows: {len(df_1m):,}")
    print("取得中: 3m …")
    df_3m = fetch_close_on_date(client, d_3m).rename(columns={"Close": "close_3m"})
    print(f"  rows: {len(df_3m):,}")

    merged = df_now.merge(df_5d, on="Code", how="outer")
    merged = merged.merge(df_1m, on="Code", how="outer")
    merged = merged.merge(df_3m, on="Code", how="outer")

    merged["change_5d_pct"] = (merged["close_now"] / merged["close_5d"] - 1.0) * 100
    merged["change_1m_pct"] = (merged["close_now"] / merged["close_1m"] - 1.0) * 100
    merged["change_3m_pct"] = (merged["close_now"] / merged["close_3m"] - 1.0) * 100
    merged.rename(columns={"Code": "code"}, inplace=True)
    merged["baseline_date"] = baseline.isoformat()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(OUT_PATH, index=False)
    print(f"\nsaved: {OUT_PATH}")
    print(f" total rows: {len(merged):,}")
    print(f" with close_now & close_5d: {merged[['close_now','close_5d']].notna().all(axis=1).sum():,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
