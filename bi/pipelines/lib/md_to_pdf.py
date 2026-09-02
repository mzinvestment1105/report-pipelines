"""Markdown レポート → 洗練された A4 PDF（Playwright Chromium・日本語フォント埋め込み）。

PM 2026-06-27: JPEG（縦長スクショ・読む気が起きない）を廃し、商品レベルの PDF で配信する。

■ 文字化け（中国語グリフ化）根絶の設計
  GHA(Ubuntu) ランナーには Yu Gothic が無く、pan-CJK の Noto Sans CJK は言語指定が無いと
  簡体字グリフへフォールバックして「日本語が中国語に見える」事故が起きる。本モジュールは
  日本語サブセットの Noto Sans/Serif JP(woff2) を **base64 で @font-face に埋め込む**ため、
  システムのフォント選択に一切依存せず、Windows でも Linux でも完全に同一の正しい日本語で
  描画される（= ローカル検証がそのまま本番の保証になる）。

■ デザイン
  雑誌/コンサルレポート級のエディトリアル体裁。明朝見出し + ゴシック本文、抑えた配色、
  リード段落ボックス、金融レポート調の表、ページ番号フッター。
"""

from __future__ import annotations

import base64
import re
from datetime import date, datetime
from pathlib import Path

import markdown as md
from playwright.sync_api import sync_playwright

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# レポート種別 → (英語キッカー, 日本語ラベル, アクセント色)
KIND_META = {
    "macro":    ("MACRO REPORT",   "マクロレポート",     "#2F6DB5"),
    "sector":   ("SECTOR REPORT",  "セクターレポート",   "#2E8B6B"),
    "movers":   ("MARKET MOVERS",  "動意銘柄レポート",   "#C8762F"),
    "ideas":    ("INVESTMENT IDEAS","投資アイデア",      "#7A5AA6"),
    "earnings": ("EARNINGS",       "決算レポート",       "#1F8A8A"),
    "themes":   ("THEMES",         "テーマレポート",     "#B8902A"),
    "stock":    ("EQUITY RESEARCH","個別銘柄レポート",   "#B5483D"),
    "largecap_weekly": ("LARGE CAP WEEKLY", "週次大型株速報", "#1F3A93"),
    "review": ("DEV REVIEW", "開発レビュー資料", "#555F73"),
}

_WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def _font_b64(name: str) -> str:
    p = _FONT_DIR / name
    return base64.b64encode(p.read_bytes()).decode("ascii")


# フォントテーマ → (本文/見出しの sans スタック, マストヘッドの serif スタック)
# すべて assets/fonts に同梱の OFL 無料フォントを @font-face 埋め込みするため GHA(Linux) でも同一描画。
FONT_THEMES = {
    "noto": ("'NotoSansJP', sans-serif", "'NotoSerifJP', serif"),
    # Meiryo 系の UD ゴシック（Morisawa BIZ UDP・Meiryo UI 代替で本番採用可）
    "biz":  ("'BIZ UDPGothic', 'NotoSansJP', sans-serif", "'BIZ UDPGothic', 'NotoSansJP', sans-serif"),
}


def _font_face_css() -> str:
    """埋め込み @font-face（base64・woff2）。システムフォント非依存で日本語を確定描画する。"""
    sans400 = _font_b64("NotoSansJP-400.woff2")
    sans500 = _font_b64("NotoSansJP-500.woff2")
    sans700 = _font_b64("NotoSansJP-700.woff2")
    serif700 = _font_b64("NotoSerifJP-700.woff2")
    biz400 = _font_b64("BIZUDPGothic-400.woff2")
    biz700 = _font_b64("BIZUDPGothic-700.woff2")
    return f"""
@font-face {{ font-family:'NotoSansJP'; font-weight:400; font-style:normal;
  src:url(data:font/woff2;base64,{sans400}) format('woff2'); font-display:block; }}
@font-face {{ font-family:'NotoSansJP'; font-weight:500; font-style:normal;
  src:url(data:font/woff2;base64,{sans500}) format('woff2'); font-display:block; }}
@font-face {{ font-family:'NotoSansJP'; font-weight:700; font-style:normal;
  src:url(data:font/woff2;base64,{sans700}) format('woff2'); font-display:block; }}
@font-face {{ font-family:'NotoSerifJP'; font-weight:700; font-style:normal;
  src:url(data:font/woff2;base64,{serif700}) format('woff2'); font-display:block; }}
@font-face {{ font-family:'BIZ UDPGothic'; font-weight:400; font-style:normal;
  src:url(data:font/woff2;base64,{biz400}) format('woff2'); font-display:block; }}
@font-face {{ font-family:'BIZ UDPGothic'; font-weight:700; font-style:normal;
  src:url(data:font/woff2;base64,{biz700}) format('woff2'); font-display:block; }}
"""


