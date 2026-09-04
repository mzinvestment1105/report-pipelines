"""レポート内相互参照の機械検出（全レポート種別横断・警告のみ）。

PM 2026-09-05 指示: 誌面で他セクション・他順位・他銘柄行を指し示す表記
（「→ 値下がり N 位を参照」「前述」「上記の」等）を全レポート種別で禁止する。
重複銘柄も各セクションで「何の会社」「なぜ動いた」を独立して書き切る。

本モジュールは検出のみを担い、配信を止めない（_common_rules.md §36 絶対配信原則）。
呼び出し側（check_mover_counts.py / workflow の警告ステップ）が結果をログへ出し、
次回生成で Claude が是正する運用とする。

使い方（ライブラリ）:
    from lib.crossref_check import find_cross_references
    hits = find_cross_references(md_text)   # [(行番号, 該当文言, 行内容), ...]

使い方（単体・警告のみで常に exit 0 / --strict で検出時 exit 3）:
    python bi/pipelines/lib/crossref_check.py --file market/daily/movers/2026-09-04.md
"""
from __future__ import annotations

import argparse
import re
import sys

# --- 禁止パターン ---------------------------------------------------------
# 誌面本文で他セクション・他順位・他銘柄行を指し示す表記。
# 「参考」「参照元」等の正当な用法・出典表記を誤検出しないよう、
# 「参照」は直前に指示対象（位 / セクション / § / 詳細は 等）を伴う形だけを拾う。
_PATTERNS: tuple[tuple[str, str], ...] = (
    ("順位参照",     r"[0-9０-９]+\s*位を参照"),
    ("矢印参照",     r"→\s*値[上下]がり"),
    ("矢印参照",     r"→\s*売買代金"),
    ("セクション参照", r"(?:セクション|章|節|欄|表|項)を参照"),
    ("セクション参照", r"セクション\s*[0-9０-９]"),
    ("セクション参照", r"§\s*[0-9０-９]"),
    ("詳細は参照",   r"詳細は[^。\n]{0,20}(?:参照|参考|のとおり|の通り)"),
    ("重要テーマ参照", r"(?:重要テーマ|本日のテーマ|上記テーマ|同テーマ|同セクション|前の?セクション|別セクション)を?参照"),
    ("前述後述",     r"前述|後述|前掲|後掲"),
    ("上記下記",     r"上記の|下記の|上記に|下記に|上記のとおり|下記のとおり|上記参照|下記参照"),
    ("同上再掲",     r"同上|再掲"),
    ("既出参照",     r"既出の(?:とおり|通り)|既出のため|既出につき"),
)

_COMPILED = tuple((label, re.compile(pat)) for label, pat in _PATTERNS)

# 誤検出の除外: レポート本文ではない機械挿入行・メタ行。
# - 品質注記（workflow が機械挿入する `> ⚠️ **品質注記**: …`）
# - HTML コメント（`<!-- OWN_THEMES_JSON … -->` 等の機械用ブロック）
# - md のリンク定義行・コードフェンス内は誌面へ出ないため対象外にする
_SKIP_LINE_RE = re.compile(r"^\s*(?:>\s*⚠️|<!--|\[[^\]]+\]:\s)")
_FENCE_RE = re.compile(r"^\s*```")


def find_cross_references(md_text: str) -> list[tuple[int, str, str]]:
    """誌面本文の相互参照表記を検出して (行番号, 該当文言, 行内容) のリストを返す。

    行番号は 1 始まり。コードフェンス内・品質注記行・HTML コメント行は対象外。
    """
    hits: list[tuple[int, str, str]] = []
    in_fence = False
    for i, line in enumerate(md_text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or _SKIP_LINE_RE.match(line):
            continue
        for label, rx in _COMPILED:
            m = rx.search(line)
            if m:
                hits.append((i, f"{label}: {m.group(0)}", line.strip()[:80]))
                break  # 1 行 1 件に留める（同一行の多重報告を避ける）
    return hits


def report_cross_references(md_text: str, prefix: str = "[参照ゲート]") -> list[tuple[int, str, str]]:
    """検出結果を stderr へ出力し、ヒット一覧を返す（配信は止めない）。"""
    hits = find_cross_references(md_text)
    if not hits:
        print(f"{prefix} [OK] レポート内相互参照の表記なし。")
        return hits
    print(f"{prefix} [警告] レポート内相互参照 {len(hits)} 件を検出（_common_rules.md §40 違反）。"
          f"配信は止めません。次回生成で各セクションを独立して書き切ってください:", file=sys.stderr)
    for lineno, hit, content in hits:
        print(f"{prefix}   L{lineno} {hit} | {content}", file=sys.stderr)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="レポート内相互参照の機械検出（警告のみ）")
    ap.add_argument("--file", required=True, help="検査対象の md ファイルパス")
    ap.add_argument("--strict", action="store_true",
                    help="検出時 exit 3 を返す（既定は警告のみで exit 0・絶対配信原則）")
    args = ap.parse_args()

    try:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"[参照ゲート] ファイル読込失敗: {e}", file=sys.stderr)
        return 1

    hits = report_cross_references(text)
    if hits and args.strict:
        return 3
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
