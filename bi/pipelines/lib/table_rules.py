"""Markdown 表の折り返し違反をテキスト段階で機械検査する（PM 2026-09-05 承認）。

背景: 「セルが折り返して読みにくい表」が個別銘柄レポートで再発した。原因は
(1) ルール文言が「原則 3 列以内を目安」「4 列以上かつコメント列が長い」という
緩い AND 条件で、3 列でも 1 セル 50 字の表を素通ししていた
(2) gate_stock_report.py が 4 列以上を warning で報告するだけで送信を止めなかった
(3) 折り返しは PDF にして初めて分かるため執筆段階で判定できなかった
の 3 点。本モジュールは (1)(2) をテキスト段階で塞ぐ。

判定基準（PM 承認・agents/stock_analyst.md §誌面の書き方 C と同一）:
  - 列は 3 列以内（5 列以上は違反）
  - 1 列目（項目名列）は 20 字以内（2026-09-10 改定・下記）
  - 数値セルは字数の制限を受けない（2026-09-10 改定・下記）
  - 本文セル（2 列目以降の和文セル）は 13 字以内（全角も 1 字）
  - 4 列の表は本文セルを 9 字以内
  - 列幅を明示指定した表（§7 大株主表・§8 需給分析表）は列位置ごとの実容量を上限とする
  - 本文セルが全て数値・記号のみの表（業績推移等）は列数・字数の制限を受けない

字数上限の由来（2026-09-07 改定）:
  旧値（3 列以下 25 字 / 4 列 15 字）はレンダラの誌面の物理容量を超えており、規律を完全に
  守って書いた表でも必ず折り返していた（2026-09-06 の個別銘柄レポート 23 本中 15 本で
  カード自動変換が発動した）。本モジュールの上限は md_to_pdf.py の誌面幅・フォント・
  padding から逆算した実容量に一致させる（BODY_WIDTH_PX / CELL_PADDING_PX / FONT_PX）。

1 列目・数値セルの改定（2026-09-10 PM 指示）:
  md_to_pdf.py の表の既定を table-layout:fixed（等幅割り）から auto（内容に合わせた配分）へ
  変え、1 列目と数値セルへ white-space:nowrap を当てた。等幅割りの前提で導いた
  「本文幅 ÷ 列数」の字数上限は、この新レイアウトともう対応しない。
    - 1 列目は等幅の枠に縛られず必要なだけ広がるため、旧上限（4 列で 9 字）は無意味である。
      「営業キャッシュフロー」（10 字）が旧上限では違反、実際の誌面では 1 行に収まる。
      代わりに **20 字** を上限とする（1 列目だけで本文幅の大半を占める表を防ぐ歯止め。
      20 字を超える項目名は表の外の文章へ移す）。
    - 数値セルは nowrap かつ内容ぶんの幅が確保されるため折り返さない。字数上限の対象から
      外す（旧実装は `2,000〜2,500` のような数値レンジを違反として報告していた）。
    - 本文セル（2 列目以降の和文セル）の上限は従来どおり据え置く。数値列が実幅まで縮む
      ぶん本文列には余裕が生まれるため、据え置きは安全側の判定になる。
  レイアウトが本文幅を超える表は md_to_pdf.py 側の安全弁が table-layout:fixed へ戻し、
  レイアウト監査 JS が overflow として記録する（フォント縮小はしない）。

列幅の自動配分（2026-10-06 PM 承認・_cr §39）:
  個別銘柄以外のレポートでは、等幅で収まらない 4 列以上の表と、等幅だと 3 行以上に折れる
  セルを持つ表に、表の内容から決めた列幅をレンダラ（md_to_pdf._apply_colfit）が当てる。
  配分はどちらも colfit_plan()（下の「列幅の自動配分」節）が決める。check_tables(md, kind=...)
  はその配分で全セルが収まる表を合格とする（列は 7 列以内・2 行まで折り返してよい長い和文の
  列は 2 列まで・ほかの列は 1 行・見出しは 2 行まで）。kind を渡さない呼び出し（個別銘柄の
  ゲート）と stock / us_stock は従来の基準のまま。現行の基準に合格する表は常に合格のまま。

使い方:
    from table_rules import check_tables
    for v in check_tables(md_text):
        print(v["line"], v["message"])

    # 銘柄コードだけで社名の無い表の検査（PM 2026-10-05）を含む送信前の表ゲートを md 1 本へ当てる
    python bi/pipelines/lib/table_rules.py research/themes/xxx.md --kind themes
"""
from __future__ import annotations

import re

