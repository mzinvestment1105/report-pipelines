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
    """
    return f"""
* {{ box-sizing:border-box; }}
html {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
body {{
  font-family:{sans};
  font-size:12pt; line-height:1.8; color:#222222;
  margin:0; padding:0;
  letter-spacing:.04em; font-feature-settings:"palt" 1;
  text-align:justify; word-break:normal; line-break:strict; overflow-wrap:break-word;
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
strong, b {{ color:#1A1A1A; font-weight:700; }}

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
li {{ margin:4px 0; padding-left:3px; text-align:justify; }}
li::marker {{ color:{accent}; }}

/* ── 表（金融レポート調・大きい表はページ分割を許可）── */
table {{
  border-collapse:collapse; width:100%; margin:13px 0 17px;
  font-size:10pt; line-height:1.6; font-variant-numeric:tabular-nums;
  text-align:left;
}}
thead th {{
  background:#1A2A44; color:#FFFFFF; font-weight:700; font-size:9.6pt;
  text-align:left; padding:8px 11px; letter-spacing:.02em;
  white-space:nowrap;
}}
tbody td {{ padding:7px 11px; border-bottom:0.6pt solid #E3E8EF; vertical-align:top; }}
tbody tr:nth-child(even) td {{ background:#F6F8FB; }}
tbody tr:last-child td {{ border-bottom:1pt solid #C8D1DD; }}

code {{
  background:#EEF1F5; padding:1px 5px; border-radius:3px; color:#B5483D;
  font-family:'Consolas','Courier New',monospace; font-size:10pt;
}}
hr {{ border:0; border-top:0.8pt solid #DBE1E9; margin:20px 0; }}

/* ── 改ページ制御（1ページ目の谷間＝妙な空白を根絶）──
   鉄則: 1ページより高くなり得るブロックには break-inside:avoid を付けない。
   大きい表・リードは分割を許可し、見出しは後続本文と連結する。 */
@media print {{
  table {{ break-inside:auto; }}
  thead {{ display:table-header-group; }}
  tr, img {{ break-inside:avoid; }}
  h2, h3, h4, h5 {{ break-after:avoid; break-inside:avoid; }}
  p, li {{ orphans:2; widows:2; }}
  blockquote {{ break-inside:auto; }}
  .masthead {{ break-inside:avoid; break-after:avoid; }}
  body > :first-child {{ margin-top:0; }}
}}
"""


_POS = "#1F8A4C"  # 上昇=緑
_NEG = "#C0392B"  # 下落=赤
# 符号付き数値（+12.3% / −4.0% / +1.39 / -3.03 / 全角＋％対応）。誤着色を抑えるため
# 「% を伴う」か「小数点を含む」場合のみ着色し、符号付き整数（銘柄コード・年号・順位）は除外する。
_RE_SIGNED = re.compile(r"([+＋−\-])(\d[\d,]*(?:\.\d+)?)(\s*[%％])?")


def _colorize_signed(m: "re.Match[str]") -> str:
    sign, num, pct = m.group(1), m.group(2), m.group(3) or ""
    if not pct and "." not in num:  # 符号付き整数（コード/年/順位）は着色しない
        return m.group(0)
    color = _NEG if sign in "−-" else _POS
    return f'<span style="color:{color};font-weight:700">{sign}{num}{pct}</span>'


def _colorize_numbers(html: str) -> str:
    """生成 HTML 中の騰落率・トレンド矢印を上昇=緑/下落=赤で確定着色する（renderer 側で保証・LLM 非依存）。

    金融レポートの一目可読性のため、本文・表セル内の +X%/−X%・符号付きリターンと 8 週トレンド帯の
    ▲▼ を色分けする。着色は render 後の HTML に対して行い、タグ属性へ符号付き数値は出ないため安全。
    """
    html = _RE_SIGNED.sub(_colorize_signed, html)
    html = (html.replace("▲", f'<span style="color:{_POS}">▲</span>')
                .replace("△", f'<span style="color:{_POS}">△</span>')
                .replace("▼", f'<span style="color:{_NEG}">▼</span>')
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
    promoted = []
    for ln in lines:
        m2 = _bold_only.match(ln.strip())
        promoted.append(f"##### {m2.group(1).strip()}" if m2 else ln)
    lines = promoted

    body_md = "\n".join(lines).strip()
    # 内部メタ表現の確定除去（PM 2026-06-27・LLM が本文に書いても renderer 側で必ず削除する）
    body_md = re.sub(r"（記事ベース[^）]*）", "", body_md)
    body_md = _inject_size_tags(body_md)

    html_body = md.markdown(body_md, extensions=["tables", "fenced_code", "sane_lists"])
    html_body = _colorize_numbers(html_body)

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
