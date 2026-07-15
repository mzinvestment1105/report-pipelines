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
    python send_report_jpeg_discord.py --kind largecap_weekly --date 2026-06-27

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
    "pts_movers": {
        "md_path": "market/daily/pts_movers/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_MOVERS",
        "label": "夜間PTS動意レポート",
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
    "largecap_weekly": {
        "md_path": "market/daily/largecap/{date}.md",
        "webhook_env": "DISCORD_WEBHOOK_LARGECAP",
        "label": "週次大型株速報",
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
        # PM 2026-06-06 追加: 数値カバレッジ品質ゲート
        # 「省略統一」（取れない項目を行から削除）により、上流データ全断時に
        # 「銘柄行だけ並んで数値が全部空」のレポートが forbidden_patterns を回避して
        # 発行される劣化モードを構造的に防止する。終値 80% 以上・売買代金/時価総額 50%
        # 以上を必須化。違反時は送信中止＆ PM 通知。
        header_with_price = 0
        header_with_value = 0
        for i, m in enumerate(entry_matches):
            start = m.start()
            end = entry_matches[i + 1].start() if i + 1 < len(entry_matches) else len(md_text_pre)
            body = md_text_pre[start:end]
            header_line = body.split("\n", 1)[0]
            if "終値" in header_line:
                header_with_price += 1
            if "売買代金" in header_line or "時価総額" in header_line:
                header_with_value += 1
        total = len(entry_matches)
        price_ratio = header_with_price / total if total > 0 else 0.0
        value_ratio = header_with_value / total if total > 0 else 0.0
        print(f"[quality_gate] 終値カバレッジ: {header_with_price}/{total} ({price_ratio:.0%})・売買代金/時価総額カバレッジ: {header_with_value}/{total} ({value_ratio:.0%})")
        if not args.force:
            if price_ratio < 0.80:
                print(f"ERROR: 終値カバレッジ低下 {price_ratio:.0%} < 80%・空数値レポート疑い")
                _notify_failure(webhook_env=cfg["webhook_env"],
                                label=cfg["label"], identifier=identifier,
                                reason=f"終値カバレッジ {header_with_price}/{total} ({price_ratio:.0%}) < 80%・上流データ全断疑いのため発行停止")
                return 1
            if value_ratio < 0.50:
                print(f"ERROR: 売買代金/時価総額カバレッジ低下 {value_ratio:.0%} < 50%・空数値レポート疑い")
                _notify_failure(webhook_env=cfg["webhook_env"],
                                label=cfg["label"], identifier=identifier,
                                reason=f"売買代金/時価総額カバレッジ {header_with_value}/{total} ({value_ratio:.0%}) < 50%・上流データ全断疑いのため発行停止")
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

    新フォーマット（PM 2026-06-28 確定）では各市場ブロックが自己完結し、市場別タイトル
    `# 動意銘柄レポート {date}（{市場}）` を先頭に持つ（市場別の「今日の注目」を含む）。
    cat 統合された統合 md を、この市場タイトルを区切りに 3 ブロックへ分割する。
    グロースの売買代金（growth_b）はタイトルを持たないため、直前のグロースブロックに
    自動で吸収される（次のタイトル or 文末までを 1 ブロックとして拾うため）。

    旧フォーマットの §0 共通ヘッダ・§9 共通フッタ・§2/§4/§6 番号分割・8a/8b/8c 抽出は廃止。

    Returns:
        [(市場ラベル, 該当 Markdown), ...] 形式（最大 3 件）
    """
    import re

    block_pattern = re.compile(
        r"(^#\s*動意銘柄レポート.*?)(?=^#\s*動意銘柄レポート|\Z)",
        flags=re.DOTALL | re.MULTILINE,
    )
    blocks = [m.group(1).rstrip() for m in block_pattern.finditer(md_text)]
    if not blocks:
        return []

    result: list[tuple[str, str]] = []
    for block in blocks:
        title_line = block.split("\n", 1)[0]
        label = next(
            (kw for kw in ("プライム", "スタンダード", "グロース") if kw in title_line),
            "ALL",
        )
        result.append((label, block))
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
