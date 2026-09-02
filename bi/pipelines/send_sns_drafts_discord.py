"""
SNS アナリストが生成した X 投稿案ファイル（research/sns/{date}_*.md など）を
Discord の SNS アナリストチャンネル（DISCORD_WEBHOOK_SNS）に優先度順で送信する。

ファイル内の `## 候補X：...` セクションを 1 候補 = 1 Discord メッセージとして送信する。
セクション名に「★Claude 推奨★」が含まれる候補は先頭に送信する。

使い方:
  python send_sns_drafts_discord.py --file research/sns/2026-05-21_morning_buzz_candidates.md
  python send_sns_drafts_discord.py --file <path> --header "明日の朝予約投稿候補"
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
_CHUNK_SIZE = 1900


def load_webhook() -> str:
    """DISCORD_WEBHOOK_SNS を解決する。

    優先順位:
      1. 環境変数 DISCORD_WEBHOOK_SNS（GitHub Actions の secrets 経由）
      2. bi/pipelines/.env （ローカル実行用。GHA 上には存在しない）
    """
    url = os.getenv("DISCORD_WEBHOOK_SNS")
    if url:
        return url
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
        url = os.getenv("DISCORD_WEBHOOK_SNS")
    if not url:
        print(
            "[ERROR] DISCORD_WEBHOOK_SNS が未設定です"
            "（環境変数または bi/pipelines/.env に設定してください）",
            file=sys.stderr,
        )
        sys.exit(1)
    return url


def split_candidates(text: str) -> tuple[str, list[tuple[str, str]]]:
    """
    ファイル全体を「冒頭メタ部 + 候補リスト」に分割する。
    候補は `## 候補` で始まるセクションを単位にする。
    返り値: (header_text, [(candidate_title, candidate_body), ...])
    """
    parts = re.split(r"(?m)^##\s+候補", text)
    head = parts[0].strip()
    candidates: list[tuple[str, str]] = []
    for chunk in parts[1:]:
        body = "## 候補" + chunk
        body = re.split(r"(?m)^##\s+(?!候補)", body)[0].rstrip()
        title_line = body.splitlines()[0].strip()
        candidates.append((title_line, body))
    return head, candidates


def reorder_by_priority(candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """★Claude 推奨★ 表記の候補を先頭に持ってくる。それ以外は元の順を保つ。"""
    recommended = [c for c in candidates if "★Claude 推奨★" in c[0] or "Claude 推奨" in c[0]]
    others = [c for c in candidates if c not in recommended]
    return recommended + others


def extract_tweet_body(candidate_body: str) -> str | None:
    """
    候補本文（Markdown）から ``` で囲まれた最初のコードブロック内容を抽出する。
    スマホで長押し → コピーで投稿本文だけ取れるようにするための単独メッセージ用。
    抽出失敗時は None。
    """
    m = re.search(r"^```\s*\n(.*?)\n^```\s*$", candidate_body, flags=re.MULTILINE | re.DOTALL)
    if not m:
        return None
    return m.group(1).strip()


def chunk_text(text: str, size: int = _CHUNK_SIZE) -> list[str]:
    """Discord の 2000 字制限に収まるよう分割する。"""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def send_message(webhook: str, content: str) -> None:
    for chunk in chunk_text(content):
        resp = requests.post(webhook, json={"content": chunk}, timeout=30)
        if resp.status_code not in (200, 204):
            print(f"[ERROR] Discord HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            sys.exit(1)
        time.sleep(0.4)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="送信する Markdown ファイルのパス")
    parser.add_argument("--header", default=None, help="冒頭に送る見出し（省略時はファイル名から自動生成）")
    parser.add_argument(
        "--hide-source",
        action="store_true",
        help="末尾の『元ファイル: <パス>』行を送らない（Discord にローカルパスを載せない運用向け）",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"[ERROR] ファイルが存在しません: {path}", file=sys.stderr)
        sys.exit(1)

    text = path.read_text(encoding="utf-8")
    head, candidates = split_candidates(text)
    if not candidates:
        print(f"[ERROR] 候補セクション（## 候補X：...）が見つかりません: {path}", file=sys.stderr)
        sys.exit(1)

    candidates = reorder_by_priority(candidates)
    webhook = load_webhook()

    header = args.header or f"**SNS 投稿案 / {path.stem}**"
    intro = f"{header}\n\n{head}\n\n---\n**優先度順に {len(candidates)} 候補を送信します。**"
    send_message(webhook, intro)
    print(f"[OK] header sent ({len(intro)} chars)")

    for i, (title, body) in enumerate(candidates, 1):
        msg = f"**[#{i}] {title}**\n\n{body}"
        send_message(webhook, msg)
        safe_title = title[:60].encode("ascii", "replace").decode("ascii")
        print(f"[OK] candidate {i}: {safe_title}")

        # スマホからコピーしやすいよう、投稿本文のみを単独メッセージで送る
        tweet_body = extract_tweet_body(body)
        if tweet_body:
            send_message(webhook, tweet_body)
            print(f"[OK]   - tweet body ({len(tweet_body)} chars) sent for copy-paste")
        else:
            print(f"[WARN] candidate {i}: code block not extracted")

    if args.hide_source:
        send_message(webhook, "---\n送信完了")
    else:
        send_message(webhook, f"---\n送信完了 / 元ファイル: `{path}`")
    print("[OK] all sent")


if __name__ == "__main__":
    main()
