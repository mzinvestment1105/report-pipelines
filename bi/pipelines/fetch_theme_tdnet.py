"""テーマ寄与銘柄の TDnet 開示取得 ETL。

theme_metrics.parquet の top_contributors からユニーク銘柄リストを抽出し、
やのしん TDnet WebAPI で過去N日分の適時開示タイトルを取得する。

出力:
  bi/outputs/theme_tdnet.parquet
  列: code, title, published_at, fetched_at
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from fetch_tdnet_disclosures import fetch_tdnet_atom

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[2]
THEME_METRICS = REPO_ROOT / "bi" / "outputs" / "theme_metrics.parquet"
OUT_PATH = REPO_ROOT / "bi" / "outputs" / "theme_tdnet.parquet"

LOOKBACK_DAYS = 14
SLEEP = 0.5


def collect_top_contributor_codes(df: pd.DataFrame) -> set[str]:
    codes: set[str] = set()
    for tc_json in df["top_contributors"].dropna():
        try:
            items = json.loads(tc_json)
        except Exception:
            continue
        for it in items:
            c = it.get("code")
            if c:
                codes.add(str(c).zfill(4))
    return codes


def main() -> int:
    print("=== テーマTDnet開示 ETL ===")
    df = pd.read_parquet(THEME_METRICS)
    # レポートに登場するテーマだけに絞る（急上昇軸 or 既注目軸 or 売買代金変化≥1.5）
    target = df[
        df["is_rise"]
        | df["is_popular_either"]
        | (df["value_change_ratio"] >= 1.5)
    ]
    print(f"対象テーマ: {len(target)} / 全テーマ {len(df)}")
    codes = collect_top_contributor_codes(target)
    print(f"top_contributors のユニーク銘柄数: {len(codes)}")
    if not codes:
        print("ERROR: 銘柄リストが空")
        return 1

    fetched_at = datetime.now(JST).isoformat(timespec="seconds")
    cutoff = datetime.now(JST) - timedelta(days=LOOKBACK_DAYS)
    rows: list[dict] = []
    codes_sorted = sorted(codes)
    fail = 0
    for i, code in enumerate(codes_sorted, 1):
        if i == 1 or i % 30 == 0 or i == len(codes_sorted):
            print(f"  [{i:3d}/{len(codes_sorted)}] code={code}")
        try:
            entries, _company_name = fetch_tdnet_atom(code)
        except Exception as ex:
            print(f"  [WARN] {code} fetch failed: {ex}")
            fail += 1
            time.sleep(SLEEP)
            continue
        for e in entries:
            pub = e.get("published") or ""
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=JST)
            except Exception:
                pub_dt = None
            if pub_dt is None or pub_dt < cutoff:
                continue
            rows.append({
                "code": code,
                "title": e.get("title", ""),
                "published_at": pub_dt.isoformat(timespec="seconds"),
                "fetched_at": fetched_at,
            })
        time.sleep(SLEEP)

    out_df = pd.DataFrame(rows)
    print(f"\n total disclosures: {len(out_df):,}")
    print(f" with disclosures  : {out_df['code'].nunique() if len(out_df) else 0}")
    print(f" fail count        : {fail}")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(OUT_PATH, index=False)
    print(f" saved: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