def _date_label(target_date: str | None) -> str:
    if not target_date:
        return ""
    try:
        d = date.fromisoformat(target_date)
    except (ValueError, TypeError):
        return target_date
    return f"{d.year}年{d.month}月{d.day}日（{_WEEKDAY_JP[d.weekday()]}）"


def _layout_css(accent: str, sans: str, serif: str) -> str:
    """可読性の一次情報（W3C jlreq / Typotheque / WCAG / Butterick）に基づく組版。

    本文 12pt・line-height 1.8・字間 0.04em・色 #222（純黒のハレーション回避）・日本語は両端
    揃え。1 行は左右 24mm マージンで全角≈38 字（CJK 最適 35〜40 字）。見出しは 1.25 スケール。

    PM 2026-08-30 実測修正: 個別銘柄レポートだけ PDF 実測 8.0pt まで潰れ「小さくて読めない」
    状態だった（動意・決算・週次大型株は同じ 12pt 指定で実測 12.0pt）。原因は table-layout:auto
    で、和文の長文セルが列幅を押し広げ表の実幅が本文幅 612px を超えていた（実測 1035px）。
    Chromium は最も広い表に合わせてページ全体を縮小するため本文まで道連れに潰れる。
    table-layout:fixed + word-break:normal で表を本文幅に収め、全種別で実測 12.0pt に揃える。
    """
    return f"""
* {{ box-sizing:border-box; }}
html {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
/* 字送りの均一化（PM 2026-08-30 実測修正）:
   "palt" 1（プロポーショナル詰め）は和文の各字を字形固有の幅へ詰めるため、字送りが不均等になり
   特に太字で「機 関 の 売り建 ては」のように見える。PDF テキストレイヤの実測で字送りのばらつきは
   palt 1 = 1.44pt に対し palt 0 = 0.24pt（6分の1）と確定したため全要素で palt を無効化する。
   font-synthesis:none は合成太字による字幅変化の予防（本フォントは実ボールド woff2 を埋め込み済み）。 */
* {{
  font-feature-settings:"palt" 0;
  font-synthesis:none; -webkit-font-synthesis:none;
}}
body {{
  font-family:{sans};
  font-size:12pt; line-height:1.8; color:#222222;
  margin:0; padding:0;
  letter-spacing:.04em; font-feature-settings:"palt" 0;
  font-synthesis:none; -webkit-font-synthesis:none;
  font-kerning:none;
  text-align:left; word-break:normal; line-break:strict; overflow-wrap:break-word;
}}

/* ── マストヘッド ── */
.masthead {{ margin:0 0 20px; padding:0 0 13px; border-bottom:2.2pt solid {accent}; }}
.masthead .kicker {{
  font-size:9pt; font-weight:700; letter-spacing:.34em; color:{accent};
  text-transform:uppercase; margin:0 0 6px;
}}
.masthead h1 {{
  font-family:{serif}; font-weight:700; font-size:24pt; color:#1A1A1A;
  margin:0 0 8px; line-height:1.32; letter-spacing:.01em; text-align:left;
}}
.masthead .meta {{ font-size:10pt; color:#6B7686; font-weight:500; letter-spacing:.02em; }}
.masthead .meta .brand {{ color:#1A1A1A; font-weight:700; letter-spacing:.06em; }}

/* ── 市場区切り見出し（本文中の h1）──
   PM 2026-09-02 確定: 動意レポートを 1 本の PDF へ統一したため、2 本目以降の
   `# 動意銘柄レポート YYYY-MM-DD（市場名）` が本文中に残る（先頭 1 本だけがマストヘッドへ昇格）。
   マストヘッドと紛れない「市場の区切り帯」として、アクセント色の塗り帯で明示する。
   強制改ページ（break-before:page）は掛けない（_cr §39 の 1/3 空白禁止に抵触するため）。 */
h1 {{
  font-family:{serif}; font-weight:700; font-size:19pt; color:#FFFFFF;
  background:{accent}; margin:30px 0 14px; padding:11px 14px;
  line-height:1.35; letter-spacing:.02em; text-align:left;
}}

/* ── 見出し（1.25 タイポグラフィックスケール）── */
h2 {{
  font-weight:700; font-size:17.5pt; color:#1A1A1A;
  margin:26px 0 10px; padding:9px 0 0; border-top:1.2pt solid #DBE1E9;
  letter-spacing:.02em; text-align:left;
}}
h3 {{
  font-size:14pt; font-weight:700; color:{accent}; margin:18px 0 7px;
  letter-spacing:.02em; text-align:left;
}}
h4 {{ font-size:12pt; font-weight:700; color:#3C4A63; margin:13px 0 5px; text-align:left; }}
/* ── h5 = テーマ内の小見出し（「何が起きたか」等）。色付き＋左バーでインライン太字と明確に区別 ── */
h5 {{
  font-size:11pt; font-weight:700; color:{accent};
  margin:17px 0 6px; padding:5px 11px;
  background:#EEF3FA; border-left:4.5pt solid {accent};
  letter-spacing:.06em; text-align:left;
}}

p {{ margin:9px 0 11px; }}
/* 太字は実ボールド字形（NotoSansJP-700 / BIZ UDPGothic-700 を @font-face 埋め込み済み）を使う。
   合成太字を禁じ、字間は親の .04em を継承させて詰め処理を挟ませない。 */
strong, b, th {{
  font-family:{sans};
  font-weight:700; font-synthesis:none; -webkit-font-synthesis:none;
  font-feature-settings:"palt" 0; font-kerning:none;
}}
strong, b {{ color:#1A1A1A; }}

/* ── リード（冒頭サマリー）= エディトリアルな前文 ── */
blockquote {{
  margin:0 0 20px; padding:14px 18px; background:#FAFBFD;
  border-left:3pt solid {accent};
  font-size:12.5pt; line-height:1.95; color:#2A3654;
}}
blockquote p {{ margin:0; }}
/* リード内太字はアクセント色（PM 2026-07-13: 黒は読みづらい・青へ戻す）。
   可読性は色を消すのでなく太字の比率で制御する（prompts 側 §24 でリードの太字を文字数比 1/3 以下に制限）。 */
blockquote strong {{ color:{accent}; }}

/* ── リスト ── */
ul, ol {{ margin:9px 0 12px; padding-left:22px; }}
li {{ margin:5px 0; padding-left:4px; text-align:left; line-height:1.75; }}
li::marker {{ color:#8A94A6; }}

/* ── 表（金融レポート調・大きい表はページ分割を許可）── */
/* table-layout:fixed が必須（PM 2026-08-30 判定・B 案採用で確定）。
   auto では和文の長いセルが列幅を押し広げ、表の実幅が本文幅 612px を超えるため
   （実測: 4011 の条件表が 1035px）、Chromium が最も広い表に合わせてページ全体を縮小し、
   本文 12pt 指定が実測 8.0pt まで潰れていた。しかも縮小率は「その回のレポートで最も広い表」
   に依存するため、同じ設定でも銘柄ごとに実測 8.0pt / 11.0pt とサイズが変動していた
   （動意・決算・週次大型株は幅超過の表が無く実測 12.0pt だったため、個別銘柄だけが潰れていた）。
   fixed + word-break:normal で列幅を本文幅内に固定し、全レポート種別・全銘柄で実測 12.0pt に揃える。 */
table {{
  border-collapse:collapse; width:100%; margin:13px 0 17px;
  table-layout:fixed;
  font-size:10.5pt; line-height:1.6; font-variant-numeric:tabular-nums;
  text-align:left;
}}
th {{ white-space:normal; word-break:normal; overflow-wrap:break-word; }}
thead th {{
  background:#1A2A44; color:#FFFFFF; font-weight:700; font-size:10.5pt;
  text-align:left; padding:8px 11px; letter-spacing:.02em;
  white-space:normal; word-break:normal; overflow-wrap:break-word;
}}
/* ヘッダも文節単位で折り返す（nowrap はページ全体の縮小を招くため使わない） */
thead th:first-child {{ white-space:normal; word-break:normal; overflow-wrap:break-word; }}
tbody td {{
  padding:7px 10px; border-bottom:0.6pt solid #E3E8EF; vertical-align:middle;
  line-height:1.55;
}}
/* 先頭列（項目名）・数値セルの折り返し制御（PM 2026-08-30 実測修正）:
   white-space:nowrap を全セルに掛けると、長いセルが1行に収まらない場合に Chromium が
   ページ全体を縮小して辻褄を合わせるため、本文が実測 12pt → 8pt まで潰れる
   （動意・決算レポートは実測 12.0pt なのに個別銘柄だけ 8.0pt だった原因）。
   1文字改行の根絶は overflow-wrap:anywhere を使わないことで足り、nowrap は不要。
   word-break:keep-all により和文は文節を割らずに折り返す。 */
tbody td:first-child {{ white-space:normal; word-break:normal; overflow-wrap:break-word; }}
/* 中間列は「短い数値なら折り返さない・長い和文なら文節で折り返す」を両立させる。
   nowrap を一律に掛けると、条件表や観測方法など長文を中間列に持つ表で1行が page 幅を
   超え、Chromium がページ全体を縮小する（本文 12pt → 実測 7pt）。max-width で
   上限を与えたうえで文節折り返しを許可し、数値列は短いため実質的に折り返されない。 */
tbody td:not(:first-child):not(:last-child) {{
  white-space:normal; word-break:normal; overflow-wrap:break-word;
}}
/* 説明文など長文を含む最終列のみ折り返しを許可するが、文節は割らない */
tbody td:last-child {{ white-space:normal; word-break:normal; overflow-wrap:break-word; }}
tbody tr:nth-child(even) td {{ background:#F6F8FB; }}
tbody tr:last-child td {{ border-bottom:1pt solid #C8D1DD; }}

/* ── テーマ表の列幅（動意・夜間PTS の「本日のテーマ」「直近2週間の熱いテーマ」）──
   table-layout:fixed は列幅を均等割りするため、6列の熱量表では「局面」「前2週比」等の
   短い数値列と「主導銘柄」「動いた理由」の長文列が同じ幅になり、長文列が1銘柄名で
   何行にも折り返して誌面が読めなくなる（2026-08-31 実測）。列数で表を判別し、
   内容量に比例した幅を colgroup 相当の nth-child で与える。fixed は維持するため
   ページ全体の縮小（本文 12pt → 8pt）は起きない。 */
/* 3列＝本日のテーマ: テーマ / 主導銘柄 / 動いた理由 */
table.theme-today th:nth-child(1), table.theme-today td:nth-child(1) {{ width:26%; }}
table.theme-today th:nth-child(2), table.theme-today td:nth-child(2) {{ width:38%; }}
table.theme-today th:nth-child(3), table.theme-today td:nth-child(3) {{ width:36%; }}
/* 6列＝直近2週間: テーマ / 局面 / 熱量 / 前2週比 / 主導銘柄 / 動いた理由 */
table.theme-heat th:nth-child(1), table.theme-heat td:nth-child(1) {{ width:17%; }}
table.theme-heat th:nth-child(2), table.theme-heat td:nth-child(2) {{ width:9%; }}
table.theme-heat th:nth-child(3), table.theme-heat td:nth-child(3) {{ width:9%; }}
table.theme-heat th:nth-child(4), table.theme-heat td:nth-child(4) {{ width:11%; }}
table.theme-heat th:nth-child(5), table.theme-heat td:nth-child(5) {{ width:28%; }}
table.theme-heat th:nth-child(6), table.theme-heat td:nth-child(6) {{ width:26%; }}
/* 短い数値列のヘッダ（局面・熱量・前2週比）だけは1行に収める。列幅を確保済みのため
   nowrap にしてもページ全体の縮小は起きない（幅超過の原因は本文セル側にあった）。 */
table.theme-heat thead th:nth-child(2), table.theme-heat thead th:nth-child(3),
table.theme-heat thead th:nth-child(4) {{ white-space:nowrap; text-align:center; }}
/* 局面（新規/加速/継続）も1行に収める */
table.theme-heat tbody td:nth-child(2) {{ white-space:nowrap; }}
/* 主導銘柄セルは <br> 区切りの銘柄リストのため行間を詰めて縦の嵩を抑える */
table.theme-today td:nth-child(2), table.theme-heat td:nth-child(5) {{ line-height:1.45; }}
table.theme-heat td:nth-child(2), table.theme-heat td:nth-child(3),
table.theme-heat td:nth-child(4) {{ text-align:center; }}

/* ── 主導銘柄の密な表（新形式・2026-08-31 PM 確定）──
   旧形式は主導銘柄セルへ複数銘柄を <br> で縦積みしたため、長い銘柄名が折り返して
   読みづらく、隣の理由セル（1文）との高さ差が大余白になっていた（PM 指摘: 主導銘柄が
   読みづらい／スカスカの表が嫌い）。新形式は1銘柄=1行の4列表にして、
   コード・騰落率は短い固定幅、銘柄名と「何の会社」に幅を配分し全セルを1行へ収める。
   列: コード / 銘柄名 / 何の会社 / 騰落率 */
/* 列幅は「1行に収まること」を実測で確認して決めた（2026-08-31）。本文幅 612px・9.5pt 時に
   銘柄名は最長17字（ダイナミックマッププラットフォーム）、「何の会社」は15字前後が上限。 */
table.theme-lead {{ margin:6px 0 15px; font-size:9.5pt; }}
table.theme-lead th:nth-child(1), table.theme-lead td:nth-child(1) {{ width:9%; }}
table.theme-lead th:nth-child(2), table.theme-lead td:nth-child(2) {{ width:42%; }}
table.theme-lead th:nth-child(3), table.theme-lead td:nth-child(3) {{ width:37%; }}
table.theme-lead th:nth-child(4), table.theme-lead td:nth-child(4) {{ width:12%; }}
/* コード・騰落率は必ず1行（幅を確保済みのためページ全体の縮小は起きない） */
table.theme-lead td:nth-child(1), table.theme-lead td:nth-child(4),
table.theme-lead thead th:nth-child(1), table.theme-lead thead th:nth-child(4) {{
  white-space:nowrap;
}}
table.theme-lead td:nth-child(4), table.theme-lead thead th:nth-child(4) {{ text-align:right; }}
table.theme-lead tbody td {{ padding:5px 8px; line-height:1.45; }}
table.theme-lead thead th {{ padding:5px 8px; font-size:9pt; }}

/* テーマ見出し行（**テーマ名**｜局面 …）は h5 へ昇格されるため、表と密に接する余白にする */
h5 + table.theme-lead, h5 + p + table.theme-lead {{ margin-top:4px; }}
/* テーマブロック（見出し+理由文+主導銘柄表）を包む単位。余白は内側の要素が持つため
   div 自身は余白を持たず、ブロック末尾の表の下マージンだけをブロック間の間隔にする。 */
.theme-block {{ margin:0; }}
.theme-block > h5:first-child {{ margin-top:14px; }}
.theme-block > table.theme-lead:last-child {{ margin-bottom:15px; }}

code {{
  background:#EEF1F5; padding:1px 5px; border-radius:3px; color:#B5483D;
  font-family:'Consolas','Courier New',monospace; font-size:10pt;
}}
hr {{ border:0; border-top:0.8pt solid #DBE1E9; margin:20px 0; }}

/* ── 改ページ制御（改ページ由来の空白をページの 1/3 以上作らない・_cr §39）──
   鉄則（2026-09-01 PM 承認で改定）: **ブロックの塊送りより空白最小を優先する**。
   `break-inside:avoid` を掛けた要素はページ末に入り切らないと丸ごと次ページへ送られ、
   前ページに要素の高さぶんの空白が残る（v11 実測: 1ページ目の下 2/3 が空白）。
   よって「見出しが単独でページ末に取り残される」ことだけを break-after:avoid で防ぎ、
   表・ブロックは途中改ページを許可する。 */
@media print {{
  /* 表は常に分割可（theme-lead も含む。thead が次ページ先頭で再描画される） */
  table {{ break-inside:auto; }}
  table.theme-lead {{ break-inside:auto; }}
  /* テーマブロックも分割可。見出し→理由文→表の先頭までの連結だけを保証する
     （2026-09-01 PM 承認・v11 の巨大余白の是正）。 */
  .theme-block {{ break-inside:auto; }}
  /* 見出しはその直後の要素と切り離さない（見出しだけがページ末に残る分断の防止）。
     break-after:avoid は「次の1要素」との連結だけを保証するので大空白を生まない。
     理由文の段落は**分割可**にする（2026-09-01）。長文の理由文へ break-inside:avoid を
     掛けると段落まるごとが次ページへ送られ、前ページに 20% 級の空白が残る実測があった。
     orphans/widows で行単位の孤立だけを抑え、段落は途中改ページを許す。 */
  .theme-block > :first-child {{ break-after:avoid; }}
  .theme-block > p {{ break-inside:auto; orphans:2; widows:2; }}
  h5:has(+ p + table.theme-lead), h5:has(+ table.theme-lead) {{ break-after:avoid; }}
  thead {{ display:table-header-group; }}
  tr, img {{ break-inside:avoid; }}
  h1, h2, h3, h4, h5 {{ break-after:avoid; break-inside:avoid; }}
  p, li {{ orphans:2; widows:2; }}
  blockquote {{ break-inside:auto; }}
  .masthead {{ break-inside:avoid; break-after:avoid; }}
  body > :first-child {{ margin-top:0; }}
}}
"""


