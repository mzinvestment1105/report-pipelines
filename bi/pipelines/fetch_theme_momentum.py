"""テーマ動意検知 ETL（日次snapshot蓄積型）。

3軸テーマ検出フレームワークの「既注目軸」「急上昇軸」を担当する。
日次実行を前提に、既存parquetへ追記（同一snapshot_dateの重複は最新で置換）する。

ソース:
  - 株探 アクセスランキング3日（既注目軸） /info/accessranking/3_2  -> 30件
  - みんかぶ 人気テーマランキング（既注目軸）  /theme/popular_ranking -> 20件
  - みんかぶ 急上昇テーマランキング（急上昇軸） /theme/rise_ranking    -> 20件

出力:
  bi/outputs/theme_momentum.parquet
  列: snapshot_date, source, rank_type, rank, theme_name, theme_url, fetched_at, top_stocks
  （top_stocks = 各テーマページからスクレイプした代表銘柄「code 名（±x%）/ …」・上位ランクのみ取得・
    GHA で 404 になる WebFetch の代替。各構成銘柄の当日値動きは「なぜ動いた」理由合成の素材になる）
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


# 2026-07-07: GHA ランナー IP が一時的に弾かれ「no rows extracted」で日次レポートが
# 落ちた実績があるため、リトライ（指数バックオフ）+ UA ローテーションを追加。
# 同日追記: 実際に観測されたブロックは 405 だった（当初のリトライ対象 403/429/5xx に
# 含まれず即 give-up していた）ため 405 を追加し、待機列も (2,5,15,40) へ延長。
_RETRY_WAITS = (2, 5, 15, 40)
_RETRY_STATUSES = (403, 405, 429, 500, 502, 503, 504)
_UA_POOL = (
    HEADERS.get("User-Agent", "Mozilla/5.0"),
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
)


def fetch(url: str) -> BeautifulSoup | None:
    last_err = ""
    for attempt, wait in enumerate((0,) + _RETRY_WAITS):
        if wait:
            time.sleep(wait)
        headers = dict(HEADERS)
        headers["User-Agent"] = _UA_POOL[attempt % len(_UA_POOL)]
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
        except Exception as e:
            last_err = str(e)
            print(f"  [WARN] fetch failed (try {attempt + 1}) {url}: {e}")
            continue
        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser")
        last_err = f"status={r.status_code}"
        print(f"  [WARN] status={r.status_code} (try {attempt + 1}) {url}")
        if r.status_code not in _RETRY_STATUSES:
            break  # 404 等はリトライしても無駄
    print(f"  [WARN] fetch giving up {url}: {last_err}")
    return None


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


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def extract_kabutan_constituents(soup: BeautifulSoup, max_n: int = 6) -> list[dict]:
    """株探テーマページの構成銘柄テーブルから上位を抽出（code・name・前日比%）。

    行構造: <td class="tac"><a href="/stock/?code=CODE">CODE</a></td>
            <td class="tal">銘柄名</td> … <td class="w50"><span class="up|down">±X.XX</span>%</td>
    先頭の 0000/0800/0950 等は指数擬似コードのため除外（実在コードは 0 始まりにならない）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(r"/stock/\?code="))
        name_td = tr.find("td", class_="tal")
        if not a or not name_td:
            continue
        m = re.search(r"code=([0-9A-Z]+)", a["href"])
        if not m:
            continue
        code = m.group(1)
        if code.startswith("0") or code in seen:
            continue
        name = _clean(name_td.get_text())
        if not name:
            continue
        pct = ""
        pct_td = tr.find("td", class_="w50")
        if pct_td:
            span = pct_td.find("span")
            if span:
                val = _clean(span.get_text())
                cls = " ".join(span.get("class", []))
                if val and val not in ("0.00", "0"):
                    sign = "-" if ("down" in cls and not val.startswith("-")) else ""
                    pct = f"{sign}{val}%"
        seen.add(code)
        out.append({"code": code, "name": name, "chg": pct})
        if len(out) >= max_n:
            break
    return out


