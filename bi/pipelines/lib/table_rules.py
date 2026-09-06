"""Markdown 表の折り返し違反をテキスト段階で機械検査する（PM 2026-09-05 承認）。

背景: 「セルが折り返して読みにくい表」が個別銘柄レポートで再発した。原因は
(1) ルール文言が「原則 3 列以内を目安」「4 列以上かつコメント列が長い」という
緩い AND 条件で、3 列でも 1 セル 50 字の表を素通ししていた
(2) gate_stock_report.py が 4 列以上を warning で報告するだけで送信を止めなかった
(3) 折り返しは PDF にして初めて分かるため執筆段階で判定できなかった
の 3 点。本モジュールは (1)(2) をテキスト段階で塞ぐ。

判定基準（PM 承認・agents/stock_analyst.md §誌面の書き方 C と同一）:
  - 列は 3 列以内（5 列以上は違反）
  - 本文セルは 13 字以内（全角も 1 字）
  - 4 列の表は本文セルを 9 字以内
  - 列幅を明示指定した表（§7 大株主表・§8 需給分析表）は列位置ごとの実容量を上限とする
  - 本文セルが全て数値・記号のみの表（業績推移等）は列数・字数の制限を受けない

字数上限の由来（2026-09-07 改定）:
  旧値（3 列以下 25 字 / 4 列 15 字）はレンダラの誌面の物理容量を超えており、規律を完全に
  守って書いた表でも必ず折り返していた（2026-09-06 の個別銘柄レポート 23 本中 15 本で
  カード自動変換が発動した）。本モジュールの上限は md_to_pdf.py の誌面幅・フォント・
  padding から逆算した実容量に一致させる（BODY_WIDTH_PX / CELL_PADDING_PX / FONT_PX）。

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

# ---------------------------------------------------------------------------
# 誌面の物理容量から導く字数上限（2026-09-07 新設）
#
# md_to_pdf.py の実装値と 1 対 1 で対応させる。片方だけを変えることを禁止する。
#   BODY_WIDTH_PX  … page = browser.new_page(viewport={"width": 612, ...})
#                     A4 210mm − 左右マージン 24mm×2 ≒ 162mm ≒ 612px（96dpi）
#   CELL_PADDING_PX… tbody td { padding:7px 10px } の左右合計
#   FONT_PX        … table { font-size:10.5pt } = 14px。5 列以上は table.cols-N の
#                     フォント縮小が効くため、その実寸を使う。
# ---------------------------------------------------------------------------
BODY_WIDTH_PX = 612
CELL_PADDING_PX = 20
FONT_PX = {5: 12.67, 6: 12.0, 7: 11.33}
DEFAULT_FONT_PX = 14.0

# 1 文字の実描画幅を「全角 1 字ぶん」を 1.0 とした比で表す（Playwright 実測・2026-09-07）。
# 実測値（font-size:10.5pt・レンダラと同一の font-face）:
#   全角のかな漢字・丸数字・全角記号 = 14.64px / 半角数字 = 9.34px
#   半角英字 = 平均 8.72px / 半角記号 = 平均 8.39px / 半角空白 = 約 3.9px
# 単純な文字数では、数値の多いセル（「729.2万株→781.8万株（+7.2%）」= 22 字だが実幅は
# 全角 16.4 字ぶん）を過大に、和文セルを過小に評価する。実幅で数えることで、
# レンダラで実際に折り返すセルだけを違反にできる。
_W_FULL = 1.0
_W_DIGIT = 9.34 / 14.64
_W_ALPHA = 8.72 / 14.64
_W_ASCII_SYM = 8.39 / 14.64
_W_SPACE = 3.9 / 14.64

# 列幅を明示指定した表: (先頭ヘッダ名, 列数) -> 各列の幅（%）。
# md_to_pdf.py の table.shareholders / table.demand の width 指定と一致させる。
COLUMN_WIDTHS = {
    # §7 大株主表（3 列版）: 株主名 / 保有比率 / 会社との関係
    ("株主名", 3): [34, 16, 50],
    # §7 大株主表（4 列版）: 株主名 / 保有比率 / 前期末比 / 会社との関係
    ("株主名", 4): [26, 15, 15, 44],
    # §8 需給分析の統合テーブル: 軸 / 指標 / 現状 / 評価基準・判定
    ("軸", 4): [20, 27, 24, 29],
}


def _font_px(ncols: int) -> float:
    return FONT_PX.get(ncols, DEFAULT_FONT_PX)


def cell_limits(ncols: int, header: list[str] | None = None) -> list[int]:
    """列位置ごとの本文セル字数上限を返す（列幅指定のない表は全列同じ値）。

    列幅を明示指定した表は列ごとに容量が違うため、単一の上限では
    「大株主表の関係列（17 字）に合わせると保有比率列の 11 字超を見逃す」
    「需給表の現状列（9 字）に 11 字を書いても素通りする」の取りこぼしが出る。
    """
    ncols = max(int(ncols or 1), 1)
    # _visible_len は「全角 1 字 = 1」で数えるため、その 1 字の実描画幅で割る。
    # 全角 1 字は font-size の 1.046 倍（実測 10.5pt = 14px 指定に対し 14.64px）。
    px = _font_px(ncols) * 1.046
    # 余裕（slack）は取らない。1 字の余裕を入れると、境界上のセル
    # （全角 13 字の株主名等）が実際には折り返すのに検査を通る（23 本の実測で
    # 見逃し 35 件）。見逃し 0 件を優先する。
    slack = 0
    key = ((header[0].strip() if header else ""), ncols)
    widths = COLUMN_WIDTHS.get(key)
    if widths and len(widths) == ncols:
        return [
            max(1, int((BODY_WIDTH_PX * w / 100 - CELL_PADDING_PX) // px) + slack)
            for w in widths
        ]
    lim = max(1, int((BODY_WIDTH_PX / ncols - CELL_PADDING_PX) // px) + slack)
    return [lim] * ncols



def _split_row(line: str) -> list[str]:
    """`| a | b |` → ['a', 'b']。前後のパイプを外してから分割する。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _char_width(ch: str) -> float:
    """1 文字の実描画幅を「全角 1 字 = 1.0」の比で返す（Playwright 実測に基づく）。"""
    o = ord(ch)
    if ch == " ":
        return _W_SPACE
    if o < 128:
        if ch.isdigit():
            return _W_DIGIT
        if ch.isalpha():
            return _W_ALPHA
        return _W_ASCII_SYM
    return _W_FULL


