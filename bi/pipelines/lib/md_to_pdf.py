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
import sys
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
   PM 2026-09-02 確定: 動意レポートを 1 本の PDF へ統一したため、市場区切りの h1 が
   本文中に残る（先頭 1 本だけがマストヘッドへ昇格）。
   PM 2026-09-04 指示: 文言を `# グロース市場` 等（市場名のみ・1 行）へ変更した。
   旧 `# 動意銘柄レポート YYYY-MM-DD（グロース）` は「レポート題名」に見えるうえ
   帯の中で 2 行に折れて読みにくかった。white-space:nowrap で 1 行に固定する。
   PM 2026-09-05 指示: 見出しへの背景塗り帯・白抜き文字・装飾バーを全面廃止し、
   素のタイトル表記（太字・サイズ・文字色のみ）へ戻す（_cr §41）。
   強制改ページ（break-before:page）は掛けない（_cr §39 の 1/3 空白禁止に抵触するため）。 */
h1 {{
  font-family:{serif}; font-weight:700; font-size:22pt; color:#1A1A1A;
  margin:32px 0 15px; padding:0;
  line-height:1.25; letter-spacing:.04em; text-align:left;
  white-space:nowrap; overflow:hidden;
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
/* ── h5 = テーマ内の小見出し（「何が起きたか」等）。色付きでインライン太字と区別する。
   PM 2026-09-05 指示: 見出しの背景塗り・装飾バーは全面禁止（_cr §41）。 ── */
h5 {{
  font-size:11pt; font-weight:700; color:{accent};
  margin:17px 0 6px; padding:0;
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

/* ── 列数に応じた自動縮小（PM 2026-09-06 指示・折り返しの予防）──
   table-layout:fixed は列幅を均等割りするため、列数が増えるほど 1 列の幅が狭まる。
   本文幅 612px の場合、5 列で約 122px・8 列で約 76px となり、10.5pt の和文では
   8 列の表の銘柄名（「7532 パン・パシフィック…」等）が 1 文字ずつ縦に折り返した
   （週次大型株 2026-09-05 実測）。列数はレンダリング前に markdown から数えられるため、
   JS の実測を待たずに CSS 側で先に文字を縮めて折り返しの発生自体を減らす。
   カード自動変換（_TABLE_CARDIFY_JS）は最後の受け皿として残し、本規則はその手前で効く。
   フォント縮小を選んだ理由: 列ごとの width 指定は表の意味を知らないと決められず
   全レポート共通には書けないが、列数による一律縮小は種別非依存で安全に効くため。 */
table.cols-5 {{ font-size:9.5pt; }}
table.cols-6 {{ font-size:9pt; }}
table.cols-7 {{ font-size:8.5pt; }}
table.cols-8plus {{ font-size:8pt; }}
table.cols-5 thead th, table.cols-6 thead th,
table.cols-7 thead th, table.cols-8plus thead th {{ font-size:inherit; padding:6px 7px; }}
table.cols-5 tbody td, table.cols-6 tbody td,
table.cols-7 tbody td, table.cols-8plus tbody td {{ padding:5px 6px; }}
/* 横あふれの保険（HTML 表示用。PDF では効かないため列数制限とカード変換で担保する）*/
.table-scroll {{ overflow-x:auto; }}
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

/* ── 個別銘柄レポート §7 大株主表・§8 需給分析表の列幅（PM 2026-09-07 承認）──
   table-layout:fixed の均等 4 分割では 1 列 153px（内容幅 133px ≒ 全角 9 字）しか取れず、
   「取締役会長（代表取締役）」（12 字）「信用買残÷5日平均出来高」（12 字）等が必ず 2 行へ
   折り返していた（2026-09-06 実測。個別銘柄レポート 23 本中 15 本で発生）。
   どちらの表も「短い数値列 + 長い説明列」という偏りを持つため、偏りに合わせて配分する。
   本 CSS の % を変えたら bi/pipelines/lib/table_rules.py の _WIDE_COL_TABLES も
   同時に変えること（片方だけの変更を禁止する）。 */
/* §7 大株主表（3 列版）: 株主名 / 保有比率 / 会社との関係。
   前期末比を置かない誌面も多いため、均等 3 分割（各 204px）をやめて
   数値列を狭め、関係列へ配分する。 */
table.shareholders3 th:nth-child(1), table.shareholders3 td:nth-child(1) {{ width:34%; }}
table.shareholders3 th:nth-child(2), table.shareholders3 td:nth-child(2) {{ width:16%; }}
table.shareholders3 th:nth-child(3), table.shareholders3 td:nth-child(3) {{ width:50%; }}
/* §7 大株主表（4 列版）: 株主名 / 保有比率 / 前期末比 / 会社との関係 */
table.shareholders th:nth-child(1), table.shareholders td:nth-child(1) {{ width:26%; }}
table.shareholders th:nth-child(2), table.shareholders td:nth-child(2) {{ width:15%; }}
table.shareholders th:nth-child(3), table.shareholders td:nth-child(3) {{ width:15%; }}
table.shareholders th:nth-child(4), table.shareholders td:nth-child(4) {{ width:44%; }}
/* §8 需給分析の統合テーブル: 軸 / 指標 / 現状 / 評価基準・判定
   §8 は 1 テーブル集約が必須で列を減らせないため、列幅で解決する。 */
table.demand th:nth-child(1), table.demand td:nth-child(1) {{ width:20%; }}
table.demand th:nth-child(2), table.demand td:nth-child(2) {{ width:27%; }}
table.demand th:nth-child(3), table.demand td:nth-child(3) {{ width:24%; }}
table.demand th:nth-child(4), table.demand td:nth-child(4) {{ width:29%; }}

/* ── テーマ系銘柄表の共通列幅（v18・2026-09-03 PM 指示）──
   PM 却下事項: (1) 時価総額列が無い (2) 本日のテーマ表と単独材料表で列幅・見た目が
   揃っていない (3) セル内で「コード」が「コー／ド」に、「テクセンドフォトマスク」
   「ニトリホールディングス」のような銘柄名が途中で折れる。
   対策: 本日のテーマ・直近2週間・初動候補の主導銘柄表（5列）と単独材料表（6列）を
   同一の列構成・列幅に統一する。コード・銘柄名・時価総額・騰落率・見出し行は
   white-space:nowrap にして1文字ずつの折り返しを根絶する。折り返し可は
   「何の会社」「材料」列のみ（_cr §39 は表の折り返し自体を禁止していないため、
   説明文列の折り返しは許容する）。
   列: コード / 銘柄名 / 何の会社 / 時価総額 / 騰落率（5列＝theme-lead）
       コード / 銘柄名 / 何の会社 / 時価総額 / 騰落率 / 材料（6列＝theme-solo）
   銘柄名は「ダイナミックマッププラットフォーム」（16字）・「テクセンドフォトマスク」
   （11字）・「ニトリホールディングス」（11字）が nowrap で1行に収まる幅を実測確保。
   本文幅 612px 時、8.5pt・銘柄名 30% ≈ 26字相当の枠を確保すれば全銘柄名が収まる。 */
table.theme-lead, table.theme-solo {{ margin:6px 0 15px; font-size:8.5pt; }}
/* 列幅（2026-09-03 PM 再指示・実測反映）: PM 指定は コード7%／銘柄名27%／時価総額10%／
   騰落率9%だったが、銘柄名27%（≈165px）は「ダイナミックマッププラットフォーム」16字を
   8.5ptで収めるには足りない（実測200px要・27%では6.0ptまで縮小しないと収まらず可読性が
   崩れる）。「この銘柄名は折れないことを維持」の指示を優先し、銘柄名だけ実測必須幅の35%を
   確保し、コード・時価総額・騰落率はPM指定どおり7%/10%/9%を維持する。差分は「何の会社」
   「材料」に回す。 */
table.theme-lead th:nth-child(1), table.theme-lead td:nth-child(1),
table.theme-solo th:nth-child(1), table.theme-solo td:nth-child(1) {{ width:7%; }}
table.theme-lead th:nth-child(2), table.theme-lead td:nth-child(2),
table.theme-solo th:nth-child(2), table.theme-solo td:nth-child(2) {{ width:35%; }}
table.theme-lead th:nth-child(4), table.theme-lead td:nth-child(4),
table.theme-solo th:nth-child(4), table.theme-solo td:nth-child(4) {{ width:10%; }}
table.theme-lead th:nth-child(5), table.theme-lead td:nth-child(5),
table.theme-solo th:nth-child(5), table.theme-solo td:nth-child(5) {{ width:9%; }}
/* 5列表（主導銘柄）: 何の会社が残り39%を丸ごと取る */
table.theme-lead th:nth-child(3), table.theme-lead td:nth-child(3) {{ width:39%; }}
/* 6列表（単独材料）: コード/銘柄名/時価総額/騰落率は5列表と同じ幅を維持し、
   残り39%を「何の会社」19%・「材料」20%へ分ける（PM 2026-09-03 再指示: 何の会社12字
   以内・材料24字以内で1〜2行に収める運用とセットで、この幅で2行以内に収まることを実測済み） */
table.theme-solo th:nth-child(3), table.theme-solo td:nth-child(3) {{ width:19%; }}
table.theme-solo th:nth-child(6), table.theme-solo td:nth-child(6) {{ width:20%; }}
/* コード・銘柄名・時価総額・騰落率・見出しは1文字改行を根絶（nowrap）。
   折り返し可は「何の会社」「材料」列のみ。 */
table.theme-lead th, table.theme-lead td:nth-child(1), table.theme-lead td:nth-child(2),
table.theme-lead td:nth-child(4), table.theme-lead td:nth-child(5),
table.theme-solo th, table.theme-solo td:nth-child(1), table.theme-solo td:nth-child(2),
table.theme-solo td:nth-child(4), table.theme-solo td:nth-child(5) {{
  white-space:nowrap;
}}
table.theme-lead td:nth-child(3), table.theme-solo td:nth-child(3),
table.theme-solo td:nth-child(6) {{
  white-space:normal; word-break:normal; overflow-wrap:break-word;
}}
table.theme-lead td:nth-child(4), table.theme-lead thead th:nth-child(4),
table.theme-lead td:nth-child(5), table.theme-lead thead th:nth-child(5),
table.theme-solo td:nth-child(4), table.theme-solo thead th:nth-child(4),
table.theme-solo td:nth-child(5), table.theme-solo thead th:nth-child(5) {{
  text-align:right; font-variant-numeric:tabular-nums;
}}
table.theme-lead tbody td, table.theme-solo tbody td {{ padding:5px 7px; line-height:1.45; }}
table.theme-lead thead th, table.theme-solo thead th {{ padding:5px 7px; font-size:8pt; }}

/* テーマ見出し行（**テーマ名**｜局面 …）は h5 へ昇格されるため、表と密に接する余白にする */
h5 + table.theme-lead, h5 + p + table.theme-lead {{ margin-top:4px; }}
/* テーマブロック（見出し+理由文+主導銘柄表）を包む単位。余白は内側の要素が持つため
   div 自身は余白を持たず、ブロック末尾の表の下マージンだけをブロック間の間隔にする。 */
.theme-block {{ margin:0; }}
.theme-block > h5:first-child {{ margin-top:14px; }}
.theme-block > table.theme-lead:last-child {{ margin-bottom:15px; }}

/* ── 折り返し表のカード変換（2026-09-07 廃止・定義のみ残置）──
   【廃止】PM はカード形式を承認していないため、カード自動変換の呼び出しを止めた
   （PM 2026-09-07）。表は常に表として出力する。本 CSS と _TABLE_CARDIFY_JS は
   不可逆な削除を避けて定義のまま残してあるが、実行経路からは外れており適用されない。
   以下は廃止前の説明である。
   セルが折り返す表は誌面として読めないため、レンダラが折り返しを実測検知して
   表を「1行=1カード」形式へ自動変換する（実装は _TABLE_CARDIFY_JS）。
   配色・フォントは既存テーマ（thead の #1A2A44・罫線 #E3E8EF・縞 #F6F8FB）に合わせる。
   カードは 1 枚ずつが独立ブロックのため、折り返しても列幅の圧迫が起きず読める。 */
.md-cards {{ margin:13px 0 17px; }}
.md-card {{
  border:0.6pt solid #DBE1E9; border-left:2.4pt solid {accent};
  background:#FFFFFF; border-radius:3px;
  padding:8px 12px 7px; margin:0 0 7px;
  font-size:10.5pt; line-height:1.6; font-variant-numeric:tabular-nums;
}}
.md-card:nth-child(even) {{ background:#F6F8FB; }}
.md-card .md-card-head {{
  font-weight:700; color:#1A2A44; font-size:11pt; line-height:1.5;
  margin:0 0 4px; padding:0 0 4px; border-bottom:0.6pt solid #E3E8EF;
  letter-spacing:.02em;
}}
.md-card .md-card-row {{ margin:0; padding:1px 0; line-height:1.6; }}
.md-card .md-card-key {{
  color:#6B7686; font-weight:700; font-size:9.5pt; letter-spacing:.02em;
  white-space:nowrap;
}}
.md-card .md-card-key::after {{ content:"："; color:#9AA3B0; font-weight:400; }}
.md-card .md-card-val {{ color:#222222; }}

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
  table.theme-solo {{ break-inside:auto; }}
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
  /* カードは 1 枚が小さいため塊送りしても大空白を生まない（_cr §39 の 1/3 基準内）。
     カード束全体は分割可のまま、1 枚のカードだけ分断させない。 */
  .md-cards {{ break-inside:auto; }}
  .md-card {{ break-inside:avoid; }}
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
    # 早期リターン。テーマ2部を持たない誌面（個別銘柄レポート等）では theme-lead 表が
    # 1つも無く、本ラッパは何も包まない。にもかかわらず正規表現は全文を走査するため、
    # 掛からないことの確認に指数時間を費やしていた（下記参照）。先に安価な部分文字列
    # 検査で抜ける（2026-09-05）。
    if '<table class="theme-lead">' not in html:
        return html

    _HEAD = r"(?:<h5[^>]*>.*?</h5>|<p><strong>(?:(?!</p>).)*?</strong>(?:(?!</p>).)*?</p>)"
    # 中間の理由段落群は `.*?`（DOTALL）ではなく `(?:(?!</p>).)*?` で書く。
    # `.*?` は DOTALL 下で `</p>` を跨げるため、N 個連続する段落を「1段落あたり
    # 区切るか跨ぐか」で分割する組み合わせが 2^N 通り生まれ、末尾の theme-lead 表が
    # 見つからない位置ではその全パターンを試し尽くしてから失敗する（catastrophic
    # backtracking）。実測で 150行=2.4秒 / 170行=19秒 / 180行=38秒 と約10行ごとに
    # 倍増し、12ページ級で事実上ハングしていた（2026-09-05 実測）。段落境界を跨げなく
    # すると分割は一意に定まり線形時間になる。誌面上の意味（見出しと表の間に挟まる
    # 理由段落の 0 個以上の連なり）は変わらない。
    pattern = re.compile(
        r"(" + _HEAD + r")\s*"
        r"((?:<p>(?!<strong>)(?:(?!</p>).)*?</p>\s*)*?)"
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

    v18（2026-09-03 PM 指示）: 全テーマ系の銘柄表を5列（コード/銘柄名/何の会社/時価総額/
    騰落率）に統一し、単独材料表のみ6列（同5列+材料）にする。旧7列の初動候補テーマ表
    （テーマ名/当日順位/上昇銘柄数/売買代金合計/局面/点灯銘柄/材料）は「全セルが折り返し
    で縦長になり読めない」と却下されたため判定・CSS とも廃止した（初動候補欄は本日の
    テーマ・直近2週間と同じ「見出し行＋材料1文＋5列表」のブロック形式へ作り直し済み）。

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
        # v18: 主導銘柄の5列表（コード/銘柄名/何の会社/時価総額/騰落率）。
        # 本日のテーマ・直近2週間・初動候補テーマの全ブロックがこの表を使う。
        if (n_cols == 5 and "コード" in head and "何の会社" in head
                and "時価総額" in head and "材料" not in head):
            return table_html.replace("<table>", '<table class="theme-lead">', 1)
        # v18: 単独材料の6列表（コード/銘柄名/何の会社/時価総額/騰落率/材料）。
        # クラスが付かないと table-layout:fixed の列幅指定が当たらず、材料列が伸びて
        # 銘柄名列が潰れる（_cr §39 のスカスカ禁止に抵触する）。
        if n_cols == 6 and "コード" in head and "何の会社" in head and "材料" in head:
            return table_html.replace("<table>", '<table class="theme-solo">', 1)
        # 旧形式（互換のため判定を残す。過去 raw の再レンダリング用）:
        #   「本日のテーマ」＝ テーマ/主導銘柄/動いた理由 の3列 → `theme-today`
        #   「直近2週間」＝ テーマ/局面/熱量/前2週比/主導銘柄/動いた理由 の6列 → `theme-heat`
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


# ── 折り返し表のカード変換（2026-09-07 廃止・定義のみ残置）────────────────────
# 【廃止】本 JS は render_markdown_to_pdf() から呼ばれない（PM 2026-09-07）。
# PM がカード形式を承認していないため、レンダラは表を常に表として出力する。
# 不可逆な削除を避けて定義のまま残す。以下は廃止前の説明である。
# set_content 後に page.evaluate で実行する。DOM を実測して折り返しを検知するため、
# markdown 段階では判定できなかった「実際に折れている表」を確実に拾える。
#
# 検知条件（いずれか）:
#   (1) いずれかの td/th の描画高さが computed line-height の 1.6 倍を超える
#       → そのセルは 2 行以上に折り返している
#   (2) table の scrollWidth が親コンテナ幅を超える → 横あふれ
#
# 変換後の形（1 行 = 1 カード）:
#   <div class="md-cards"><div class="md-card">
#     <div class="md-card-head">{先頭セル}</div>
#     <div class="md-card-row"><span class="md-card-key">{ヘッダ名}</span>
#       <span class="md-card-val">{値}</span></div> …
#   </div>…</div>
#
# theme-lead / theme-solo / theme-today / theme-heat も対象に含める（PM 指示）。
# 列数に応じたフォント縮小クラスを付与する（PM 2026-09-06）。
# カード変換（_TABLE_CARDIFY_JS）の「実測して折れていたら変換する」判定より前に走らせ、
# そもそも折れない誌面幅へ収める。列数が多い表ほど 1 列が狭くなるため段階的に縮める。
_TABLE_COLS_CLASS_JS = r"""
() => {
  let n = 0;
  for (const t of document.querySelectorAll('table')) {
    const hrow = t.querySelector('thead tr') || t.querySelector('tr');
    if (!hrow) continue;
    const c = hrow.children.length;
    let cls = null;
    if (c >= 8) cls = 'cols-8plus';
    else if (c === 7) cls = 'cols-7';
    else if (c === 6) cls = 'cols-6';
    else if (c === 5) cls = 'cols-5';
    if (cls) { t.classList.add(cls); n++; }
    // 列幅を明示指定する表へクラスを付ける（ヘッダ名で判別する。PM 2026-09-07）。
    // 見出し文字列ではなく表自身のヘッダで判別するため、誌面の見出し表記が変わっても効く。
    // 条件を満たさない一般の表には一切影響しない。
    const hs = Array.from(hrow.children).map((x) => (x.textContent || '').trim());
    // §7 大株主表。前期末比列の有無で 4 列版・3 列版の両方が使われる。
    if (hs[0] === '株主名' && hs.indexOf('会社との関係') >= 0) {
      if (c === 4) t.classList.add('shareholders');
      else if (c === 3) t.classList.add('shareholders3');
    }
    // §8 需給分析の統合テーブル: 軸 / 指標 / 現状 / 評価基準 / 判定
    if (c === 4 && hs[0] === '軸' && hs[1] === '指標' && hs[2] === '現状') {
      t.classList.add('demand');
    }
  }
  return n;
}
"""

_TABLE_CARDIFY_JS = r"""
() => {
  const WRAP_FACTOR = 1.6;   // 行高の何倍を超えたら「折り返している」とみなすか
  const SLACK_PX = 2;        // 横あふれ判定のゆとり

  // 数値・記号のみのセル（業績推移表・比較表）。列数・折り返しの制限を受けない。
  // table_rules.py の _NUMERIC_CELL と同じ趣旨（両者を揃えて運用する）。
  const NUMERIC = /^[\s0-9,.+\-±%％〜～~/（）()円株倍日年月期件回名口万億兆千百人時分秒中間予想末初pt―ー—–−]*$/;

  const lineHeightOf = (el) => {
    const cs = getComputedStyle(el);
    let lh = parseFloat(cs.lineHeight);
    if (!isFinite(lh) || lh <= 0) {
      const fs = parseFloat(cs.fontSize) || 12;
      lh = fs * 1.2;
    }
    return lh;
  };

  // セルの中身を display:block の span で包み、その span の高さで折り返しを測る。
  // td は vertical-align:middle かつ行の最大高に揃うため、td 自身の矩形では
  // 「そのセルが折れているか」を判定できない（実測でどのセルも同じ高さになる）。
  const wrapRatio = (c) => {
    const sp = document.createElement('span');
    sp.style.display = 'block';
    while (c.firstChild) sp.appendChild(c.firstChild);
    c.appendChild(sp);
    const r = sp.getBoundingClientRect().height / lineHeightOf(c);
    // 包んだ span はそのまま残す（display:block でも見た目は変わらない）。
    return r;
  };

  const bodyRowsOf = (table) => {
    const tb = table.querySelector('tbody');
    return tb ? Array.from(tb.rows) : Array.from(table.rows).slice(1);
  };

  // ラベル列（1 列目）を除く本文セルが全て数値・記号のみなら数値表として除外する。
  const isNumericTable = (table) => {
    const cells = [];
    for (const tr of bodyRowsOf(table)) {
      for (let i = 1; i < tr.cells.length; i++) {
        cells.push((tr.cells[i].textContent || '').trim());
      }
    }
    if (!cells.length) return false;
    return cells.every((t) => !t || NUMERIC.test(t));
  };

  const isWrapped = (table) => {
    if (isNumericTable(table)) return false;
    // (2) 横あふれ
    const parentW = table.parentElement
      ? table.parentElement.clientWidth
      : table.clientWidth;
    if (table.scrollWidth > (parentW || table.clientWidth) + SLACK_PX) return true;
    // (1) セルの折り返し。ラベル列（1 列目）の折り返しは許容する
    //     （「営業キャッシュフロー」等の項目名が 2 行になるのは読みやすさを損なわない）。
    for (const tr of bodyRowsOf(table)) {
      for (let i = 1; i < tr.cells.length; i++) {
        const c = tr.cells[i];
        if (!(c.textContent || '').trim()) continue;
        if (wrapRatio(c) > WRAP_FACTOR) return true;
      }
    }
    return false;
  };

  const headerNames = (table) => {
    const hrow = table.querySelector('thead tr') || table.querySelector('tr');
    if (!hrow) return [];
    return Array.from(hrow.children).map((c) => (c.textContent || '').trim());
  };

  const cardify = (table) => {
    const heads = headerNames(table);
    const rows = bodyRowsOf(table);
    if (!rows.length) return false;

    const wrap = document.createElement('div');
    wrap.className = 'md-cards';

    for (const tr of rows) {
      const cells = Array.from(tr.cells);
      if (!cells.length) continue;
      const card = document.createElement('div');
      card.className = 'md-card';

      // 先頭セルを太字見出しにする。
      const head = document.createElement('div');
      head.className = 'md-card-head';
      head.innerHTML = cells[0].innerHTML;
      card.appendChild(head);

      // 残りのセルを「ヘッダ名: 値」の 1 行ずつで表示する。
      for (let i = 1; i < cells.length; i++) {
        const val = (cells[i].textContent || '').trim();
        if (!val) continue;
        const row = document.createElement('div');
        row.className = 'md-card-row';
        const k = document.createElement('span');
        k.className = 'md-card-key';
        k.textContent = heads[i] !== undefined ? heads[i] : '';
        const v = document.createElement('span');
        v.className = 'md-card-val';
        v.innerHTML = cells[i].innerHTML;
        if (k.textContent) row.appendChild(k);
        row.appendChild(v);
        card.appendChild(row);
      }
      wrap.appendChild(card);
    }

    table.parentNode.replaceChild(wrap, table);
    return true;
  };

  let converted = 0;
  // 判定中に DOM を書き換えると後続の実測がずれるため、先に対象を確定させる。
  const targets = Array.from(document.querySelectorAll('table')).filter(isWrapped);
  for (const t of targets) {
    if (cardify(t)) converted++;
  }
  return converted;
}
"""


# ── 誌面の実測検査（PM 2026-09-07）───────────────────────────
# PDF へ落とす直前の DOM を実測し、本文セルが 2 行以上へ折り返していないかを見る。
# テキスト段階の字数検査（table_rules.py）は「全角 1 字 = 14px」の推定のため、
# 数字・半角記号が多いセルを過小評価し、和文の多いセルを過大評価する。
# 本検査は実際の誌面を測るため、字数の見積もりが将来ずれても必ず折り返しを捕まえる。
# ラベル列（1 列目）の折り返しは旧カード判定と同じく許容する。
_LAYOUT_AUDIT_JS = r"""
() => {
  const WRAP_FACTOR = 1.6;
  const NUMERIC = /^[\s0-9,.+\-±%％〜～~/（）()円株倍日年月期件回名口万億兆千百人時分秒中間予想末初pt―ー—–−]*$/;
  const lineHeightOf = (el) => {
    const cs = getComputedStyle(el);
    let lh = parseFloat(cs.lineHeight);
    if (!isFinite(lh) || lh <= 0) lh = (parseFloat(cs.fontSize) || 12) * 1.2;
    return lh;
  };
  const bodyRowsOf = (t) => {
    const tb = t.querySelector('tbody');
    return tb ? Array.from(tb.rows) : Array.from(t.rows).slice(1);
  };
  const wrapped = [];
  const tables = Array.from(document.querySelectorAll('table'));
  for (const t of tables) {
    const hrow = t.querySelector('thead tr') || t.querySelector('tr');
    const heads = hrow
      ? Array.from(hrow.children).map((c) => (c.textContent || '').trim())
      : [];
    for (const tr of bodyRowsOf(t)) {
      for (let i = 1; i < tr.cells.length; i++) {
        const c = tr.cells[i];
        const txt = (c.textContent || '').trim();
        if (!txt) continue;
        const sp = document.createElement('span');
        sp.style.display = 'block';
        while (c.firstChild) sp.appendChild(c.firstChild);
        c.appendChild(sp);
        const r = sp.getBoundingClientRect().height / lineHeightOf(c);
        if (r > WRAP_FACTOR) {
          wrapped.push({
            table: heads[0] || '',
            col: heads[i] || String(i),
            text: txt.slice(0, 40),
            lines: Math.round(r * 100) / 100,
            numeric: NUMERIC.test(txt),
          });
        }
      }
    }
  }
  return { n_tables: tables.length, wrapped: wrapped };
}
"""


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
    # 初動候補テーマの見出し行 `**1位 …継続11日目**　（本日のテーマ欄にも掲載）`（v18）。
    # 閉じ `**` の直後に全角スペース+注記が続くだけで `｜` を伴わないため上の
    # _bold_head_meta では拾えず、行全体も太字ではないため _bold_only にも掛からない。
    # そのため 1位/3位/5位（継続テーマ＝注記あり）だけ h5 昇格から漏れ、7位/8位（新規
    # テーマ＝注記なし・_bold_only で昇格）とオレンジ帯装飾が食い違っていた
    # （2026-09-03 実測）。先頭を `**N位 …**` に限定する（`**何の会社**：…`・
    # `**なぜ動いた**：…` 等の本文中の太字プレフィックス段落を誤って見出し化しないため）。
    _bold_head_note = re.compile(r"^\*\*(\d+位[^*]+)\*\*(\s*\S.+)$")
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
        m4 = _bold_head_note.match(stripped)
        if m4:
            promoted.append(f"##### {m4.group(1).strip()}{m4.group(2).rstrip()}")
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
        # 折り返しの実測には「印刷時の本文幅」でレイアウトさせる必要がある。既定ビューポート
        # （1280px）のまま測ると A4 では折れている表が折れていないと判定される（実測: 新株
        # 予約権の 5 列表・需給の 4 列表が 1280px では 1 行に収まり検知漏れした）。
        # A4 幅 210mm − 左右マージン 24mm×2 = 162mm ≒ 612px（96dpi）を本文幅として与える。
        page = browser.new_page(viewport={"width": 612, "height": 900})
        # 明示タイムアウト（120秒）。無言のまま CPU を回し続ける事故を防ぎ、上限を
        # 超えたら例外で落とす（2026-09-05）。`page.pdf()` は本バージョンの Playwright
        # では timeout 引数を受け取らないため、ページ既定のタイムアウトで掛ける。
        page.set_default_timeout(120_000)
        page.set_content(full_html, wait_until="load", timeout=120_000)
        # 列数に応じたフォント縮小クラスと、列幅を明示指定する表のクラスを付与する。
        # 失敗しても誌面生成は止めない（_cr §36 配信絶対の原則）。
        try:
            scaled = int(page.evaluate(_TABLE_COLS_CLASS_JS) or 0)
            if scaled:
                print(
                    f"[md_to_pdf] col-scaled tables: {scaled}（5列以上の表を段階的に縮小）",
                    file=sys.stderr,
                )
        except Exception as e:  # noqa: BLE001
            print(f"[md_to_pdf] col-class skipped: {e}", file=sys.stderr)
        # カード自動変換は廃止した（PM 2026-09-07）。表は常に表として出力する。
        # PM はカード形式（1行=1ブロック・先頭セルを見出し・残りを「ヘッダ名: 値」）を
        # 承認しておらず、レンダラが誌面の形式を承認なく書き換えることを禁止する。
        # 折り返す表は執筆側の規律違反であり、テキスト段階のゲート
        # （bi/pipelines/lib/table_rules.py の check_tables()）が error で送信を止める。
        # 折り返しの発生自体は §7・§8 の列幅明示指定と、実容量へ是正した字数上限で潰す。
        # _TABLE_CARDIFY_JS と .md-cards / .md-card の CSS は復帰を容易にするため
        # 定義のまま残し、呼び出しのみを止める（不可逆な削除をしない）。
        cardified = 0
        print(
            f"[md_to_pdf] cardified tables: {cardified}"
            "（カード自動変換は廃止・表は常に表として出力する）",
            file=sys.stderr,
        )
        render_markdown_to_pdf.last_cardified = cardified
        # 誌面の実測検査（PM 2026-09-07）。page.pdf() の直前に DOM を測り、
        # 2 行以上へ折り返した本文セルを last_layout_report へ格納する。
        # 呼び出し元（send_report_pdf_discord.py）がこれを読み、個別銘柄レポートは
        # 1 件でも折り返しがあれば送信を止め、定時発行は _cr §36 により警告に留める。
        # 測定は DOM を書き換える（span で包む）が見た目は変わらない。
        try:
            layout = page.evaluate(_LAYOUT_AUDIT_JS) or {}
        except Exception as e:  # noqa: BLE001
            layout = {"error": str(e), "n_tables": 0, "wrapped": []}
            print(f"[md_to_pdf] layout audit skipped: {e}", file=sys.stderr)
        render_markdown_to_pdf.last_layout_report = layout
        n_wrapped = len(layout.get("wrapped") or [])
        print(
            f"[md_to_pdf] layout audit: tables={layout.get('n_tables', 0)} "
            f"wrapped_cells={n_wrapped}",
            file=sys.stderr,
        )
        for w in (layout.get("wrapped") or [])[:8]:
            print(
                f"[md_to_pdf]   折り返し: 表「{w.get('table')}」列「{w.get('col')}」 "
                f"{w.get('lines')}行 「{w.get('text')}」",
                file=sys.stderr,
            )
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


# 直近レンダリングでカード変換した表の数（呼び出し元が参照できるようにする）。
render_markdown_to_pdf.last_cardified = 0
# 直近レンダリングの誌面実測結果（PM 2026-09-07）。
# {"n_tables": int, "wrapped": [{"table","col","text","lines","numeric"}, ...]}
render_markdown_to_pdf.last_layout_report = {"n_tables": 0, "wrapped": []}
