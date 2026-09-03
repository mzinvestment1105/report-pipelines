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
# 2026-09-03 PM 決定: 辞書を日次スナップショットで残し、3か月後に初動候補の再検証を行う。
# 辞書更新は月次（screening_master.yml の第1営業日ゲート）なので、内容が変わった時だけ
# ファイルを増やす。変化のない日はインデックスにも行を足さない。
SNAPSHOT_DIR = (
    REPO_ROOT / "bi" / "outputs" / "analysis" / "theme_radar" / "theme_master_snapshots"
)
SNAPSHOT_INDEX = (
    REPO_ROOT / "bi" / "outputs" / "analysis" / "theme_radar" / "theme_master_snapshot_index.parquet"
)


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



def save_theme_master_snapshot(
    df: pd.DataFrame,
    snapshot_date: str | None = None,
    snapshot_dir: Path | None = None,
    index_path: Path | None = None,
) -> Path | None:
    """辞書のスナップショットを保存し、インデックスへ1行追記する。

    2026-09-03 PM 決定。初動候補テーマの再検証には「その日どの辞書で判定したか」が要る
    ため、辞書を更新したときの中身を残す。ただし辞書更新は月次であり毎日は変わらないので、
    **直前スナップショットと内容ハッシュが同一なら保存しない**（ファイルを増やさない）。

    Returns:
        新規保存したスナップショットのパス。内容が変わっていなければ None。
    """
    import hashlib

    if df is None or df.empty:
        return None
    sdir = Path(snapshot_dir) if snapshot_dir else SNAPSHOT_DIR
    ipath = Path(index_path) if index_path else SNAPSHOT_INDEX
    date_str = str(snapshot_date or datetime.now(JST).date().isoformat())

    # 内容ハッシュ: 取得時刻列（毎回変わる）を除いた本体で判定する
    body = df.drop(columns=[c for c in ("fetched_at",) if c in df.columns], errors="ignore")
    body = body.sort_values(list(body.columns)).reset_index(drop=True)
    sha = hashlib.sha256(
        body.to_csv(index=False).encode("utf-8", errors="replace")
    ).hexdigest()

    idx = None
    if ipath.exists():
        try:
            idx = pd.read_parquet(ipath)
            if not idx.empty and str(idx.iloc[-1].get("sha256") or "") == sha:
                print(f" snapshot: 内容が直前と同一のためスキップ（sha={sha[:12]}）")
                return None
        except Exception:
            idx = None

    sdir.mkdir(parents=True, exist_ok=True)
    out = sdir / f"{date_str}.parquet"
    if out.exists():
        print(f" snapshot: {out.name} は既存のため上書きしません")
        return None
    df.to_parquet(out, index=False)

    n_themes = int(df["slug"].nunique()) if "slug" in df.columns else 0
    row = pd.DataFrame([{
        "date": date_str,
        "snapshot_file": out.name,
        "sha256": sha,
        "n_rows": int(len(df)),
        "n_themes": n_themes,
    }])
    ipath.parent.mkdir(parents=True, exist_ok=True)
    new = pd.concat([idx, row], ignore_index=True) if idx is not None else row
    new.to_parquet(ipath, index=False)
    print(f" snapshot: {out} ({len(df):,}行 / {n_themes:,}テーマ / sha={sha[:12]})")
    return out


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
    # 辞書の日次スナップショット（内容が変わった時だけファイルを増やす）
    try:
        save_theme_master_snapshot(df)
    except Exception as e:
        print(f" [WARN] snapshot 保存に失敗: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
