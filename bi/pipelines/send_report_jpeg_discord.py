"""汎用レポート→JPEG Discord 送信スクリプト。

レポート種別と日付を指定して、Markdown レポートを JPEG 化 → Discord 添付送信。
共通レンダリングは lib/md_to_jpeg.py。

使い方:
    python send_report_jpeg_discord.py --kind macro --date 2026-05-18
    python send_report_jpeg_discord.py --kind sector --date 2026-05-18
    python send_report_jpeg_discord.py --kind movers --date 2026-05-18
    python send_report_jpeg_discord.py --kind ideas --date 2026-05-18
    python send_report_jpeg_discord.py --kind themes --date 2026-05-18
    python send_report_jpeg_discord.py --kind earnings --month 2026-05

レポート種別ごとに Markdown パスと送信先 Webhook が自動選択される。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.md_to_jpeg import render_markdown_to_jpeg  # noqa: E402

JST = timezone(timedelta(hours=9))
load_dotenv(Path(__file__).resolve().parent / ".env")


KIND_CONFIG = {
    "macro": {
        "md_path": "market/daily/macro/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_MACRO",
        "label": "マクロ経済レポート",
    },
    "sector": {
        "md_path": "market/daily/sector/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_SECTOR",
        "label": "セクター週次レポート",
    },
    "movers": {
        "md_path": "market/daily/movers/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_MOVERS",
        "label": "動意銘柄レポート",
    },
    "ideas": {
        "md_path": "market/daily/ideas/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_IDEAS",
        "label": "投資アイデアレポート",
    },
    "themes": {
        "md_path": "market/daily/theme/{date}_themes_summary.md",
        "webhook_env": "DISCORD_WEBHOOK_THEME",
        "label": "テーマ動意サマリー",
    },
    "earnings": {
        "md_path": "research/earnings/{month}_overview.md",
        "webhook_env": "DISCORD_WEBHOOK_EARNINGS",
        "label": "決算シーズン総括",
    },
    "stock": {
        "md_path": "research/stocks/{code}/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_RESEARCH",
        "label": "個別銘柄レポート",
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=list(KIND_CONFIG), required=True)
    parser.add_argument("--date", help="YYYY-MM-DD（JST・日次レポート用）")
    parser.add_argument("--month", help="YYYY-MM（月次レポート用・earnings 等）")
    parser.add_argument("--code", help="銘柄コード（stock 用）")
    parser.add_argument("--skip-send", action="store_true")
    args = parser.parse_args()

    cfg = KIND_CONFIG[args.kind]

    # Markdown パス解決
    if args.kind == "earnings":
        month_str = args.month or datetime.now(JST).strftime("%Y-%m")
        md_path = REPO_ROOT / cfg["md_path"].format(month=month_str)
        identifier = month_str
    elif args.kind == "stock":
        if not args.code:
            print("ERROR: --code is required for stock kind")
            return 1
        date_str = args.date or datetime.now(JST).strftime("%Y-%m-%d")
        md_path = REPO_ROOT / cfg["md_path"].format(code=args.code, date=date_str)
        identifier = f"{args.code}_{date_str}"
    else:
        date_str = args.date or datetime.now(JST).strftime("%Y-%m-%d")
        md_path = REPO_ROOT / cfg["md_path"].format(date=date_str)
        identifier = date_str

    if not md_path.exists():
        print(f"ERROR: report not found: {md_path}")
        return 1

    # Webhook
    webhook = os.getenv(cfg["webhook_env"])
    if not webhook:
        print(f"ERROR: {cfg['webhook_env']} not set")
        return 1

    out_dir = REPO_ROOT / "bi" / "outputs" / "report_jpegs"
    out_path = out_dir / f"{args.kind}_{identifier}.jpg"

    print(f"[1/3] rendering {args.kind} → JPEG")
    md_text = md_path.read_text(encoding="utf-8")
    # Discord 送信は PM 個人閲覧のみ・ブランド表示 OK
    # SNS 用画像生成時は md_to_jpeg.render_markdown_to_jpeg を直接呼び出し footer=None を指定すること
    render_markdown_to_jpeg(md_text, out_path, kind=args.kind, footer="@noctra_jp / Mizuki Fund")
    print(f"  saved: {out_path}  size={out_path.stat().st_size:,} bytes")

    if args.skip_send:
        return 0

    if out_path.stat().st_size > 9_500_000:
        print(f"WARNING: JPEG exceeds 9.5MB ({out_path.stat().st_size:,} bytes). Discord may reject.")

    print(f"[2/3] sending to Discord ({cfg['webhook_env']})")
    content = f"**{cfg['label']}** {identifier}"
    payload = {
        "content": content,
        "attachments": [{"id": 0, "filename": out_path.name}],
    }
    with out_path.open("rb") as f:
        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": (out_path.name, f, "image/jpeg"),
        }
        r = requests.post(webhook, files=files)
    print(f"[3/3] status: {r.status_code}  bytes={out_path.stat().st_size:,}")
    if r.status_code >= 400:
        print(f"  body: {r.text[:500]}")
        return 1
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
