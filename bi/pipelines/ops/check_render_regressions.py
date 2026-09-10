#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""共有 PDF レンダラ（lib/md_to_pdf.py）の誌面回帰チェック（読み取り専用）。

lib/md_to_pdf.py は macro / macro_evening / movers / movers_weekly / pts_movers /
sector / sector_full / earnings / largecap_weekly / stock の全レポート種別で共有され
ている。ある 1 種別のために入れた誌面変更が他種別のレイアウトを壊しても、PM が PDF を
開くまで誰も気づかない（実例: stock 向け v6 変更を無条件適用し、macro/movers/sector の
8 列表が重なり・マストヘッド見出しが途中で切れ・赤太字がリード文へ漏れた）。

本スクリプトは各種別の「ディスク上で最も新しい md」をテンポラリへ PDF 化し、PyMuPDF
（fitz）で描画結果そのものを検査して、この種の回帰をコミット前に機械検知する。

チェック項目（種別ごとに PASS / FAIL）:
  RENDER_OK        レンダリングが例外なく完了した
  PALETTE_OK       PaletteViolation（許可色以外の混入）が出ていない
  TITLE_COMPLETE   md 先頭 H1 の全文が 1 ページ目のテキストに含まれる（切れ検知）
  NO_CELL_OVERLAP  同一行（y 帯）の語同士が x 方向で重なって描かれていない
  WITHIN_MARGINS   本文の描画が左右の印刷可能マージンを越えていない

本番成果物（bi/outputs/report_pdfs/）は一切触らない。出力先は --out-dir 既定の
テンポラリで、--keep を付けない限り実行後に削除する。

exit code:
  0 = 全種別・全チェック PASS（SKIPPED は成功扱い）
  1 = いずれかのチェックが FAIL、または引数不正

使い方:
  python bi/pipelines/ops/check_render_regressions.py                    # 全種別
  python bi/pipelines/ops/check_render_regressions.py --kinds stock      # 種別限定
  python bi/pipelines/ops/check_render_regressions.py --kinds macro,movers --keep

運用:
  lib/md_to_pdf.py を編集したら、コミット前に本スクリプトを全種別で走らせる。
  コンソールへは ASCII の要約表のみを出し、和文の該当箇所（重なった語・切れた見出し）は
  --report の UTF-8 ファイルへ書く（Windows コンソールの cp932 で化けるため）。