_POS = "#1F8A4C"  # 上昇=緑
_NEG = "#C0392B"  # 下落=赤
# 符号付き数値（+12.3% / −4.0% / ▲22百万円 / +271百万円 / +1.39 / 全角＋％対応）。
# 着色条件は「% を伴う」「小数点を含む」「金額・株数などの単位を伴う」のいずれか。
# 単位も小数点も無い裸の符号付き整数（銘柄コード・年号・順位）のみ除外する
# （PM 2026-08-30: ▲22百万円・+271百万円 が黒のままだった不具合の是正）。
_SIGNED_UNIT = "百万円|千円|億円|兆円|円|株|口|件|社|倍|pt|ポイント|%|％"
_RE_SIGNED = re.compile(
    r"([+＋−\-▲△])(\d[\d,]*(?:\.\d+)?)(\s*(?:" + _SIGNED_UNIT + r"))?"
)


def _colorize_signed(m: "re.Match[str]") -> str:
    sign, num, unit = m.group(1), m.group(2), m.group(3) or ""
    # 単位も小数点も無い符号付き整数（コード/年/順位）は着色しない
    if not unit and "." not in num:
        return m.group(0)
    color = _NEG if sign in "−-▲△" else _POS
    return f'<span style="color:{color};font-weight:700">{sign}{num}{unit}</span>'


