"""テーマ早見図（動意・夜間PTS 動意の冒頭ブロック用の図解 1 枚）。

2026-10-05 見本（PM 承認済みの冒頭ブロック案）。誌面 md の「## 本日のテーマ」節を
プログラムが解析し、上位テーマ（最大 3 位）ごとに
  テーマ名 / テーマ見出しの括弧書き（副題として括弧ごと表示）/ 主導銘柄の騰落率の横棒
を 1 つの図（HTML/CSS のベクタ描画）にまとめる。モデルが数値を書き写す工程は無い。
括弧書きは材料（「大口受注と需要見通し引き上げ」）の号も業種の列挙（「衣料・外食・家具」）の号も
あるため、「起点の材料：」等の欄名を付けず、見出しと同じ括弧書きのまま副題に出す（2026-10-06）。

■ 動作条件（後方互換）
  誌面 md に目印行 `<!-- THEME_FIGURE -->`（単独の 1 行）がある時だけ働く。
  - 目印なし        : md を一切変更しない（従来と同じ PDF になる）。
  - 目印あり・テーマ節あり : 目印の位置へ図を差し込む。
  - 目印あり・テーマ節なし／主導銘柄表が読めない : 目印を取り除くだけで図は出さない
    （代わりの図は作らない）。
  目印は HTML コメントのため、本モジュールを持たない旧レンダラでも誌面に何も出ない。

■ 呼び出し方（lib/md_to_pdf.py の render_markdown_to_pdf が使う）
  body_md, fig = prepare(body_md)              # markdown 変換の前
  html = markdown(body_md) …（着色・表クラス付与）
  html = inject(html, fig, colorize=…)         # 着色の後。fig が None なら何もしない

■ 配色
  lib/md_to_pdf.py の PALETTE（_cr §48）の色だけを使う。棒＝濃紺 #1A2A44、基線＝灰 #8A94A6、
  区切り罫＝灰罫 #DBE1E9 / #E3E8EF、文字＝黒 #1A1A1A。騰落率の文字の緑赤は本文と同じく
  レンダラの自動着色（colorize 引数）に任せ、本モジュールは色を決めない。
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Callable

MARKER = "<!-- THEME_FIGURE -->"
# markdown 変換を素通りさせるための置き換え文字列（英大文字のみ・着色や表処理に掛からない）
_PLACEHOLDER = "MZTHEMEFIGUREPLACEHOLDER"

MAX_THEMES = 3   # 上位 3 位まで
MAX_ROWS = 6     # 1 テーマあたりの棒の上限（1 ページ目に収めるための上限）

_RE_SECTION = re.compile(r"^##\s+本日のテーマ\s*$")
_RE_SECTION_END = re.compile(r"^#{1,2}\s")
# `**1位 テーマ名（括弧書き）**` ＋ 任意の後続（点灯日数などの注記）
_RE_THEME_HEAD = re.compile(r"^\*\*\s*(\d+)\s*位\s*(.+?)\s*\*\*")


@dataclass
class _Row:
    code: str
    name: str
    value_text: str   # md のセル文字列そのまま（例 "+4.3%"）
    value: float      # 棒の長さの計算用


@dataclass
class _Theme:
    rank: str
    name: str
    material: str
    rows: list[_Row] = field(default_factory=list)


# ── 解析 ────────────────────────────────────────────────


def _split_title(title: str) -> tuple[str, str]:
    """`テーマ名（括弧書き）` を (テーマ名, 括弧書き) に分ける。末尾の全角括弧を対応付けて外す。"""
    t = title.strip()
    if not t.endswith("）"):
        return t, ""
    depth = 0
    for i in range(len(t) - 1, -1, -1):
        ch = t[i]
        if ch == "）":
            depth += 1
        elif ch == "（":
            depth -= 1
            if depth == 0:
                name = t[:i].strip()
                if not name:
                    return t, ""
                return name, t[i + 1:-1].strip()
    return t, ""


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _parse_value(text: str) -> float | None:
    """騰落率セル（+4.3% / ＋4.3% / -2.1% / ▼2.1% / −2.1%）を数値にする。読めなければ None。"""
    s = text.replace("*", "").strip()
    s = s.replace("＋", "+").replace("−", "-").replace("▼", "-").replace("ー", "-")
    s = s.replace("％", "%").replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_table(lines: list[str]) -> list[_Row]:
    """主導銘柄表（コード / 銘柄名 / … / 騰落率）を行に変換する。列は見出し名で特定する。"""
    if len(lines) < 3:
        return []
    head = _cells(lines[0])
    try:
        i_code = head.index("コード")
        i_name = head.index("銘柄名")
        i_val = head.index("騰落率")
    except ValueError:
        return []
    rows: list[_Row] = []
    for ln in lines[2:]:  # 0=見出し行 1=区切り行
        c = _cells(ln)
        if len(c) <= max(i_code, i_name, i_val):
            continue
        v = _parse_value(c[i_val])
        if v is None:
            continue
        rows.append(_Row(code=c[i_code], name=c[i_name], value_text=c[i_val], value=v))
    return rows


def extract_themes(body_md: str) -> list[_Theme]:
    """`## 本日のテーマ` 節から上位テーマ（最大 MAX_THEMES）を取り出す。節が無ければ空。"""
    lines = body_md.replace("\r\n", "\n").split("\n")
    start = None
    for i, ln in enumerate(lines):
        if _RE_SECTION.match(ln.strip()):
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for j in range(start, len(lines)):
        if _RE_SECTION_END.match(lines[j]):
            end = j
            break

    themes: list[_Theme] = []
    cur: _Theme | None = None
    i = start
    while i < end:
        ln = lines[i].strip()
        m = _RE_THEME_HEAD.match(ln)
        if m:
            name, material = _split_title(m.group(2).replace("**", ""))
            cur = _Theme(rank=m.group(1), name=name, material=material)
            themes.append(cur)
            i += 1
            continue
        if ln.startswith("|") and cur is not None and not cur.rows:
            block = []
            while i < end and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            cur.rows = _parse_table(block)
            continue
        i += 1

    out = [t for t in themes if t.rows]
    return out[:MAX_THEMES]


# ── 描画 ────────────────────────────────────────────────

# 全角英数（ＫＯＫＵＳＡＩ 等）を半角へ寄せて図のラベル幅を抑える（図の表示だけ・本文は不変）。
_ZEN_ALNUM = {chr(c): chr(c - 0xFEE0) for c in range(0xFF10, 0xFF1A)}
_ZEN_ALNUM.update({chr(c): chr(c - 0xFEE0) for c in range(0xFF21, 0xFF3B)})
_ZEN_ALNUM.update({chr(c): chr(c - 0xFEE0) for c in range(0xFF41, 0xFF5B)})
_ZEN_ALNUM.update({"＆": "&", "．": ".", "　": " "})


def _label_text(s: str) -> str:
    s = "".join(_ZEN_ALNUM.get(ch, ch) for ch in s)
    return re.sub(r" {2,}", " ", s).strip()


def _esc(s: str) -> str:
    return _html.escape(s.replace("**", ""), quote=False)


# CSS（図がある時だけ誌面に入る。PALETTE 内の色のみ。符号付き数値を書かないこと:
# <style> の中身もテキストノードとして符号正規化の対象になり得るため）。
_CSS = """
.theme-fig { margin:2px 0 16px; padding:7px 0 3px; border-top:0.8pt solid #DBE1E9;
  border-bottom:0.8pt solid #DBE1E9; }
.theme-fig .tf-cap { font-size:10.5pt; font-weight:700; color:#1A1A1A; line-height:1.5;
  letter-spacing:.02em; margin:0 0 2px; }
.theme-fig .tf-theme { padding:5px 0 5px; }
.theme-fig .tf-theme + .tf-theme { border-top:0.6pt solid #E3E8EF; }
.theme-fig .tf-head { font-size:12pt; font-weight:700; color:#1A1A1A; line-height:1.5; }
.theme-fig .tf-mat { font-size:10.5pt; color:#1A1A1A; line-height:1.5; margin:0 0 3px; }
.theme-fig .tf-row { display:flex; align-items:center; font-size:10.5pt; line-height:1.4;
  margin:2px 0; color:#1A1A1A; }
.theme-fig .tf-label { flex:0 0 42%; padding-right:8px; overflow-wrap:anywhere; }
.theme-fig .tf-track { flex:1 1 auto; display:flex; align-items:center; min-height:16px;
  border-left:0.8pt solid #8A94A6; }
.theme-fig .tf-bar { height:10px; background:#1A2A44; border-radius:0 3px 3px 0; flex:none; }
.theme-fig .tf-val { margin-left:6px; white-space:nowrap; font-variant-numeric:tabular-nums; }
.theme-fig .tf-div { border-left:0; }
.theme-fig .tf-neg { flex:1 1 50%; display:flex; justify-content:flex-end; align-items:center;
  border-right:0.8pt solid #8A94A6; }
.theme-fig .tf-pos { flex:1 1 50%; display:flex; align-items:center; }
.theme-fig .tf-neg .tf-bar { border-radius:3px 0 0 3px; }
.theme-fig .tf-neg .tf-val { margin-left:0; margin-right:6px; }
"""


def _bar(ratio: float, room_px: int) -> str:
    r = max(0.0, min(1.0, ratio))
    return f'<div class="tf-bar" style="width:calc((100% - {room_px}px) * {r:.4f})"></div>'


def render_html(themes: list[_Theme], colorize: Callable[[str], str] | None = None) -> str:
    """テーマ早見図の HTML を返す。colorize は本文と同じ符号付き数値の着色関数。"""
    paint = colorize or (lambda s: s)
    vals = [r.value for t in themes for r in t.rows[:MAX_ROWS]]
    max_abs = max((abs(v) for v in vals), default=0.0) or 1.0
    diverging = any(v < 0 for v in vals)

    parts = [f"<style>{_CSS}</style>", '<div class="theme-fig">',
             '<div class="tf-cap">テーマ早見図　本日のテーマと主導銘柄の騰落率</div>']
    for t in themes:
        parts.append('<div class="tf-theme">')
        name = paint(_esc(_label_text(t.name)))
        parts.append(f'<div class="tf-head">{_esc(t.rank)}位　{name}</div>')
        if t.material:
            mat = paint(_esc(_label_text(t.material)))
            # 欄名を付けず、見出しの括弧書きを括弧ごと副題にする（内容が材料でも業種名でも誤表示にならない）
            parts.append(f'<div class="tf-mat">（{mat}）</div>')
        rows = sorted(t.rows[:MAX_ROWS], key=lambda r: r.value, reverse=True)
        for r in rows:
            label = _esc(f"{r.code} {_label_text(r.name)}")
            val = f'<span class="tf-val">{paint(_esc(r.value_text))}</span>'
            ratio = abs(r.value) / max_abs
            # 棒の最大長は「帯の幅 − 値ラベルの幅」。値ラベル（例 ＋22.53%）が右余白を
            # 越えないよう 78px を確保する（10.5pt 太字 7 字 ≒ 64px ＋ 間隔 6px ＋ 余裕）。
            if not diverging:
                track = f'<div class="tf-track">{_bar(ratio, 78)}{val}</div>'
            elif r.value < 0:
                track = ('<div class="tf-track tf-div"><div class="tf-neg">'
                         f'{val}{_bar(ratio, 74)}</div><div class="tf-pos"></div></div>')
            else:
                track = ('<div class="tf-track tf-div"><div class="tf-neg"></div>'
                         f'<div class="tf-pos">{_bar(ratio, 74)}{val}</div></div>')
            parts.append(f'<div class="tf-row"><div class="tf-label">{label}</div>{track}</div>')
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


# ── md_to_pdf からの入口 ─────────────────────────────────────


def prepare(body_md: str) -> tuple[str, list[_Theme] | None]:
    """目印行を処理する。目印が無ければ (body_md, None) をそのまま返す（無変更）。

    目印があれば、最初の目印を置き換え文字列に、2 つ目以降を空行にする。
    テーマが取れない号では全ての目印を取り除き None を返す（図は出さない）。
    """
    if MARKER not in body_md:
        return body_md, None
    lines = body_md.split("\n")
    themes = extract_themes("\n".join(ln for ln in lines if ln.strip() != MARKER))
    out: list[str] = []
    placed = False
    for ln in lines:
        if ln.strip() == MARKER:
            if themes and not placed:
                out.append(_PLACEHOLDER)
                placed = True
            else:
                out.append("")
            continue
        out.append(ln)
    return "\n".join(out), (themes or None)


def inject(html_body: str, themes: list[_Theme] | None,
           colorize: Callable[[str], str] | None = None) -> str:
    """markdown 変換後の HTML の置き換え文字列を図に差し替える。themes が None なら無変更。"""
    if not themes:
        return html_body
    fig = render_html(themes, colorize)
    new, n = re.subn(rf"<p>\s*{_PLACEHOLDER}\s*</p>", lambda _m: fig, html_body, count=1)
    if n == 0:
        new = html_body.replace(_PLACEHOLDER, fig, 1)
    return new
