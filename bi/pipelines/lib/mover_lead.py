"""動意レポート（日次・夜間 PTS）の冒頭ブロックを機械で組み立てる（PM 承認 2026-10-05・10-06 反映）。

冒頭ブロック = (1) 表題行「{レポート名} {日付}（曜）— {テーマ見出し}」 (2) 引用ブロックの要点 3 行
(3) テーマ早見図の目印行 `<!-- THEME_FIGURE -->`（図そのものは lib/theme_figure.py が描く）。

■ 生成枠が書くもの（HTML コメントのため、本処理が動かなくても誌面には何も出ない）
    <!-- MOVERS_LEAD
    見出し：{テーマ見出し}
    - **{短い括り名}**：{どのテーマ・どの銘柄（コード）が・何の材料で動いたか}
    - …
    - …
    MOVERS_LEAD -->
  日次は themes 枠のファイル先頭、夜間 PTS は表題行の直後に置く。日付・曜日・レポート名・目印は
  生成枠に書かせず、本処理が組み立てる。

■ 発行を止めない（_cr §36）
  ブロックが無い・形式が崩れている・本文に無い数値やコードを含む等の時は、冒頭ブロックを載せず
  従来の形式のまま md を残し（コメントだけ取り除く）、状態を GitHub Actions の出力へ書く
  （workflow が BI へ通知する）。本処理の例外も握りつぶして従来形式に倒す（終了コードは常に 0）。

使い方:
  python lib/mover_lead.py --kind movers --file market/daily/movers/{date}.md --date {date} \
      --lead-file /tmp/movers_lead.txt --github-output "$GITHUB_OUTPUT"
  python lib/mover_lead.py --kind pts_movers --file market/daily/pts_movers/{date}.md --date {date} ...
  python lib/mover_lead.py --kind movers --check {生成枠のファイル}   # 書き手の保存前検査（書き換えない）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from datetime import date as _date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LEAD_OPEN = "<!-- MOVERS_LEAD"
LEAD_CLOSE = "MOVERS_LEAD -->"
FIGURE_MARKER = "<!-- THEME_FIGURE -->"
# 要点の引用ブロックと、その直後に来うる別の引用ブロック（品質注記・本号未掲載の 1 行）が
# markdown 上で 1 つの箱に結合されないための区切り（HTML コメント・誌面には出ない）。
LEAD_END = "<!-- /MOVERS_LEAD -->"

# 見出し（テーマ見出し部分）の上限。全角 1 字＝1、半角英大文字＝1、その他の半角英数記号＝0.5 で数える
# （明朝の半角英大文字は全角の半分より広く、英単語は途中で折れずに次行へ送られるため大文字を 1 とする）。
# 2026-10-06 実測（lib/md_to_pdf.py のマストヘッド 24pt・A4）: 表題が 2 行に収まる上限は
# 日次「動意銘柄レポート 2026-10-05（月）— 」の後ろに全角 19 字、夜間 PTS「夜間PTS動意レポート 10/2（金）— 」
# の後ろに全角 20 字。禁則処理で 1 字送られる余裕と日付の桁増（10/15 等）を見込み、両方 18 に揃える。
HEADLINE_MAX = 18.0
HEADLINE_MIN = 4.0
BULLET_MAX = 80.0   # 要点 1 行の上限（太字記号を除いた幅）。1 ページ目を圧迫しないため
N_BULLETS = 3

_WEEKDAY = "月火水木金土日"
_DOC_LABEL = {"movers": "動意銘柄レポート", "pts_movers": "夜間PTS動意レポート"}

# 要点・見出しに書かない語（_cr §38 の予想・推奨の禁止、§40 の相互参照禁止、
# agents/mover_analyst.md の削除済＝地合い・需給の解説）。含まれていたら冒頭ブロックごと載せない。
BANNED = (
    "地合い", "需給", "資金流入", "資金が向", "資金の流れ", "買い場", "押し目", "売り場", "狙い目",
    "可能性", "見込み", "期待", "思惑", "とみられ", "だろう", "思われ", "推奨",
    "上記", "前述", "参照",
)

_RE_CODE = re.compile(r"[（(]([0-9]{3}[0-9A-Z])[）)]")
_RE_NUM = re.compile(r"[+\-＋−▲▼]?(\d[\d,]*(?:\.\d+)?)\s*(%|％|円|億円|兆円|倍)")


# ── 解析 ─────────────────────────────────────────────


def width(s: str) -> float:
    """全角 1・半角英大文字 1・その他の半角 0.5 で数えた幅。太字記号 ** は数えない。"""
    s = s.replace("**", "")
    return sum(1.0 if (unicodedata.east_asian_width(ch) in ("F", "W", "A") or "A" <= ch <= "Z") else 0.5
               for ch in s)


def split_lead(md_text: str) -> tuple[str | None, str]:
    """md から最初の MOVERS_LEAD ブロックの中身を取り出し、全てのブロックを除いた md と返す。"""
    lines = md_text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    body: list[str] | None = None
    first: str | None = None
    inside = False
    for ln in lines:
        s = ln.strip()
        if not inside and s.startswith(LEAD_OPEN):
            inside = True
            body = []
            rest = s[len(LEAD_OPEN):]
            if rest.strip().endswith(LEAD_CLOSE):  # 1 行で閉じた場合
                inside = False
                if first is None:
                    first = rest.strip()[: -len(LEAD_CLOSE)]
            continue
        if inside:
            if s.endswith(LEAD_CLOSE):
                head = s[: -len(LEAD_CLOSE)].strip()
                if head:
                    body.append(head)
                inside = False
                if first is None:
                    first = "\n".join(body)
                continue
            body.append(ln)
            continue
        out.append(ln)
    if inside:  # 閉じていないブロック: 以降を本文へ戻さず（壊れた入力）、中身は不採用
        first = None
    # ブロックを抜いた跡の連続空行を 1 つに詰める
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return first, text


def parse_lead(raw: str) -> tuple[str, list[str]]:
    """ブロックの中身を (見出し, 要点の行) に分ける。"""
    headline = ""
    bullets: list[str] = []
    for ln in raw.split("\n"):
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^見出し\s*[：:]\s*(.*)$", s)
        if m:
            headline = m.group(1).strip()
            continue
        s = re.sub(r"^>\s*", "", s)
        if s.startswith(("- ", "* ", "・")):
            bullets.append("- " + re.sub(r"^(?:- |\* |・)\s*", "", s).strip())
        else:
            bullets.append("?" + s)  # 箇条書きでない行（不合格の判定に使う）
    return headline, bullets


def _norm_num(s: str) -> str:
    return s.replace(",", "").replace("％", "%")


def validate(headline: str, bullets: list[str], body: str) -> list[str]:
    """不合格の理由（和文）を返す。空なら合格。"""
    errs: list[str] = []
    if not headline:
        errs.append("見出しが無い")
    else:
        w = width(headline)
        if w > HEADLINE_MAX:
            errs.append(f"見出しが長い（全角換算 {w:g} 字・上限 {HEADLINE_MAX:g} 字）")
        if w < HEADLINE_MIN:
            errs.append("見出しが短すぎる")
        if any(ch in headline for ch in "#|>\n") or headline.startswith(("—", "-")):
            errs.append("見出しに使えない記号がある")
    if any(b.startswith("?") for b in bullets):
        errs.append("箇条書きでない行がある")
    real = [b for b in bullets if b.startswith("- ")]
    if len(real) != N_BULLETS:
        errs.append(f"要点が {len(real)} 行（3 行が必要）")
    body_norm = _norm_num(body)
    body_codes = set(re.findall(r"(?<![0-9A-Z])([0-9]{3}[0-9A-Z])(?![0-9A-Z])", body))
    for i, b in enumerate(real, 1):
        text = b[2:]
        if width(text) > BULLET_MAX:
            errs.append(f"要点 {i} 行目が長い（全角換算 {width(text):g} 字）")
        codes = _RE_CODE.findall(text)
        if not codes:
            errs.append(f"要点 {i} 行目に銘柄コードが無い")
        for c in codes:
            if c not in body_codes:
                errs.append(f"要点 {i} 行目のコード {c} が本文に無い")
        if any(ch in text for ch in "#|>"):
            errs.append(f"要点 {i} 行目に使えない記号がある")
    for src in [headline] + [b[2:] for b in real]:
        for m in _RE_NUM.finditer(src):
            core = _norm_num(m.group(1) + m.group(2))
            if core not in body_norm:
                errs.append(f"数値 {m.group(0)} が本文に無い")
        for w_ in BANNED:
            if w_ in src:
                errs.append(f"使わない語「{w_}」がある")
    # 重複を除いて順序を保つ
    seen: set[str] = set()
    return [e for e in errs if not (e in seen or seen.add(e))]


# ── 組み立て ───────────────────────────────────────────


def date_label(kind: str, target: str) -> str:
    d = _date.fromisoformat(target)
    wd = _WEEKDAY[d.weekday()]
    if kind == "pts_movers":
        return f"{d.month}/{d.day}（{wd}）"
    return f"{target}（{wd}）"


def build_block(kind: str, target: str, headline: str, bullets: list[str]) -> list[str]:
    title = f"# {_DOC_LABEL[kind]} {date_label(kind, target)}— {headline}"
    quote = ["> " + b for b in bullets]
    return [title, "", *quote, "", FIGURE_MARKER, "", LEAD_END, ""]


def apply(kind: str, md_text: str, target: str, lead_raw: str | None) -> tuple[str, str, str]:
    """(新しい md, status, reason) を返す。status: applied / absent / invalid。"""
    in_md, md_wo = split_lead(md_text)
    raw = lead_raw if (lead_raw and lead_raw.strip()) else in_md
    # 既に組み立て済みの号（再実行）には二度付けしない
    if FIGURE_MARKER in md_wo or LEAD_END in md_wo:
        return md_wo, "applied", "既に冒頭ブロックがある（再実行・二重には付けない）"
    if not raw or not raw.strip():
        return md_wo, "absent", "生成枠の出力に冒頭ブロック（MOVERS_LEAD）が無い"
    headline, bullets = parse_lead(raw)
    errs = validate(headline, bullets, md_wo)
    if errs:
        return md_wo, "invalid", "・".join(errs[:4])
    block = build_block(kind, target, headline, [b for b in bullets if b.startswith("- ")])
    lines = md_wo.split("\n")
    if kind == "pts_movers":
        idx = next((i for i, ln in enumerate(lines) if re.match(r"^#\s+夜間PTS動意レポート", ln)), None)
        if idx is None:
            return md_wo, "invalid", "表題行（# 夜間PTS動意レポート）が見つからない"
        rest = lines[idx + 1:]
        while rest and not rest[0].strip():
            rest.pop(0)
        new = lines[:idx] + block + rest
    else:
        # 日次の結合 md は表題行を持たない（従来は送信スクリプトが付けていた）。先頭へ置く。
        rest = lines[:]
        while rest and not rest[0].strip():
            rest.pop(0)
        new = block + rest
    return "\n".join(new), "applied", ""


def _expected(kind: str, md_text: str) -> bool:
    """冒頭ブロックがあるべき号か（無い時に BI へ知らせるか）。日次はテーマ欄がある号だけ。"""
    if kind == "pts_movers":
        return True
    return bool(re.search(r"^##\s+本日のテーマ\s*$", md_text, re.MULTILINE))


def _gh_out(path: str | None, **kv: str) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={str(v).replace(chr(10), ' ')}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=sorted(_DOC_LABEL), required=True)
    ap.add_argument("--file", help="組み立て対象の誌面 md（その場で書き換える）")
    ap.add_argument("--date", help="対象日 YYYY-MM-DD")
    ap.add_argument("--lead-file", help="日次: 結合工程が themes 枠から抜き出したブロックの中身")
    ap.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    ap.add_argument("--check", metavar="MD", help="書き手の保存前検査（書き換えない・NG なら exit 1）")
    args = ap.parse_args()

    if args.check:
        text = Path(args.check).read_text(encoding="utf-8")
        raw, body = split_lead(text)
        if not raw:
            print("LEAD CHECK: NG 冒頭ブロック（<!-- MOVERS_LEAD … MOVERS_LEAD -->）が無い")
            return 1
        headline, bullets = parse_lead(raw)
        errs = validate(headline, bullets, body)
        print(f"見出しの幅: 全角換算 {width(headline):g} 字（上限 {HEADLINE_MAX:g}）")
        if errs:
            print("LEAD CHECK: NG " + "／".join(errs))
            return 1
        print("LEAD CHECK: OK")
        return 0

    if not (args.file and args.date):
        ap.error("--file と --date が必要です")
    path = Path(args.file)
    try:
        md_text = path.read_text(encoding="utf-8")
        lead_raw = None
        if args.lead_file and Path(args.lead_file).is_file():
            lead_raw = split_lead(Path(args.lead_file).read_text(encoding="utf-8"))[0]
        new, status, reason = apply(args.kind, md_text, args.date, lead_raw)
        if new != md_text:
            tmp = path.with_suffix(path.suffix + ".lead.tmp")
            tmp.write_text(new, encoding="utf-8")
            os.replace(tmp, path)
    except Exception as e:  # 発行を止めない: 何があっても従来形式のまま終える
        status, reason = "error", f"組み立て処理の失敗（{type(e).__name__}）"
        md_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    notify = "true" if (status != "applied" and _expected(args.kind, md_text)) else "false"
    print(f"冒頭ブロック: status={status} notify={notify}" + (f" reason={reason}" if reason else ""))
    _gh_out(args.github_output, status=status, reason=reason, notify=notify)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
