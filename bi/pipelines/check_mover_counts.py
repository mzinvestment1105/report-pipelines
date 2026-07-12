#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""動意レポート 送信前 件数ゲート（PM 2026-06-28 確定）。

merge 後の統合 md（{date}.md / {date}_weekly.md）を市場ブロックへ分割し、
各市場の「値上がり / 値下がり / 売買代金」セクションの銘柄エントリ（### N位 …）
件数を機械カウントして、規定数に満たなければ exit 3 で Discord 送信をブロックする。
exit code 規約（2026-07-07 統一）: 0=合格 / 1=インフラ失敗（ファイル読込不可等） /
3=品質ゲート不合格（送信中止）。workflow 側は 3 を「⛔ 送信中止」通知に分岐し
インフラ失敗（❌）と区別する。
加えて各エントリ行に 終値・時価総額・売買代金 が全て揃っているかの完備ゲートも行うが、
これは警告のみで送信は止めない（PM 2026-07-01・1銘柄の項目欠落で全体未達になる事故を回避）。
送信を止めるのは件数不足と市場ブロック欠落だけ。数値欠落は make_mover_report の yfinance 補完で極力埋める。

規定数（日次・週次共通）:
  プライム  : 値上がり 5 / 値下がり 5 / 売買代金 5
  スタンダード: 値上がり 5 / 値下がり 5 / 売買代金 5
  グロース  : 値上がり 5 / 値下がり 5 / 売買代金 10  ← growth_b 脱落事故の再発防止

使い方:
  python check_mover_counts.py --file market/daily/movers/2026-06-26.md
  python check_mover_counts.py --file market/daily/movers/2026-06-26.md --date 2026-06-26
  （--date 指定時はタイトル行の対象日一致も検査＝日付品質ゲート。省略時は従来動作。
    2026-07-09 に 7/8 データを「本日」として配信した日付品質事故の再発防止・PM 2026-07-12）
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

# 各エントリ行に必須の項目（PM 2026-06-30 確定・欠落配信防止）。
# 例: 売買代金欠落（### 1位 7815 東京ボード工業 …（終値 306円 / 時価総額 9億円）） を送信前に止める。
REQUIRED_FIELDS = ("終値", "時価総額", "売買代金")

# 完備ゲートは PM 2026-07-01 で「警告のみ・送信は止めない」へ変更。
# IPO 直後の未登録銘柄（589A・464A 等）で 1 項目でも欠落すると、以前はレポート全体が
# 送信停止し「1銘柄の欠落で全体が届かない」事故が繰り返された。完備は品質警告に留め、
# 送信を止めるのは件数ゲート（規定数不足）と市場ブロック欠落だけとする。
# 数値欠落自体は make_mover_report.py の yfinance 補完で極力埋める。
FATAL_FIELDS = ()  # 完備ゲートで送信停止する項目は無し（全項目 警告のみ）


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


def check_entry_fields(lines: list[str]) -> list[tuple[str, list[str]]]:
    """値上がり/値下がり/売買代金の各 ### エントリ行が終値・時価総額・売買代金を全て含むか検査。
    欠落があれば (見出し抜粋, 欠落項目リスト) を返す。"""
    incomplete: list[tuple[str, list[str]]] = []
    cur_sec: str | None = None
    for line in lines:
        if SECTION_RE.match(line):
            cur_sec = "in_section"
            continue
        if line.startswith("## "):
            cur_sec = None
            continue
        if cur_sec and ENTRY_RE.match(line):
            missing = [f for f in REQUIRED_FIELDS if f not in line]
            if missing:
                incomplete.append((line.strip()[:48], missing))
    return incomplete


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="統合 md ファイルパス")
    ap.add_argument("--date", default="",
                    help="対象日 YYYY-MM-DD（指定時はタイトル行の日付一致も検査・省略時は従来動作＝検証スキップ）")
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

    # 対象日タイトルゲート（PM 2026-07-12 確定・--date 指定時のみ・省略時は従来動作）。
    # 7/8 データを「本日（7/9）」として記載した 2026-07-09 日付品質事故の再発防止レイヤー②。
    # 統合 md の全タイトル行（# 動意銘柄レポート …）が対象日文字列を含むことを機械検査し、
    # 別日付レポートの誤配信を送信前に止める（exit 3 = ⛔ 送信中止）。
    if args.date:
        title_lines = [ln for ln in text.splitlines() if TITLE_RE.match(ln)]
        bad_titles = [ln for ln in title_lines if args.date not in ln]
        print(f"[日付ゲート] タイトル {len(title_lines)} 件中 対象日 {args.date} 一致 "
              f"{len(title_lines) - len(bad_titles)} 件")
        for ln in bad_titles:
            failures.append(f"タイトル日付が対象日 {args.date} と不一致: {ln.strip()[:60]}")

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
        # 完備ゲート: 各エントリに 終値・時価総額・売買代金 が揃っているか（PM 2026-06-30）。
        # 終値・売買代金の欠落は送信ブロック（バグ検知）。時価総額のみ欠落は警告して送信継続
        # （IPO 直後の未提供銘柄で全体停止しないため・PM 2026-07-01）。
        for head, missing in check_entry_fields(lines):
            fatal = [f for f in missing if f in FATAL_FIELDS]
            warn = [f for f in missing if f not in FATAL_FIELDS]
            if warn:
                print(f"[完備ゲート:警告] {market} 項目欠落[{'/'.join(warn)}]（送信は継続）: {head}",
                      file=sys.stderr)
            if fatal:
                print(f"[完備ゲート] {market} 項目欠落[{'/'.join(fatal)}]: {head}", file=sys.stderr)
                failures.append(f"{market} エントリ項目欠落[{'/'.join(fatal)}]: {head}")

    if failures:
        print("\n[件数ゲート] [NG] 品質ゲート不合格のため送信を中止します:", file=sys.stderr)
        for f_ in failures:
            print(f"  - {f_}", file=sys.stderr)
        return 3  # 品質ゲート不合格（インフラ失敗 exit 1 と区別・⛔ 通知分岐用）

    print("\n[件数ゲート] [OK] 全市場・全セクションが規定件数を満たしています。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
