"""みんかぶ人気テーマ Top 10 + 急上昇テーマ Top 10 の各テーマで関連度順 Top 5 銘柄＋時価総額を取得。

使い方:
  python fetch_themes_summary_data.py

前提:
  - bi/outputs/theme_momentum.parquet が最新（[fetch_theme_momentum.py](bi/pipelines/fetch_theme_momentum.py) を事前実行）
  - bi/outputs/screening_master.parquet が最新

出力:
  bi/outputs/themes_summary_top5.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_minkabu_themes import fetch_theme_detail

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[2]

MOMENTUM = REPO_ROOT / "bi" / "outputs" / "theme_momentum.parquet"
SCREENING = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"
OUT = REPO_ROOT / "bi" / "outputs" / "themes_summary_top5.json"


def main() -> int:
    mom = pd.read_parquet(MOMENTUM)
    latest = mom["snapshot_date"].max()
    print(f"latest snapshot: {latest}")

    sub = mom[(mom["snapshot_date"] == latest) & (mom["source"] == "minkabu")]
    popular = sub[sub["rank_type"] == "popular"].sort_values("rank").head(10)
    rise = sub[sub["rank_type"] == "rise"].sort_values("rank").head(10)

    scr = pd.read_parquet(SCREENING)
    scr["Code"] = scr["Code"].astype(str).str.replace(".0", "", regex=False).str.zfill(4)
    scr_map = scr.set_index("Code")[["CompanyName", "MarketCodeName", "Close", "MarketCap"]].to_dict("index")

    results: list[dict] = []

    def process(theme_row, rank_type: str) -> dict:
        theme_name = theme_row["theme_name"]
        url = theme_row["theme_url"]
        slug = url.rstrip("/").split("/")[-1]
        rank = int(theme_row["rank"])
        print(f"  [{rank_type}#{rank}] {theme_name}")
        try:
            stocks = fetch_theme_detail(slug, max_pages=3)
        except Exception as e:
            print(f"    fetch_theme_detail ERROR: {e}")
            stocks = []
        top5 = []
        for s in stocks[:5]:
            code = s["code"]
            info = scr_map.get(code, {})
            mcap_okuyen = info.get("MarketCap")
            mcap_okuyen = round(mcap_okuyen / 1e8, 1) if mcap_okuyen else None
            top5.append({
                "code": code,
                "stock_name_minkabu": s["stock_name"],
                "company_name": info.get("CompanyName"),
                "market": info.get("MarketCodeName"),
                "close": info.get("Close"),
                "mcap_okuyen": mcap_okuyen,
            })
        time.sleep(0.5)
        return {
            "rank_type": rank_type,
            "rank": rank,
            "theme_name": theme_name,
            "theme_url": url,
            "slug": slug,
            "top5": top5,
        }

    print("\n=== 人気テーマ Top 10 ===")
    for _, r in popular.iterrows():
        results.append(process(r, "popular"))

    print("\n=== 急上昇テーマ Top 10 ===")
    for _, r in rise.iterrows():
        results.append(process(r, "rise"))

    out_data = {
        "snapshot_date": str(latest),
        "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
        "themes": results,
    }
    OUT.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