def _colorize_numbers(html: str) -> str:
    """生成 HTML 中の騰落率・トレンド矢印を上昇=緑/下落=赤で確定着色する（renderer 側で保証・LLM 非依存）。

    金融レポートの一目可読性のため、本文・表セル内の +X%/−X%・符号付きリターンと 8 週トレンド帯の
    ▲▼ を色分けする。着色は render 後の HTML に対して行い、タグ属性へ符号付き数値は出ないため安全。
    """
    html = _RE_SIGNED.sub(_colorize_signed, html)
    # 単独の ▲△ は着色しない（和文会計では ▲12.3% がマイナスを意味するため、
    # 上昇矢印として緑に塗ると増減を逆に読ませる）。▼▽ のみ下落として扱う。
    html = (html.replace("▼", f'<span style="color:{_NEG}">▼</span>')
                .replace("▽", f'<span style="color:{_NEG}">▽</span>'))
    return html


# 時価総額サイズ目印（PM 2026-06-30 確定・LLM 非依存で renderer が付与）。
# 個別株を列挙する見出し（### …（… 時価総額 X億/兆円 …））に対し時価総額で区分タグを挿入:
#   100億以上=無印 / 50〜100億未満=〔小型 ◯◯億〕 / 50億未満=〔極小 ◯◯億・対象外〕（赤太字）。
# PM は100億未満を基本回避・50億以下は禁止リスト（playbook/entry_exit_rules.md §3-6）。
_SIZE_MCAP_RE = re.compile(r"時価総額\s*([\d,]+(?:\.\d+)?)\s*(兆|億)円")
_SIZE_PCT_RE = re.compile(r"(\s*[+\-−][\d.]+\s*%\s*)(（)")


