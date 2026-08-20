"""共通レンダリングモジュール：Markdown → HTML → JPEG（Playwright Chromium）。

全レポート（マクロ / セクター / 動意 / アイデア / 決算 / テーマ / 個別銘柄）が共通利用。
- 日本語フォント Yu Gothic 明示固定（CJK 共通フォントフォールバック防止）
- 容量 8MB 以下（device_scale_factor=1・JPEG quality=88）
- iPhone Discord でプレビュー対応・PC でも保存不要
"""
from __future__ import annotations

import re
from pathlib import Path

import markdown as md
from playwright.sync_api import sync_playwright


CSS = """
* { box-sizing: border-box; }
body {
    font-family: "Yu Gothic", "YuGothic", "Noto Sans CJK JP", "Noto Sans JP", "メイリオ", "Meiryo", sans-serif;
    font-size: 24px;
    line-height: 1.8;
    color: #1a1a1a;
    background: #FFFFFF;
    margin: 0;
    padding: 36px 44px;
    width: 1080px;
}
h1 {
    font-size: 44px;
    color: #0F1419;
    border-bottom: 5px solid #FFD700;
    padding-bottom: 14px;
    margin: 0 0 24px;
}
h2.popular {
    font-size: 32px;
    color: #0F1419;
    background: linear-gradient(90deg, #FFF3B0 0%, #FFFBEA 100%);
    border-left: 10px solid #FFD700;
    padding: 14px 18px;
    margin: 36px 0 16px;
    border-radius: 6px;
}
h2.rise {
    font-size: 32px;
    color: #0F1419;
    background: linear-gradient(90deg, #C2EEF7 0%, #ECF9FC 100%);
    border-left: 10px solid #00BFD4;
    padding: 14px 18px;
    margin: 36px 0 16px;
    border-radius: 6px;
}
h2.part {
    font-size: 36px;
    color: #FFFFFF;
    background: #2C3E50;
    border-left: 10px solid #4A90E2;
    padding: 16px 18px;
    margin: 48px 0 22px;
    border-radius: 6px;
}
h2 {
    font-size: 32px;
    color: #0F1419;
    background: #EEEEEE;
    border-left: 10px solid #888;
    padding: 14px 18px;
    margin: 32px 0 14px;
    border-radius: 6px;
}
h3 {
    font-size: 26px;
    color: #2C3E50;
    margin: 22px 0 10px;
    border-bottom: 2px dotted #888;
    padding-bottom: 6px;
}
h4 { font-size: 24px; color: #2C3E50; margin: 16px 0 8px; }
p { margin: 10px 0 12px; }
strong, b { color: #C0392B; font-weight: bold; }
blockquote {
    background: #F4F4F4;
    border-left: 6px solid #888;
    margin: 14px 0;
    padding: 14px 18px;
    color: #444;
    font-size: 22px;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 14px 0;
    font-size: 22px;
}
table th {
    background: #2C3E50;
    color: #FFFFFF;
    padding: 12px 14px;
    border: 1px solid #1A2532;
    text-align: left;
    font-weight: bold;
}
table td {
    padding: 11px 14px;
    border: 1px solid #D0D0D0;
    background: #FFFFFF;
}
table tr:nth-child(even) td { background: #FAFAFA; }
code {
    background: #EFEFEF;
    padding: 2px 6px;
    border-radius: 3px;
    color: #C0392B;
    font-family: "Consolas", monospace;
    font-size: 22px;
}
hr {
    border: 0;
    border-top: 2px solid #CCCCCC;
    margin: 28px 0;
}
ul, ol { padding-left: 30px; margin: 10px 0 14px; }
li { margin: 5px 0; }
.subhead { font-weight: 700; color: #2C3E50; font-size: 25px; margin: 16px 0 4px; }
.footer-brand {
    margin-top: 40px;
    padding-top: 18px;
    border-top: 1px solid #CCC;
    color: #888;
    font-size: 18px;
    text-align: right;
}
"""

# レポート種別ごとのアクセントカラー（h1 下線色）
ACCENT_BY_KIND = {
    "macro": "#4A90E2",       # 青：マクロ
    "sector": "#27AE60",      # 緑：セクター
    "movers": "#E67E22",      # オレンジ：動意
    "ideas": "#9B59B6",       # 紫：アイデア
    "earnings": "#16A085",    # 青緑：決算
    "themes": "#FFD700",      # 金：テーマ
    "stock": "#C0392B",       # 赤：個別銘柄
    "largecap_weekly": "#1F3A93",  # ネイビー：週次大型株速報
}

# レポート種別ごとのページ幅（px）。テーマは横長 4 列テーブル（動意の理由が長文）が
# 1080px だと詰まって文字の壁になるため広げる。未指定の種別は DEFAULT_PAGE_WIDTH。
PAGE_WIDTH_BY_KIND = {
    "themes": 1440,
}
DEFAULT_PAGE_WIDTH = 1080

# テーマレポートの急上昇 Top10 テーブル専用 CSS（kind=="themes" のみ追加適用）。
# table-layout: fixed + 列幅固定で「動意の理由」(長文) を読める幅に割り当て、
# word-break で日本語長文をセル内で素直に折り返す。順位は中央寄せ。
THEMES_TABLE_CSS = """
table { table-layout: fixed; font-size: 23px; }
table td { word-break: break-word; line-height: 1.7; vertical-align: top; }
table th:nth-child(1), table td:nth-child(1) { width: 5%; text-align: center; }
table th:nth-child(2), table td:nth-child(2) { width: 23%; }
table th:nth-child(3), table td:nth-child(3) { width: 46%; }
table th:nth-child(4), table td:nth-child(4) { width: 26%; }
/* 代表銘柄列：全角ローマ字社名を1文字ずつ分割せず単語単位で折り返す（はみ出す時のみ強制改行） */
table td:nth-child(4) { word-break: keep-all; overflow-wrap: anywhere; }
"""


