"""20テーマサマリーレポートを Discord テーマアナリストチャンネル（DISCORD_WEBHOOK_THEME）へ送信。

使い方:
  python send_themes_summary_discord.py                     # 今日の日付（JST）
  python send_themes_summary_discord.py --date 2026-05-17  # 指定日
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(Path(__file__).resolve().parent / ".env")

WEBHOOK = os.getenv("DISCORD_WEBHOOK_THEME")
if not WEBHOOK:
    print("ERROR: DISCORD_WEBHOOK_THEME not set")
    sys.exit(1)

MAX_CHARS = 1900  # Discord limit 2000 から余裕を取る


def split_at_boundaries(text: str, max_chars: int) -> list[str]:
    """見出し（## / ###）境界で優先的に分割しつつ max_chars 以下のチャンクに分割。"""
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if buf_len + line_len > max_chars and buf:
            chunks.append("\n".join(buf))
            buf = []
            buf_len = 0
        if line.startswith("## ") and buf_len > max_chars * 0.6:
            chunks.append("\n".join(buf))
            buf = []
            buf_len = 0
        buf.append(line)
        buf_len += line_len
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="送信対象日（YYYY-MM-DD・JST）", default=None)
    args = parser.parse_args()
    date_str = args.date or datetime.now(JST).strftime("%Y-%m-%d")
    report_path = REPO_ROOT / "market" / "daily" / "theme" / f"{date_str}_themes_summary.md"
    if not report_path.exists():
        print(f"ERROR: report not found: {report_path}")
        return 1
    text = report_path.read_text(encoding="utf-8")
    chunks = split_at_boundaries(text, MAX_CHARS)
    print(f"file: {report_path}")
    print(f"total chars: {len(text)}  chunks: {len(chunks)}")
    for i, c in enumerate(chunks, 1):
        r = requests.post(WEBHOOK, json={"content": c})
        status = r.status_code
        print(f"POST chunk {i:2d}/{len(chunks)}: {status}  chars={len(c)}")
        if status >= 400:
            print(f"  body: {r.text[:300]}")
            return 1
        time.sleep(1.0)
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
