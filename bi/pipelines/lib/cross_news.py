"""当日の横断ニュース（テーマのきっかけ候補）を動意 raw の 1 節として作る。

2026-10-06 PM 承認（テーマ欄改修 B）。
  - 背景: 2026-10-05 にセキュリティ関連株（153A・4493・3692・5246・338A・4417）が上昇したが、
    きっかけの他社の事故の報道（大和証券グループ本社の委託先への不正アクセス公表）が
    動意 raw のどの入力にも無かった。動意 raw の材料は「動いた銘柄自身の」開示・報道しか
    集めないため、動いていない会社の事故・事件・政策の報道は構造的に入らない。
  - 本モジュールは、動いた銘柄に限らない当日の報道見出しを RSS から集め、見出し・公表時刻・
    出所だけを並べた節を返す。キーワードで特定テーマに絞らない（全件を時刻順に並べる）。
  - 情報源は GitHub Actions のランナーから取得できる無料の RSS 2 本（2026-10-06 実測で GET 200）。
    株探の市場ニュースは GHA のランナーから HTTP 405 で拒否されるため使わない
    （theme_report_daily.yml と mover_report_daily.yml の 9/28〜10/5 の実行ログで確認）。
  - 窓: 前営業日 15:30 〜 締め時刻（動意日次=当日 16:30・夜間 PTS=当日 21:00）。
  - 取得失敗時は節の中に「取得失敗」と明示し、呼び出し側の処理は止めない（_cr §36）。
    黙って空の節にしない。RSS が窓の開始時点まで遡って保持していない場合もその旨を書く。
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

JST = timezone(timedelta(hours=9))
OPEN_START = dtime(15, 30)         # 窓の開始 = 前営業日の大引け（lib/move_days と同じ境界）

# (表示名, URL)。表示名は raw の各行に出す出所。
SOURCES: list[tuple[str, str]] = [
    ("NHK 経済", "https://www.nhk.or.jp/rss/news/cat5.xml"),
    ("Yahoo!ニュース IT", "https://news.yahoo.co.jp/rss/topics/it.xml"),
]
MAX_ITEMS = 60                     # 節全体の件数上限（超えたら新しい順に残し、省略件数を明記）
TIMEOUT = 15
RETRIES = 2
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HEADING = "## 当日の横断ニュース（テーマのきっかけ候補）"


def parse_rss(content: bytes) -> list[dict]:
    """RSS 2.0 の item を {title, link, published(JST aware)} の一覧にする。"""
    root = ET.fromstring(content)
    out: list[dict] = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        if not title or not pub:
            continue
        try:
            dt = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        out.append({"title": title, "link": (it.findtext("link") or "").strip(),
                    "published": dt.astimezone(JST)})
    return out


def fetch_feed(url: str) -> list[dict]:
    """RSS を取得して parse する。失敗時は最後の例外を送出する。"""
    last: Exception | None = None
    for _ in range(RETRIES + 1):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()
            return parse_rss(r.content)
        except Exception as e:  # 通信・HTTP・XML の全失敗を同じ扱いにする
            last = e
    raise last if last else RuntimeError("fetch failed")


def _md(dt: datetime) -> str:
    """動意 raw の材料ラベルと同じ `M/D HH:MM` 形式（月日はゼロ埋めしない）。"""
    return f"{dt.month}/{dt.day} {dt:%H:%M}"


def _norm(title: str) -> str:
    t = unicodedata.normalize("NFKC", title)
    return re.sub(r"[\s　「」『』【】（）()・、。,.!！?？:：\-－—]", "", t)


def window(target: date, prev: date, close_hm: str) -> tuple[datetime, datetime]:
    hh, mm = (int(x) for x in close_hm.split(":"))
    start = datetime.combine(prev, OPEN_START, tzinfo=JST)
    end = datetime.combine(target, dtime(hh, mm), tzinfo=JST)
    return start, end


def build_section(target: date, prev: date, close_hm: str,
                  fetched: dict[str, list[dict] | Exception] | None = None) -> str:
    """横断ニュースの節（markdown）を返す。例外は送出しない。

    fetched を渡すとその取得結果（表示名 → item 一覧 または 例外）を使う（検証用）。
    渡さなければ SOURCES を実際に取得する。
    """
    start, end = window(target, prev, close_hm)
    notes: list[str] = []
    rows: list[tuple[datetime, str, str]] = []
    seen: set[str] = set()
    for name, url in SOURCES:
        try:
            got = fetched[name] if fetched is not None and name in fetched else fetch_feed(url)
            if isinstance(got, Exception):
                raise got
        except Exception as e:
            notes.append(f"- 取得失敗: {name}（{type(e).__name__}）")
            continue
        if got:
            oldest = min(i["published"] for i in got)
            if oldest > start:
                notes.append(f"- 保持切れ: {name} は {_md(oldest)} 以降の見出しのみ保持"
                             f"（窓の開始 {_md(start)} からその時刻までは取得できず）")
        n_in = 0
        for i in got:
            if not (start <= i["published"] <= end):
                continue
            key = _norm(i["title"])
            if key in seen or (i["link"] and i["link"] in seen):
                continue
            seen.add(key)
            if i["link"]:
                seen.add(i["link"])
            rows.append((i["published"], name, i["title"]))
            n_in += 1
        notes.append(f"- 取得: {name} {n_in} 件（窓内・重複除去後）")

    rows.sort(key=lambda r: r[0])
    omitted = 0
    if len(rows) > MAX_ITEMS:
        omitted = len(rows) - MAX_ITEMS
        rows = rows[-MAX_ITEMS:]           # 締め時刻に近い新しい見出しを残す

    all_failed = all(n.startswith("- 取得失敗") for n in notes if not n.startswith("- 保持切れ")) \
        and any(n.startswith("- 取得失敗") for n in notes)
    lines = [
        HEADING,
        "",
        f"> 動いた銘柄に限らない報道見出し（{_md(start)}〜{_md(end)} 公表・時刻は日本時間・"
        "出所付き・機械はテーマを絞っていない）。他社の事故・事件・政策など、テーマのきっかけの事実確認に使う。",
        "",
    ]
    if all_failed:
        lines.append("取得失敗（全情報源）。この節の見出しは使えない。")
    elif not rows:
        lines.append("窓内の見出しなし（取得は成功）。")
    else:
        for dt, name, title in rows:
            lines.append(f"- {_md(dt)}｜{name}｜{title}")
        if omitted:
            lines.append(f"- （件数上限 {MAX_ITEMS} 件のため古い順に {omitted} 件を省略）")
    lines += ["", "取得状況:"] + notes + [""]
    return "\n".join(lines)


def insert_section(raw_md: str, section: str, before: str) -> str:
    """raw の `before` 見出しの直前へ節を挿入する（見出しが無ければ末尾へ追加）。"""
    idx = raw_md.find("\n" + before)
    if idx < 0:
        return raw_md.rstrip("\n") + "\n\n" + section
    return raw_md[: idx + 1] + section + "\n" + raw_md[idx + 1:]


def _main() -> None:
    ap = argparse.ArgumentParser(description="当日の横断ニュース節を標準出力へ出す（確認用）")
    ap.add_argument("--date", required=True)
    ap.add_argument("--prev", required=True, help="前営業日 YYYY-MM-DD")
    ap.add_argument("--close", default="16:30")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print(build_section(date.fromisoformat(a.date), date.fromisoformat(a.prev), a.close))


if __name__ == "__main__":
    _main()
