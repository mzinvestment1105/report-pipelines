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
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_stock_report import run_gate  # noqa: E402
from lib.md_to_pdf import render_markdown_to_pdf  # noqa: E402
from lib.quality_gate import (  # noqa: E402
    find_duplication,
    strip_repeated_annotations,
    strip_vix_rows,
)
from send_report_jpeg_discord import (  # noqa: E402
    KIND_CONFIG,
    _lookup_company_name,
)

JST = timezone(timedelta(hours=9))
load_dotenv(Path(__file__).resolve().parent / ".env")

# 送信種別 → md_to_pdf のテーマ kind（アクセント色・キッカー）
_PDF_KIND = {
    "macro": "macro", "macro_evening": "macro",
    "sector": "sector", "sector_full": "sector",
    "movers": "movers", "movers_weekly": "movers", "pts_movers": "movers",
    "ideas": "ideas", "scout": "ideas",
    "themes": "themes", "earnings": "earnings", "stock": "stock",
    "largecap_weekly": "largecap_weekly",
}


# 市場区切り h1。2026-09-04 PM 指示で `# 動意銘柄レポート {date}（グロース）` から
# `# グロース市場` へ変更した（誌面で「レポート題名」に見え、帯が2行に折れたため）。
# 旧形式も受け付けて過去 md の再レンダーを壊さない。
_MOVERS_MARKET_TITLE_RE = re.compile(
    r"^#\s*(?:プライム|スタンダード|グロース)市場\s*$"
    r"|^#\s*動意銘柄レポート.*?[（(](?:プライム|スタンダード|グロース)[)）]",
    re.MULTILINE,
)


def _ensure_movers_doc_title(md_text: str, identifier: str, doc_label: str) -> str:
    """動意 md の先頭に文書タイトル H1 を必ず 1 本置く。

    md_to_pdf は「最初に現れた H1」をマストヘッド見出しへ昇格し本文から除く。
    統合 1 本化後の md は先頭がテーマ2部（`##`）で、最初の H1 が市場区切り
    （2026-09-04 以降は `# グロース市場`・以前は `# 動意銘柄レポート {date}（グロース）`）
    になるため、放置するとマストヘッドがその市場名を名乗り区切り帯も1本消える。
    文書タイトル `# 動意銘柄レポート {date}` を先頭へ足してマストヘッド用に消費させ、
    3 本の市場 h1 は全て本文の区切り帯として残す。
    既に市場名でない H1 が先頭にある md には何もしない（二重付与しない）。
    """
    stripped = md_text.lstrip()
    first_h1 = re.search(r"^#\s+(.*)$", md_text, re.MULTILINE)
    if first_h1 and not _MOVERS_MARKET_TITLE_RE.match(first_h1.group(0)):
        return md_text  # 既に文書タイトルがある
    if not first_h1:
        return md_text  # 市場タイトルすら無い（想定外）ので触らない
    label = f"# {doc_label} {identifier}"
    return label + "\n\n" + stripped


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
    parser.add_argument(
        "--skip-gate",
        action="store_true",
        help="個別銘柄レポートの送信前機械ゲートを飛ばす（緊急時のバイパス）",
    )
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

    # 個別銘柄レポートの送信前機械ゲート（PM 2026-08-30 承認・gate_stock_report.py）
    # errors があれば PDF を生成せずに中止する。warnings は表示のみで続行する。
    if args.kind == "stock":
        if args.skip_gate:
            print("GATE: --skip-gate 指定のため送信前機械ゲートを飛ばしました（緊急バイパス）")
        else:
            errors, warnings, info = run_gate(md_text, args.code)
            for m in info:
                print("  OK   " + m)
            for m in warnings:
                print("  WARN " + m)
            for m in errors:
                print("  NG   " + m)
            if errors:
                print("GATE: FAIL（送信中止・PDF は生成しません）")
                return 1
            print("GATE: PASS")

    # 品質確定処理（非ブロッキング・PM 2026-06-28）: 「配信を止めない・token を使わない」を最優先
    # するため、送信のブロック（失敗）も自動再生成（token 増）も行わない。生成 LLM が無視する
    # ソフト指示に頼らず、Python で必ず効く確定除去のみで品質を担保する:
    #   (1) VIX / 日経 VI の表行を機械除去（PM: 出力に不要・両方外す。LLM が ─ 行を再追加するため）
    #   (2) 同一注釈 （…） の 2 回目以降を機械除去（注釈は一度で十分・反復は重複説明。語は残す）
    # 除去結果は GHA ログに残す。除去後の残重複は find_duplication で監視のみ（配信は継続）。
    if args.kind in ("macro", "macro_evening"):
        md_text = strip_vix_rows(md_text)
        md_text, removed = strip_repeated_annotations(md_text)
        if removed:
            print("QUALITY: 重複注釈を確定除去（配信は継続）:", removed)
        residual = find_duplication(md_text)
        if residual:
            print("QUALITY MONITOR WARNING (残注釈重複・配信は継続):", residual)

    # 動意（日次・週次）は 1 メッセージ・1 PDF で送る（PM 2026-09-02 確定）。
    # 旧仕様は split_movers_by_market() で市場別 3 PDF へ分割し 3 メッセージ送っていたが、
    # PM 判断で 1 本へ統一した（ファイル名も日本語市場名を含まない半角英数のみになる）。
    # テーマ2部（## 本日のテーマ／## 直近2週間の熱いテーマ）は md 冒頭にあるため、
    # 統合 PDF でも 1 ページ目の先頭に来る。市場タイトル（# …（グロース）等）は
    # 本文中の h1 として市場区切り帯にレンダリングされる（lib/md_to_pdf.py の h1 スタイル）。
    if args.kind in ("movers", "movers_weekly"):
        md_text = _ensure_movers_doc_title(md_text, identifier, cfg["label"])

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