def extract_minkabu_constituents(soup: BeautifulSoup, max_n: int = 6) -> list[dict]:
    """みんかぶテーマページの関連銘柄から上位を抽出（code・name）。

    構造: <a href="/stock/CODE"><p class="text-xs …">CODE</p><p class="… font-bold …">銘柄名</p></a>
    """
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=re.compile(r"^/stock/[0-9]")):
        ps = a.find_all("p")
        if len(ps) < 2:
            continue
        code = _clean(ps[0].get_text())
        name = _clean(ps[1].get_text())
        if not re.match(r"^[0-9][0-9A-Z]{3}$", code) or not name:
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name, "chg": ""})
        if len(out) >= max_n:
            break
    return out


def fetch_constituents(source: str, theme_url: str) -> str:
    """テーマページを取得し『code 名（±x%）/ …』形式の代表銘柄文字列を返す（WebFetch 代替）。

    株探はデフォルトがコード昇順で小型新規上場が先頭に来るため、売買代金降順
    （stm=2&col=val）で取得して主力銘柄を代表に据える。みんかぶは関連度順で良好なため素のまま。
    """
    url = theme_url
    if source == "kabutan" and "/themes/" in theme_url:
        sep = "&" if "?" in theme_url else "?"
        url = f"{theme_url}{sep}market=0&capitalization=-1&stc=&stm=2&col=val"
    soup = fetch(url)
    time.sleep(SLEEP)
    if soup is None:
        return ""
    items = (
        extract_kabutan_constituents(soup) if source == "kabutan"
        else extract_minkabu_constituents(soup)
    )
    parts = []
    for it in items:
        if it.get("chg"):
            parts.append(f"{it['code']} {it['name']}（{it['chg']}）")
        else:
            parts.append(f"{it['code']} {it['name']}")
    return " / ".join(parts)


def attach_top_stocks(new_rows: list[dict], max_rank: int = 12) -> None:
    """各テーマの代表銘柄をテーマページからスクレイプし new_rows に top_stocks を付与する。

    report が使う上位（rank<=max_rank）に限定し、同一 theme_url は 1 回だけ取得（dedupe）。
    """
    print("\n[代表銘柄] テーマページから構成銘柄を取得中（WebFetch 代替）...")
    cache: dict[str, str] = {}
    n = 0
    for row in new_rows:
        if row["rank"] > max_rank:
            row["top_stocks"] = ""
            continue
        url = row["theme_url"]
        if url not in cache:
            cache[url] = fetch_constituents(row["source"], url)
            n += 1
        row["top_stocks"] = cache[url]
    for row in new_rows:
        row.setdefault("top_stocks", "")
    got = sum(1 for v in cache.values() if v)
    print(f"  取得テーマ数: {n}（うち代表銘柄あり {got}）")


def main() -> int:
    print("=== テーマ動意検知 ETL ===")
    snapshot_date = datetime.now(JST).date().isoformat()
    fetched_at = datetime.now(JST).isoformat(timespec="seconds")
    new_rows: list[dict] = []
    # ソース別の成否サマリ（部分成功時は続行・全滅時の ERROR 出力に含める）
    source_status: dict[str, str] = {}

    print(f"\n[1/3] 株探 アクセスランキング3日（既注目軸）")
    print(f"  URL: {KABUTAN_ACCESS_URL}")
    soup = fetch(KABUTAN_ACCESS_URL)
    time.sleep(SLEEP)
    if soup is None:
        source_status["株探 アクセスランキング3日"] = "取得失敗（ブロック/接続不可）"
    if soup is not None:
        items = extract_kabutan_access(soup)
        source_status["株探 アクセスランキング3日"] = f"OK {len(items)}件"
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
    if soup is None:
        source_status["みんかぶ 人気テーマ"] = "取得失敗（ブロック/接続不可）"
    if soup is not None:
        items = extract_minkabu_ranking(soup)
        source_status["みんかぶ 人気テーマ"] = f"OK {len(items)}件"
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
    if soup is None:
        source_status["みんかぶ 急上昇テーマ"] = "取得失敗（ブロック/接続不可）"
    if soup is not None:
        items = extract_minkabu_ranking(soup)
        source_status["みんかぶ 急上昇テーマ"] = f"OK {len(items)}件"
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

    summary = " / ".join(f"{k}: {v}" for k, v in source_status.items())
    print(f"\n[ソース別サマリ] {summary}")

    if not new_rows:
        print(f"\nERROR: no rows extracted（3ソース全滅）  [ソース別サマリ] {summary}")
        return 1

    attach_top_stocks(new_rows)

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
