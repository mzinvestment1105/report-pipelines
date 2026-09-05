"""Markdown 表の折り返し違反をテキスト段階で機械検査する（PM 2026-09-05 承認）。

背景: 「セルが折り返して読みにくい表」が個別銘柄レポートで再発した。原因は
(1) ルール文言が「原則 3 列以内を目安」「4 列以上かつコメント列が長い」という
緩い AND 条件で、3 列でも 1 セル 50 字の表を素通ししていた
(2) gate_stock_report.py が 4 列以上を warning で報告するだけで送信を止めなかった
(3) 折り返しは PDF にして初めて分かるため執筆段階で判定できなかった
の 3 点。本モジュールは (1)(2) をテキスト段階で塞ぐ。

判定基準（PM 承認・agents/stock_analyst.md §誌面の書き方 C と同一）:
  - 列は 3 列以内
  - 本文セルは 25 字以内（全角も 1 字）
  - 4 列の表は本文セルを 15 字以内
  - 本文セルが全て数値・記号のみの表（業績推移等）は列数・字数の制限を受けない

使い方:
    from table_rules import check_tables
    for v in check_tables(md_text):
        print(v["line"], v["message"])
"""
from __future__ import annotations

import re

# 数値セルとみなすパターン。数字・符号・小数点・カンマ・％・円/株/倍/日/年月・
# 範囲記号（〜・-）・空値記号（―・ー・-・—）だけで構成されるセル。
# 「2,086億円」「+213.4%」「1,331〜1,520円」「2026年12月期」「―」「4,742千株」等を通す。
_NUMERIC_CELL = re.compile(
    r"^[\s0-9,.\-+±%％〜～~/（）()"
    r"円株倍日年月期件回名口万億兆千百pt人時分秒中間予想末初"
    r"―ー—–\u2212]*$"
)

# 表の区切り行 `|---|---|`
_SEPARATOR = re.compile(r"^\|[\s:\-|]+\|$")

# テキスト列とみなすヘッダ語。この列を持つ表は「本文セルが数値のみ」の除外に
# 該当させない（本文が空欄・記号でも、埋めれば長文になる列であるため）。
_TEXT_COL = re.compile(
    r"割当先|備考|関係|理由|内容|条件|コメント|概要|説明|状態|状況|区分|目的"
    r"|評価|判定|所感|読み|材料|事象|イベント|注記|条項|ロックアップ|株主名"
)

# 字数カウントから除外するマークダウン装飾・HTML
_DECOR = re.compile(r"\*\*|__|`|<br\s*/?>|</?[a-zA-Z][^>]*>")

REMEDY = "長文セルは表の下の文章へ移す／列を減らす"


def _split_row(line: str) -> list[str]:
    """`| a | b |` → ['a', 'b']。前後のパイプを外してから分割する。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _visible_len(cell: str) -> int:
    """装飾を除いた見た目の文字数（全角も 1 字として数える）。"""
    return len(_DECOR.sub("", cell).strip())


def _is_numeric_cell(cell: str) -> bool:
    body = _DECOR.sub("", cell).strip()
    if not body:
        return True
    return bool(_NUMERIC_CELL.match(body))


def _extract_tables(md_text: str) -> list[dict]:
    """markdown の表を抽出する。

    返り値の各要素: {"header_line": int, "header": list[str], "rows": list[list[str]]}
    header_line は 1 始まりの行番号（ヘッダ行）。
    """
    lines = md_text.replace("\r\n", "\n").split("\n")
    tables: list[dict] = []
    i = 0
    n = len(lines)
    while i < n - 1:
        cur = lines[i].strip()
        nxt = lines[i + 1].strip()
        if cur.startswith("|") and cur.count("|") >= 2 and _SEPARATOR.match(nxt):
            header = _split_row(cur)
            rows: list[list[str]] = []
            j = i + 2
            while j < n:
                s = lines[j].strip()
                if not s.startswith("|"):
                    break
                if _SEPARATOR.match(s):
                    j += 1
                    continue
                rows.append(_split_row(s))
                j += 1
            tables.append({"header_line": i + 1, "header": header, "rows": rows})
            i = j
            continue
        i += 1
    return tables


def check_tables(md_text: str) -> list[dict]:
    """markdown 本文の表を検査し、違反のリストを返す。

    各違反 dict のキー:
      line     … ヘッダ行の行番号（1 始まり）
      ncols    … 列数
      kind     … "columns" / "cell_length"
      longest  … 最長の本文セル（先頭 40 字）
      length   … その文字数
      limit    … 適用した字数上限（kind == "cell_length" のとき）
      message  … 人が読む 1 行メッセージ（対処文込み）
      remedy   … 対処文
    """
    violations: list[dict] = []

    for t in _extract_tables(md_text):
        header = t["header"]
        rows = t["rows"]
        ncols = len(header)
        line = t["header_line"]

        # 本文セル（ヘッダを除く全セル）を集める。
        body_cells = [c for r in rows for c in r]
        if not body_cells:
            continue

        # 除外: 本文セルが全て数値・記号のみの表（業績推移表・比較表など）。
        # ラベル列（1 列目）は文字列でも許すため、2 列目以降で判定する。
        # PM 2026-09-05 改定: 本文セルが空欄・記号ばかりでも、テキスト列
        # （「割当先」「備考」「関係」等）を持つ表は数値表ではないため除外しない。
        # 旧実装は本文セルだけを見ていたため、埋まっていないテキスト列を含む表が
        # 「数値のみ」と誤判定されて列数・字数の検査を素通りしていた。
        non_label = [c for r in rows for c in r[1:]] if ncols >= 2 else []
        text_cols = [h for h in (header[1:] if ncols >= 2 else []) if _TEXT_COL.search(h)]
        if non_label and all(_is_numeric_cell(c) for c in non_label) and not text_cols:
            continue

        # (a) 列数 5 以上
        if ncols >= 5:
            longest = max(body_cells, key=_visible_len)
            violations.append(
                {
                    "line": line,
                    "ncols": ncols,
                    "kind": "columns",
                    "longest": _DECOR.sub("", longest).strip()[:40],
                    "length": _visible_len(longest),
                    "limit": None,
                    "remedy": REMEDY,
                    "message": (
                        f"L{line}: {ncols}列の表（5列以上は禁止）。"
                        f"最長セル {_visible_len(longest)}字「"
                        f"{_DECOR.sub('', longest).strip()[:40]}」 → 対処: {REMEDY}"
                    ),
                }
            )
            continue

        # (b)(c) 字数上限。4 列は 15 字、3 列以下は 25 字。
        limit = 15 if ncols == 4 else 25
        over = [c for c in body_cells if _visible_len(c) > limit]
        if over:
            longest = max(over, key=_visible_len)
            violations.append(
                {
                    "line": line,
                    "ncols": ncols,
                    "kind": "cell_length",
                    "longest": _DECOR.sub("", longest).strip()[:40],
                    "length": _visible_len(longest),
                    "limit": limit,
                    "remedy": REMEDY,
                    "message": (
                        f"L{line}: {ncols}列の表に{limit}字超の本文セルが{len(over)}件"
                        f"（上限{limit}字）。最長 {_visible_len(longest)}字「"
                        f"{_DECOR.sub('', longest).strip()[:40]}」 → 対処: {REMEDY}"
                    ),
                }
            )

    return violations