def _size_tag(mcap_oku: float | None) -> str | None:
    if mcap_oku is None:
        return None
    if mcap_oku <= 50:
        return f'<span style="color:{_NEG};font-weight:700">〔極小 {mcap_oku:.0f}億・対象外〕</span>'
    if mcap_oku < 100:
        return f'〔小型 {mcap_oku:.0f}億〕'
    return None


def _inject_size_tags(md_text: str) -> str:
    """個別株見出し行の時価総額からサイズ目印タグを銘柄名直後に挿入する（極小は赤太字）。"""
    out = []
    for line in md_text.split("\n"):
        if line.startswith("### ") and "時価総額" in line and "〔" not in line:
            mm = _SIZE_MCAP_RE.search(line)
            if mm:
                val = float(mm.group(1).replace(",", ""))
                oku = val * 10000 if mm.group(2) == "兆" else val
                tag = _size_tag(oku)
                if tag:
                    new, n = _SIZE_PCT_RE.subn(rf" {tag}\1\2", line, count=1)
                    if n == 0:
                        new = line.replace("（", f" {tag}（", 1)
                    line = new
        out.append(line)
    return "\n".join(out)


def _wrap_theme_blocks(html: str) -> str:
    """テーマブロック（見出し + 理由段落 + 主導銘柄表）を1つの div へ束ねる。

    2026-09-01 PM 承認で役割を変更した。旧版はこの div へ `break-inside:avoid` を掛けて
    ブロックを丸ごと1ページへ収めていたが、ブロックがページ末に入り切らないと**丸ごと
    次ページへ送られ**、前ページにブロック高ぶんの空白が残った（v11 実測で1ページ目の
    下 2/3 が空白）。_cr §39 は「改ページ由来の空白をページの 1/3 以上作らない」を
    塊送りより優先すると定めるため、現在は div を**分割可**にしている。

    この div の役割は、見出し・理由文が直後の要素と切り離されないよう CSS の
    `break-after:avoid` を掛ける足場を作ることだけ（`.theme-block > :first-child`・
    `.theme-block > p`）。表は途中改ページを許すので、ページ末に空白が残らない。

    包む対象は「直後に table.theme-lead を持つ見出し」に限定する（テーマ2部のみ）。
    """
    # 見出し →（任意の理由段落）→ theme-lead 表 までを1ブロックとして包む。
    # 2026-09-01: 見出しは h5 だけでなく `<p><strong>…</strong></p>`（誌面の
    # `**1位 テーマ名**｜…` 行が変換された形）も対象にする。実際のテーマ2部の誌面は
    # 太字段落で見出しを書いており、h5 限定だと本ラッパが一度も掛からず、見出し＋理由文
    # だけがページ末に残る分断が v10 まで残っていた（8/28 実測で「仮想通貨」が分断）。
    # `**1位 テーマ名**（10日中7日点灯）` のように太字の後ろに素の文字が続く形も見出しと
    # みなす（v11 の点灯日数表記）。`</strong></p>` で閉じる形だけを見ていると掛からない。
    _HEAD = r"(?:<h5[^>]*>.*?</h5>|<p><strong>(?:(?!</p>).)*?</strong>(?:(?!</p>).)*?</p>)"
    pattern = re.compile(
        r"(" + _HEAD + r")\s*"
        r"((?:<p>(?!<strong>).*?</p>\s*)*?)"
        r"(<table class=\"theme-lead\">.*?</table>)",
        flags=re.DOTALL,
    )

    def _repl(m: re.Match) -> str:
        return (
            '<div class="theme-block">'
            + m.group(1) + m.group(2) + m.group(3)
            + "</div>"
        )

    return pattern.sub(_repl, html)


