"""
Finnhub API からグローバルニュース・経済カレンダーを取得し、
マクロレポート用の入力ファイル（_finnhub_raw.md）を生成する。

使い方:
  python fetch_finnhub.py                      # 今日分を生成
  python fetch_finnhub.py --date 2026-04-12    # 日付指定

exit codes:
  0  正常生成
  1  エラー（APIキー未設定・接続失敗等）
  2  取得件数ゼロ（スキップ）

環境変数:
  FINNHUB_API_KEY  (必須) Finnhub API キー（https://finnhub.io で無料取得）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT  = Path(__file__).resolve().parents[2]
MARKET_DIR = REPO_ROOT / "market" / "daily"
_ENV_PATH  = Path(__file__).resolve().parent / ".env"

BASE_URL = "https://finnhub.io/api/v1"

# 取得するニュースカテゴリ
NEWS_CATEGORIES = [
    ("general", "一般市場ニュース"),
    ("forex",   "為替・FX関連ニュース"),
]

# 経済カレンダー: 今日から何日先まで取得するか
CALENDAR_DAYS_AHEAD = 7


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def jst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def unix_to_jst(ts: int) -> str:
    """Unix timestamp → JST 文字列"""
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=9)))
    return dt.strftime("%Y-%m-%d %H:%M JST")


def get(path: str, params: dict, api_key: str, retries: int = 3) -> dict | list | None:
    """Finnhub API GET ラッパー（リトライ付き）"""
    params["token"] = api_key
    url = BASE_URL + path
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 403:
                # 有料プラン限定エンドポイント — リトライ不要
                print(f"  [プラン制限] {path} は無料プランで利用不可（403）", file=sys.stderr)
                return None
            if resp.status_code == 429:
                print(f"  [レート制限] {attempt+1}/{retries} 回目 — 2秒待機", file=sys.stderr)
                time.sleep(2)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"  [ERROR] {path}: {e}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# ニュース取得
# ---------------------------------------------------------------------------

def fetch_news(category: str, api_key: str, since_hours: int = 24) -> list[dict]:
    """指定カテゴリの直近 since_hours 時間以内のニュースを取得"""
    data = get("/news", {"category": category}, api_key)
    if not data or not isinstance(data, list):
        return []

    cutoff = time.time() - since_hours * 3600
    items = [item for item in data if item.get("datetime", 0) >= cutoff]

    # 重複除去（同一 URL）
    seen: set[str] = set()
    unique = []
    for item in items:
        url = item.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(item)

    # 新しい順
    unique.sort(key=lambda x: x.get("datetime", 0), reverse=True)
    return unique[:30]  # 最大30件


def format_news_section(label: str, items: list[dict]) -> str:
    if not items:
        return f"## {label}\n\n（記事なし）\n"

    lines = [f"## {label}（直近24h・最大30件）", ""]
    for item in items:
        ts   = unix_to_jst(item.get("datetime", 0))
        src  = item.get("source", "")
        head = item.get("headline", "").strip()
        url  = item.get("url", "")
        summary = item.get("summary", "").strip()

        lines.append(f"- [{ts}] **{head}** — {src}")
        if summary:
            # 要約を120文字で切る
            short = summary[:120] + ("…" if len(summary) > 120 else "")
            lines.append(f"  > {short}")
        if url:
            lines.append(f"  {url}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 経済カレンダー取得
# ---------------------------------------------------------------------------

def fetch_economic_calendar(api_key: str, days_ahead: int = 7) -> list[dict]:
    today     = date.today()
    from_date = today.strftime("%Y-%m-%d")
    to_date   = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    data = get("/calendar/economic", {"from": from_date, "to": to_date}, api_key)
    if not data or not isinstance(data, dict):
        return []

    events = data.get("economicCalendar", [])
    # 主要国のみ絞り込み（US, JP, EU, GB, CN）
    major = {"US", "JP", "EU", "GB", "CN"}
    filtered = [e for e in events if e.get("country", "").upper() in major]

    # 時刻順
    filtered.sort(key=lambda x: (x.get("time", "") or ""))
    return filtered


def format_calendar_section(events: list[dict]) -> str:
    if not events:
        return "## 経済指標カレンダー（今後7日間）\n\n（無料プランでは利用不可 — 有料プランへのアップグレードで取得可能）\n"

    lines = [
        "## 経済指標カレンダー（今後7日間・主要国）",
        "",
        "| 日時(UTC) | 国 | 指標名 | 前回値 | 予想値 | 重要度 |",
        "|-----------|----|----|-----|-----|------|",
    ]

    importance_map = {1: "低", 2: "中", 3: "⚠️ 高"}

    for ev in events:
        t        = ev.get("time", "")[:16]   # "2026-04-15T21:30"
        country  = ev.get("country", "")
        event    = ev.get("event", "")
        prev     = ev.get("prev", "—") or "—"
        estimate = ev.get("estimate", "—") or "—"
        imp_raw  = ev.get("impact", 1)
        try:
            imp_val = int(imp_raw)
        except (TypeError, ValueError):
            imp_val = 1
        imp = importance_map.get(imp_val, "低")

        # 時刻表示: UTC → JST は +9h (表示上はUTCと明記)
        lines.append(f"| {t} | {country} | {event} | {prev} | {estimate} | {imp} |")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown 生成
# ---------------------------------------------------------------------------

def build_markdown(
    news_sections: list[tuple[str, list[dict]]],
    calendar_events: list[dict],
    target_date: str,
) -> str:
    total_news = sum(len(items) for _, items in news_sections)

    parts = [
        f"# Finnhub グローバルデータ（{target_date}）",
        "",
        f"- **取得日時**: {jst_now().strftime('%Y-%m-%d %H:%M JST')}",
        f"- **ニュース件数**: {total_news} 件",
        f"- **経済カレンダー**: {len(calendar_events)} 件",
        "",
        "---",
        "",
    ]

    for label, items in news_sections:
        parts.append(format_news_section(label, items))
        parts.append("---")
        parts.append("")

    parts.append(format_calendar_section(calendar_events))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(_ENV_PATH)

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    args = parser.parse_args()
    target_date: str = args.date

    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] FINNHUB_API_KEY が未設定です。.env に追記してください。", file=sys.stderr)
        sys.exit(1)

    print(f"[Finnhub] {target_date} 分を取得します...")

    # ニュース取得
    news_sections: list[tuple[str, list[dict]]] = []
    for category, label in NEWS_CATEGORIES:
        print(f"  ニュース取得: {label} ({category})...")
        items = fetch_news(category, api_key)
        print(f"    → {len(items)} 件")
        news_sections.append((label, items))
        time.sleep(0.5)  # レート制限対策

    # 経済カレンダー取得
    print(f"  経済カレンダー取得（今後{CALENDAR_DAYS_AHEAD}日）...")
    calendar_events = fetch_economic_calendar(api_key, CALENDAR_DAYS_AHEAD)
    print(f"    → {len(calendar_events)} 件")

    total = sum(len(items) for _, items in news_sections)
    if total == 0 and len(calendar_events) == 0:
        print("[SKIP] 取得件数ゼロ")
        sys.exit(2)

    # Markdown 生成・保存
    md = build_markdown(news_sections, calendar_events, target_date)
    out_path = MARKET_DIR / f"{target_date}_finnhub_raw.md"
    out_path.write_text(md, encoding="utf-8")

    chars = len(md)
    tokens_est = chars // 3
    print(f"\n保存完了: {out_path.name}")
    print(f"文字数: {chars:,}  推定トークン: {tokens_est:,}")
    print("\n→ generate_macro_report.py を実行するとこのファイルが自動的にプロンプトに追加されます。")


if __name__ == "__main__":
    main()
