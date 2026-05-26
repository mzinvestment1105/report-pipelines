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

# PM 2026-05-23 確定: Discord 8MB 上限超過時の自動再圧縮閾値（HTTP 413 防止）
DISCORD_MAX_BYTES = 7_500_000  # 8MB の余裕分を取った保守的閾値


def ensure_under_discord_limit(jpeg_path: Path, max_bytes: int = DISCORD_MAX_BYTES) -> None:
    """JPEG が Discord 上限を超えていたら Pillow で段階的に画質を下げて再エンコードする。

    各市場で render_markdown_to_jpeg が 17MB 等の大きな JPEG を出す場合があり
    （Section 0+1+2+3+8a+9 を 1 枚にレンダリングすると Japanese 字体ヘビーで巨大化）、
    Discord の 8MB ファイル上限で HTTP 413 エラーになるため自動圧縮で対処。
    """
    if not jpeg_path.exists():
        return
    size = jpeg_path.stat().st_size
    if size <= max_bytes:
        return
    try:
        from PIL import Image
    except ImportError:
        print(f"WARNING: PIL/Pillow 未インストール・JPEG 圧縮スキップ ({size:,} bytes)")
        return
    print(f"[compress] {jpeg_path.name} は {size:,} bytes (> {max_bytes:,})・段階的に画質再エンコード")
    img = Image.open(jpeg_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 画質を 80 → 70 → 60 → 50 → 40 → 30 と段階的に下げる
    for q in (80, 70, 60, 50, 40, 30):
        img.save(jpeg_path, "JPEG", quality=q, optimize=True)
        new_size = jpeg_path.stat().st_size
        print(f"  quality={q} → {new_size:,} bytes")
        if new_size <= max_bytes:
            return
    # それでも大きい場合は解像度も下げる
    for scale in (0.85, 0.70, 0.55):
        w, h = img.size
        nw, nh = int(w * scale), int(h * scale)
        img_resized = img.resize((nw, nh), Image.LANCZOS)
        img_resized.save(jpeg_path, "JPEG", quality=70, optimize=True)
        new_size = jpeg_path.stat().st_size
        print(f"  scale={scale} ({nw}x{nh}) quality=70 → {new_size:,} bytes")
        if new_size <= max_bytes:
            return
    print(f"WARNING: 圧縮しても {jpeg_path.stat().st_size:,} bytes・Discord 送信で 413 になる可能性")


KIND_CONFIG = {
    "macro": {
        "md_path": "market/daily/macro/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_MACRO",
        "label": "マクロ経済レポート（朝刊）",
    },
    "macro_evening": {
        "md_path": "market/daily/macro/{date}_evening.md",
        "webhook_env": "DISCORD_WEBHOOK_MACRO",
        "label": "マクロ経済レポート（夕刊）",
    },
    "sector": {
        "md_path": "market/daily/sector/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_SECTOR",
        "label": "セクター日次レポート（短縮版）",
    },
    "sector_full": {
        "md_path": "market/daily/sector/{date}_full.md",
        "webhook_env": "DISCORD_WEBHOOK_SECTOR",
        "label": "セクター週次レポート（フルバージョン）",
    },
    "movers": {
        "md_path": "market/daily/movers/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_MOVERS",
        "label": "動意銘柄レポート",
    },
    "movers_weekly": {
        "md_path": "market/daily/movers/{date}_weekly.md",
        "webhook_env": "DISCORD_WEBHOOK_MOVERS",
        "label": "動意銘柄レポート（週次）",
    },
    "ideas": {
        "md_path": "market/daily/ideas/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_IDEAS",
        "label": "投資アイデアレポート",
    },
    "scout": {
        "md_path": "market/daily/scout/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_IDEAS",
        "label": "Scout Radar（プロトタイプ）",
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


def _lookup_company_name(code: str, md_path: "Path | None" = None) -> str:
    """銘柄コードから会社名を返す。Markdown先頭行 → screening_master の順で試みる。

    Markdown先頭行の形式: `# {コード} {銘柄名} Deep Dive レポート（...）`
    screening_master は全角英字（ＰＫＳＨＡ等）が入るため2番目の手段とする。
    """
    import re as _re
    # 1st: Markdown 先頭行からパース（最も正確な日本語銘柄名が取れる）
    if md_path is not None and md_path.exists():
        try:
            first_line = md_path.read_text(encoding="utf-8").split("\n")[0]
            # `# 3993 パークシャテクノロジー Deep Dive レポート` → 銘柄名部分を抽出
            m = _re.match(r"^#\s+\S+\s+(.+?)(?:\s+Deep\s+Dive|\s+レポート|\s*（|\s*\()", first_line)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    # 2nd: screening_master（全角英字になる場合があるが無いよりまし）
    try:
        import pandas as pd
        master_path = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"
        if master_path.exists():
            master = pd.read_parquet(master_path, columns=["Code", "CompanyName"])
            master["Code"] = master["Code"].astype(str).str[:4]
            row = master[master["Code"] == str(code)[:4]]
            if not row.empty:
                name = row.iloc[0]["CompanyName"]
                if isinstance(name, str) and name:
                    return name
    except Exception:
        pass
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=list(KIND_CONFIG), required=True)
    parser.add_argument("--date", help="YYYY-MM-DD（JST・日次レポート用）")
    parser.add_argument("--month", help="YYYY-MM（月次レポート用・earnings 等）")
    parser.add_argument("--code", help="銘柄コード（stock 用）")
    parser.add_argument("--skip-send", action="store_true")
    parser.add_argument("--force", action="store_true", help="フォールバック表記検証をスキップして強制送信")
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

    # PM 2026-05-25 確定: データなしの銘柄では需給セクション全体を省略可（フォールバック表記禁止）
    # PM 2026-05-26 確定: 検証ロジックは「フォールバック表記が混入していたら NG」に変更
    # データなしで需給セクションを省略するのは正しい挙動・送信中止しない
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
        # フォールバック表記の混入を検出（PM 2026-05-25 明示禁止パターン）
        forbidden_patterns = [
            "取得失敗・調査要",
            "raw データに直近数値なし",
            "raw データに該当数値なし",
            "信用倍率 N/A",
            "発行株数: N/A",
            "発行済株数: N/A",
            "信用残: 買 ─ / 売 ─",
        ]
        violations: list[tuple[str, str]] = []
        for i, m in enumerate(entry_matches):
            start = m.start()
            end = entry_matches[i + 1].start() if i + 1 < len(entry_matches) else len(md_text_pre)
            body = md_text_pre[start:end]
            header_line = body.split("\n", 1)[0].strip("# ").strip()
            for pat in forbidden_patterns:
                if pat in body:
                    violations.append((header_line, pat))
                    break
        if violations:
            if args.force:
                print(f"WARNING: フォールバック表記が {len(violations)} 件混入していますが --force のため送信続行")
            else:
                print(f"ERROR: フォールバック表記が {len(violations)} 件混入: {violations[:5]}")
                _notify_failure(webhook_env=cfg["webhook_env"],
                                label=cfg["label"], identifier=identifier,
                                reason=f"フォールバック表記 {len(violations)} 件混入 / 全 {len(entry_matches)} 銘柄")
                return 1
        # 需給セクション集計（参考情報・送信中止しない）
        with_supply = sum(1 for m in entry_matches
                          if "需給" in md_text_pre[m.start():(entry_matches[entry_matches.index(m) + 1].start() if entry_matches.index(m) + 1 < len(entry_matches) else len(md_text_pre))])
        print(f"[guard] 動意レポート検証 OK（全 {len(entry_matches)} 銘柄・需給ありデータ {with_supply} 銘柄・データなし省略 {len(entry_matches) - with_supply} 銘柄）")

    # Webhook
    webhook = os.getenv(cfg["webhook_env"])
    if not webhook:
        print(f"ERROR: {cfg['webhook_env']} not set")
        return 1

    out_dir = REPO_ROOT / "bi" / "outputs" / "report_jpegs"

    md_text = md_path.read_text(encoding="utf-8")

    # movers / movers_weekly は市場別 3 セット（プライム・スタンダード・グロース）に分割して送信
    # PM 2026-05-23 ご指示: 動意レポートは「プライム / スタンダード / グロースで画像は分けて」
    if args.kind in ("movers", "movers_weekly"):
        markets = split_movers_by_market(md_text)
        if not markets:
            print("ERROR: 動意レポートの市場別分割に失敗・通常モードでフォールバック")
            markets = [("ALL", md_text)]

        prefix = "movers_weekly" if args.kind == "movers_weekly" else "movers"
        if args.skip_send:
            for label, md_part in markets:
                p = out_dir / f"{prefix}_{identifier}_{label.lower()}.jpg"
                render_markdown_to_jpeg(md_part, p, kind=args.kind, footer="@noctra_jp / Mizuki Fund")
                print(f"  saved: {p}  size={p.stat().st_size:,} bytes")
            return 0

        for label, md_part in markets:
            p = out_dir / f"{prefix}_{identifier}_{label.lower()}.jpg"
            print(f"[render] movers/{label} → {p.name}")
            render_markdown_to_jpeg(md_part, p, kind=args.kind, footer="@noctra_jp / Mizuki Fund")
            print(f"  saved: {p}  size={p.stat().st_size:,} bytes")
            ensure_under_discord_limit(p)

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

    # PM 2026-05-26 確定: 画像分割は絶対禁止・単一ページ JPEG に統一（CLAUDE.md §画像分割絶対禁止 準拠）
    print(f"[1/3] rendering {args.kind} -> JPEG")
    render_markdown_to_jpeg(md_text, out_path, kind=args.kind, footer="@noctra_jp / Mizuki Fund")
    print(f"  saved: {out_path}  size={out_path.stat().st_size:,} bytes")

    if args.skip_send:
        return 0

    ensure_under_discord_limit(out_path)

    if out_path.stat().st_size > 9_500_000:
        print(f"WARNING: JPEG exceeds 9.5MB ({out_path.stat().st_size:,} bytes). Discord may reject.")

    print(f"[2/3] sending to Discord ({cfg['webhook_env']})")
    if args.kind == "stock" and args.code:
        company_name = _lookup_company_name(args.code, md_path)
        display_id = f"{args.code} {company_name}　{date_str}" if company_name else identifier
    else:
        display_id = identifier
    content = f"**{cfg['label']}** {display_id}"
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
    header_match = re.search(r"^(.*?)(?=^## 2[a-z]?\.)", md_text, flags=re.DOTALL | re.MULTILINE)
    common_header = header_match.group(1).rstrip() if header_match else ""

    # セクション 9（明日のスイング戦略メモ）を抽出（共通フッタ）
    footer_match = re.search(r"(^## 9\..*?)(?=^## \d+[a-z]?\.|\Z)", md_text, flags=re.DOTALL | re.MULTILINE)
    common_footer = footer_match.group(1).rstrip() if footer_match else ""

    # PM 2026-05-25 確定: section 番号は 8a/8b/8c のように英字接尾辞対応
    # （3 並列 Claude 実行で市場別 8a/8b/8c に分かれるため）
    section_pattern = re.compile(r"^(## \d+[a-z]?\..*?)(?=^## \d+[a-z]?\.|\Z)", flags=re.DOTALL | re.MULTILINE)
    sections = {m.group(1).split("\n", 1)[0].strip(): m.group(1).rstrip() for m in section_pattern.finditer(md_text)}

    # 売買代金セクション 8 を市場別に取得（8a/8b/8c または単一 8）
    def _extract_market_in_section8(market_kw: str) -> str:
        """セクション 8 (8a/8b/8c) から特定市場部分を抽出する。"""
        # まず 8a/8b/8c の英字接尾辞付きで該当市場を直接探す
        market_to_suffix = {"プライム": "a", "スタンダード": "b", "グロース": "c"}
        suffix = market_to_suffix.get(market_kw, "")
        if suffix:
            for k, v in sections.items():
                if k.startswith(f"## 8{suffix}."):
                    return v.rstrip()
        # 単一 `## 8. 売買代金` 内のサブヘッダで分割するレガシー形式
        section_8 = next((v for k, v in sections.items() if k.startswith("## 8.")), "")
        if not section_8:
            return ""
        sub_pattern = re.compile(r"(^### .*?)(?=^### |\Z)", flags=re.DOTALL | re.MULTILINE)
        subs = sub_pattern.findall(section_8)
        for sub in subs:
            first_line = sub.split("\n", 1)[0]
            if market_kw in first_line:
                return sub.rstrip()
        return section_8

    def _find_section(prefix_num: int, market_kw: str | None = None) -> str:
        """セクション番号と任意のキーワードでセクションを検索する。"""
        for k, v in sections.items():
            # 数字直後の英字接尾辞（8a/8b/8c）も許容
            if re.match(rf"^## {prefix_num}[a-z]?\.", k):
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