def _tag_theme_tables(html: str) -> str:
    """テーマ2部の表に列幅用のクラスを付ける（ヘッダ行の内容で機械判定する）。

    新形式（2026-08-31 PM 確定・スカスカ表の解消）:
      テーマごとに `**テーマ名**｜局面…` の見出し行を置き、その下へ主導銘柄の
      4列表「コード / 銘柄名 / 何の会社 / 騰落率」を置く。1銘柄=1行で全セルが
      1行に収まるため、旧形式のような結合セル縦積みと理由セルの大余白が出ない。
      → クラス `theme-lead`

    旧形式（互換のため判定を残す。過去 raw の再レンダリング用）:
      「本日のテーマ」＝ テーマ/主導銘柄/動いた理由 の3列 → `theme-today`
      「直近2週間」＝ テーマ/局面/熱量/前2週比/主導銘柄/動いた理由 の6列 → `theme-heat`

    見出し文字列ではなく表自身のヘッダで判別するため、誌面の見出し表記が変わっても効く。
    table-layout:fixed は維持する（外すと 12pt シュリンク回帰が再発するため）。
    """

    def _repl(m: re.Match) -> str:
        table_html = m.group(0)
        head = table_html[: table_html.find("</thead>") + 8] if "</thead>" in table_html else table_html
        # `<thead>` 自体が部分文字列 "<th" を含むため `count("<th")` は列数+1 になる
        # （2026-08-31 実測。旧 3列判定 `n_cols == 3` が一度も成立しておらず、
        #  「本日のテーマ」表に列幅が当たっていなかった原因）。`<th>` で数える。
        n_cols = head.count("<th>")
        # 新形式: 主導銘柄の4列表（コード/銘柄名/何の会社/騰落率）
        if n_cols == 4 and "コード" in head and "何の会社" in head and "騰落率" in head:
            return table_html.replace("<table>", '<table class="theme-lead">', 1)
        # 旧形式
        if "動いた理由" not in head or "主導銘柄" not in head:
            return table_html
        if n_cols >= 6 and "局面" in head:
            cls = "theme-heat"
        elif n_cols == 3:
            cls = "theme-today"
        else:
            return table_html
        return table_html.replace("<table>", f'<table class="{cls}">', 1)

    return re.sub(r"<table>.*?</table>", _repl, html, flags=re.DOTALL)


