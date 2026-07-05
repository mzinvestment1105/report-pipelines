"""
TDnet 適時開示フェッチ

やのしん WebAPI（無料・認証不要）経由で銘柄の適時開示 Atom を取得し、
各開示のタイトル・PDF本文をまとめた Markdown を出力する。
AI 分析（Haiku 要約 / Sonnet 投資インパクト）は Claude Code に任せる。

使い方:
  python fetch_tdnet_disclosures.py --code 7256
  python fetch_tdnet_disclosures.py --code 7256 --days 30
  python fetch_tdnet_disclosures.py --code 7256 --no-pdf   # PDF本文取得スキップ

出力: research/stocks/{code}_{date}_tdnet_raw.md
"""

from __future__ import annotations

import argparse
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from dotenv import load_dotenv

from jq_client_utils import normalize_code_4

OUTPUT_DIR = Path("../../research/stocks")
_ENV_PATH = Path(__file__).resolve().parent / ".env"

# やのしん TDnet WebAPI（Atom 0.3）
_TDNET_ATOM_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.atom"

# Atom 0.3 名前空間
_NS = {"a": "http://purl.org/atom/ns#"}


# ---------------------------------------------------------------------------
# Atom 取得
# ---------------------------------------------------------------------------

def fetch_tdnet_atom(code4: str) -> list[dict]:
    """やのしん TDnet Atom（0.3）を取得して開示リストを返す。"""
    url = _TDNET_ATOM_URL.format(code=code4)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    entries = []
    company_name = ""

    for entry in root.findall("a:entry", _NS):
        def _text(tag: str) -> str:
            el = entry.find(tag, _NS)
            return el.text.strip() if el is not None and el.text else ""

        published_str = _text("a:issued") or _text("a:created") or _text("a:modified")

        link_el = entry.find("a:link[@rel='alternate']", _NS)
        link_href = link_el.get("href", "") if link_el is not None else ""

        # rd.php?{PDF_URL} 形式から PDF URL を抽出
        pdf_url = ""
        if "rd.php?" in link_href:
            pdf_url = link_href.split("rd.php?", 1)[1]
        elif link_href.endswith(".pdf"):
            pdf_url = link_href

        raw_title = _text("a:title")
        # "河西工:タイトル" → 会社名とタイトルを分割
        if ":" in raw_title and not company_name:
            company_name = raw_title.split(":", 1)[0].strip()
        title = raw_title.split(":", 1)[1].strip() if ":" in raw_title else raw_title

        entries.append({
            "title": title,
            "published": published_str,
            "summary": _text("a:summary"),
            "pdf_url": pdf_url,
            "link": link_href,
        })

    return entries, company_name


def filter_by_days(entries: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    result = []
    for e in entries:
        try:
            dt = datetime.fromisoformat(e["published"].replace("Z", "+00:00"))
            if dt >= cutoff:
                result.append(e)
        except Exception:
            result.append(e)
    return result


# ---------------------------------------------------------------------------
# PDF 本文取得
# ---------------------------------------------------------------------------

def fetch_pdf_text(pdf_url: str, max_chars: int = 3000) -> str:
    """PDF URL からテキストを取得。失敗時は空文字。"""
    if not pdf_url:
        return ""
    try:
        from io import BytesIO, StringIO
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        resp = requests.get(pdf_url, timeout=20)
        resp.raise_for_status()
        out = StringIO()
        extract_text_to_fp(BytesIO(resp.content), out, laparams=LAParams(), output_type="text", codec=None)
        text = out.getvalue()
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[PDF取得失敗: {e}]"


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(_ENV_PATH)

    parser = argparse.ArgumentParser(description="TDnet 適時開示フェッチ")
    parser.add_argument("--code", required=True, help="銘柄コード（例: 7256）")
    parser.add_argument("--days", type=int, default=60, help="直近N日分（デフォルト60）")
    parser.add_argument("--no-pdf", action="store_true", help="PDF本文取得をスキップ")
    args = parser.parse_args()

    code4 = normalize_code_4(args.code)
    today_str = date.today().strftime("%Y-%m-%d")
    out_path = OUTPUT_DIR / f"{code4}_{today_str}_tdnet_raw.md"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"TDnet取得: code={code4} days={args.days}")
    entries, company_name = fetch_tdnet_atom(code4)
    entries = filter_by_days(entries, args.days)
    display_name = company_name or code4
    print(f"  {display_name}: 直近{args.days}日 {len(entries)}件")

    if not entries:
        out_path.write_text(
            f"# {display_name}（{code4}）適時開示\n\n直近{args.days}日の開示はありません。\n",
            encoding="utf-8",
        )
        print(f"出力: {out_path}")
        return

    # PDF 本文取得
    if not args.no_pdf:
        for i, e in enumerate(entries):
            if e["pdf_url"]:
                print(f"  PDF [{i+1}/{len(entries)}] {e['title'][:50]}")
                e["pdf_text"] = fetch_pdf_text(e["pdf_url"])
                time.sleep(0.5)
            else:
                e["pdf_text"] = ""
    else:
        for e in entries:
            e["pdf_text"] = ""

    # Markdown 出力
    lines = [
        f"# {display_name}（{code4}）適時開示 生データ",
        f"",
        f"- **取得日**: {today_str}",
        f"- **対象期間**: 直近{args.days}日",
        f"- **開示件数**: {len(entries)}件",
        f"- **PDF本文**: {'取得済み' if not args.no_pdf else 'スキップ'}",
        f"",
        f"---",
        f"",
        f"## 開示一覧",
        f"",
    ]

    for i, e in enumerate(entries, 1):
        lines += [
            f"### [{i}] {e['published'][:10]}　{e['title']}",
            f"",
            f"- 開示日時: {e['published'][:16]}",
        ]
        if e.get("pdf_url"):
            lines.append(f"- PDF: {e['pdf_url']}")
        if e.get("pdf_text"):
            lines += [
                f"",
                f"**PDF本文（抜粋）:**",
                f"",
                f"```",
                e["pdf_text"],
                f"```",
            ]
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"出力: {out_path}")
    print(f"\n次のステップ: Claude Code に「{code4}のTDnetレポートを作って」と依頼してください。")
    print(f"（ファイル: {out_path.name}）")


if __name__ == "__main__":
    main()
