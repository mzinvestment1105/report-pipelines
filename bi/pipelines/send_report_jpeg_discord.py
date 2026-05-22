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

    # PM 2026-05-22 確定: 動意レポートは「需給（信用・株価水準）」セクションが
    # 全銘柄エントリに含まれていなければ送信中止する（不完全レポートの発信防止）。
    # [prompts/_common_rules.md §2-B] [memory feedback_mover_supply_required.md]
    if args.kind == "movers":
        md_text_pre = md_path.read_text(encoding="utf-8")
        import re as _re
        # 銘柄エントリ見出し（### N位 XXXX / ### XXXX）の行番号を取得
        entry_pattern = _re.compile(r"^###\s+(?:\d+位\s+)?\d{3,4}[A-Z]?\s", _re.MULTILINE)
        entry_matches = list(entry_pattern.finditer(md_text_pre))
        if not entry_matches:
            print("ERROR: 動意レポートに銘柄エントリが見つかりません。送信中止。")
            _notify_failure(webhook_env=cfg["webhook_env"],
                            label=cfg["label"], identifier=identifier,
                            reason="銘柄エントリ 0 件")
            return 1
        # 各銘柄エントリの本文範囲を取得し、需給ブロックが含まれているか検証
        missing: list[str] = []
        for i, m in enumerate(entry_matches):
            start = m.start()
            end = entry_matches[i + 1].start() if i + 1 < len(entry_matches) else len(md_text_pre)
            body = md_text_pre[start:end]
            header_line = body.split("\n", 1)[0].strip("# ").strip()
            if "需給" not in body:
                missing.append(header_line)
        if missing:
            print(f"ERROR: 需給セクション欠落 {len(missing)} 件: {missing[:5]}")
            _notify_failure(webhook_env=cfg["webhook_env"],
                            label=cfg["label"], identifier=identifier,
                            reason=f"需給セクション欠落 {len(missing)} 件 / 全 {len(entry_matches)} 銘柄")
            return 1
        print(f"[guard] 動意レポート需給セクション検証 OK（{len(entry_matches)} 銘柄）")

    # Webhook
    webhook = os.getenv(cfg["webhook_env"])
    if not webhook:
        print(f"ERROR: {cfg['webhook_env']} not set")
        return 1

    out_dir = REPO_ROOT / "bi" / "outputs" / "report_jpegs"

    md_text = md_path.read_text(encoding="utf-8")

    # movers は市場別 3 セット（プライム・スタンダード・グロース）に分割して送信
    # PM 2026-05-23 ご指示: 動意レポートは「プライム / スタンダード / グロースで画像は分けて」
    if args.kind == "movers":
        markets = split_movers_by_market(md_text)
        if not markets:
            print("ERROR: 動意レポートの市場別分割に失敗・通常モードでフォールバック")
            markets = [("ALL", md_text)]

        if args.skip_send:
            for label, md_part in markets:
                p = out_dir / f"movers_{identifier}_{label.lower()}.jpg"
                render_markdown_to_jpeg(md_part, p, kind=args.kind, footer="@noctra_jp / Mizuki Fund")
                print(f"  saved: {p}  size={p.stat().st_size:,} bytes")
            return 0

        for label, md_part in markets:
            p = out_dir / f"movers_{identifier}_{label.lower()}.jpg"
            print(f"[render] movers/{label} → {p.name}")
            render_markdown_to_jpeg(md_part, p, kind=args.kind, footer="@noctra_jp / Mizuki Fund")
            print(f"  saved: {p}  size={p.stat().st_size:,} bytes")

            content = f"**{cfg['label']}（{label}）** {identifier}"
            payload = {
                "content": content,
                "attachments": [{"id": 0, "filename": p.name}],
            }
            with p.open("rb") as f:
                files = {
                    "payload_json": (None, json.dumps(payload), "application/json"),
                    "files[0]": (p.name, f, "image/jpeg"),
                }
                r = requests.post(webhook, files=files)
            print(f"  status: {r.status_code}")
            if r.status_code >= 400:
                print(f"    body: {r.text[:300]}")
                return 1
        print("DONE")
        return 0

    out_path = out_dir / f"{args.kind}_{identifier}.jpg"

    print(f"[1/3] rendering {args.kind} → JPEG")
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


