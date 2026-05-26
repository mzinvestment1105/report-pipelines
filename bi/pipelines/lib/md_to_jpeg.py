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
}


def markdown_to_html(text: str) -> str:
    """Markdown → HTML 変換。h2 にクラス付与（人気/急上昇/Part）。"""
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


def render_markdown_to_jpeg_paged(
    md_text: str,
    out_dir: Path,
    basename: str,
    kind: str = "macro",
    quality: int = 88,
    footer: str | None = None,
    max_page_height: int = 6000,
) -> list[Path]:
    """Markdown を JPEG 複数ページに変換して保存（長尺レポート対応）。

    コンテンツ高さが max_page_height を超える場合は自動的に複数ページ JPEG に分割。
    PM 2026-05-26 確定: 個別銘柄レポート（stock）等の長尺レポートを「前と同じような縦長表示」で
    Discord で正しく表示するために導入。単一の極端な縦長画像（10000px+）は Discord プレビューで
    縦横比保持表示の結果「左寄せ細長」に見えるため、6000px 程度の適切な比率で分割する。

    Args:
        md_text: Markdown 全文
        out_dir: 出力ディレクトリ
        basename: 出力ファイル名のベース（例: stock_280A_2026-05-26）。複数ページの場合は basename_p1.jpg, basename_p2.jpg ... で出力
        kind: レポート種別
        quality: JPEG 品質
        footer: フッター（ブランド表示・None なら非表示）
        max_page_height: 1 ページあたりの最大高さ（px・デフォルト 6000）

    Returns:
        生成された JPEG ファイルパスのリスト（順序保持・1〜N ページ）
    """
    accent = ACCENT_BY_KIND.get(kind, "#FFD700")
    custom_css = CSS.replace("border-bottom: 5px solid #FFD700;", f"border-bottom: 5px solid {accent};")

    html_body = markdown_to_html(md_text)
    footer_html = f'<div class="footer-brand">{footer}</div>' if footer else ""
    full_html = f"""<!DOCTYPE html>
<html lang=\"ja\">
<head>
<meta charset=\"utf-8\" />
<style>{custom_css}</style>
</head>
<body>
{html_body}
{footer_html}
</body>
</html>"""

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        page = ctx.new_page()
        page.set_content(full_html, wait_until="load")
        total_height = page.evaluate("document.body.scrollHeight")

        if total_height <= max_page_height:
            out_path = out_dir / f"{basename}.jpg"
            page.screenshot(path=str(out_path), full_page=True, type="jpeg", quality=quality)
            paths.append(out_path)
        else:
            # clip パラメータは viewport ベースなので、コンテンツ全体を viewport に収める必要がある
            page.set_viewport_size({"width": 1080, "height": int(total_height)})
            num_pages = (total_height + max_page_height - 1) // max_page_height
            for i in range(num_pages):
                y = i * max_page_height
                h = min(max_page_height, total_height - y)
                out_path = out_dir / f"{basename}_p{i+1}.jpg"
                page.screenshot(
                    path=str(out_path),
                    clip={"x": 0, "y": y, "width": 1080, "height": h},
                    type="jpeg",
                    quality=quality,
                )
                paths.append(out_path)

        browser.close()

    return paths


def render_markdown_to_jpeg(md_text: str, out_path: Path, kind: str = "macro", quality: int = 88, footer: str | None = None) -> Path:
    """Markdown を JPEG に変換して保存。

    身バレ防止：footer は **デフォルト None**（非表示）。
    Discord 内部送信時のみ呼び出し側で明示的に footer="@noctra_jp / Mizuki Fund" を指定する。
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
    custom_css = CSS.replace("border-bottom: 5px solid #FFD700;", f"border-bottom: 5px solid {accent};")

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
        ctx = browser.new_context(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        page = ctx.new_page()
        page.set_content(full_html, wait_until="load")
        page.screenshot(path=str(out_path), full_page=True, type="jpeg", quality=quality)
        browser.close()
    return out_path