"""
from __future__ import annotations

import argparse
import glob as globmod
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PIPELINES = REPO_ROOT / "bi" / "pipelines"
sys.path.insert(0, str(PIPELINES))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# send_report_pdf_discord.py と同一の取り込み方（種別定義の単一の真実）
from send_report_jpeg_discord import KIND_CONFIG  # noqa: E402
from lib.md_to_pdf import PaletteViolation, render_markdown_to_pdf  # noqa: E402

# 送信種別 → md_to_pdf のテーマ kind。send_report_pdf_discord.py の _PDF_KIND と同値。
# 送信スクリプト側は Discord/requests/.env を巻き込むため、定義だけをここへ写している。
_PDF_KIND = {
    "macro": "macro", "macro_evening": "macro",
    "sector": "sector", "sector_full": "sector",
    "movers": "movers", "movers_weekly": "movers", "pts_movers": "movers",
    "ideas": "ideas", "scout": "ideas",
    "themes": "themes", "earnings": "earnings", "stock": "stock",
    "largecap_weekly": "largecap_weekly",
}

CHECKS = ["RENDER_OK", "PALETTE_OK", "TITLE_COMPLETE", "NO_CELL_OVERLAP", "WITHIN_MARGINS"]

# ---- 判定の閾値（A4・72pt/inch のポイント座標系） ----

# 同一テキスト行とみなす y 中心のずれ（pt）。行間より十分小さく、
# 上付き・下付き程度のずれは同一行として拾う。
SAME_LINE_TOL_PT = 3.0
# 語の x 範囲がこれ以上重なったら「重なって描かれている」と判定（pt）。
# 文字の字送りの丸めで隣接語が僅かに食い込むことがあるため 1.2pt の余裕を取る。
OVERLAP_EPS_PT = 1.2
# 印刷可能域からのはみ出し許容（pt）。
# 和文は語間に空白が無いため PyMuPDF の get_text("words") が 1 文節〜1 行分を丸ごと
# 1 語として返し、その bbox の右端は最終グリフの字送り分だけ字面より広く出る（実測で
# 行末が 0.3〜3.8pt 超過する行が macro/sector/scout に出た。目視では収まっている）。
# 本当の表はみ出しは数十 pt 単位で外へ出るため、4pt まで許容しても検知力は落ちない。
MARGIN_TOL_PT = 4.0
# md_to_pdf の page.pdf(margin=...) と同値。左右 24mm（本文幅を決める値なので固定）。
MARGIN_MM = 24.0
# 上下マージン（PM 2026-09-08 指示で 16/13mm）。フッタ帯の判定にだけ使う。
MARGIN_TOP_MM = 16.0
MARGIN_BOTTOM_MM = 13.0
MM_TO_PT = 72.0 / 25.4


# ---- md の解決 ----

def _glob_pattern(md_path_tpl: str) -> str:
    """KIND_CONFIG の md_path テンプレを、日付/月/コードを埋めた glob 文字列にする。"""
    pat = md_path_tpl
    pat = pat.replace("{date}", "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]")
    pat = pat.replace("{month}", "[0-9][0-9][0-9][0-9]-[0-9][0-9]")
    pat = pat.replace("{code}", "*")
    return pat


def _resolve_latest_md(kind: str) -> "Path | None":
    """種別の md_path パターンに合致する、ディスク上で最も新しい md を返す。

    「新しい」はファイル名順（先頭が YYYY-MM-DD / YYYY-MM のため辞書順＝日付順）で判定
    する。mtime は git checkout や再生成で前後するため使わない。
    stock は research/stocks/{code}/{date}.md で、コード直下に thesis_master.md 等の
    日付でない md が同居するため、日付名のものだけを対象にする。
    """
    cfg = KIND_CONFIG[kind]
    pattern = _glob_pattern(cfg["md_path"])
    hits = [Path(p) for p in globmod.glob(str(REPO_ROOT / pattern))]
    hits = [p for p in hits if p.is_file()]
    if not hits:
        return None
    # 日付部分（ファイル名先頭）とフルパスの2段で並べ、最も新しい日付を採る
    hits.sort(key=lambda p: (p.name, str(p)))
    return hits[-1]


def _identifier(kind: str, md_path: Path) -> str:
    """要約表に出す md の日付（または月・コード_日付）。"""
    stem = md_path.stem
    if kind == "stock":
        return f"{md_path.parent.name}_{stem}"
    return stem


def _first_h1(md_text: str) -> str:
    """md 先頭の H1 テキスト（`# ` 以降）を返す。無ければ空文字。"""
    m = re.search(r"^#\s+(.+?)\s*$", md_text.replace("\r\n", "\n"), re.MULTILINE)
    return m.group(1).strip() if m else ""


def _norm_ws(s: str) -> str:
    """空白の正規化。全角空白・改行・連続空白を 1 個の半角空白に潰す。

    PDF 側は字間調整で語が分割されて抽出されるため、比較前に両側で空白を全て除く形にも
    使う（_norm_tight）。
    """
    return re.sub(r"[\s　]+", " ", s).strip()


def _norm_tight(s: str) -> str:
    """空白を完全に除いた比較用文字列。PDF の抽出は語境界に空白が入るため。"""
    return re.sub(r"[\s　]+", "", s)


# ---- PDF 検査 ----

def _check_title_complete(doc, h1: str) -> "tuple[bool, list[str]]":
    """md 先頭 H1 の全文が 1 ページ目に描かれているか。

    マストヘッド見出しが CSS の折り返し抑止や省略で途中までしか出ないと FAIL。
    md_to_pdf は先頭 H1 をマストヘッドへ昇格し本文からは除くため、1 ページ目だけを見る。
    """
    if not h1:
        return True, ["H1 が md に無いため判定対象外（PASS 扱い）"]
    if doc.page_count == 0:
        return False, ["ページが 0 枚"]
    page1 = _norm_tight(doc.load_page(0).get_text("text"))
    target = _norm_tight(h1)
    if target and target in page1:
        return True, []
    # どこまで出ているかを示す（切れ位置の特定）
    longest = ""
    for end in range(len(target), 0, -1):
        if target[:end] in page1:
            longest = target[:end]
            break
    detail = (
        f"H1「{h1}」が 1 ページ目に完全一致しない。"
        f"描画されている先頭部分=「{longest}」（{len(longest)}/{len(target)} 文字）"
    )
    return False, [detail]


def _check_cell_overlap(doc) -> "tuple[bool, list[str]]":
    """同一テキスト行に属する語同士が x 方向で重なって描かれていないか。

    表の列幅が本文幅を超えると、セルの内容が隣のセルの上へ重ねて描かれる
    （実例: 「▼0.10%」が「600」の上に印字された）。テキスト抽出では隣接した別語に
    見えるため、bbox の重なりでしか検知できない。
    y 中心が SAME_LINE_TOL_PT 以内の語を同一行とみなし、x 範囲の重なりが
    OVERLAP_EPS_PT を超えるペアを違反として報告する。
    """
    offenders: list[str] = []
    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        # words: (x0, y0, x1, y1, word, block_no, line_no, word_no)
        words = [w for w in page.get_text("words") if w[4].strip()]
        # y 中心でソートし、同一行の語を束ねる
        words.sort(key=lambda w: ((w[1] + w[3]) / 2.0, w[0]))
        line: list[tuple] = []
        line_yc = None
        for w in words:
            yc = (w[1] + w[3]) / 2.0
            if line_yc is None or abs(yc - line_yc) <= SAME_LINE_TOL_PT:
                line.append(w)
                line_yc = yc if line_yc is None else line_yc
            else:
                offenders += _overlaps_in_line(line, pno)
                line = [w]
                line_yc = yc
        offenders += _overlaps_in_line(line, pno)
    return (not offenders), offenders


def _overlaps_in_line(line: "list[tuple]", pno: int) -> "list[str]":
    """1 行分の語リストから x 範囲が重なるペアを抽出する。"""
    out: list[str] = []
    line = sorted(line, key=lambda w: w[0])
    for i in range(len(line) - 1):
        a = line[i]
        for b in line[i + 1:]:
            if b[0] >= a[2] - OVERLAP_EPS_PT:
                break  # x 昇順なので以降は重ならない
            overlap = min(a[2], b[2]) - max(a[0], b[0])
            if overlap > OVERLAP_EPS_PT:
                out.append(
                    f"p{pno + 1}: 「{a[4]}」(x {a[0]:.1f}-{a[2]:.1f}) と "
                    f"「{b[4]}」(x {b[0]:.1f}-{b[2]:.1f}) が {overlap:.1f}pt 重なる"
                )
    return out


def _check_within_margins(doc) -> "tuple[bool, list[str]]":
    """描画された本文の語が左右の印刷可能マージンを越えていないか。

    表が本文幅を超えて右へ流れると、ページ端（マージンの外）まで文字が出る。

    ヘッダ／フッタ帯は判定から除く。md_to_pdf は Chromium の display_header_footer で
    「MIZUKI FUND」とページ番号を描いており、この running footer は仕様上マージン帯の
    「中」（＝本文ボックスの外側）へ左右いっぱいに置かれる。本文と同じ基準で測ると全ページ
    が必ず違反になり、本当の本文はみ出しが埋もれるため、上下マージンの外側にある語は
    本文でないものとして除外する。
    """
    left_limit = MARGIN_MM * MM_TO_PT - MARGIN_TOL_PT
    body_top = MARGIN_TOP_MM * MM_TO_PT - MARGIN_TOL_PT
    offenders: list[str] = []
    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        right_limit = page.rect.width - MARGIN_MM * MM_TO_PT + MARGIN_TOL_PT
        body_bottom = page.rect.height - MARGIN_BOTTOM_MM * MM_TO_PT + MARGIN_TOL_PT
        for w in page.get_text("words"):
            if not w[4].strip():
                continue
            if w[3] < body_top or w[1] > body_bottom:
                continue  # running header / footer（本文ボックス外）
            if w[0] < left_limit or w[2] > right_limit:
                offenders.append(
                    f"p{pno + 1}: 「{w[4]}」 x {w[0]:.1f}-{w[2]:.1f} "
                    f"（許容 {left_limit:.1f}-{right_limit:.1f}）"
                )
    return (not offenders), offenders


# ---- 1 種別の実行 ----

def check_kind(kind: str, out_dir: Path) -> dict:
    """1 種別を PDF 化して全チェックを回し、結果 dict を返す。

    返り値: {kind, status, ident, md, results: {check: bool|None}, details: [str]}
      status = "OK"（全 PASS）/ "FAIL" / "SKIPPED"（md が無い）
    """
    rec: dict = {
        "kind": kind, "status": "SKIPPED", "ident": "-", "md": None,
        "results": {c: None for c in CHECKS}, "details": [],
    }
    md_path = _resolve_latest_md(kind)
    if md_path is None:
        rec["details"].append(f"md が見つからない（パターン: {KIND_CONFIG[kind]['md_path']}）")
        return rec

    rec["md"] = md_path
    rec["ident"] = _identifier(kind, md_path)
    md_text = md_path.read_text(encoding="utf-8")
    pdf_kind = _PDF_KIND.get(kind, "macro")
    # 日次種別はファイル名先頭の日付を誌面の日付ラベルに使う（送信スクリプトと同じ）
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", md_path.stem)
    target_date = m.group(1) if m else None
    out_path = out_dir / f"{kind}_{rec['ident']}.pdf"

    try:
        render_markdown_to_pdf(md_text, out_path, kind=pdf_kind, target_date=target_date)
        rec["results"]["RENDER_OK"] = True
        rec["results"]["PALETTE_OK"] = True
    except PaletteViolation as e:
        rec["results"]["RENDER_OK"] = True   # 描画自体は到達している
        rec["results"]["PALETTE_OK"] = False
        rec["status"] = "FAIL"
        rec["details"].append(f"PaletteViolation: {e}")
        return rec
    except Exception as e:
        rec["results"]["RENDER_OK"] = False
        rec["results"]["PALETTE_OK"] = False
        rec["status"] = "FAIL"
        rec["details"].append(f"{type(e).__name__}: {e}")
        rec["details"].append(traceback.format_exc())
        return rec

    if not out_path.exists():
        rec["results"]["RENDER_OK"] = False
        rec["status"] = "FAIL"
        rec["details"].append("レンダリングは例外を出さなかったが PDF が生成されていない")
        return rec

    import fitz  # 遅延 import（レンダリング失敗時に検査系の依存を要求しない）

    with fitz.open(str(out_path)) as doc:
        ok, det = _check_title_complete(doc, _first_h1(md_text))
        rec["results"]["TITLE_COMPLETE"] = ok
        rec["details"] += [f"[TITLE_COMPLETE] {d}" for d in det] if not ok else []

        ok, det = _check_cell_overlap(doc)
        rec["results"]["NO_CELL_OVERLAP"] = ok
        rec["details"] += [f"[NO_CELL_OVERLAP] {d}" for d in det]

        ok, det = _check_within_margins(doc)
        rec["results"]["WITHIN_MARGINS"] = ok
        rec["details"] += [f"[WITHIN_MARGINS] {d}" for d in det]

    rec["status"] = "OK" if all(v is not False for v in rec["results"].values()) else "FAIL"
    return rec


# ---- 出力 ----

def _cell(v) -> str:
    return "PASS" if v is True else ("FAIL" if v is False else "-")


def print_summary(records: "list[dict]") -> None:
    """ASCII のみの要約表をコンソールへ出す（和文は report ファイル側）。"""
    headers = ["KIND", "MD", "STATUS"] + CHECKS
    rows = [[r["kind"], r["ident"], r["status"]] + [_cell(r["results"][c]) for c in CHECKS]
            for r in records]
    widths = [max(len(str(x)) for x in [h] + [row[i] for row in rows])
              for i, h in enumerate(headers)]
    sep = "-+-".join("-" * w for w in widths)
    print(" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print(sep)
    for row in rows:
        print(" | ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def write_report(records: "list[dict]", report_path: Path, out_dir: Path) -> None:
    """和文を含む詳細を UTF-8 で書き出す（コンソールへは出さない）。"""
    lines = ["# PDF レンダラ回帰チェック 詳細", "", f"出力先: {out_dir}", ""]
    for r in records:
        lines.append(f"## {r['kind']}  [{r['status']}]  md={r['ident']}")
        if r["md"]:
            lines.append(f"- md: {r['md']}")
        for c in CHECKS:
            lines.append(f"- {c}: {_cell(r['results'][c])}")
        if r["details"]:
            lines.append("")
            lines.append("### 該当箇所")
            for d in r["details"][:200]:
                lines.append(f"- {d}")
            if len(r["details"]) > 200:
                lines.append(f"- …ほか {len(r['details']) - 200} 件")
        lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="shared PDF renderer regression checks (lib/md_to_pdf.py)")
    parser.add_argument(
        "--kinds",
        help="comma separated report kinds (default: all in KIND_CONFIG). e.g. --kinds stock,macro")
    parser.add_argument(
        "--out-dir",
        help="scratch dir for rendered PDFs (default: a temp dir; never report_pdfs/)")
    parser.add_argument(
        "--report", help="path of the UTF-8 detail report (default: <out-dir>/render_regressions.md)")
    parser.add_argument(
        "--keep", action="store_true", help="keep the rendered PDFs for manual inspection")
    args = parser.parse_args()

    if args.kinds:
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
        unknown = [k for k in kinds if k not in KIND_CONFIG]
        if unknown:
            print(f"ERROR: unknown kind(s): {','.join(unknown)}")
            print(f"  available: {','.join(KIND_CONFIG)}")
            return 1
    else:
        kinds = list(KIND_CONFIG)

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
        made_temp = False
    else:
        out_dir = Path(tempfile.mkdtemp(prefix="render_regressions_"))
        made_temp = True
    out_dir.mkdir(parents=True, exist_ok=True)
    # 本番成果物を絶対に触らない（--out-dir 誤指定の保険）
    real_pdfs = (REPO_ROOT / "bi" / "outputs" / "report_pdfs").resolve()
    if out_dir == real_pdfs:
        print(f"ERROR: --out-dir must not be the real artifact dir: {real_pdfs}")
        return 1

    report_path = Path(args.report).expanduser().resolve() if args.report \
        else out_dir / "render_regressions.md"

    print(f"out-dir: {out_dir}")
    records = []
    for kind in kinds:
        print(f"[{kind}] rendering...", flush=True)
        records.append(check_kind(kind, out_dir))

    print("")
    print_summary(records)
    write_report(records, report_path, out_dir)
    print("")
    print(f"detail report: {report_path}")

    failed = [r["kind"] for r in records if r["status"] == "FAIL"]
    skipped = [r["kind"] for r in records if r["status"] == "SKIPPED"]
    if skipped:
        print(f"SKIPPED (no md on disk): {','.join(skipped)}")
    if failed:
        print(f"RESULT: FAIL ({len(failed)}/{len(records)}) -> {','.join(failed)}")
    else:
        print(f"RESULT: PASS ({len(records) - len(skipped)} checked, {len(skipped)} skipped)")

    if not args.keep:
        for r in records:
            pdf = out_dir / f"{r['kind']}_{r['ident']}.pdf"
            if pdf.exists():
                pdf.unlink()
        if made_temp and not any(out_dir.iterdir() if out_dir.exists() else []):
            shutil.rmtree(out_dir, ignore_errors=True)
    else:
        print(f"kept PDFs in: {out_dir}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