def split_movers_by_market(md_text: str) -> list[tuple[str, str]]:
    """動意レポート Markdown を市場別（プライム / スタンダード / グロース）に分割する。

    各市場用 Markdown には以下を含める：
    - 共通ヘッダ（タイトル + セクション 0 地合いサマリー + セクション 1 セクター別フロー）
    - 市場固有セクション（値上がり Top・値下がり Bottom）
    - 売買代金（市場別部分のみ）
    - セクション 9 明日のスイング戦略メモ（共通フッタ）

    セクション番号と見出しに含まれる「プライム」「スタンダード」「グロース」キーワードで判定。

    Returns:
        [(市場ラベル, 該当 Markdown), ...] 形式・3 件
    """
    import re

    # トップタイトル + セクション 0・1 を抽出（共通ヘッダ）
    header_match = re.search(r"^(.*?)(?=^## 2\.)", md_text, flags=re.DOTALL | re.MULTILINE)
    common_header = header_match.group(1).rstrip() if header_match else ""

    # セクション 9（明日のスイング戦略メモ）を抽出（共通フッタ）
    footer_match = re.search(r"(^## 9\..*)", md_text, flags=re.DOTALL | re.MULTILINE)
    common_footer = footer_match.group(1).rstrip() if footer_match else ""

    # セクション 2-8 を見出し単位で抽出
    section_pattern = re.compile(r"^(## \d+\..*?)(?=^## \d+\.|\Z)", flags=re.DOTALL | re.MULTILINE)
    sections = {m.group(1).split("\n", 1)[0].strip(): m.group(1).rstrip() for m in section_pattern.finditer(md_text)}

    # 売買代金セクション内部を市場別に分割（セクション 8）
    section_8 = next((v for k, v in sections.items() if k.startswith("## 8.")), "")

    def _extract_market_in_section8(market_kw: str) -> str:
        """セクション 8 内から特定市場部分を抽出する。"""
        if not section_8:
            return ""
        # `### プライム` `### スタンダード` `### グロース` のようなサブヘッダで分割
        sub_pattern = re.compile(r"(^### .*?)(?=^### |\Z)", flags=re.DOTALL | re.MULTILINE)
        subs = sub_pattern.findall(section_8)
        for sub in subs:
            first_line = sub.split("\n", 1)[0]
            if market_kw in first_line:
                return sub.rstrip()
        # サブヘッダで分かれていない場合は元のまま含める（pre-split）
        return section_8

    def _find_section(prefix_num: int, market_kw: str | None = None) -> str:
        """セクション番号と任意のキーワードでセクションを検索する。"""
        for k, v in sections.items():
            if k.startswith(f"## {prefix_num}."):
                if market_kw is None or market_kw in k:
                    return v
        return ""

    # 市場別構成（セクション番号と該当市場キーワード）
    market_setup: list[tuple[str, list[str]]] = [
        ("プライム", [
            _find_section(2),  # 2. プライム 値上がり Top 5
            _find_section(3),  # 3. プライム 値下がり Bottom 5
            _extract_market_in_section8("プライム"),
        ]),
        ("スタンダード", [
            _find_section(4),  # 4. スタンダード 値上がり Top 5
            _find_section(5),  # 5. スタンダード 値下がり Bottom 5
            _extract_market_in_section8("スタンダード"),
        ]),
        ("グロース", [
            _find_section(6),  # 6. グロース 値上がり Top 10
            _find_section(7),  # 7. グロース 値下がり Bottom 5
            _extract_market_in_section8("グロース"),
        ]),
    ]

    result: list[tuple[str, str]] = []
    for market_label, body_parts in market_setup:
        body_parts_clean = [p for p in body_parts if p]
        if not body_parts_clean:
            continue
        # セクション 8 を含む場合は「## 8. 売買代金（{市場}）」見出しとして付与
        # （pre-split の場合は元の見出しがそのまま残るのでスキップ）
        body_text = "\n\n".join(body_parts_clean)
        md_part = f"{common_header}\n\n{body_text}\n\n{common_footer}".strip()
        result.append((market_label, md_part))

    return result


def _notify_failure(*, webhook_env: str, label: str, identifier: str, reason: str) -> None:
    """需給ガード等で送信中止になった際に Discord へ失敗通知を送る。"""
    webhook = os.getenv(webhook_env)
    if not webhook:
        return
    try:
        requests.post(
            webhook,
            json={
                "content": (
                    f"❌ **{label} 発行停止** {identifier}\n"
                    f"理由: {reason}\n"
                    f"信用情報・株価水準セクションが揃わない不完全レポートのため送信を停止しました。"
                )
            },
            timeout=10,
        )
    except Exception as e:
        print(f"failure notify failed: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
