"""レポート品質ゲート（確定処理）。同一材料の重複説明を機械検出し、送信前にブロックする。

PM 2026-06-27: 「同一材料を複数セクションで重複説明しない」というソフトな指示は生成 LLM に
無視される実績があるため（マクロ朝刊で Apple/Oracle 等が 4〜5 セクションに重複）、生成物に
依存しない確定処理として、同一の固有名詞材料が説明されるセクション数を数え、閾値以上なら
「重複あり」と判定する。呼び出し側はこれを使って自動送信を保留し、重複レポートが PM に届く
ことを構造的に防ぐ。
"""
from __future__ import annotations

import re
from collections import Counter

# 注釈対象・一般語で誤検出しやすい略語・指数名は材料から除外する
_STOP = {
    "FOMC", "FRB", "GDP", "CPI", "PCE", "VIX", "ETF", "REIT", "IPO", "PER",
    "PBR", "ROE", "ROIC", "NQN", "TDNet", "EDINET", "QUICK", "JST", "ADR",
    "PMI", "BOJ", "ECB", "WTI", "OPEC",
}

_SECTION_RE = re.compile(r"^#{2,3}\s")
_LATIN_RE = re.compile(r"[A-Z][A-Za-z]{2,}")
_KATA_RE = re.compile(r"[ァ-ヴ]{4,}ー?")


def _prose_sections(md_text: str) -> list[str]:
    """## / ### 見出しで本文を分割する。表（| 始まり）行・イベントカレンダー（★ 含む）行は
    数値の併記であって重複説明ではないため、判定対象から除外する。"""
    sections: list[list[str]] = []
    buf: list[str] = []
    for ln in md_text.split("\n"):
        if _SECTION_RE.match(ln):
            if buf:
                sections.append("\n".join(buf))
                buf = []
        elif ln.lstrip().startswith("|") or "★" in ln:
            continue
        else:
            buf.append(ln)
    if buf:
        sections.append("\n".join(buf))
    return sections


def _materials(text: str) -> set[str]:
    """固有名詞らしい材料（英字大文字始まり 3 字以上・カタカナ 4 字以上）を抽出する。"""
    ents = set(_LATIN_RE.findall(text)) | set(_KATA_RE.findall(text))
    return {e for e in ents if e not in _STOP}


def find_duplication(md_text: str, max_section_spread: int = 4) -> list[str]:
    """同一材料が説明されるセクション数が max_section_spread 以上のものを違反文字列で返す。
    空リストなら重複なし（送信可）。"""
    spread: Counter[str] = Counter()
    for body in _prose_sections(md_text):
        for e in _materials(body):
            spread[e] += 1
    return [
        f"「{e}」が {n} セクションで重複説明"
        for e, n in spread.most_common()
        if n >= max_section_spread
    ]
