"""個別銘柄レポートの送信前機械ゲート（PM 2026-08-30 承認）。

Step 5.5 の grep 系検査の機械化。判断が要る検査（数値の羅列・重複・希薄化4点・目視系）は
スキル側（.claude/commands/stock-report.md）に残置する。

本スクリプトは verify_report_numbers.verify()（数値整合の検算）を取り込み、加えて
誌面の書き方・禁止文言の grep 系検査をまとめて実行する。errors（送信停止）と
warnings（表示のみ・送信は継続）を区別する。

使い方:
    python gate_stock_report.py --code 7256 --md research/stocks/7256/2026-08-30.md
    python gate_stock_report.py --code 7256 --date 2026-08-30

exit 0 = PASS（送信可）/ exit 1 = FAIL（送信中止）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_report_numbers import verify as verify_numbers  # noqa: E402

# --- errors 側の grep 検査（1 件でもヒットしたら送信中止） -------------------
# (name, pattern, ヒットした場合の対処)
FORBIDDEN_PATTERNS: list[tuple[str, str, str]] = [
    (
        "禁止文言（未取得の告白）",
        r"未取得|データ未取得|一次情報未取得|確認できておらず|確認次第|特定できていない"
        r"|特定できず|読んでいない|読めていない|取得を行っていない|記載は行わない|記載しない",
        "文言を消すのではなく (1) 一次情報で取得して実数で埋める "
        "(2) 該当行・該当セクションごと削除する のいずれかで直す",
    ),
    (
        "遡及説明（事後判明情報での値動き説明）",
        r"水面下|未開示|後から判明|事後的に判明|当時は開示されて",
        "開示前の値動きは当時公開されていた情報のみで説明し、できなければ要因不明と書く",
    ),
    (
        "推測語",
        r"可能性が高い|思われる|考えられる|だろう|とみられる|はずだ",
        "一次情報で事実を固め、その事実が支える範囲で断定的に書く",
    ),
    (
        "レポート内参照",
        r"前述|後述|上記の|下記の|前掲|後掲|セクション[0-9０-９]|§[0-9]",
        "参照をやめて値・条件・前提をその場で再掲する",
    ),
    (
        "33業種加重平均が主比較",
        r"セクター加重平均|業種加重平均|33分類",
        "事業実態で選んだ同業個社（Step 4.2 で確定した peers）との比較表へ差し替える",
    ),
]

# §1 基本情報の「セクター」行は 33 業種の分類名を正当に書く場所のため、
# 「33業種加重平均が主比較」の検出から除外する（比較表での使用は引き続き検出する）。
SECTOR_LABEL_ROW = re.compile(r"^\|\s*(セクター|業種|東証33業種)\s*\|")

# --- warnings 側 -----------------------------------------------------------
UNIT_IN_CELL = re.compile(r"[0-9][^|]*?(百万円|千円|億円|株|円|%|％)")
WIDE_TABLE_ROW = re.compile(r"^\|([^|]*\|){4,}$")
SEPARATOR_ROW = re.compile(r"^\|[\s:\-|]+\|$")
HEADER_UNIT = re.compile(r"（単位")


def _grep(md: str, pattern: str) -> list[tuple[int, str]]:
    pat = re.compile(pattern)
    out = []
    for i, line in enumerate(md.splitlines(), 1):
        if pat.search(line):
            out.append((i, line.strip()))
    return out


def _check_unit_in_cell(md: str) -> list[tuple[int, str]]:
    """表のデータ行のセル内に単位が入っている行を拾う（誤検知しうるため warning）。"""
    out = []
    lines = md.splitlines()
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s.startswith("|"):
            continue
        if SEPARATOR_ROW.match(s) or HEADER_UNIT.search(s):
            continue
        # 次行が区切り行 = この行はヘッダ行。ヘッダへの単位表記は可
        if i < len(lines) and SEPARATOR_ROW.match(lines[i].strip()):
            continue
        cells = s.strip("|").split("|")
        if any(UNIT_IN_CELL.search(c) for c in cells):
            out.append((i, s))
    return out


def run_gate(md: str, code: str) -> tuple[list[str], list[str], list[str]]:
    """(errors, warnings, info) を返す。errors が空なら送信可。"""
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []

    # 1. 禁止 grep 系（errors）
    for name, pattern, remedy in FORBIDDEN_PATTERNS:
        hits = _grep(md, pattern)
        if name.startswith("33業種"):
            hits = [(i, t) for i, t in hits if not SECTOR_LABEL_ROW.match(t)]
        if hits:
            detail = " / ".join(f"L{i}: {t[:60]}" for i, t in hits[:5])
            more = f"（他 {len(hits) - 5} 件）" if len(hits) > 5 else ""
            errors.append(f"[{name}] {len(hits)}件ヒット: {detail}{more} → 対処: {remedy}")
        else:
            info.append(f"[{name}] ヒットなし")

    # 2. 反応スコアの欠落（errors）
    n_score = len(re.findall("反応スコア", md))
    if n_score < 3:
        errors.append(
            f"[反応スコアの欠落] 「反応スコア」の出現が {n_score} 回（3 回未満）。"
            " → 対処: 直近材料の上位 3 件それぞれに反応スコア"
            "（日中値幅 × 出来高 5 日平均比）を数値で入れる"
        )
    else:
        info.append(f"[反応スコア] {n_score} 回（3 回以上）")

    # 3. 数値整合の検算（verify_report_numbers）
    v_err, v_warn = verify_numbers(md, code)
    errors.extend(v_err)
    warnings.extend(v_warn)
    if not v_err:
        info.append("[数値整合検算] OK")

    # 4. セル内単位（warnings）
    unit_hits = _check_unit_in_cell(md)
    if unit_hits:
        detail = " / ".join(f"L{i}: {t[:50]}" for i, t in unit_hits[:5])
        more = f"（他 {len(unit_hits) - 5} 件）" if len(unit_hits) > 5 else ""
        warnings.append(
            f"[セル内単位] {len(unit_hits)}件: {detail}{more}"
            " → 対処: セル内から単位を外し、表の外に「（単位: 百万円）」等でまとめて示す"
        )
    else:
        info.append("[セル内単位] ヒットなし")

    # 5. 4 列以上の表の行数（warnings・報告のみ）
    wide = [i for i, line in enumerate(md.splitlines(), 1) if WIDE_TABLE_ROW.match(line.strip())]
    if wide:
        warnings.append(
            f"[4列以上の表] {len(wide)}行。"
            " → 対処: 長文セルを含む表は該当列を表の外の文章へ移す"
            "（短語のみ・数値のみの表は制限を受けない）"
        )
    else:
        info.append("[4列以上の表] なし")

    return errors, warnings, info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="銘柄コード")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--md", help="レポートの相対パス（--date より優先）")
    args = ap.parse_args()

    if args.md:
        md_path = Path(args.md)
        if not md_path.is_absolute():
            md_path = REPO_ROOT / args.md
    else:
        md_path = REPO_ROOT / "research" / "stocks" / args.code / f"{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: report not found: {md_path}")
        return 1

    errors, warnings, info = run_gate(md_path.read_text(encoding="utf-8"), args.code)

    print(f"GATE: {md_path.name}（{args.code}）")
    for m in info:
        print("  OK   " + m)
    for m in warnings:
        print("  WARN " + m)
    for m in errors:
        print("  NG   " + m)

    if errors:
        print("GATE: FAIL（送信中止）")
        return 1
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