def render_markdown_to_pdf(
    md_text: str,
    out_path: Path,
    kind: str = "macro",
    target_date: str | None = None,
    brand: str = "MIZUKI FUND",
    font_theme: str = "noto",
) -> Path:
    """Markdown を A4 PDF にレンダリングして保存し、パスを返す。

    font_theme: 'noto'（既定・明朝見出し＋ゴシック本文）/ 'biz'（Meiryo 系 UD ゴシック・本文も
    見出しも BIZ UDPGothic）。いずれも assets/fonts 同梱の OFL フォントを埋め込むため GHA でも同一。
    """
    kicker, jp_label, accent = KIND_META.get(kind, KIND_META["macro"])
    sans_stack, serif_stack = FONT_THEMES.get(font_theme, FONT_THEMES["noto"])

    # 先頭 H1 をマストヘッド見出しに昇格し本文からは除去（重複回避）
    lines = md_text.replace("\r\n", "\n").split("\n")
    title = jp_label
    for i, ln in enumerate(lines):
        m = re.match(r"^#\s+(.*)$", ln.strip())
        if m:
            title = m.group(1).strip()
            del lines[i]
            break

    # 行全体が太字だけの段落（**…**）= テーマ内の小見出し。GHA 生成 Claude が `#####` でなく
    # `**太字**` で書くため、ここで確定的に h5 へ昇格し、本文中のインライン強調太字と体裁を明確に
    # 分ける（PM 指示・LLM 出力に依存せず renderer 側で保証する）。
    _bold_only = re.compile(r"^\*\*([^*]+)\*\*$")
    # テーマ2部の見出し行 `**テーマ名**｜局面 加速・熱量 171・前2週比 +146`（_cr §38）。
    # 行全体が太字ではないため上の _bold_only では拾えないが、意味は同じ小見出しなので
    # 併せて h5 へ昇格する（2026-08-31 追加）。全角縦棒の後ろはメタ情報として残す。
    _bold_head_meta = re.compile(r"^\*\*([^*]+)\*\*(｜.+)$")
    promoted = []
    for ln in lines:
        stripped = ln.strip()
        m2 = _bold_only.match(stripped)
        if m2:
            promoted.append(f"##### {m2.group(1).strip()}")
            continue
        m3 = _bold_head_meta.match(stripped)
        if m3:
            promoted.append(f"##### {m3.group(1).strip()}{m3.group(2).rstrip()}")
            continue
        promoted.append(ln)
    lines = promoted

    body_md = "\n".join(lines).strip()
    # 内部メタ表現の確定除去（PM 2026-06-27・LLM が本文に書いても renderer 側で必ず削除する）
    body_md = re.sub(r"（記事ベース[^）]*）", "", body_md)
    body_md = _inject_size_tags(body_md)

    html_body = md.markdown(body_md, extensions=["tables", "fenced_code", "sane_lists"])
    html_body = _colorize_numbers(html_body)
    html_body = _tag_theme_tables(html_body)
    html_body = _wrap_theme_blocks(html_body)

    date_label = _date_label(target_date)
    masthead = f"""<header class="masthead">
  <div class="kicker">{kicker}</div>
  <h1>{title}</h1>
  <div class="meta"><span class="brand">{brand}</span>{('　｜　' + date_label) if date_label else ''}</div>
</header>"""

    full_html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"/>
<style>{_font_face_css()}{_layout_css(accent, sans_stack, serif_stack)}</style></head>
<body>{masthead}{html_body}</body></html>"""

    footer_tpl = (
        f'<div style="width:100%; font-family:sans-serif; font-size:7pt; color:#9aa3b0;'
        f' padding:0 14mm; display:flex; justify-content:space-between;">'
        f'<span>{brand}</span>'
        f'<span class="pageNumber"></span>/<span class="totalPages"></span>'
        f'</div>'
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(full_html, wait_until="load")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=footer_tpl,
            margin={"top": "20mm", "bottom": "16mm", "left": "24mm", "right": "24mm"},
        )
        browser.close()
    return out_path
