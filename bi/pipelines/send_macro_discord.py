"""
マクロレポートを Discord のマクロチャンネルに送信する。
本文をテキストで送った後、MDファイルを添付する。

使い方:
  python send_macro_discord.py          # 今日のレポートを自動検索
  python send_macro_discord.py --date 2026-04-05  # 日付指定
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from raw_data_validator import check_news_raw

MARKET_DIR = Path(__file__).resolve().parent / ".." / ".." / "market" / "daily"
MACRO_DIR  = MARKET_DIR / "macro"
_ENV_PATH = Path(__file__).resolve().parent / ".env"
_CHUNK_SIZE = 1900
JST = timezone(timedelta(hours=9))

REQUIRED_SECTIONS = [
    (r"##\s*1[.．]\s*市況スナップショット",  "市況スナップショット（セクション1）",   "## 1. 市況スナップショット\n\n（要追記）\n"),
    (r"##\s*2[.．]\s*今日の重要テーマ",      "今日の重要テーマ（セクション2）",       "## 2. 今日の重要テーマ\n\n（要追記）\n"),
    (r"##\s*[34][.．]\s*総合見通し",          "総合見通し（セクション3or4）",          "## 4. 総合見通し\n\n（要追記）\n"),
    (r"📌\s*Deep Research",                  "Deep Research候補（末尾）",             "## 📌 Deep Research 候補\n\n- [ ] （要追記）\n"),
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


def find_report(target_date: str) -> Path:
    candidate = MACRO_DIR / f"{target_date}.md"
    if not candidate.exists():
        raise FileNotFoundError(
            f"{candidate} が見つかりません。"
        )
    return candidate


def ensure_deep_research_prompt_if_used(target_date: str) -> None:
    """
    Deep Research結果を使う場合は、同日のプロンプト発行ファイルが存在することを強制する。
    """
    dr_path = MACRO_DIR / f"{target_date}_deep_research.md"
    if not dr_path.exists():
        return
    prompt_path = MACRO_DIR / f"{target_date}_deep_research_prompt.md"
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"{prompt_path} が見つかりません。"
            "Deep Research 利用時はプロンプト発行が必須です。"
        )
    prompt_mtime_jst = datetime.fromtimestamp(prompt_path.stat().st_mtime, tz=timezone.utc).astimezone(JST).date()
    if prompt_mtime_jst != date.fromisoformat(target_date):
        raise RuntimeError(
            f"{prompt_path.name} は当日更新ではありません（mtime={prompt_mtime_jst}）。"
            "Deep Research 利用時は当日プロンプト発行が必須です。"
        )


def _split_chunks(text: str) -> list[str]:
    chunks = []
    while text:
        if len(text) <= _CHUNK_SIZE:
            chunks.append(text)
            break
        split = text[:_CHUNK_SIZE].rfind("\n")
        if split == -1:
            split = _CHUNK_SIZE
        chunks.append(text[:split])
        text = text[split:]
    return chunks


def send_to_discord(webhook_url: str, report_path: Path, target_date: str) -> None:
    text = report_path.read_text(encoding="utf-8")
    chunks = _split_chunks(text)
    total = len(chunks)

    for i, chunk in enumerate(chunks):
        r = requests.post(webhook_url, json={"content": chunk})
        r.raise_for_status()
        print(f"  本文 [{i+1}/{total}] 送信完了")

    caption = json.dumps({"content": f"📎 マクロレポート ({target_date})"})
    with open(report_path, "rb") as f:
        r = requests.post(
            webhook_url,
            data={"payload_json": caption},
            files={"file": (report_path.name, f, "text/plain")},
        )
    r.raise_for_status()
    print(f"  添付ファイル 送信完了: {report_path.name}")


def main() -> None:
    load_dotenv(_ENV_PATH)

    parser = argparse.ArgumentParser(description="マクロレポートを Discord に送信")
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"), help="日付 例: 2026-04-05")
    parser.add_argument("--fix", action="store_true", help="不足セクションをスタブで自動補完して送信")
    parser.add_argument("--skip-raw-check", action="store_true", help="news_raw.md の存在チェックをスキップ（Claude直接生成時に使用）")
    args = parser.parse_args()

    webhook_url = os.environ.get("DISCORD_WEBHOOK_MACRO", "").strip()
    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_MACRO が .env に未設定です。Discord でwebhookを作成して .env に追加してください。")

    # 生データの取得品質チェック（存在・タイムスタンプ・件数）
    if not args.skip_raw_check:
        check_news_raw(MARKET_DIR / f"{args.date}_news_raw.md", target_date=args.date)
    else:
        print("[SKIP] news_raw チェックをスキップしました（--skip-raw-check）")

    report_path = find_report(args.date)
    ensure_deep_research_prompt_if_used(args.date)
    report_text = report_path.read_text(encoding="utf-8")
    print(f"送信: {report_path.name}  ({report_path.stat().st_size / 1024:.1f} KB)")

    # バリデーション
    errors = validate_report(report_text)
    if errors and args.fix:
        report_text = fix_sections(report_path, report_text)
        errors = validate_report(report_text)

    if errors:
        print("\n[ERROR] 送信を中止しました。以下が不足しています:")
        for e in errors:
            print(f"  - {e}")
        print(f"\n合計 {len(errors)} 件の不足。レポートを修正してから再実行してください。")
        raise SystemExit(1)

    send_to_discord(webhook_url, report_path, args.date)
    print("完了")


if __name__ == "__main__":
    main()
