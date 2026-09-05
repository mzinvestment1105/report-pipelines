"""個別銘柄レポートの送信前機械ゲート（PM 2026-08-30 承認）。

Step 5.5 の grep 系検査の機械化。判断が要る検査（数値の羅列・重複・希薄化4点・目視系）は
スキル側（.claude/commands/stock-report.md）に残置する。

本スクリプトは verify_report_numbers.verify()（数値整合の検算）を取り込み、加えて
誌面の書き方・禁止文言の grep 系検査をまとめて実行する。errors（送信停止）と
warnings（表示のみ・送信は継続）を区別する。

使い方:
    python gate_stock_report.py --code 7256 --md research/stocks/7256/2026-08-30.md
    python gate_stock_report.py --code 7256 --date 2026-08-30

exit 0 = PASS（送信可）/ exit 1 = FAIL（送信中止）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from table_rules import check_tables  # noqa: E402
from verify_report_numbers import verify as verify_numbers  # noqa: E402

# --- 必須セクション見出し（正本 = agents/stock_analyst.md §レポート構成）-----------
# 正本から `### {番号}. {見出し}` を読み取り、番号と主要語で緩くマッチさせる。
# 7-B（IPO ロックアップ）は IPO 銘柄のみのため任意とする。
STOCK_ANALYST_MD = REPO_ROOT / "agents" / "stock_analyst.md"
OPTIONAL_SECTIONS = {"7-B"}


def _required_sections() -> list[tuple[str, str]]:
    """正本から (番号, 主要語) のリストを読む。読めなければ空（検査をスキップ）。"""
    if not STOCK_ANALYST_MD.exists():
        return []
    out: list[tuple[str, str]] = []
    in_body = False
    for line in STOCK_ANALYST_MD.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("## レポート構成"):
            in_body = True
            continue
        if in_body and s.startswith("## ") and not s.startswith("## レポート構成"):
            break
        if not in_body:
            continue
        m = re.match(r"^###\s+([0-9]+(?:-[A-Z])?)\.?\s+(.+)$", s)
        if not m:
            continue
        num, title = m.group(1), m.group(2)
        if num in OPTIONAL_SECTIONS:
            continue
        # 「4. 業績トレンド」→ 主要語は括弧・注記を落とした先頭語
        key = re.split(r"[（(・]", title)[0].strip()
        if key:
            out.append((num, key))
    return out


# 希薄化（新株予約権）の必須 4 点のうち、行使価・期間・現在値との位置関係を検査する。
DILUTION_TERMS = {
    "行使価": r"行使価[額格]",
    "権利行使期間": r"行使期[間限]|権利行使期間",
    "現在値との位置関係": r"現在値|現株価|株価水準|行使圏|現値",
}

# --- errors 側の grep 検査（1 件でもヒットしたら送信中止） -------------------
# (name, pattern, ヒットした場合の対処)
FORBIDDEN_PATTERNS: list[tuple[str, str, str]] = [
    (
        "禁止文言（未取得の告白）",
        r"未取得|データ未取得|一次情報未取得|確認できておらず|確認次第|特定できていない"
        r"|特定できず|読んでいない|読めていない|取得を行っていない|記載は行わない|記載しない",
        "文言を消すのではなく (1) 一次情報で取得して実数で埋める "
        "(2) 該当行・該当セクションごと削除する のいずれかで直す",
    ),
    (
        "遡及説明（事後判明情報での値動き説明）",
        r"水面下|未開示|後から判明|事後的に判明|当時は開示されて",
        "開示前の値動きは当時公開されていた情報のみで説明し、できなければ要因不明と書く",
    ),
    (
        "推測語",
        r"可能性が高い|思われる|考えられる|だろう|とみられる|はずだ",
        "一次情報で事実を固め、その事実が支える範囲で断定的に書く",
    ),
    (
        "レポート内参照",
        r"前述|後述|上記の|下記の|前掲|後掲|セクション[0-9０-９]|§[0-9]",
        "参照をやめて値・条件・前提をその場で再掲する",
    ),
    (
        "33業種加重平均が主比較",
        r"セクター加重平均|業種加重平均|33分類",
        "事業実態で選んだ同業個社（Step 4.2 で確定した peers）との比較表へ差し替える",
    ),
]

# §1 基本情報の「セクター」行は 33 業種の分類名を正当に書く場所のため、
# 「33業種加重平均が主比較」の検出から除外する（比較表での使用は引き続き検出する）。
SECTOR_LABEL_ROW = re.compile(r"^\|\s*(セクター|業種|東証33業種)\s*\|")

# --- warnings 側 -----------------------------------------------------------
UNIT_IN_CELL = re.compile(r"[0-9][^|]*?(百万円|千円|億円|株|円|%|％)")
SEPARATOR_ROW = re.compile(r"^\|[\s:\-|]+\|$")
HEADER_UNIT = re.compile(r"（単位")


def _grep(md: str, pattern: str) -> list[tuple[int, str]]:
    pat = re.compile(pattern)
    out = []
    for i, line in enumerate(md.splitlines(), 1):
        if pat.search(line):
            out.append((i, line.strip()))
    return out


def _check_unit_in_cell(md: str) -> list[tuple[int, str]]:
    """表のデータ行のセル内に単位が入っている行を拾う（誤検知しうるため warning）。"""
    out = []
    lines = md.splitlines()
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s.startswith("|"):
            continue
        if SEPARATOR_ROW.match(s) or HEADER_UNIT.search(s):
            continue
        # 次行が区切り行 = この行はヘッダ行。ヘッダへの単位表記は可
        if i < len(lines) and SEPARATOR_ROW.match(lines[i].strip()):
            continue
        cells = s.strip("|").split("|")
        if any(UNIT_IN_CELL.search(c) for c in cells):
            out.append((i, s))
    return out




# 銘柄コード（4 桁数字、または 3 桁数字 + 英字1文字。例 7203 / 485A）
_CODE_IN_CELL = re.compile(r"^[0-9]{3}[0-9A-Z]$")
# 同業比較表とみなす見出し（この直後の表を対象にする）
_PEER_HEADING = re.compile(r"^#{2,4}\s.*(バリュエーション比較|同業比較|競合比較)", re.M)
# 大株主表とみなす見出し
_SHAREHOLDER_HEADING = re.compile(r"^#{2,4}\s.*大株主", re.M)
# 基準日を示す語
_ASOF_TERMS = re.compile(r"時点|基準日|現在")


def _tables_with_lines(md: str) -> list[tuple[int, list[str], list[list[str]]]]:
    """(ヘッダ行番号, ヘッダ, 本文行) を返す。table_rules と同じ抽出をここでも使う。"""
    lines = md.replace("\r\n", "\n").split("\n")
    out = []
    i = 0
    while i < len(lines) - 1:
        cur, nxt = lines[i].strip(), lines[i + 1].strip()
        if cur.startswith("|") and cur.count("|") >= 2 and SEPARATOR_ROW.match(nxt):
            split = lambda s: [c.strip() for c in s.strip().strip("|").split("|")]
            header = split(cur)
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                if not SEPARATOR_ROW.match(lines[j].strip()):
                    rows.append(split(lines[j]))
                j += 1
            out.append((i + 1, header, rows))
            i = j
            continue
        i += 1
    return out


def _screening_codes() -> set[str] | None:
    """screening_master.parquet の Code 列。読めなければ None（検査スキップ）。"""
    pq = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"
    if not pq.exists():
        return None
    try:
        import pandas as pd

        df = pd.read_parquet(pq, columns=["Code"])
    except Exception:  # noqa: BLE001
        return None
    return {str(c).strip().upper() for c in df["Code"].dropna().tolist()}


def _check_peer_codes(md: str, code: str) -> str | None:
    """同業比較表に載る銘柄コードが screening_master に在籍するかを検査する。

    在籍しない = 上場廃止・コード誤りの可能性があるため error にする
    （正本 agents/stock_analyst.md §5「保存前 Grep チェック」の機械化）。
    """
    master = _screening_codes()
    if master is None:
        return None

    lines = md.replace("\r\n", "\n").split("\n")
    # 同業比較の見出し行番号を集める
    heads = [i + 1 for i, ln in enumerate(lines) if _PEER_HEADING.match(ln.strip())]
    if not heads:
        return None

    missing: list[str] = []
    for hline, header, rows in _tables_with_lines(md):
        # 見出しの直後（30 行以内）の表だけを同業比較表とみなす
        if not any(0 < hline - h <= 30 for h in heads):
            continue
        cand: list[str] = []
        # ヘッダに並ぶコード（`| 指標 | 485A | 6674 | …`）と、本文セルのコードの両方を見る
        cand.extend(c.upper() for c in header if _CODE_IN_CELL.match(c.upper()))
        for r in rows:
            cand.extend(c.upper() for c in r if _CODE_IN_CELL.match(c.upper()))
        for c in cand:
            if c != str(code).upper() and c not in master:
                missing.append(f"L{hline}: {c}")

    if missing:
        uniq = sorted(set(missing))
        return (
            f"[同業比較の銘柄コード] screening_master 未登録が {len(uniq)}件: "
            f"{' / '.join(uniq[:5])}"
            " → 対処: 上場廃止・コード誤りの可能性があるため、"
            "get_symbol_info で上場状態を確認し、在籍する同業へ差し替える"
        )
    return None


def _check_shareholder_asof(md: str) -> str | None:
    """大株主の表の前後 5 行以内に基準日を示す語があるかを検査する。"""
    lines = md.replace("\r\n", "\n").split("\n")
    heads = [i + 1 for i, ln in enumerate(lines) if _SHAREHOLDER_HEADING.match(ln.strip())]
    if not heads:
        return None

    for hline, header, rows in _tables_with_lines(md):
        if not any(0 < hline - h <= 30 for h in heads):
            continue
        if not any("株主" in c for c in header):
            continue
        lo = max(0, hline - 1 - 5)
        hi = min(len(lines), hline - 1 + len(rows) + 2 + 5)
        window = "\n".join(lines[lo:hi])
        if not _ASOF_TERMS.search(window):
            return (
                f"[大株主表の基準日] L{hline} の大株主表の前後 5 行に"
                "「時点」「基準日」「現在」のいずれもない。"
                " → 対処: 報告書の種別と時点を明記する"
                "（例「◯◯期半期報告書・◯年◯月末時点」）"
            )
    return None


# --- 「株価が反応した上位3件」ブロックの反応形式検査（PM 2026-09-05）-----------
# 直近材料の各件は「何が起きた」「株価の反応」「なぜそう動いたか」の3ラベルを持ち、
# 「株価の反応」は冒頭で方向（前日比 ±X%・陽線/陰線）を明示する。
_REACTION_HEADING = re.compile(r"株価が反応した上位\s*[0-9０-９]*\s*件")
_REACTION_LABELS = (
    ("**何が起きた**", r"\*\*何が起きた\*\*"),
    ("**株価の反応**", r"\*\*株価の反応\*\*"),
    ("**なぜそう動いたか**", r"\*\*なぜそう動いたか\*\*"),
)
# 符号付きパーセンテージ。全角マイナス（−・－）とハイフン各種を許容する。
_SIGNED_PCT = re.compile(r"[+＋\-−－—–]\s?[0-9０-９]+(?:[.．][0-9０-９]+)?\s*[%％]")
_CANDLE = re.compile(r"陽線|陰線")

REACTION_REMEDY = (
    "株価の反応は冒頭で方向（前日比 ±X%・陽線/陰線）を明示し、"
    "4本値（始値・高値・安値・終値）と出来高倍率を書く"
)


def _reaction_body(md: str, label_line_idx: int, lines: list[str]) -> str:
    """`**株価の反応**:` 行と、それに続く本文（次の空行または次のラベルまで）を返す。"""
    buf = [lines[label_line_idx]]
    for k in range(label_line_idx + 1, min(label_line_idx + 4, len(lines))):
        s = lines[k].strip()
        if not s or s.startswith("- **") or s.startswith("**"):
            break
        buf.append(lines[k])
    return "\n".join(buf)


def _check_reaction_format(md: str) -> list[str]:
    """反応3件ブロックのラベル・方向明示を検査する。

    「株価が反応した上位N件」の見出しが無い誌面（該当セクションを持たない
    レポート種別）では検査をスキップして空リストを返す。
    """
    if not _REACTION_HEADING.search(md):
        return []

    errs: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")

    # (a) 3ラベルがそれぞれ 3 回以上出現するか
    for name, pat in _REACTION_LABELS:
        n = len(re.findall(pat, md))
        if n < 3:
            errs.append(
                f"[反応形式のラベル欠落] 「{name}」の出現が {n} 回（3 回未満）。"
                f" → 対処: {REACTION_REMEDY}"
            )

    # (b)(c) 「株価の反応」の各行に 前日比 + 符号付き% + 陽線/陰線 があるか
    for i, raw in enumerate(lines):
        if not re.search(r"\*\*株価の反応\*\*", raw):
            continue
        body = _reaction_body(md, i, lines)
        lacking: list[str] = []
        if "前日比" not in body:
            lacking.append("「前日比」")
        if not _SIGNED_PCT.search(body):
            lacking.append("符号付きパーセンテージ（例 +9.5% / -9.88%）")
        if not _CANDLE.search(body):
            lacking.append("「陽線」または「陰線」")
        if lacking:
            errs.append(
                f"[反応形式の方向明示] L{i + 1} の株価の反応に "
                f"{' / '.join(lacking)} が無い。 → 対処: {REACTION_REMEDY}"
            )

    return errs


def run_gate(md: str, code: str) -> tuple[list[str], list[str], list[str]]:
    """(errors, warnings, info) を返す。errors が空なら送信可。"""
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []

    # 1. 禁止 grep 系（errors）
    for name, pattern, remedy in FORBIDDEN_PATTERNS:
        hits = _grep(md, pattern)
        if name.startswith("33業種"):
            hits = [(i, t) for i, t in hits if not SECTOR_LABEL_ROW.match(t)]
        if hits:
            detail = " / ".join(f"L{i}: {t[:60]}" for i, t in hits[:5])
            more = f"（他 {len(hits) - 5} 件）" if len(hits) > 5 else ""
            errors.append(f"[{name}] {len(hits)}件ヒット: {detail}{more} → 対処: {remedy}")
        else:
            info.append(f"[{name}] ヒットなし")

    # 2. 反応スコアの欠落（errors）
    n_score = len(re.findall("反応スコア", md))
    if n_score < 3:
        errors.append(
            f"[反応スコアの欠落] 「反応スコア」の出現が {n_score} 回（3 回未満）。"
            " → 対処: 直近材料の上位 3 件それぞれに反応スコア"
            "（日中値幅 × 出来高 5 日平均比）を数値で入れる"
        )
    else:
        info.append(f"[反応スコア] {n_score} 回（3 回以上）")

    # 2b. 反応3件の記述形式（errors・PM 2026-09-05）
    #     見出しが無い誌面ではスキップされる（_check_reaction_format 内でガード）。
    if _REACTION_HEADING.search(md):
        reaction_errs = _check_reaction_format(md)
        if reaction_errs:
            errors.extend(reaction_errs)
        else:
            info.append("[反応形式] 3ラベル・方向明示（前日比 ±X%・陽線/陰線）すべて在籍")
    else:
        info.append("[反応形式] 「株価が反応した上位N件」の見出しなし → 検査スキップ")

    # 3. 数値整合の検算（verify_report_numbers）
    v_err, v_warn = verify_numbers(md, code)
    errors.extend(v_err)
    warnings.extend(v_warn)
    if not v_err:
        info.append("[数値整合検算] OK")

    # 4. セル内単位（warnings）
    unit_hits = _check_unit_in_cell(md)
    if unit_hits:
        detail = " / ".join(f"L{i}: {t[:50]}" for i, t in unit_hits[:5])
        more = f"（他 {len(unit_hits) - 5} 件）" if len(unit_hits) > 5 else ""
        warnings.append(
            f"[セル内単位] {len(unit_hits)}件: {detail}{more}"
            " → 対処: セル内から単位を外し、表の外に「（単位: 百万円）」等でまとめて示す"
        )
    else:
        info.append("[セル内単位] ヒットなし")

    # 5. 折り返す表（errors・PM 2026-09-05 承認で warning → error へ格上げ）
    #    旧実装は「4 列以上の行数」を warning で報告するだけで送信を止めなかったため、
    #    3 列で 1 セル 50 字のシナリオ表・5 列の新株予約権表が素通りした。
    tbl_violations = check_tables(md)
    if tbl_violations:
        for v in tbl_violations[:8]:
            errors.append(f"[折り返す表] {v['message']}")
        if len(tbl_violations) > 8:
            errors.append(f"[折り返す表] 他 {len(tbl_violations) - 8} 件")
    else:
        info.append("[折り返す表] なし")

    # 6. 必須セクション見出しの欠落（errors）
    req = _required_sections()
    if not req:
        info.append("[必須セクション] 正本を読めず検査スキップ")
    else:
        missing = []
        for num, key in req:
            # 番号（`## 4.` / `### 4.`）と主要語のどちらかで緩くマッチさせる。
            num_pat = re.compile(r"^#{2,4}\s+" + re.escape(num) + r"[.\s]", re.M)
            key_pat = re.compile(r"^#{2,4}\s.*" + re.escape(key), re.M)
            if not (num_pat.search(md) and key_pat.search(md)):
                missing.append(f"{num}. {key}")
        if missing:
            errors.append(
                f"[必須セクションの欠落] {len(missing)}件: {' / '.join(missing)}"
                " → 対処: 正本 agents/stock_analyst.md §レポート構成の見出しを"
                "番号付きで追加し、中身を一次情報で埋める"
            )
        else:
            info.append(f"[必須セクション] {len(req)}件すべて在籍")

    # 7. 希薄化の記載 4 点（errors）
    if "新株予約権" in md:
        lacking = [
            name for name, pat in DILUTION_TERMS.items() if not re.search(pat, md)
        ]
        if lacking:
            errors.append(
                f"[希薄化の記載不足] 「新株予約権」を扱う誌面に {' / '.join(lacking)} が無い。"
                " → 対処: 回号ごとに 割当先・行使価額・権利行使期間・現在値との位置関係 の"
                "4 点をセットで書く（1 点でも欠けたら不合格）"
            )
        else:
            info.append("[希薄化の記載] 行使価・期間・現在値との位置関係すべて在籍")

    # 8. 同業比較表の銘柄コードが screening_master に在籍するか（errors）
    peer_err = _check_peer_codes(md, code)
    if peer_err:
        errors.append(peer_err)
    else:
        info.append("[同業比較の銘柄コード] screening_master 在籍を確認")

    # 9. 大株主表に基準日の明示があるか（errors）
    sh_err = _check_shareholder_asof(md)
    if sh_err:
        errors.append(sh_err)
    else:
        info.append("[大株主表の基準日] 明示あり")

    return errors, warnings, info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="銘柄コード")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--md", help="レポートの相対パス（--date より優先）")
    args = ap.parse_args()

    if args.md:
        md_path = Path(args.md)
        if not md_path.is_absolute():
            md_path = REPO_ROOT / args.md
    else:
        md_path = REPO_ROOT / "research" / "stocks" / args.code / f"{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: report not found: {md_path}")
        return 1

    errors, warnings, info = run_gate(md_path.read_text(encoding="utf-8"), args.code)

    print(f"GATE: {md_path.name}（{args.code}）")
    for m in info:
        print("  OK   " + m)
    for m in warnings:
        print("  WARN " + m)
    for m in errors:
        print("  NG   " + m)

    if errors:
        print("GATE: FAIL（送信中止）")
        return 1
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