def _visible_len(cell: str) -> int:
    """装飾を除いた見た目の幅を「全角 1 字 = 1」で数え、切り上げた整数で返す。

    全角も半角も 1 字と数えていた旧実装は、数値・半角記号の多いセル
    （「729.2万株→781.8万株（+7.2%）」= 22 字・実幅は全角 16.4 字ぶん）を
    実際には折り返さないのに違反と判定していた。レンダラの実測幅で数える。
    """
    body = _DECOR.sub("", cell).strip()
    if not body:
        return 0
    import math

    return math.ceil(sum(_char_width(c) for c in body) - 1e-9)


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

        # (b)(c) 字数上限。レンダラの誌面幅からの逆算値（2026-09-07 改定）。
        # 本文幅 612px ÷ 列数 − 左右 padding 20px を全角 1 字 14px（10.5pt）で割る。
        # 旧値（4 列 15 字 / 3 列以下 25 字）は誌面の実容量（4 列 9 字 / 3 列 13 字）を
        # 超えており、規律を守った表でも折り返していた。
        # 列幅を明示指定した表（§7 大株主表・§8 需給分析表）は列位置ごとに上限が違う。
        limits = cell_limits(ncols, header)
        over: list[tuple[str, int]] = []  # (セル, その列の上限)
        for r in rows:
            for i, c in enumerate(r):
                lim_i = limits[i] if i < len(limits) else limits[-1]
                if _visible_len(c) > lim_i:
                    over.append((c, lim_i))
        if over:
            # 「上限をどれだけ超えたか」が最も大きいセルを代表として報告する。
            longest, limit = max(over, key=lambda x: _visible_len(x[0]) - x[1])
            widthed = (header[0].strip() if header else "", ncols) in COLUMN_WIDTHS
            note = "・列幅指定表のため列位置ごとの上限" if widthed else ""
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
                        f"L{line}: {ncols}列の表に字数上限超の本文セルが{len(over)}件"
                        f"（この列の上限{limit}字{note}）。最長 {_visible_len(longest)}字「"
                        f"{_DECOR.sub('', longest).strip()[:40]}」 → 対処: {REMEDY}"
                    ),
                }
            )

    return violations
# ---------------------------------------------------------------------------
# 全レポート種別横断の表ゲート（PM 2026-09-06 指示）
#
# 背景: check_tables() は 2026-09-05 に新設したが、呼び出し元が
# gate_stock_report.py（個別銘柄レポート専用）だけだったため、マクロ・セクター・
# 動意・テーマ・週次大型株の各レポートは列数・セル長の検査を一切受けずに送信
# されていた。実際に週次大型株 2026-09-05 の md は 8 列・最長 32 字の横断比較表を
# 3 つ含み、PDF で銘柄名が 1 文字ずつ縦に折り返した。
#
# 送信を止めるかどうかは種別で分ける（_cr §36 配信絶対の原則）:
#   - 個別銘柄レポート（PM が都度依頼して受け取る）→ error として送信中止
#   - GHA が定時発行するレポート（マクロ・セクター・動意・テーマ・大型株）
#     → 送信は止めず、違反を GHA ログへ error 相当の強い警告として残す
#     （カード自動変換は 2026-09-07 に廃止したため誌面上の受け皿は無い）
# ---------------------------------------------------------------------------

# 送信を止めてよい種別（PM が都度受け取るレポート）。
BLOCKING_KINDS = frozenset({"stock"})


def gate_report_tables(md_text: str, kind: str) -> tuple[list[str], list[str]]:
    """レポート種別を問わず表を検査し (errors, warnings) を返す。

    kind が BLOCKING_KINDS に含まれる場合のみ違反を errors へ入れる。
    それ以外の種別は warnings へ入れて送信を継続させる（_cr §36）。
    呼び出し元は errors が非空なら PDF を生成せず中止する。
    """
    violations = check_tables(md_text)
    if not violations:
        return [], []
    msgs = [f"表の折り返し: {v['message']}" for v in violations]
    if kind in BLOCKING_KINDS:
        return msgs, []
    head = (
        f"表の折り返し違反が {len(violations)} 件あります"
        f"（種別 {kind} は配信絶対の原則により送信は継続します。"
        "カード自動変換は 2026-09-07 に廃止しており誌面上の救済はありません。"
        "次回の生成で本文を直してください）"
    )
    return [], [head] + msgs