_EMPTY_LABEL_BULLET_RE = re.compile(r"^\s*-\s+\*\*([^*]+?)\*\*\s*[:：]\s*$")


def _promote_empty_label_bullets(text: str) -> str:
    """中身が直下の子箇条書きにある『空の親ラベル箇条書き』（例: ``- **材料**:`` /
    ``- **資金流入の文脈…**:`` / ``- **需給…**:``）を、空の • 箇条書きではなく
    太字サブ見出しに変換する。子箇条書きは後続でトップレベル list として描画される。
    PM 2026-06-16: 空ラベルの • が並ぶレイアウト崩れ対策。"""
    out: list[str] = []
    for ln in text.split("\n"):
        m = _EMPTY_LABEL_BULLET_RE.match(ln)
        if m:
            out.extend(["", f'<p class="subhead">{m.group(1).strip()}</p>', ""])
        else:
            out.append(ln)
    return "\n".join(out)


# 時価総額サイズ目印（PM 2026-06-30・md_to_pdf と同仕様・LLM 非依存で renderer が付与）:
# 100億以上=無印 / 50〜100億未満=〔小型〕 / 50億未満=〔極小・対象外〕（赤太字）。
_SIZE_MCAP_RE = re.compile(r"時価総額\s*([\d,]+(?:\.\d+)?)\s*(兆|億)円")
_SIZE_PCT_RE = re.compile(r"(\s*[+\-−][\d.]+\s*%\s*)(（)")
_SIZE_NEG = "#C0392B"


def _size_tag(mcap_oku):
    if mcap_oku is None:
        return None
    if mcap_oku <= 50:
        return f'<span style="color:{_SIZE_NEG};font-weight:700">〔極小 {mcap_oku:.0f}億・対象外〕</span>'
    if mcap_oku < 100:
        return f'〔小型 {mcap_oku:.0f}億〕'
    return None


def _inject_size_tags(md_text: str) -> str:
    """個別株見出し行の時価総額からサイズ目印タグを銘柄名直後に挿入（極小は赤太字）。"""
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


def markdown_to_html(text: str) -> str:
    """Markdown → HTML 変換。h2 にクラス付与（人気/急上昇/Part）。"""
    text = _promote_empty_label_bullets(text)
    text = _inject_size_tags(text)
    html = md.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])

    def rewrite_h2(m: re.Match) -> str:
        title = m.group(1)
        if title.startswith("人気#"):
            return f'<h2 class="popular">{title}</h2>'
        if title.startswith("急上昇#"):
            return f'<h2 class="rise">{title}</h2>'
        if title.startswith("Part "):
            return f'<h2 class="part">{title}</h2>'
        return f"<h2>{title}</h2>"

    html = re.sub(r"<h2>(.+?)</h2>", rewrite_h2, html)
    html = re.sub(
        r"<h1>(Part \d+\..+?)</h1>",
        r'<h2 class="part">\1</h2>',
        html,
    )
    return html


def render_markdown_to_jpeg_paged(*args, **kwargs):
    """【削除済関数・呼び出し禁止】PM 2026-05-26 確定: 画像分割は絶対禁止。

    本関数は CLAUDE.md §画像分割絶対禁止 ルールに違反するため、呼び出し時に
    必ず RuntimeError を送出する。将来このスタブを再実装することも禁止。
    """
    raise RuntimeError(
        "render_markdown_to_jpeg_paged は使用禁止です。"
        "CLAUDE.md §画像分割絶対禁止 に従い、render_markdown_to_jpeg（単一ページ）を使用してください。"
    )


def render_markdown_to_jpeg(md_text: str, out_path: Path, kind: str = "macro", quality: int = 88, footer: str | None = None) -> Path:
    """Markdown を JPEG に変換して保存。

    身バレ防止：footer は **デフォルト None**（非表示）。
    Discord 内部送信時のみ呼び出し側で明示的に footer="Market Report" を指定する。
    SNS 用画像生成時の指定忘れによる身バレを構造的に防ぐ設計。

    Args:
        md_text: Markdown 全文
        out_path: 出力 JPEG パス
        kind: レポート種別（macro / sector / movers / ideas / earnings / themes / stock）
        quality: JPEG 品質（88 推奨・容量と画質のバランス）
        footer: 画像下部に表示するブランド文字列。None なら非表示（デフォルト・SNS 安全）

    Returns:
        生成された JPEG ファイルパス
    """
    accent = ACCENT_BY_KIND.get(kind, "#FFD700")
    page_width = PAGE_WIDTH_BY_KIND.get(kind, DEFAULT_PAGE_WIDTH)
    custom_css = CSS.replace("border-bottom: 5px solid #FFD700;", f"border-bottom: 5px solid {accent};")
    custom_css = custom_css.replace("width: 1080px;", f"width: {page_width}px;")
    if kind == "themes":
        custom_css += THEMES_TABLE_CSS

    html_body = markdown_to_html(md_text)
    footer_html = f'<div class="footer-brand">{footer}</div>' if footer else ""
    full_html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<style>{custom_css}</style>
</head>
<body>
{html_body}
{footer_html}
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": page_width, "height": 1920}, device_scale_factor=1)
        page = ctx.new_page()
        page.set_content(full_html, wait_until="load")
        page.screenshot(path=str(out_path), full_page=True, type="jpeg", quality=quality)
        browser.close()
    return out_path
