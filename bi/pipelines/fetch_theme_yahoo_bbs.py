"""テーマ寄与銘柄のYahoo!ファイナンス掲示板 ETL（Phase 6 v2・市場別）。

theme_metrics_{market}.parquet の表示対象テーマ（急上昇/既注目/売買代金変化≥1.5）の
top_contributors 上位5銘柄について Yahoo掲示板の最新投稿を取得する。

出力:
  bi/outputs/theme_yahoo_bbs_{market}.parquet
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from make_mover_report import fetch_yahoo_bbs

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[2]

MAX_POSTS_PER_CODE = 20
SLEEP = 1.5  # IPブロック回避のためsleep延長


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["growth", "prime", "standard", "all"], default="growth")
    args = parser.parse_args()
    market = args.market
    theme_metrics_path = REPO_ROOT / "bi" / "outputs" / f"theme_metrics_{market}.parquet"
    out_path = REPO_ROOT / "bi" / "outputs" / f"theme_yahoo_bbs_{market}.parquet"

    print(f"=== テーマYahoo掲示板 ETL (market={market}) ===")
    df = pd.read_parquet(theme_metrics_path)
    target = df[
        df["is_rise"]
        | df["is_popular_either"]
        | (df["value_change_ratio"] >= 1.5)
    ]
    print(f"対象テーマ: {len(target)} / 全テーマ {len(df)}")
    codes = sorted(collect_top_contributor_codes(target))
    print(f"対象銘柄: {len(codes)}")
    if not codes:
        print("ERROR: 銘柄リスト空")
        return 1
    fetched_at = datetime.now(JST).isoformat(timespec="seconds")
    rows: list[dict] = []
    fail = 0
    for i, code in enumerate(codes, 1):
        if i == 1 or i % 30 == 0 or i == len(codes):
            print(f"  [{i:3d}/{len(codes)}] code={code}")
        try:
            result = fetch_yahoo_bbs(code, max_posts=MAX_POSTS_PER_CODE)
        except Exception as e:
            print(f"  [WARN] {code} failed: {e}")
            fail += 1
            time.sleep(SLEEP)
            continue
        if not result["posts"]:
            fail += 1
        for p in result["posts"]:
            rows.append({
                "code": code,
                "sentiment": result.get("sentiment", ""),
                "post_no": p.get("no", ""),
                "post_date": p.get("date", ""),
                "post_body": p.get("body", ""),
                "yes_count": int(p.get("yes", 0)),
                "no_count": int(p.get("no_count", 0)),
                "fetched_at": fetched_at,
            })
        time.sleep(SLEEP)

    out_df = pd.DataFrame(rows)
    print(f"\n total posts: {len(out_df):,}")
    print(f" unique codes with posts: {out_df['code'].nunique() if len(out_df) else 0}")
    print(f" fail count: {fail}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(out_path, index=False)
    print(f" saved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
