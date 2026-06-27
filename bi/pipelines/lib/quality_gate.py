"""レポート品質ゲート（確定処理・非ブロッキング）。

PM 2026-06-28: 「配信を止めない・token を使わない」を最優先とするため、品質処理は
(1) 生成物に依存しない確定除去（Python で機械的に必ず効く）と (2) 非ブロッキング監視
のみで構成する。送信のブロック（失敗）・自動再生成（token 増）は一切行わない。

確定除去:
- strip_vix_rows         : 市況スナップショット表から VIX / 日経 VI 行を機械除去
                           （PM: VIX は出力に不要・両方外す。生成 LLM が ─ 行を再追加するため）
- strip_repeated_annotations : 同一の注釈 （…） が2回以上出る場合、初出のみ残し2回目以降の
                           注釈だけを確定除去（中学生注釈は一度で十分・反復は重複説明）。語は残す。

監視（非ブロッキング）:
- find_duplication       : 同一注釈の反復を検出して文字列で返す（PM 2026-06-28 で基準変更。
                           旧「固有名詞のセクション横断数」は中心材料で誤検出するため廃止）。
"""
from __future__ import annotations

import re
from collections import Counter

# VIX / 日経 VI の表行（先頭セルが当該指標の行）を丸ごと除去する。先頭セル限定なので
# 本文中の VIX 言及は消さず、スナップショット表のプレースホルダ行のみを対象とする。
_VIX_ROW_RE = re.compile(
    r"^\s*\|\s*(?:米\s*VIX|日経\s*VI|VIX|VI)\s*\|.*\n?",
    re.MULTILINE,
)

# 実体のある注釈（中学生向け用語説明）を対象にする。短い数値・日付括弧（（5月）（現物比）
# （+1.2%）等）は対象外にするため内側 6 文字以上に限定する。
_ANNOT_RE = re.compile(r"（[^（）]{6,}）")


def strip_vix_rows(md_text: str) -> str:
    """市況スナップショット表から VIX / 日経 VI の行を確定除去する。"""
    return _VIX_ROW_RE.sub("", md_text)


def strip_repeated_annotations(md_text: str) -> tuple[str, list[str]]:
    """同一の注釈 （…） が2回以上出る場合、初出を残し2回目以降の注釈だけを確定除去する。
    語そのものは本文に残す（注釈括弧だけ消える）。

    returns (cleaned_text, removed) — removed は ["（…注釈…） を N 回除去（初出のみ保持）", ...]。
    """
    seen: set[str] = set()
    removed_counts: Counter[str] = Counter()

    def repl(m: re.Match[str]) -> str:
        ann = m.group(0)
        if ann in seen:
            removed_counts[ann] += 1
            return ""  # 2回目以降は注釈括弧を削除（直前の語は残る）
        seen.add(ann)
        return ann

    cleaned = _ANNOT_RE.sub(repl, md_text)
    removed = [
        f"{ann} を {c} 回除去（初出のみ保持）"
        for ann, c in removed_counts.most_common()
    ]
    return cleaned, removed


def find_duplication(md_text: str) -> list[str]:
    """同一の注釈 （…） が2回以上出現するものを違反文字列で返す（非ブロッキング監視）。
    strip_repeated_annotations 後に呼べば残注釈重複の検証になり、通常は空リストになる。"""
    counts = Counter(_ANNOT_RE.findall(md_text))
    return [
        f"{ann} が {n} 回重複注釈"
        for ann, n in counts.most_common()
        if n >= 2
    ]