# 数値セルとみなすパターン。数字・符号・小数点・カンマ・％・円/株/倍/日/年月・
# 範囲記号（〜・-）・空値記号（―・ー・-・—）だけで構成されるセル。
# 「2,086億円」「＋213.4%」「1,331〜1,520円」「2026年12月期」「―」「4,742千株」等を通す。
# PM 2026-09-09: 誌面の増減符号を全角 `＋` とマイナスの `▼` に統一したため、
# `＋`・`▼▽`・和文会計の `▲△` を含むセルも数値セルとして扱う。これらを外すと
# 「572万→543万株（▼5.1%）」のような増減セルが本文セル扱いになり、数値列に
# 本文列の狭い字数上限が当たって折り返し検査が誤検知する。
_NUMERIC_CELL = re.compile(
    r"^[\s0-9,.\-+±%％〜～~/（）()＋▲△▼▽"
    r"円株倍日年月期件回名口万億兆千百pt人時分秒中間予想末初→"
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

# 1 列目（項目名列）の字数上限（PM 2026-09-10 指示）。
# レンダラが 1 列目を white-space:nowrap にし table-layout:auto で必要な幅を配分するため、
# 「本文幅 ÷ 列数」の等幅前提の上限は当たらない。1 列目だけで誌面幅の大半を占める表を
# 防ぐ歯止めとしてのみ効かせる。列幅を % で明示指定した表（COLUMN_WIDTHS）は
# レンダラ側も fixed のままなので従来どおり列位置ごとの実容量を使う。
FIRST_COL_LIMIT = 20

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


# 誌面骨格が列名・列順を固定した表のヘッダ署名（5 列以上のものだけを持つ）。
# 正本 agents/stock_analyst.md の「誌面骨格」節が唯一の定義元であり、
# report_skeleton.load() がそれをパースして供給する（ここへ値を写さない）。
_SKELETON_FIXED_HEADERS: set[str] | None = None


def _skeleton_fixed_headers() -> set[str]:
    global _SKELETON_FIXED_HEADERS
    if _SKELETON_FIXED_HEADERS is None:
        try:
            import report_skeleton  # noqa: PLC0415

            sk = report_skeleton.load()
            _SKELETON_FIXED_HEADERS = set(sk.table_headers) if sk.loaded else set()
        except Exception:  # noqa: BLE001
            _SKELETON_FIXED_HEADERS = set()
    return _SKELETON_FIXED_HEADERS


def _is_skeleton_fixed_table(header: list[str]) -> bool:
    """骨格が列名・列順を固定した表か（§5 同業比較は列の「型」で判定する）。"""
    sig = " / ".join(re.sub(r"\s+", "", c.strip()) for c in header)
    if sig in _skeleton_fixed_headers():
        return True
    # §5 同業比較は社名が列名に入るため型だけで判定する。
    if (
        len(header) >= 3
        and re.sub(r"\s+", "", header[0].strip()) == "指標"
        and header[-1].strip().endswith("平均")
    ):
        return True
    return False


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
        # 列幅を % で明示指定した表はレンダラ側も table-layout:fixed のままであり、
        # 1 列目も指定幅の中で折り返す。従来どおり列位置ごとの実容量を上限にする。
        return [
            max(1, int((BODY_WIDTH_PX * w / 100 - CELL_PADDING_PX) // px) + slack)
            for w in widths
        ]
    lim = max(1, int((BODY_WIDTH_PX / ncols - CELL_PADDING_PX) // px) + slack)
    # 1 列目はレンダラが nowrap ＋ 内容ぶんの幅を確保するため、等幅前提の上限を当てない
    # （PM 2026-09-10）。20 字の歯止めだけを掛ける。
    return [FIRST_COL_LIMIT] + [lim] * (ncols - 1)



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


def _table_violation(t: dict) -> dict | None:
    """表 1 つを現行の基準（等幅の字数上限・5 列以上の禁止）で検査し、違反なら dict を返す。

    check_tables() の判定本体を表 1 つ単位へ切り出したもの（2026-10-06。判定は変えていない）。
    列幅の自動配分（colfit_plan）も「現行の検査に合格するか」をこの関数で判定する。
    t は _extract_tables() の 1 要素（header / rows / header_line）。
    """
    header = t["header"]
    rows = t["rows"]
    ncols = len(header)
    line = t["header_line"]

    # 本文セル（ヘッダを除く全セル）を集める。
    body_cells = [c for r in rows for c in r]
    if not body_cells:
        return None

    # 除外: 本文セルが全て数値・記号のみの表（業績推移表・比較表など）。
    # ラベル列（1 列目）は文字列でも許すため、2 列目以降で判定する。
    # PM 2026-09-05 改定: 本文セルが空欄・記号ばかりでも、テキスト列
    # （「割当先」「備考」「関係」等）を持つ表は数値表ではないため除外しない。
    # 旧実装は本文セルだけを見ていたため、埋まっていないテキスト列を含む表が
    # 「数値のみ」と誤判定されて列数・字数の検査を素通りしていた。
    non_label = [c for r in rows for c in r[1:]] if ncols >= 2 else []
    text_cols = [h for h in (header[1:] if ncols >= 2 else []) if _TEXT_COL.search(h)]
    if non_label and all(_is_numeric_cell(c) for c in non_label) and not text_cols:
        return None

    # (a) 列数 5 以上
    # 例外: 誌面骨格（agents/stock_analyst.md「誌面骨格」節）が列名・列順を固定した表は、
    # PM 2026-09-07 承認の骨格が列数まで含めて確定させているため列数検査から外す
    # （レンダラは cols-5/6/7 のクラスで font-size を自動縮小して折り返しを防ぐ）。
    # 字数の検査（(b)(c)）は骨格固定表にもそのまま適用する。
    if ncols >= 5 and not _is_skeleton_fixed_table(header):
        longest = max(body_cells, key=_visible_len)
        return (
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

    # (b)(c) 字数上限。レンダラの誌面幅からの逆算値（2026-09-07 改定）。
    # 本文幅 612px ÷ 列数 − 左右 padding 20px を全角 1 字 14px（10.5pt）で割る。
    # 旧値（4 列 15 字 / 3 列以下 25 字）は誌面の実容量（4 列 9 字 / 3 列 13 字）を
    # 超えており、規律を守った表でも折り返していた。
    # 列幅を明示指定した表（§7 大株主表・§8 需給分析表）は列位置ごとに上限が違う。
    limits = cell_limits(ncols, header)
    widthed_table = (header[0].strip() if header else "", ncols) in COLUMN_WIDTHS
    over: list[tuple[str, int]] = []  # (セル, その列の上限)
    for r in rows:
        for i, c in enumerate(r):
            # 数値セルは字数の制限を受けない（PM 2026-09-10）。レンダラが
            # class="num" を付けて white-space:nowrap にし、table-layout:auto が
            # 内容ぶんの幅を配分するため折り返さない。旧実装は `2,000〜2,500`
            # のような数値レンジを違反として報告していた。
            # 列幅を % で明示指定した表は fixed のままで数値も折り返しうるため除外しない。
            if not widthed_table and i > 0 and _is_numeric_cell(c):
                continue
            lim_i = limits[i] if i < len(limits) else limits[-1]
            if _visible_len(c) > lim_i:
                over.append((c, lim_i))
    if over:
        # 「上限をどれだけ超えたか」が最も大きいセルを代表として報告する。
        longest, limit = max(over, key=lambda x: _visible_len(x[0]) - x[1])
        widthed = (header[0].strip() if header else "", ncols) in COLUMN_WIDTHS
        note = "・列幅指定表のため列位置ごとの上限" if widthed else ""
        return (
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
    return None


def check_tables(md_text: str, kind: str | None = None) -> list[dict]:
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

    kind（2026-10-06 追加）: 送信種別名（send_report_pdf_discord.py の --kind）。
      None（既定）… 従来どおり等幅の基準だけで判定する（個別銘柄ゲートが使う）。
      それ以外 … 現行の基準に不合格でも、列幅の自動配分（colfit_plan）で全セルが収まる表は
        合格とする（レンダラも同じ配分で組むため）。stock / us_stock は自動配分の対象外。
      現行の基準に合格する表は kind に関係なく合格のまま（合格→違反の変化は起きない）。
    """
    violations: list[dict] = []
    for t in _extract_tables(md_text):
        v = _table_violation(t)
        if v is None:
            continue
        if kind is not None:
            plan = colfit_plan(t["header"], t["rows"], kind)
            if plan["switch"]:
                continue  # 自動配分で全セルが収まる（PDF もこの配分で組まれる）
            if plan["problem"] and kind not in STOCK_LAYOUT_KINDS:
                v["colfit_problem"] = plan["problem"]
                v["message"] = (
                    v["message"].replace("（5列以上は禁止）", "（列幅を自動配分しても収まらない）")
                    + f"（自動配分できない理由: {plan['problem']}）"
                )
        violations.append(v)
    return violations


# ---------------------------------------------------------------------------
# 列幅の自動配分（PM 2026-10-06 承認・_cr §39）
#
# 背景: 個別銘柄以外のレポートのレンダラ（md_to_pdf.py の v5 組版）は table-layout:fixed で
# 列を等幅に割るため、4 列以上の表では第 1 列の社名（「4417 グローバルセキュリティエキスパート」）
# が 3〜4 行に折れ、数値の列は幅が余っていた。そのため _cr §39 は「和文セルを含む表は 3 列以内」
# と定め、銘柄比較の表が 3 列ずつに細切れになっていた（PM 指摘 2026-10-05）。
#
# 本節は表の内容（各セルの表示幅）から列幅の配分（%）を決める唯一の関数 colfit_plan() を持つ。
# PDF 生成（md_to_pdf._apply_colfit）と表ゲート（check_tables(kind=...)）の両方がこの関数を
# 呼ぶため、生成と検査の判定はずれない。
#
# 切り替える表（後方互換を最優先・PM 承認）:
#   等幅で現行の検査に合格する表は出力を 1 バイトも変えない。切り替えるのは
#   (a) 4 列以上で、現行の検査（_table_violation）に不合格の表
#   (b) 等幅だと 3 行以上に折れる本文セルがある表
#   のうち、下の配分で全セルが収まる表だけである。個別銘柄レポート（kind stock / us_stock。
#   v6 組版で列幅は既に内容依存）と、列幅を個別に固定した表（テーマ系の主導銘柄表・大株主表・
#   需給分析表）は対象外。
#
# 配分: 数値・短い語の列は最長の値が 1 行に収まる最小幅（white-space:nowrap）を確保し、
#   残りを長い和文の列（2 行まで折り返してよい列・最大 2 列）へ内容量に比例して配る。
#   社名の列（「コード 社名」の列。無ければ第 1 列）には下限と上限を設ける。列見出しは 2 行まで
#   折り返してよい。表幅は常に本文幅ちょうど（100%）。
#
# 等幅（table-layout:fixed）にした経緯（git b38817a7・8ed5271d）: 2026-08-30、auto レイアウトで
#   和文の長いセルが列幅を押し広げ表の実幅が本文幅 612px を超え、Chromium がページ全体を縮小して
#   本文 12pt が実測 8pt まで潰れた。2026-09-10 に v6 の auto ＋ nowrap を全種別へ当てた際は
#   7〜8 列の表でセルが隣列へ重なった。本方式は fixed を保ったまま列ごとの % を与えるため表幅は
#   本文幅を超えず、nowrap は内容幅＋余裕を確保した列にだけ当てる（どちらの不具合も再発しない）。
# ---------------------------------------------------------------------------

# 自動配分の上限（_cr §39 の数字と一致させる。片方だけを変えることを禁止する）。
COLFIT_MAX_COLS = 7          # 列数の上限
COLFIT_MAX_WRAP_COLS = 2     # 2 行まで折り返してよい長い和文の列の数（第 1 列を含む）
COLFIT_WRAP_LINES = 2        # 長い和文の列のセルの行数の上限（ほかの列は 1 行）
COLFIT_HEADER_LINES = 2      # 列見出しの行数の上限
COLFIT_HEADER_MIN_CHARS = 4  # 見出しを折る時も 1 行に全角 4 字ぶんを残す（「順／位」の 1 字折れを防ぐ）
COLFIT_NAME_MIN_PCT = 20.0   # 社名の列の下限（1 行で収まる幅がこれ未満ならその幅）
COLFIT_NAME_MAX_PCT = 45.0   # 社名の列の上限
COLFIT_SLACK_PX = 4.0        # 1 列あたりの推定誤差の余裕

# 自動配分を当てない種別（v6 組版 = table-layout:auto で列幅が既に内容依存・凍結フォーマット）。
# send_report_pdf_discord.py の送信種別名と md_to_pdf.py の組版種別名の両方を持つ。
STOCK_LAYOUT_KINDS = frozenset({"stock", "us_stock"})

# md_to_pdf.py の v5 組版（個別銘柄以外）の実寸。片方だけを変えることを禁止する。
#   table { font-size:10.5pt }・cols-5 { 9.5pt }・cols-6 { 9pt }・cols-7 { 8.5pt }・cols-8plus { 8pt }
#   tbody td { padding:5px 8px }・cols-5 以上は { padding:5px 6px }
#   thead th { padding:6px 9px }・cols-5 以上は { padding:6px 7px }
#   body { letter-spacing:.04em }（本文 12pt=16px で計算され 0.64px が表へ継承される）
_V5_FONT_PT = {5: 9.5, 6: 9.0, 7: 8.5}
_V5_FONT_PT_DEFAULT = 10.5
_V5_FONT_PT_8PLUS = 8.0
_LETTER_SPACING_PX = 0.64
# 字面の幅（em）。上の Playwright 実測（_W_* の元の px 値）から字間 0.64px を除き 14px で割った値。
_EM_DIGIT = (9.34 - 0.64) / 14.0
_EM_ALPHA = (8.72 - 0.64) / 14.0
_EM_SYM = (8.39 - 0.64) / 14.0
_EM_SPACE = (3.9 - 0.64) / 14.0
_HEADER_BOLD = 1.04  # 列見出しは太字のため 4% 広く見積もる
# 符号はレンダラが全角の ＋／▼ へ正規化するため全角で見積もる（md 上の + - のままでも同じ幅になる）。
_SIGN_CHARS = frozenset("+-−–—")
# 行頭に来られない字（line-break:strict の禁則。直前の塊へ寄せる）と行末に来られない字（直後へ寄せる）。
_NO_START = frozenset(
    "）)」』】〕〉》］]｝}、。，．,.・：；:;！？!?ー〜～%％"
    "ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶ々ゝゞヽヾ"
)
_NO_END = frozenset("（(「『【〔〈《［[｛{＋+▼▲△▽−-￥¥$＄#＃")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def colfit_plain(cell: str) -> str:
    """md のセル文字列・HTML のセル内容のどちらからも同じ表示文字列を作る。"""
    import html as _html

    s = _MD_LINK.sub(r"\1", cell or "")
    s = _DECOR.sub("", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _v5_metrics(ncols: int) -> tuple[float, float, float]:
    """(表の文字 px, 本文セルの左右 padding 合計, 見出しセルの左右 padding 合計)。"""
    if ncols >= 8:
        pt = _V5_FONT_PT_8PLUS
    else:
        pt = _V5_FONT_PT.get(ncols, _V5_FONT_PT_DEFAULT)
    small = ncols >= 5
    return pt * 4.0 / 3.0, (12.0 if small else 16.0), (14.0 if small else 18.0)


def _glyph_px(ch: str, font_px: float) -> float:
    if ch in _SIGN_CHARS:
        em = 1.0
    elif ch == " ":
        em = _EM_SPACE
    elif ord(ch) < 128:
        em = _EM_DIGIT if ch.isdigit() else (_EM_ALPHA if ch.isalpha() else _EM_SYM)
    else:
        em = 1.0
    return em * font_px + _LETTER_SPACING_PX


def _is_word_char(ch: str) -> bool:
    """改行できない語を作る字（半角の英数記号・ギリシャ文字・全角数字）。"""
    o = ord(ch)
    return (o < 128 and ch != " ") or 0x0370 <= o <= 0x03FF or 0xFF10 <= o <= 0xFF19


def _atoms(text: str, font_px: float, scale: float = 1.0) -> list[list]:
    """改行してよい位置で区切った塊 [幅px, 空白か] の列（禁則は保守的に塊へ寄せる）。"""
    atoms: list[list] = []
    glue_next = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in (" ", "　"):
            atoms.append([_glyph_px(ch, font_px) * scale, True])
            glue_next = False
            i += 1
            continue
        if _is_word_char(ch):
            j = i
            while j < n and _is_word_char(text[j]):
                j += 1
        else:
            j = i + 1
        seg = text[i:j]
        w = sum(_glyph_px(c, font_px) for c in seg) * scale
        if atoms and not atoms[-1][1] and (glue_next or seg[0] in _NO_START):
            atoms[-1][0] += w
        else:
            atoms.append([w, False])
        glue_next = seg[-1] in _NO_END
        i = j
    return atoms


def _one_line_px(atoms: list[list]) -> float:
    """1 行に収めた時の幅（先頭・末尾の空白を除く）。"""
    idx = [k for k, a in enumerate(atoms) if not a[1]]
    if not idx:
        return 0.0
    return sum(a[0] for a in atoms[idx[0]: idx[-1] + 1])


def _count_lines(atoms: list[list], avail: float) -> int:
    """幅 avail に左から詰めた時の行数（ブラウザの行分割と同じ先頭からの詰め方）。"""
    import math

    if not any(not a[1] for a in atoms):
        return 0
    avail = max(avail, 1.0)
    lines, cur, pend, started = 1, 0.0, 0.0, False
    for w, sp in atoms:
        if sp:
            if started:
                pend += w
            continue
        if started and cur + pend + w <= avail + 1e-6:
            cur += pend + w
        else:
            if started:
                lines += 1
            if w > avail + 1e-6:  # 1 塊が行幅を超える（overflow-wrap で塊の途中で折れる）
                extra = math.ceil(w / avail - 1e-9) - 1
                lines += extra
                cur = w - extra * avail
            else:
                cur = w
            started = True
        pend = 0.0
    return lines


def _min_px(atoms: list[list], lines: int) -> float:
    """lines 行以内に収まる最小の内容幅（px）。"""
    one = _one_line_px(atoms)
    if lines <= 1 or one == 0.0:
        return one
    lo = max(a[0] for a in atoms if not a[1])
    hi = one
    if _count_lines(atoms, lo) <= lines:
        return lo
    for _ in range(40):
        mid = (lo + hi) / 2
        if _count_lines(atoms, mid) <= lines:
            hi = mid
        else:
            lo = mid
    return hi


def fixed_width_table(header: list[str]) -> str | None:
    """列幅を個別に固定した表なら、その表のクラス名を返す（自動配分の対象外）。

    md_to_pdf.py の _tag_theme_tables（テーマ系）と _TABLE_COLS_CLASS_JS（大株主表・需給分析表）の
    判定と同じ条件である。片方だけを変えることを禁止する。
    """
    hs = [colfit_plain(h) for h in header]
    n = len(hs)
    head = "".join(hs)
    if n == 5 and "コード" in head and "何の会社" in head and "時価総額" in head and "材料" not in head:
        return "theme-lead"
    if n == 6 and "コード" in head and "何の会社" in head and "材料" in head:
        return "theme-solo"
    if "動いた理由" in head and "主導銘柄" in head and ((n >= 6 and "局面" in head) or n == 3):
        return "theme-heat" if n >= 6 else "theme-today"
    if n and hs[0] == "株主名" and "会社との関係" in hs and n in (3, 4):
        return "shareholders" if n == 4 else "shareholders3"
    if n == 4 and hs[:3] == ["軸", "指標", "現状"]:
        return "demand"
    return None


def colfit_plan(header: list[str], rows: list[list[str]], kind: str | None) -> dict:
    """表 1 つの列幅の配分を決める（PDF 生成と表ゲートが共有する唯一の判定）。

    header / rows は md のセル文字列。kind は送信種別名（send_report_pdf_discord.py の --kind）
    または組版種別名（md_to_pdf の kind）。

    返り値のキー:
      switch    … True なら自動配分を当てる（PDF はこの配分で組み、表ゲートは合格とする）
      widths    … 列幅（%・合計 100）。switch のときだけ値を持つ
      wrap_cols … 2 行まで折り返してよい列の位置（0 始まり）。ほかの列は 1 行（nowrap）
      problem   … 自動配分を当てない理由（人が読む 1 文）
      trigger   … "4cols_fail"（4 列以上で現行の検査に不合格）/ "equal_3lines"（等幅で 3 行以上）/ None
      equal_max_lines … 等幅で組んだ時の本文セルの最大行数（推定）
    """
    ncols = len(header)
    plan: dict = {
        "ncols": ncols, "switch": False, "widths": None, "wrap_cols": [],
        "problem": None, "trigger": None, "equal_max_lines": None, "old_ok": None,
    }
    if not kind or kind in STOCK_LAYOUT_KINDS:
        plan["problem"] = "個別銘柄レポートは自動配分の対象外"
        return plan
    if ncols < 2:
        return plan
    fixed = fixed_width_table(header)
    if fixed:
        plan["problem"] = f"列幅を個別に固定した表（{fixed}）"
        return plan
    body = [[colfit_plain(r[j]) if j < len(r) else "" for j in range(ncols)] for r in rows]
    if not any(c for r in body for c in r):
        return plan
    old_ok = _table_violation({"header": header, "rows": rows, "header_line": 0}) is None
    plan["old_ok"] = old_ok
    font_px, td_pad, th_pad = _v5_metrics(ncols)
    W = float(BODY_WIDTH_PX)

    cell_atoms = [[_atoms(c, font_px) for c in r] for r in body]
    eq_avail = W / ncols - td_pad
    eq_lines = max((_count_lines(a, eq_avail) for r in cell_atoms for a in r), default=0)
    plan["equal_max_lines"] = eq_lines
    if eq_lines >= 3:
        plan["trigger"] = "equal_3lines"
    elif ncols >= 4 and not old_ok:
        plan["trigger"] = "4cols_fail"
    else:
        if not old_ok:
            plan["problem"] = "3 列以下の表は等幅で組むため従来の字数上限が当たる"
        return plan
    if ncols > COLFIT_MAX_COLS:
        plan["problem"] = f"{ncols} 列（上限 {COLFIT_MAX_COLS} 列）"
        return plan

    slack = COLFIT_SLACK_PX
    need1 = [max((_one_line_px(r[j]) for r in cell_atoms), default=0.0) + td_pad + slack
             for j in range(ncols)]
    need2 = [max((_min_px(r[j], COLFIT_WRAP_LINES) for r in cell_atoms), default=0.0) + td_pad + slack
             for j in range(ncols)]
    numeric = [all(_is_numeric_cell(r[j]) for r in body) for j in range(ncols)]
    hmin = []
    for j in range(ncols):
        ha = _atoms(colfit_plain(header[j]), font_px, _HEADER_BOLD)
        h1 = _one_line_px(ha) + th_pad + slack
        h2 = _min_px(ha, COLFIT_HEADER_LINES) + th_pad + slack
        hcap = COLFIT_HEADER_MIN_CHARS * (font_px + _LETTER_SPACING_PX) * _HEADER_BOLD + th_pad + slack
        hmin.append(min(h1, max(h2, hcap)))
    name_col = None
    for j in range(ncols):
        if sum(_code_with_name(r[j]) for r in body) >= 0.6 * len(body):
            name_col = j
            break
    if name_col is None and not numeric[0]:
        name_col = 0
    name_lo = name_hi = 0.0
    if name_col is not None:
        name_lo = min(need1[name_col], W * COLFIT_NAME_MIN_PCT / 100)
        name_hi = W * COLFIT_NAME_MAX_PCT / 100

    def _mins(wrap: tuple) -> list | None:
        m = []
        for j in range(ncols):
            v = max(need2[j] if j in wrap else need1[j], hmin[j])
            if j == name_col:
                v = max(v, name_lo)
                if v > name_hi + 1e-6:
                    return None
            m.append(v)
        return m

    from itertools import combinations

    cands = [j for j in range(ncols) if not numeric[j]]
    best = None
    for k in range(0, COLFIT_MAX_WRAP_COLS + 1):
        for combo in combinations(cands, k):
            m = _mins(combo)
            if m is None or sum(m) > W + 1e-6:
                continue
            if best is None or sum(m) < sum(best[1]) - 1e-9:
                best = (combo, m)
        if best is not None:
            break
    if best is None:
        n_long = sum(1 for j in cands if need1[j] > W / ncols)
        if n_long > COLFIT_MAX_WRAP_COLS:
            plan["problem"] = (
                f"1 行に収まらない文字の列が {n_long} 列ある（{COLFIT_WRAP_LINES} 行まで折り返せるのは"
                f" {COLFIT_MAX_WRAP_COLS} 列まで）"
            )
        else:
            plan["problem"] = (
                f"長い列（最大 {COLFIT_MAX_WRAP_COLS} 列）を {COLFIT_WRAP_LINES} 行・ほかの列を 1 行に"
                "収めると本文幅を超える"
            )
        return plan

    wrap, widths = best[0], list(best[1])
    caps = [W] * ncols
    if name_col is not None:
        caps[name_col] = name_hi
    left = W - sum(widths)
    # 余りはまず折り返す列を 1 行に収まる幅まで（内容量に比例して）広げ、残りを全列へ幅に比例して配る。
    grow = {j: min(need1[j], caps[j]) - widths[j] for j in wrap}
    grow = {j: g for j, g in grow.items() if g > 0}
    tot = sum(grow.values())
    if tot > 0:
        take = min(left, tot)
        for j, g in grow.items():
            widths[j] += take * g / tot
        left -= take
    for _ in range(ncols + 1):
        if left <= 1e-6:
            break
        room = [j for j in range(ncols) if widths[j] < caps[j] - 1e-6]
        if not room:
            break
        base = sum(widths[j] for j in room)
        given = 0.0
        for j in room:
            add = min(left * widths[j] / base, caps[j] - widths[j])
            widths[j] += add
            given += add
        left -= given
    pct = [round(w / W * 100, 1) for w in widths]
    diff = round(100.0 - sum(pct), 1)
    if diff:
        j = max(range(ncols), key=lambda k: (k in wrap, pct[k]))
        pct[j] = round(pct[j] + diff, 1)
    plan.update(switch=True, widths=pct, wrap_cols=list(wrap))
    return plan


def extract_tables(md_text: str) -> list[dict]:
    """md の表を抽出する（公開名。md_to_pdf.py が自動配分の判定に使う）。"""
    return _extract_tables(md_text)


# ---------------------------------------------------------------------------
# 銘柄コードだけで社名の無い表の検査（PM 2026-10-05 指示・_cr §7）
#
# 背景: 2026-10-05 のテーマ調査レポートで、指示書が 10 列の表を求め、執筆側が表を
# 2 つに分けた際に 2 表目を「コード | 合致度 | 終値 | …」とコードだけで組んだため、
# 何の銘柄か読めない表が送信された。§7（銘柄名はコードとセット）の既存の検査は
# 「社名にコードが付いているか」だけを見ており、「コードに社名が付いているか」を
# 見ていなかった。
#
# 判定（表ごと）:
#   (1) 本体セルの 6 割以上（3 行以上）が銘柄コードだけ（4 桁・末尾英字可・先頭は
#       1〜9。太字・バッククォート・`.T` 付きは許容）の列がある。先頭 0 の指数コード
#       （0086 等）は銘柄コードではないため拾わない
#   (2) その列が年・年度・決算期・金額・株数などの列ではない。列見出しと値の分布の
#       両方で判定し、見出しに「コード」「銘柄」等が無い列は、値に英字付きコード
#       （338A 等）がある場合か、1 列目で年号らしい値（1900〜2099）が無い場合だけ拾う
#   (3) 同じ表に社名が無い（見出しが 社名・銘柄名・銘柄・会社・企業・名称・name を含む
#       別の列も、「コード 社名」形式のセルが 6 割以上の列も無い）
# ---------------------------------------------------------------------------

# セル全体が銘柄コードだけ（装飾を外した後に判定する）。
_CODE_ONLY_CELL = re.compile(r"^([1-9]\d{2}[0-9A-Z])(?:\.T)?$")
# 先頭が銘柄コードのセル。名称の判定は _code_with_name() が行う
# （\s は全角空白も含む）。
_CODE_HEAD_CELL = re.compile(
    r"^[1-9]\d{2}[0-9A-Z](?:\.T)?\s*[（(「]?"
    r"([^\s\d０-９,，.．/／|（）()「」+＋▼▲△▽%％〜～~→\-]+)"
)
# コード列とみなす見出し語。末尾が数量語（銘柄数・コード比 等）の見出しは除く。
_CODE_HDR = re.compile(r"コード|銘柄|証券|code|ticker|ティッカー", re.IGNORECASE)
_CODE_HDR_QTY_TAIL = re.compile(r"(?:数|率|額|比|価格?|順位)[）)]?$")
# 年・期間・金額・株数などの列見出し（コード列から外す）。
_NOT_CODE_HDR = re.compile(
    r"年|期|月|日|FY|year|date|時|数|率|額|価|円|株|億|万|千|%|％|比|高|量|残|倍|値|益"
    r"|売上|利益|PER|PBR|時価|点|順位|位",
    re.IGNORECASE,
)
# 社名の列とみなす見出し語（強い語は除外語の一部を問わない）。
_NAME_HDR_STRONG = re.compile(r"社名|銘柄名|会社名|企業名|名称|name|company", re.IGNORECASE)
_NAME_HDR_WEAK = re.compile(r"銘柄|会社|企業")
_NAME_HDR_EXCL = re.compile(r"何の|概要|関係|説明|事業|内容")
_NAME_HDR_WEAK_EXCL = re.compile(r"コード|code|数|率|額|比", re.IGNORECASE)
# 「コード＋名称」の名称側が単位だけ（2025年・1200億円・500万株 等）なら名称とみなさない。
_UNIT_CHARS = set("年月日期度億万千百兆円株倍人件回台個枚本点週時分秒歳社名口")
_UNIT_TOKENS = {"pt", "bp", "bps", "x"}

CODE_ONLY_REMEDY = "第 1 列を『コード 社名』にしてください"


def _plain(cell: str) -> str:
    return _DECOR.sub("", cell).strip()


def _is_code_only(cell: str) -> bool:
    return bool(_CODE_ONLY_CELL.match(_plain(cell)))


def _code_with_name(cell: str) -> bool:
    """セルが「コード 社名」形式（コードの直後に和文・英字の名称が続く）か。"""
    m = _CODE_HEAD_CELL.match(_plain(cell))
    if not m:
        return False
    tok = m.group(1)
    if tok.lower() in _UNIT_TOKENS:
        return False
    for ch in tok:
        o = ord(ch)
        if (
            0x3041 <= o <= 0x3096  # ひらがな
            or 0x30A1 <= o <= 0x30FA  # カタカナ（中黒・長音を除く）
            or 0xFF66 <= o <= 0xFF9D  # 半角カタカナ
            or ch.isascii() and ch.isalpha()
            or 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A  # 全角英字
        ):
            return True
        if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or ch == "々") and ch not in _UNIT_CHARS:
            return True
    return False


def _hdr(header: list[str], j: int) -> str:
    return re.sub(r"\s+", "", _plain(header[j])) if j < len(header) else ""


def _is_name_header(h: str) -> bool:
    if not h or _NAME_HDR_EXCL.search(h):
        return False
    if _NAME_HDR_STRONG.search(h):
        return True
    return bool(_NAME_HDR_WEAK.search(h)) and not _NAME_HDR_WEAK_EXCL.search(h)


def _col(rows: list[list[str]], j: int) -> list[str]:
    return [(r[j] if j < len(r) else "") for r in rows]


def _is_code_column(header: list[str], rows: list[list[str]], j: int) -> bool:
    """j 列目が「銘柄コードだけ」の列か（年・年度・決算期・金額・株数の列は拾わない）。"""
    cells = _col(rows, j)
    codes = [_plain(c) for c in cells if _is_code_only(c)]
    if len(rows) < 3 or len(codes) < 3 or len(codes) < 0.6 * len(rows):
        return False
    h = _hdr(header, j)
    if h and _CODE_HDR.search(h) and not _CODE_HDR_QTY_TAIL.search(h):
        return True
    if h and _NOT_CODE_HDR.search(h):
        return False
    # 見出しで決まらない列（空見出し・無関係な語）は値で判定し、迷う値は拾わない。
    vals = [_CODE_ONLY_CELL.match(c).group(1) for c in codes]
    if any(v[-1].isalpha() for v in vals):
        return True  # 338A のような英字付きコードは年・金額になり得ない
    if j == 0 and not any(1900 <= int(v) <= 2099 for v in vals):
        return True
    return False


def check_code_only_tables(md_text: str) -> list[dict]:
    """銘柄コードだけで社名の無い表を検出する（PM 2026-10-05・_cr §7）。

    各違反 dict のキー:
      table    … 文書内の表の通し番号（1 始まり）
      line     … ヘッダ行の行番号（1 始まり）
      column   … コードだけの列の見出し
      examples … コードの例（先頭 3 件）
      message  … 人が読む 1 行メッセージ（対処文込み）
    """
    out: list[dict] = []
    for n, t in enumerate(_extract_tables(md_text), start=1):
        header, rows = t["header"], t["rows"]
        if len(rows) < 3:
            continue
        ncols = max([len(header)] + [len(r) for r in rows])
        code_cols = [j for j in range(ncols) if _is_code_column(header, rows, j)]
        if not code_cols:
            continue
        has_name = False
        for j in range(ncols):
            cells = _col(rows, j)
            if sum(_code_with_name(c) for c in cells) >= 0.6 * len(rows):
                has_name = True
                break
            if j in code_cols:
                continue
            if _is_name_header(_hdr(header, j)) and sum(_is_code_only(c) for c in cells) < 0.6 * len(rows):
                has_name = True
                break
        if has_name:
            continue
        j = code_cols[0]
        col_name = _plain(header[j]) if j < len(header) else ""
        examples = [_plain(c) for c in _col(rows, j) if _is_code_only(c)][:3]
        out.append(
            {
                "table": n,
                "line": t["header_line"],
                "column": col_name,
                "examples": examples,
                "message": (
                    f"表 {n}: 銘柄コードだけで社名がありません。{CODE_ONLY_REMEDY}"
                    f"（L{t['header_line']}・列「{col_name}」・例 {'／'.join(examples)}）"
                ),
            }
        )
    return out


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

# 銘柄コードだけの表（check_code_only_tables）を送信時に error として止める種別
# （PM 2026-10-05 指示）。ローカルで生成し送信前に直せる個別銘柄レポートだけに限る。
# 定期の自動配信の種別（マクロ・セクター・動意・夜間PTS・テーマ・決算・大型株・
# アイデア・スカウト等）と未登録の種別は _cr §36 配信絶対の原則により送信を止めず
# warning とする。書き手が保存前に使うコマンド検査（_main）は種別を問わず、
# コードだけの表が 1 つでもあれば FAIL を返す（違反 0 件が保存の必須条件）。
CODE_ONLY_BLOCKING_KINDS = frozenset({"stock", "us_stock"})


def gate_report_tables(md_text: str, kind: str) -> tuple[list[str], list[str]]:
    """レポート種別を問わず表を検査し (errors, warnings) を返す。

    折り返す表（check_tables）は kind を渡して判定する（列幅の自動配分で収まる表は合格。2026-10-06）。
    折り返す表（check_tables）は kind が BLOCKING_KINDS に含まれる場合のみ errors へ入れ、
    それ以外の種別は warnings へ入れて送信を継続させる（_cr §36）。
    銘柄コードだけの表（check_code_only_tables）は kind が CODE_ONLY_BLOCKING_KINDS に
    含まれる場合に errors へ入れ、それ以外は warnings へ入れる（PM 2026-10-05）。
    呼び出し元は errors が非空なら PDF を生成せず中止する。
    """
    errors: list[str] = []
    warnings: list[str] = []
    violations = check_tables(md_text, kind)
    if violations:
        msgs = [f"表の折り返し: {v['message']}" for v in violations]
        if kind in BLOCKING_KINDS:
            errors.extend(msgs)
        else:
            head = (
                f"表の折り返し違反が {len(violations)} 件あります"
                f"（種別 {kind} は配信絶対の原則により送信は継続します。"
                "カード自動変換は 2026-09-07 に廃止しており誌面上の救済はありません。"
                "次回の生成で本文を直してください）"
            )
            warnings.extend([head] + msgs)
    code_only = check_code_only_tables(md_text)
    if code_only:
        msgs = [f"コードだけの表: {v['message']}" for v in code_only]
        if kind in CODE_ONLY_BLOCKING_KINDS:
            errors.extend(msgs)
        else:
            warnings.append(
                f"銘柄コードだけで社名の無い表が {len(code_only)} 件あります"
                f"（種別 {kind} は配信絶対の原則により送信は継続します。"
                "次回の生成で第 1 列を『コード 社名』に直してください）"
            )
            warnings.extend(msgs)
    return errors, warnings


def _main(argv: list[str] | None = None) -> int:
    """md 1 本を書き手の保存前検査として検査する（送信・PDF 生成はしない）。

    折り返す表は送信前の表ゲートと同じ判定（種別ごとに error / warning）で表示する。
    銘柄コードだけの表は種別を問わず 1 つでもあれば FAIL を返す（PM 2026-10-05。
    送信時ゲートは自動配信の種別を止めないため、書き手側で必ず直させる）。

    例: python bi/pipelines/lib/table_rules.py research/themes/xxx.md --kind themes
    """
    import argparse
    import sys
    from pathlib import Path

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass
    ap = argparse.ArgumentParser(description="誌面 md の表を保存前に検査する（コードだけの表は種別を問わず FAIL）")
    ap.add_argument("md", help="検査する md のパス")
    ap.add_argument(
        "--kind",
        default="themes",
        help="レポート種別（send_report_pdf_discord.py の --kind と同じ。既定 themes）",
    )
    args = ap.parse_args(argv)
    text = Path(args.md).read_text(encoding="utf-8")
    errors, warnings = gate_report_tables(text, args.kind)
    code_only = check_code_only_tables(text)
    # 保存前検査では、コードだけの表を種別を問わず NG として表示し FAIL にする。
    warnings = [m for m in warnings if not m.startswith(("コードだけの表: ", "銘柄コードだけで社名の無い表が"))]
    errors = [m for m in errors if not m.startswith("コードだけの表: ")]
    errors += [f"コードだけの表: {v['message']}" for v in code_only]
    for m in warnings:
        print("WARN " + m)
    for m in errors:
        print("NG   " + m)
    print(
        f"銘柄コードだけの表: {len(code_only)} 件 / "
        f"折り返す表: {len(check_tables(text, args.kind))} 件 / "
        f"判定: {'FAIL' if errors else 'PASS'}（種別 {args.kind}）"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(_main())
