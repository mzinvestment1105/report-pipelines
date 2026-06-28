#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""動意レポート 送信前 件数ゲート（PM 2026-06-28 確定）。

merge 後の統合 md（{date}.md / {date}_weekly.md）を市場ブロックへ分割し、
各市場の「値上がり / 値下がり / 売買代金」セクションの銘柄エントリ（### N位 …）
件数を機械カウントして、規定数に満たなければ exit 1 で Discord 送信をブロックする。

規定数（日次・週次共通）:
  プライム  : 値上がり 5 / 値下がり 5 / 売買代金 5
  スタンダード: 値上がり 5 / 値下がり 5 / 売買代金 5
  グロース  : 値上がり 5 / 値下がり 5 / 売買代金 10  ← growth_b 脱落事故の再発防止

使い方:
  python check_mover_counts.py --file market/daily/movers/2026-06-26.md
"""
from __future__ import annotations

import argparse
import re
import sys

# Windows コンソール（cp932）でも日本語・記号で落ちないよう utf-8 へ寄せる
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# 市場名（タイトル行 / 区分判定）→ 期待件数 {値上がり, 値下がり, 売買代金}
EXPECTED = {
    "プライム":    {"値上がり": 5, "値下がり": 5, "売買代金": 5},
    "スタンダード": {"値上がり": 5, "値下がり": 5, "売買代金": 5},
    "グロース":    {"値上がり": 5, "値下がり": 5, "売買代金": 10},
}

TITLE_RE   = re.compile(r"^#\s*動意銘柄レポート", )
SECTION_RE = re.compile(r"^##\s*(値上がり|値下がり|売買代金)")
ENTRY_RE   = re.compile(r"^###\s")


def detect_market(title_line: str) -> str | None:
    for name in EXPECTED:
        if name in title_line:
            return name
    return None


def parse_blocks(text: str) -> list[tuple[str, list[str]]]:
    """(市場名, その市場ブロックの行リスト) を返す。タイトルなしの先頭塊は無視。"""
    blocks: list[tuple[str, list[str]]] = []
    cur_market: str | None = None
    cur_lines: list[str] = []
    for line in text.splitlines():
        if TITLE_RE.match(line):
            if cur_market is not None:
                blocks.append((cur_market, cur_lines))
            cur_market = detect_market(line)
            cur_lines = []
        else:
            if cur_market is not None:
                cur_lines.append(line)
    if cur_market is not None:
        blocks.append((cur_market, cur_lines))
    return blocks


def count_sections(lines: list[str]) -> dict[str, int]:
    """ブロック内の各セクション（値上がり/値下がり/売買代金）の ### エントリ数。"""
    counts: dict[str, int] = {}
    cur_sec: str | None = None
    for line in lines:
        m = SECTION_RE.match(line)
        if m:
            cur_sec = m.group(1)
            counts.setdefault(cur_sec, 0)
            continue
        if line.startswith("## "):  # 別の ## セクション（今日の注目等）に出たら抜ける
            cur_sec = None
            continue
        if cur_sec and ENTRY_RE.match(line):
            counts[cur_sec] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="統合 md ファイルパス")
    args = ap.parse_args()

    try:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"[件数ゲート] ファイル読込失敗: {e}", file=sys.stderr)
        return 1

    blocks = parse_blocks(text)
    seen_markets = {m for m, _ in blocks}
    failures: list[str] = []

    for market, exp in EXPECTED.items():
        if market not in seen_markets:
            failures.append(f"市場ブロック『{market}』が見つからない")
            continue

    for market, lines in blocks:
        exp = EXPECTED.get(market)
        if not exp:
            continue
        counts = count_sections(lines)
        for sec, need in exp.items():
            got = counts.get(sec, 0)
            mark = "OK" if got == need else ("不足" if got < need else "過多")
            print(f"[件数ゲート] {market} {sec}: {got}/{need} {mark}")
            if got < need:
                failures.append(f"{market} {sec} が {got} 件（規定 {need} 件）")

    if failures:
        print("\n[件数ゲート] [NG] 規定件数を満たさないため送信を中止します:", file=sys.stderr)
        for f_ in failures:
            print(f"  - {f_}", file=sys.stderr)
        return 1

    print("\n[件数ゲート] [OK] 全市場・全セクションが規定件数を満たしています。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
