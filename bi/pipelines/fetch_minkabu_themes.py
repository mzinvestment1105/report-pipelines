"""みんかぶ テーマ一覧スクレイピング ETL。

3軸テーマ検出フレームワークの「網羅軸」第2ソース。
/theme?page=1..N を巡回してテーマ名を全網羅し、各 /theme/{name} で構成銘柄を抽出する。

検証済み事実:
  - /theme?page=1〜50 は有効（20テーマ/ページ）、page=100 で空ページ
  - 1ページ20テーマ × 50ページ = 最大1,000テーマ
  - 個別テーマ /theme/{URLエンコード名} で /stock/XXXX 銘柄リンクが取れる

出力:
  bi/outputs/theme_master_minkabu.parquet
  列: theme_name, theme_url, code, stock_name, source, fetched_at
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin

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
BASE = "https://minkabu.jp"

PAGE_MIN = 1
PAGE_MAX = 60  # 余裕を持って60まで試す（50で空になることは検証済）
PAGE_INDEX_URL = "{base}/theme?page={page}"
RESERVED_PATHS = {"popular_ranking", "rise_ranking", "new", ""}

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "bi" / "outputs" / "theme_master_minkabu.parquet"


def fetch_index_page(page: int) -> list[dict]:
    """/theme?page=N から (theme_name, theme_path) を抽出。空なら空リスト。"""
    url = PAGE_INDEX_URL.format(base=BASE, page=page)
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except Exception as e:
        print(f"  [WARN] page={page} fetch failed: {e}")
        return []
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # /theme/XXXX 形式（予約パス除外）
        m = re.match(r"^/theme/([^/?#]+)/?$", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in RESERVED_PATHS:
            continue
        name = a.get_text(strip=True)
        if not name or len(name) > 80:
            continue
        # 同じslugで複数表示テキストがある場合は最初の正規っぽいテキストを優先
        out.setdefault(slug, name)
    return [{"slug": slug, "theme_name": name} for slug, name in out.items()]


_STOCK_HREF_RE = re.compile(r"^/stock/([\dA-Z]{4})(?:[/?#].*)?$")
_STOCK_CODE_RE = re.compile(r"/stock/([\dA-Z]{4})")


def fetch_theme_detail(slug: str, max_pages: int = 10) -> list[dict]:
    """/theme/{slug} から個別銘柄リスト抽出（ページネーション全巡回・関連度順）。

    新規上場銘柄（コード末尾「A」「B」等）も拾うため、正規表現は [\\dA-Z]{4}。
    """
    stocks: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        url = f"{BASE}/theme/{slug}" + (f"?page={page}" if page > 1 else "")
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        except Exception as e:
            print(f"  [WARN] slug={slug!r} page={page} fetch failed: {e}")
            break
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        page_codes: list[str] = []
        for a in soup.find_all("a", href=_STOCK_HREF_RE):
            m = _STOCK_CODE_RE.search(a["href"])
            if not m:
                continue
            code = m.group(1)
            if code in seen:
                continue
            name = a.get_text(strip=True)
            if not name or len(name) > 80:
                continue
            seen.add(code)
            page_codes.append(code)
            stocks.append({"code": code, "stock_name": name})
        if not page_codes:
            break  # 銘柄が取れないページに到達したら終了
        time.sleep(0.3)
    return stocks


def main() -> int:
    print("=== みんかぶ テーマ ETL ===")
    fetched_at = datetime.now(JST).isoformat(timespec="seconds")

    # Step 1: 全 page を巡回して theme リスト構築
    all_themes: dict[str, str] = {}  # slug -> name
    empty_streak = 0
    for page in range(PAGE_MIN, PAGE_MAX + 1):
        entries = fetch_index_page(page)
        if not entries:
            empty_streak += 1
            print(f"  [index] page={page:3d}  EMPTY (streak={empty_streak})")
            if empty_streak >= 3:
                print(f"  [index] 3連続空 → 走査終了")
                break
            time.sleep(SLEEP)
            continue
        empty_streak = 0
        added = 0
        for e in entries:
            if e["slug"] not in all_themes:
                all_themes[e["slug"]] = e["theme_name"]
                added += 1
        print(f"  [index] page={page:3d}  themes={len(entries):3d}  new={added:3d}  total={len(all_themes):4d}")
        time.sleep(SLEEP)

    print(f"\nテーマ収集完了: unique slug = {len(all_themes)}")
    if not all_themes:
        print("ERROR: no themes collected")
        return 1

    # Step 2: 個別テーマページを巡回して構成銘柄取得
    rows: list[dict] = []
    fail = 0
    items = list(all_themes.items())
    for i, (slug, name) in enumerate(items, 1):
        if i == 1 or i % 50 == 0 or i == len(items):
            print(f"  [detail] [{i:4d}/{len(items)}] slug={slug[:40]:40s}  name={name[:30]}")
        stocks = fetch_theme_detail(slug)
        time.sleep(SLEEP)
        if not stocks:
            fail += 1
            continue
        for s in stocks:
            rows.append({
                "theme_name": name,
                "slug": slug,
                "theme_url": f"{BASE}/theme/{slug}",
                "code": s["code"],
                "stock_name": s["stock_name"],
                "source": "minkabu",
                "fetched_at": fetched_at,
            })

    df = pd.DataFrame(rows)
    print(f"\n total mappings: {len(df):,}")
    print(f" unique themes : {df['slug'].nunique() if len(df) else 0}")
    print(f" unique stocks : {df['code'].nunique() if len(df) else 0}")
    print(f" fail count    : {fail}")
    if df.empty:
        print(" ERROR: no rows extracted")
        return 1
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    print(f" saved: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
