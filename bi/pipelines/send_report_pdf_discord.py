"""汎用レポート→PDF Discord 送信スクリプト。

レポート種別と日付を指定して、Markdown レポートを A4 PDF 化（lib/md_to_pdf）→ Discord 添付送信。
レポート種別ごとの Markdown パス・送信先 Webhook は send_report_jpeg_discord の KIND_CONFIG を共有。

PM 2026-06-27: 縦長 JPEG を廃し、商品レベルの A4 PDF（日本語フォント埋め込み・文字化けなし）で配信。

使い方:
    python send_report_pdf_discord.py --kind macro --date 2026-06-27
    python send_report_pdf_discord.py --kind sector --date 2026-06-27
    python send_report_pdf_discord.py --kind earnings --month 2026-06
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
from lib.md_to_pdf import render_markdown_to_pdf  # noqa: E402
from lib.quality_gate import find_duplication  # noqa: E402
from send_report_jpeg_discord import (  # noqa: E402
    KIND_CONFIG,
    _lookup_company_name,
    split_movers_by_market,
)

JST = timezone(timedelta(hours=9))
load_dotenv(Path(__file__).resolve().parent / ".env")

# 送信種別 → md_to_pdf のテーマ kind（アクセント色・キッカー）
_PDF_KIND = {
    "macro": "macro", "macro_evening": "macro",
    "sector": "sector", "sector_full": "sector",
    "movers": "movers", "movers_weekly": "movers",
    "ideas": "ideas", "scout": "ideas",
    "themes": "themes", "earnings": "earnings", "stock": "stock",
}


def _post_pdf(webhook: str, pdf_path: Path, content: str) -> bool:
    payload = {"content": content, "attachments": [{"id": 0, "filename": pdf_path.name}]}
    with pdf_path.open("rb") as f:
        files = {
            "payload_json": (None, json.dumps(payload, ensure_ascii=False), "application/json"),
            "files[0]": (pdf_path.name, f, "application/pdf"),
        }
        r = requests.post(webhook, files=files)
    print(f"  status {r.status_code}  bytes={pdf_path.stat().st_size:,}")
    if r.status_code >= 400:
        print(f"  body: {r.text[:400]}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=list(KIND_CONFIG), required=True)
    parser.add_argument("--date", help="YYYY-MM-DD（JST・日次レポート用）")
    parser.add_argument("--month", help="YYYY-MM（月次レポート用・earnings 等）")
    parser.add_argument("--code", help="銘柄コード（stock 用）")
    parser.add_argument("--skip-send", action="store_true")
    args = parser.parse_args()

    cfg = KIND_CONFIG[args.kind]
    pdf_kind = _PDF_KIND.get(args.kind, "macro")

    # Markdown パス解決（JPEG 版と同一規則）
    date_str: str | None = None
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

    webhook = os.getenv(cfg["webhook_env"])
    if not webhook and not args.skip_send:
        print(f"ERROR: {cfg['webhook_env']} not set")
        return 1

    out_dir = REPO_ROOT / "bi" / "outputs" / "report_pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_text = md_path.read_text(encoding="utf-8")

    # 品質ゲート（確定処理・PM 2026-06-27）: マクロは同一材料の重複説明を機械検出し、基準超なら
    # 自動送信を保留する。ソフトな「重複禁止」指示が生成 LLM に無視される実績への構造的対策で、
    # 重複レポートが PM に届くことを防ぐ。保留時は webhook に理由を通知し非ゼロ終了する。
    if args.kind in ("macro", "macro_evening") and not args.skip_send:
        dups = find_duplication(md_text)
        if dups:
            alert = (
                "⚠️ 品質ゲート: 重複検出のため自動送信を保留しました（"
                + "・".join(dups[:5])
                + "）。テンプレ厳格化または再生成が必要です。"
            )
            if webhook:
                requests.post(webhook, json={"content": alert})
            print("QUALITY GATE BLOCKED:", dups)
            return 1

    # 動意は市場別（プライム/スタンダード/グロース）に分割して 3 PDF 送信
    if args.kind in ("movers", "movers_weekly"):
        markets = split_movers_by_market(md_text) or [("ALL", md_text)]
        prefix = "movers_weekly" if args.kind == "movers_weekly" else "movers"
        ok = True
        for label, part in markets:
            p = out_dir / f"{prefix}_{identifier}_{label.lower()}.pdf"
            print(f"[render] movers/{label} -> {p.name}")
            render_markdown_to_pdf(part, p, kind=pdf_kind, target_date=date_str)
            if not args.skip_send:
                ok = _post_pdf(webhook, p, f"**{cfg['label']}（{label}）** {identifier}") and ok
        print("DONE" if ok else "DONE WITH ERRORS")
        return 0 if ok else 1

    out_path = out_dir / f"{args.kind}_{identifier}.pdf"
    print(f"[1/2] rendering {args.kind} -> PDF")
    render_markdown_to_pdf(md_text, out_path, kind=pdf_kind, target_date=date_str)
    print(f"  saved: {out_path}  size={out_path.stat().st_size:,} bytes")
    if args.skip_send:
        return 0

    if args.kind == "stock" and args.code:
        company = _lookup_company_name(args.code, md_path)
        display_id = f"{args.code} {company}　{date_str}" if company else identifier
    else:
        display_id = identifier

    print(f"[2/2] sending to Discord ({cfg['webhook_env']})")
    return 0 if _post_pdf(webhook, out_path, f"**{cfg['label']}** {display_id}") else 1


if __name__ == "__main__":
    raise SystemExit(main())
