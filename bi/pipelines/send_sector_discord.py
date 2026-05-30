"""
セクター週次レポートを Discord に送信する。

  セクターチャンネル → DISCORD_WEBHOOK_SECTOR (_sector_analysis.md)

分析ファイルが存在しない場合はエラー終了（生データは送らない）。

使い方:
  python send_sector_discord.py
  python send_sector_discord.py --date 2026-04-11
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

from raw_data_validator import check_sector_raw

MARKET_DIR  = Path(__file__).resolve().parent / ".." / ".." / "market" / "daily"
SECTOR_DIR  = MARKET_DIR / "sector"
_ENV_PATH   = Path(__file__).resolve().parent / ".env"
_CHUNK_SIZE = 1900

REQUIRED_SECTIONS = [
    (r"##\s*[01][.．]\s*(今週の)?サマリー",           "サマリー（セクション0）",               "## 0. 今週のサマリー\n\n（要追記）\n"),
    (r"##\s*1[.．]\s*セクター週次パフォーマンス",       "セクター週次パフォーマンス一覧（セクション1）","## 1. セクター週次パフォーマンス一覧\n\n（要追記）\n"),
    (r"##\s*5[.．]\s*総括",                            "総括・来週のシナリオ（セクション5）",    "## 5. 総括・来週のシナリオ\n\n（要追記）\n"),
    # PM 2026-05-30 確定: Deep Research 候補セクションを全廃。必須セクションリストから削除。
]


def validate_report(text: str) -> list[str]:
    """レポートの必須要素を確認し、不足項目のリストを返す。空リストなら合格。"""
    errors: list[str] = []
    for pattern, label, _stub in REQUIRED_SECTIONS:
        if not re.search(pattern, text):
            errors.append(f"セクションが存在しない: {label}")
    return errors


def fix_sections(path: Path, text: str) -> str:
    """不足セクションのスタブを末尾に追記してファイルを上書きする。修正後テキストを返す。"""
    appended: list[str] = []
    for pattern, label, stub in REQUIRED_SECTIONS:
        if not re.search(pattern, text):
            text = text.rstrip() + "\n\n" + stub
            appended.append(label)
    if appended:
        path.write_text(text, encoding="utf-8")
        print("[FIX] スタブを追記しました:")
        for a in appended:
            print(f"  + {a}")
    return text


def find_analysis(target_date: str) -> Path:
    candidate = SECTOR_DIR / f"{target_date}.md"
    if not candidate.exists():
        raise FileNotFoundError(
            f"{candidate} が見つかりません。"
            "先に Claude Code で分析を実行してください。\n"
            f"  1. python make_sector_raw.py --date {target_date} [--deep-research-file dr.txt]\n"
            "  2. Claude Code に raw ファイルを読ませて market/daily/sector/{target_date}.md を生成\n"
            f"  3. python send_sector_discord.py --date {target_date}"
        )
    return candidate


def _send(webhook: str, text: str) -> None:
    """1900 文字ずつ分割して送信。"""
    while text:
        if len(text) <= _CHUNK_SIZE:
            chunk, text = text, ""
        else:
            split = text[:_CHUNK_SIZE].rfind("\n")
            if split == -1:
                split = _CHUNK_SIZE
            chunk, text = text[:split], text[split:]
        r = requests.post(webhook, json={"content": chunk})
        r.raise_for_status()


def main() -> None:
    load_dotenv(_ENV_PATH)

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--fix", action="store_true", help="不足セクションをスタブで自動補完して送信")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_SECTOR", "").strip()
    if not webhook:
        raise ValueError("DISCORD_WEBHOOK_SECTOR が未設定です。.env に追加してください。")

    # 生データの取得品質チェック（存在・タイムスタンプ・件数）
    check_sector_raw(MARKET_DIR / f"{args.date}_sector_raw.md", target_date=args.date)

    analysis_path = find_analysis(args.date)
    analysis_text = analysis_path.read_text(encoding="utf-8")
    print(f"送信: {analysis_path.name}  ({analysis_path.stat().st_size / 1024:.1f} KB)")

    # バリデーション
    errors = validate_report(analysis_text)
    if errors and args.fix:
        analysis_text = fix_sections(analysis_path, analysis_text)
        errors = validate_report(analysis_text)

    if errors:
        print("\n[ERROR] 送信を中止しました。以下が不足しています:")
        for e in errors:
            print(f"  - {e}")
        print(f"\n合計 {len(errors)} 件の不足。レポートを修正してから再実行してください。")
        raise SystemExit(1)

    SEP = "=" * 40
    _send(webhook, f"{SEP}\nセクター週次レポート　{args.date}\n{SEP}\n\n{analysis_text}")
    print("セクター週次レポート 送信完了")


if __name__ == "__main__":
    main()
