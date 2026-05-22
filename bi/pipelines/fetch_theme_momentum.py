"""テーマ動意検知 ETL（日次snapshot蓄積型）。

3軸テーマ検出フレームワークの「既注目軸」「急上昇軸」を担当する。
日次実行を前提に、既存parquetへ追記（同一snapshot_dateの重複は最新で置換）する。

ソース:
  - 株探 アクセスランキング3日（既注目軸） /info/accessranking/3_2  -> 30件
  - みんかぶ 人気テーマランキング（既注目軸）  /theme/popular_ranking -> 20件
  - みんかぶ 急上昇テーマランキング（急上昇軸） /theme/rise_ranking    -> 20件

出力:
  bi/outputs/theme_momentum.parquet
  列: snapshot_date, source, rank_type, rank, theme_name, theme_url, fetched_at
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}
SLEEP = 0.5
TIMEOUT = 15
JST = timezone(timedelta(hours=9))

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "bi" / "outputs" / "theme_momentum.parquet"

KABUTAN_ACCESS_URL = "https://kabutan.jp/info/accessranking/3_2"
MINKABU_POPULAR_URL = "https://minkabu.jp/theme/popular_ranking"
MINKABU_RISE_URL = "https://minkabu.jp/theme/rise_ranking"
MINKABU_BASE = "https://minkabu.jp"
KABUTAN_BASE = "https://kabutan.jp"
RESERVED_MINKABU_PATHS = {"popular_ranking", "rise_ranking", "new", ""}


def fetch(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except Exception as e:
        print(f"  [WARN] fetch failed {url}: {e}")
        return None
    if r.status_code != 200:
        print(f"  [WARN] status={r.status_code} {url}")
        return None
    return BeautifulSoup(r.text, "html.parser")


def extract_kabutan_access(soup: BeautifulSoup) -> list[dict]:
    """株探アクセスランキング 30件抽出（出現順がランクとして信頼できる前提）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=re.compile(r"/themes/\?theme=")):
        href = a["href"]
        name = a.get_text(strip=True)
        if not name or len(name) > 80:
            continue
        if name in seen:
            continue
        seen.add(name)
        full_url = href if href.startswith("http") else f"{KABUTAN_BASE}{href}"
        out.append({"rank": len(out) + 1, "theme_name": name, "theme_url": full_url})
        if len(out) >= 30:
            break
    return out


def extract_minkabu_ranking(soup: BeautifulSoup, max_n: int = 20) -> list[dict]:
    """みんかぶランキング系のページから 20件抽出。"""
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.match(r"^/theme/([^/?#]+)/?$", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in RESERVED_MINKABU_PATHS:
            continue
        name = a.get_text(strip=True)
        if not name or len(name) > 80:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        out.append({
            "rank": len(out) + 1,
            "theme_name": name,
            "theme_url": f"{MINKABU_BASE}/theme/{slug}",
        })
        if len(out) >= max_n:
            break
    return out


def main() -> int:
    print("=== テーマ動意検知 ETL ===")
    snapshot_date = datetime.now(JST).date().isoformat()
    fetched_at = datetime.now(JST).isoformat(timespec="seconds")
    new_rows: list[dict] = []

    print(f"\n[1/3] 株探 アクセスランキング3日（既注目軸）")
    print(f"  URL: {KABUTAN_ACCESS_URL}")
    soup = fetch(KABUTAN_ACCESS_URL)
    time.sleep(SLEEP)
    if soup is not None:
        items = extract_kabutan_access(soup)
        print(f"  抽出: {len(items)}件")
        for it in items:
            new_rows.append({
                "snapshot_date": snapshot_date,
                "source": "kabutan",
                "rank_type": "access_3d",
                "rank": it["rank"],
                "theme_name": it["theme_name"],
                "theme_url": it["theme_url"],
                "fetched_at": fetched_at,
            })
        if items[:5]:
            print(f"  top5: {[i['theme_name'] for i in items[:5]]}")

    print(f"\n[2/3] みんかぶ 人気テーマランキング（既注目軸）")
    print(f"  URL: {MINKABU_POPULAR_URL}")
    soup = fetch(MINKABU_POPULAR_URL)
    time.sleep(SLEEP)
    if soup is not None:
        items = extract_minkabu_ranking(soup)
        print(f"  抽出: {len(items)}件")
        for it in items:
            new_rows.append({
                "snapshot_date": snapshot_date,
                "source": "minkabu",
                "rank_type": "popular",
                "rank": it["rank"],
                "theme_name": it["theme_name"],
                "theme_url": it["theme_url"],
                "fetched_at": fetched_at,
            })
        if items[:5]:
            print(f"  top5: {[i['theme_name'] for i in items[:5]]}")

    print(f"\n[3/3] みんかぶ 急上昇テーマランキング（急上昇軸）")
    print(f"  URL: {MINKABU_RISE_URL}")
    soup = fetch(MINKABU_RISE_URL)
    time.sleep(SLEEP)
    if soup is not None:
        items = extract_minkabu_ranking(soup)
        print(f"  抽出: {len(items)}件")
        for it in items:
            new_rows.append({
                "snapshot_date": snapshot_date,
                "source": "minkabu",
                "rank_type": "rise",
                "rank": it["rank"],
                "theme_name": it["theme_name"],
                "theme_url": it["theme_url"],
                "fetched_at": fetched_at,
            })
        if items[:5]:
            print(f"  top5: {[i['theme_name'] for i in items[:5]]}")

    if not new_rows:
        print("\nERROR: no rows extracted")
        return 1

    new_df = pd.DataFrame(new_rows)

    # 既存parquetがあれば追記（同一 snapshot_date×source×rank_type は新で上書き）
    if OUT_PATH.exists():
        old_df = pd.read_parquet(OUT_PATH)
        key = (
            (old_df["snapshot_date"] == snapshot_date)
            & (old_df["source"].isin(new_df["source"].unique()))
            & (old_df["rank_type"].isin(new_df["rank_type"].unique()))
        )
        keep = old_df[~key]
        merged = pd.concat([keep, new_df], ignore_index=True)
        print(f"\n既存parquet: {len(old_df):,}行 → 当日分置換 → {len(merged):,}行")
    else:
        merged = new_df
        print(f"\n新規parquet: {len(merged):,}行")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(OUT_PATH, index=False)
    print(f"saved: {OUT_PATH}")
    print(f"snapshot_date={snapshot_date}  追加行数={len(new_df)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
