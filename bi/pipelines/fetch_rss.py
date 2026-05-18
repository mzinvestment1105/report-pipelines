"""
マクロ経済 RSS パイプライン

yfinance（株価指数ニュース）と RSS フィード（日銀・財務省・Yahoo等）を統合取得し、
macro_analyst / sector_analyst が参照する日次ニュースファイルに出力する。

既存の fetch_macro_news.py を置き換える統合版。

使い方:
  python fetch_rss.py                   # 全フィード + yfinance（スナップショット）
  python fetch_rss.py --new-only        # 未掲載だけ載せる（.rss_seen 使用）
  python fetch_rss.py --no-yfinance     # RSSのみ
  python fetch_rss.py --days 14         # 直近N日（未指定時は RSS_LOOKBACK_DAYS または 30）

環境変数:
  RSS_LOOKBACK_DAYS       さかのぼり日数（--days 未指定時）
  RSS_MAX_ITEMS_PER_FEED  フィードあたり最大件数（未設定時は min(35, 10+日数)）
  RSS_TIMELINE_MAX        時系列セクションの最大行数（既定 50）

出力: market/daily/YYYY-MM-DD_news_raw.md
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import yaml
from dotenv import load_dotenv

REPO_ROOT   = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).resolve().parent / "ops" / "rss_config.yaml"
OUTPUT_DIR  = REPO_ROOT / "market" / "daily"
SEEN_PATH   = OUTPUT_DIR / ".rss_seen.txt"
_ENV_PATH   = Path(__file__).resolve().parent / ".env"

JST = timezone(timedelta(hours=9))

EpochUTC = datetime(1970, 1, 1, tzinfo=timezone.utc)

# yfinance マクロティッカー（既存 fetch_macro_news.py と同じ）
MACRO_TICKERS = {
    "S&P500 (^GSPC)": "^GSPC",
    "日経平均 (^N225)": "^N225",
    "ダウ (^DJI)": "^DJI",
    "ドル円 (USDJPY=X)": "USDJPY=X",
}


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _hash(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode()).hexdigest()


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        return set(SEEN_PATH.read_text(encoding="utf-8").splitlines())
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_PATH.write_text("\n".join(sorted(seen)), encoding="utf-8")


def parse_entry_dt(entry: dict) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        val = entry.get(field)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def is_recent(entry: dict, days: int) -> bool:
    dt = parse_entry_dt(entry)
    if dt is None:
        return True
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    return dt >= cutoff


def clean_summary(text: str, max_len: int = 200) -> str:
    text = re.sub(r"<[^>]+>", "", text or "").strip()
    return text[:max_len]


def resolved_lookback_days(cli_days: int | None) -> int:
    if cli_days is not None:
        return max(1, cli_days)
    env = os.environ.get("RSS_LOOKBACK_DAYS", "").strip()
    if env:
        return max(1, int(env))
    return 30


def max_items_per_feed(days: int) -> int:
    env = os.environ.get("RSS_MAX_ITEMS_PER_FEED", "").strip()
    if env:
        return max(1, int(env))
    return min(35, 10 + days)


def timeline_max_rows() -> int:
    env = os.environ.get("RSS_TIMELINE_MAX", "").strip()
    if env:
        return max(5, int(env))
    return 50


def yfinance_max_per_ticker(days: int) -> int:
    return min(15, max(8, 4 + days // 5))


def parse_yfinance_pub(pub: str) -> datetime | None:
    if not pub or not isinstance(pub, str):
        return None
    s = pub.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# yfinance ニュース取得
# ---------------------------------------------------------------------------

def fetch_yfinance_news(lookback_days: int) -> dict[str, list[dict]]:
    """マクロ指標ごとのニュースを取得。{label: [items]} を返す。"""
    try:
        import yfinance as yf
    except ImportError:
        print("  [SKIP] yfinance not installed")
        return {}

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    cap = yfinance_max_per_ticker(lookback_days)
    results: dict[str, list[dict]] = {}

    for label, ticker in MACRO_TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            news = t.news or []
            items: list[dict] = []
            for n in news:
                c = n.get("content", {})
                title = c.get("title", "").strip()
                summary = c.get("summary", "").replace("\xa0", " ").strip()[:200]
                pub_raw = c.get("pubDate", "") or ""
                url = c.get("canonicalUrl", {}).get("url", "")
                if not title:
                    continue
                dt = parse_yfinance_pub(pub_raw)
                if dt is not None and dt < cutoff:
                    continue
                pub_display = pub_raw[:10] if len(pub_raw) >= 10 and pub_raw[4] == "-" else ""
                if dt is not None:
                    pub_display = dt.astimezone(JST).strftime("%Y-%m-%d")
                items.append({
                    "title": title,
                    "summary": summary,
                    "published": pub_display,
                    "link": url,
                    "sort_dt": dt,
                })
            items.sort(
                key=lambda it: it["sort_dt"] or EpochUTC,
                reverse=True,
            )
            if items:
                results[label] = items[:cap]
        except Exception as e:
            print(f"  [ERROR] yfinance {ticker}: {e}")
    return results


# ---------------------------------------------------------------------------
# RSS フィード取得
# ---------------------------------------------------------------------------

def fetch_feed(url: str, days: int) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        items: list[dict] = []
        for entry in feed.entries:
            if not is_recent(entry, days):
                continue
            dt = parse_entry_dt(entry)
            if dt:
                date_jst = dt.astimezone(JST).strftime("%Y-%m-%d")
                sort_dt = dt
            else:
                raw = (entry.get("published") or entry.get("updated") or "").strip()
                date_jst = raw[:10] if len(raw) >= 10 and raw[4] == "-" else ""
                sort_dt = None
            items.append({
                "title":     entry.get("title", "").strip(),
                "link":      entry.get("link", "").strip(),
                "summary":   clean_summary(entry.get("summary", "")),
                "published": entry.get("published", ""),
                "date_jst":  date_jst,
                "sort_dt":   sort_dt,
            })
        items.sort(
            key=lambda it: it["sort_dt"] or EpochUTC,
            reverse=True,
        )
        return items
    except Exception as e:
        print(f"  [ERROR] {url}: {e}")
        return []


def item_display_date(it: dict) -> str:
    return it.get("date_jst") or (it.get("published") or "")[:10]


def item_sort_dt(it: dict) -> datetime | None:
    st = it.get("sort_dt")
    if st is not None:
        return st
    dj = item_display_date(it)
    if len(dj) >= 10 and dj[4] == "-" and dj[7] == "-":
        try:
            local = datetime.strptime(dj[:10], "%Y-%m-%d").replace(tzinfo=JST)
            return local.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def haiku_summarize(items: list[dict], feed_name: str, max_input: int) -> str:
    return ""


# ---------------------------------------------------------------------------
# Markdown 出力
# ---------------------------------------------------------------------------

def build_timeline_table(
    rss_by_agent: dict[str, list[tuple[str, list[dict], str]]],
    max_rows: int,
) -> list[str]:
    rows: list[tuple[datetime, str, str, str, str]] = []
    seen: set[str] = set()
    for agent_key in ("macro_analyst", "sector_analyst"):
        for feed_name, items, _ in rss_by_agent.get(agent_key, []):
            for it in items:
                link = it.get("link") or ""
                if not link or link in seen:
                    continue
                seen.add(link)
                st = item_sort_dt(it)
                if st is None:
                    continue
                rows.append(
                    (st, item_display_date(it), feed_name, it.get("title", ""), link)
                )
    rows.sort(key=lambda x: x[0], reverse=True)
    if not rows:
        return []
    lines = [
        "## 時系列インデックス（配信日の新しい順・重複URL除外）",
        "",
        "| 配信日(JST目安) | ソース | 見出し |",
        "| --- | --- | --- |",
    ]
    for _st, dmy, feed_name, title, link in rows[:max_rows]:
        safe_title = title.replace("|", "\\|")
        lines.append(f"| {dmy} | {feed_name} | [{safe_title}]({link}) |")
    lines.append("")
    lines.append(
        "*配信日はRSSの日時をJSTに寄せた目安です。記事内のイベント開催日・統計基準日とは異なる場合があります。*"
    )
    lines.append("")
    return lines


def build_markdown(
    today_str: str,
    yf_news: dict[str, list[dict]],
    rss_by_agent: dict[str, list[tuple[str, list[dict], str]]],
    days: int,
    timeline_rows: int,
) -> str:
    lines = [
        f"# マクロニュース 生データ ({today_str})",
        f"",
        f"> yfinance + RSS から自動取得。macro_analyst / sector_analyst に渡してレポートを生成する。",
        f"- **生成日時**: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST",
        f"- **対象期間**: 直近{days}日（記事の**掲載・配信日**がこの窓内のものを収集）",
        f"- **日付の扱い**: 一覧の日付は原則**配信日**です。イベントの開催日・統計の参照期間は見出し・本文・一次ソースで要確認してください。",
        f"",
    ]

    tl = build_timeline_table(rss_by_agent, timeline_rows)
    if tl:
        lines.extend(tl)

    # yfinance マクロニュース
    if yf_news:
        lines += ["## 株価指数・為替ニュース（yfinance）", ""]
        for label, items in yf_news.items():
            lines += [f"### {label}", ""]
            for it in items:
                lines.append(f"- **{it['published']}** {it['title']}")
                if it["summary"]:
                    lines.append(f"  > {it['summary'][:180]}")
            lines.append("")

    # macro_analyst 向け RSS
    macro_feeds = rss_by_agent.get("macro_analyst", [])
    if macro_feeds:
        lines += ["## マクロ経済 RSS（日銀・財務省・市場ニュース）", ""]
        for feed_name, items, summary in macro_feeds:
            lines += [f"### {feed_name}", ""]
            if summary:
                lines += [f"**要約（Haiku）:** {summary}", ""]
            for it in items:
                date_str = item_display_date(it)
                lines.append(f"- **{date_str}** [{it['title']}]({it['link']})")
                if it["summary"]:
                    lines.append(f"  > {it['summary'][:150]}")
            lines.append("")

    # sector_analyst 向け RSS
    sector_feeds = rss_by_agent.get("sector_analyst", [])
    if sector_feeds:
        lines += ["## セクター・業界 RSS", ""]
        for feed_name, items, summary in sector_feeds:
            lines += [f"### {feed_name}", ""]
            for it in items:
                date_str = item_display_date(it)
                lines.append(f"- **{date_str}** [{it['title']}]({it['link']})")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(_ENV_PATH)

    parser = argparse.ArgumentParser(description="マクロ経済 RSS + yfinance 統合ニュース取得")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="直近N日分（未指定時は環境変数 RSS_LOOKBACK_DAYS、なければ30）",
    )
    parser.add_argument("--no-yfinance", action="store_true", help="yfinanceニュースをスキップ")
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="未掲載（.rss_seen に無い）記事だけを Markdown に載せる。連続実行で RSS 欄が空になりやすい",
    )
    parser.add_argument(
        "--category",
        default="all",
        help="互換・予約（GitHub Actions 用。現状は rss_config の全フィードを取得）",
    )
    args = parser.parse_args()

    days = resolved_lookback_days(args.days)
    per_feed_cap = max_items_per_feed(days)
    tl_max = timeline_max_rows()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    out_path = OUTPUT_DIR / f"{today_str}_news_raw.md"

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    feeds = config.get("feeds", {})
    seen: set[str] = load_seen() if args.new_only else set()

    # yfinance
    yf_news: dict[str, list[dict]] = {}
    if not args.no_yfinance:
        print("yfinance マクロニュース取得中...")
        yf_news = fetch_yfinance_news(days)
        print(f"  → {sum(len(v) for v in yf_news.values())}件")

    # RSS フィード
    rss_by_agent: dict[str, list[tuple[str, list[dict], str]]] = {}
    for feed_name, cfg in feeds.items():
        agent    = cfg.get("agent", "macro_analyst")
        use_llm  = cfg.get("llm", False)
        url      = cfg.get("url", "")

        print(f"RSS取得: {feed_name}")
        items = fetch_feed(url, days)
        if not items:
            print("  → 直近期間に記事なし")
            continue

        if args.new_only:
            out_items = [it for it in items if _hash(it["title"], it["link"]) not in seen]
            seen.update(_hash(it["title"], it["link"]) for it in out_items)
            if not out_items:
                print("  → 新着なし")
                continue
            print(f"  → {len(out_items)}件（新着のみ）")
        else:
            out_items = items[:per_feed_cap]
            print(f"  → {len(out_items)}件（スナップショット、上限 {per_feed_cap}）")

        summary = ""
        if use_llm and out_items:
            print("  Haiku 要約中...")
            summary = haiku_summarize(out_items, feed_name, max_input=per_feed_cap)

        rss_by_agent.setdefault(agent, []).append((feed_name, out_items, summary))
        time.sleep(0.5)

    if args.new_only:
        save_seen(seen)

    md = build_markdown(today_str, yf_news, rss_by_agent, days, tl_max)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n出力: {out_path}")
    print("→ Claude Code に「マクロレポートを作って」と依頼してください。")


if __name__ == "__main__":
    main()
