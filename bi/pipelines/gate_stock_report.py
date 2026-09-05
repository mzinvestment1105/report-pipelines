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

# Windows 既定の cp932 だと日本語メッセージや「≈」等の記号を print した瞬間に
# UnicodeEncodeError で落ち、肝心の最終判定行（GATE: PASS / FAIL）が出力されない。
# 標準出力・標準エラーを UTF-8（変換不能文字は置換）へ張り替えて出力を守る。
# 再設定に失敗しても検査そのものは続行する（フェイルオープン）。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

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


# --- 希薄化・資本異動セクションの記載形式検査（PM 2026-09-05）-------------------
# 回数は希薄化の大きさを表さないため、回数列を禁止し希薄化率（%）を必須とする。
# 生株数（分割前・分割後）を主表示にすることも禁止する。
_DILUTION_HEADING = re.compile(r"^#{2,6}\s.*(希薄化|資本異動)", re.M)
# 回数だけの列（「実施回数」「発行回数」「回数」）
_COUNT_COL = re.compile(r"回数")
# 生株数の分割前後を列に出すことの禁止
_SPLIT_COL = re.compile(r"分割前|分割後")
# 希薄化率の記載（「希薄化」または「潜在」と % が同一行に共起する）
_DILUTION_PCT = re.compile(r"(希薄化|潜在)[^\n]*[0-9０-９][^\n]*[%％]|[0-9０-９][^\n]*[%％][^\n]*(希薄化|潜在)")

DILUTION_PCT_REMEDY = (
    "各資本異動について (1) 当時の発行済株式数に対する希薄化率（%）"
    " (2) 現在の発行済株式数に対する比率（%）を書く"
    "（分母の出典と基準日を併記する）"
)
DILUTION_COUNT_REMEDY = (
    "回数の列・行を削除し、発行株数と希薄化率（%）の列へ置き換える"
    "（回数は本文の一文としてのみ補足する）"
)
DILUTION_SPLIT_REMEDY = (
    "分割前・分割後の生株数の列を削除し、"
    "「現在の発行済株式数に対する潜在希薄化率（%）」を主表示にする"
    "（換算の前提は表の直下の文章で書く）"
)


def _dilution_ranges(md: str) -> list[tuple[int, int]]:
    """希薄化・資本異動セクションの (開始行, 終了行) を 0 始まりで返す。

    見出しの次の行から、同じかそれより浅いレベルの見出しの直前までを範囲とする。
    """
    lines = md.replace("\r\n", "\n").split("\n")
    ranges: list[tuple[int, int]] = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        m = re.match(r"^(#{2,6})\s.*(希薄化|資本異動)", s)
        if not m:
            continue
        level = len(m.group(1))
        end = len(lines)
        for j in range(i + 1, len(lines)):
            m2 = re.match(r"^(#{2,6})\s", lines[j].strip())
            if m2 and len(m2.group(1)) <= level:
                end = j
                break
        ranges.append((i, end))

    # 見出しではなく本文の導入文（「増資・希薄化の履歴は以下のとおり」等）で
    # 始まる表も対象にする。導入文からの 12 行を範囲に加える。
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("#") or s.startswith("|"):
            continue
        if not re.search(r"(増資|希薄化|資本異動)[^\n]{0,20}(履歴|一覧|推移|内訳)", s):
            continue
        if any(lo <= i < hi for lo, hi in ranges):
            continue
        ranges.append((i, min(len(lines), i + 12)))

    return ranges


def _check_dilution(md: str) -> list[str]:
    """希薄化・資本異動セクションの回数列禁止・希薄化率必須・生株数列禁止を検査する。

    該当セクションが無い誌面では検査をスキップして空リストを返す。
    """
    ranges = _dilution_ranges(md)
    if not ranges:
        return []

    errs: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")

    # (i) 表ヘッダの禁止列（回数 / 分割前 / 分割後）
    for hline, header, _rows in _tables_with_lines(md):
        if not any(lo < hline - 1 <= hi for lo, hi in ranges):
            continue
        bad_count = [c for c in header if _COUNT_COL.search(c)]
        if bad_count:
            errs.append(
                f"[回数列の禁止] L{hline} の表ヘッダに回数の列がある: "
                f"{' / '.join(bad_count)}"
                f" → 対処: {DILUTION_COUNT_REMEDY}"
            )
        bad_split = [c for c in header if _SPLIT_COL.search(c)]
        if bad_split:
            errs.append(
                f"[生株数の列の禁止] L{hline} の表ヘッダに分割前後の株数の列がある: "
                f"{' / '.join(bad_split)}"
                f" → 対処: {DILUTION_SPLIT_REMEDY}"
            )

    # (ii) セクション内に希薄化率（%）の記載が 1 件も無い
    for lo, hi in ranges:
        body = "\n".join(lines[lo:hi])
        if not _DILUTION_PCT.search(body):
            title = lines[lo].strip().lstrip("# ").strip()
            errs.append(
                f"[希薄化率（%）の欠落] L{lo + 1}「{title}」に "
                "希薄化率・潜在希薄化率の % 表記が 1 件も無い。"
                f" → 対処: {DILUTION_PCT_REMEDY}"
            )

    return errs


# --- IPO ロックアップ節の必須化検査（PM 2026-09-05）---------------------------
# 上場から 2 年以内の銘柄は §7-B「IPO ロックアップ」を必須とする。
_LOCKUP_HEADING = re.compile(r"^#{2,6}\s.*(ロックアップ|IPO)", re.M)
_LOCKUP_PCT = re.compile(r"ロックアップ[^\n]*[0-9０-９][^\n]*[%％]|[0-9０-９][^\n]*[%％][^\n]*ロックアップ")
# 本文中の「YYYY年M月D日付で…上場いたしました」「YYYY年M月の上場」等から上場年月を拾う。
# 「上場」が先に来る形（「上場…YYYY年M月」）は、上場と無関係の後段の日付を拾う事故が
# あるため採らない。日付が先行し、その直後に上場が来る形のみを採用する。
_LISTING_DATE = re.compile(
    r"(20[0-9]{2})\s*年\s*([0-9]{1,2})\s*月(?:\s*([0-9]{1,2})\s*日)?"
    r"\s*(?:付|付け)?\s*(?:で|に|の)?\s*"
    r"(?:東京証券取引所[^\n]{0,12}?)?(?:に)?(?:株式を)?(?:新規)?上場"
)

LOCKUP_REMEDY = (
    "上場 2 年以内の銘柄は §7-B「IPO ロックアップ・新規上場株主分析」を必須とし、"
    "(1) 株主ごとのロックアップ条件と対象株数の発行済比% "
    "(2) 各大株主の現在の保有比率と上場時からの増減（基準日併記） "
    "(3) 各株主の会社との関係 "
    "(4) 解除済・解除予定株数の発行済比%と 5 日平均出来高に対する日数換算 を書く"
)

# --- §7-B 内の必須要素検査（PM 2026-09-06 指摘・_common_rules §42）------------
# 上場 2 年以内の銘柄は §7-B 冒頭に IPO 価格情報ブロック（想定価格・仮条件・公募価格・
# 初値・初値の公募価格比・上場日・主幹事）と、株主×ロックアップ対応表を必ず置く。
PRICE_BLOCK_REMEDY = (
    "§7-B の冒頭に IPO 価格情報ブロックを置き、想定価格・仮条件（下限〜上限）・"
    "公募価格（決定価格）・初値・初値の公募価格比（円と %）・上場日・主幹事を全て書く"
    "（出典は有価証券届出書および訂正届出書）。公募価格・初値を他セクションへ散らさない"
)
SHAREHOLDER_TABLE_REMEDY = (
    "§7-B に株主×ロックアップ対応表（3 列固定「株主名 / 上場時発行済比% / "
    "ロックアップ条件」）をパイプ記法の表で置く。上場時の上位 10 名以上を列挙し、"
    "現在上位から外れた株主も行として残す（同一条件でも行を集約しない）"
)
# §7-B 節の開始見出し（「7-B」「IPO ロックアップ」等）
_LOCKUP_SECTION_START = re.compile(r"^(#{2,6})\s.*(ロックアップ|IPO).*$", re.M)
# パイプ記法の表とみなす行（行頭が | ）
_PIPE_ROW = re.compile(r"^\s*\|")


def _lockup_section_body(md: str) -> str:
    """§7-B（IPO ロックアップ節）の本文だけを切り出す。

    見出しが見つからなければ空文字。節の終端は「同じ階層以上の次の見出し」とする。
    """
    m = _LOCKUP_SECTION_START.search(md)
    if not m:
        return ""
    level = len(m.group(1))
    start = m.end()
    tail = md[start:]
    for nxt in re.finditer(r"^(#{1,6})\s", tail, re.M):
        if len(nxt.group(1)) <= level:
            return tail[: nxt.start()]
    return tail


def _has_pipe_table(text: str) -> bool:
    """行頭 | の行が 3 行以上連続するブロックが 1 つでもあれば True。"""
    run = 0
    for line in text.splitlines():
        if _PIPE_ROW.match(line):
            run += 1
            if run >= 3:
                return True
        else:
            run = 0
    return False


def _earliest_listing_ym(text: str) -> str | None:
    """本文から「YYYY年M月[D日]付で…上場」を全件拾い、最も古い年月を返す。

    生データは複数の開示を連結したものであり、後段に上場と無関係の日付が並ぶ。
    上場日は各開示に繰り返し現れるため、最古を採るのが最も安定する。
    """
    yms = []
    for m in _LISTING_DATE.finditer(text):
        try:
            yms.append(f"{m.group(1)}-{int(m.group(2)):02d}")
        except (TypeError, ValueError):
            continue
    return min(yms) if yms else None


def _listing_date(md: str, code: str) -> tuple[str | None, str]:
    """(上場日 YYYY-MM-DD 相当, 取得元) を返す。判定できなければ (None, 理由)。

    優先順位: (1) 生データ research/stocks/{code}_*_data.md
              (2) screening_master の上場日列
              (3) レポート本文の「上場」+ 年月の記載
    """
    # (1) 生データ
    for p in sorted((REPO_ROOT / "research" / "stocks").glob(f"{code}_*_data.md")) + sorted(
        (REPO_ROOT / "research" / "stocks" / str(code)).glob(f"{code}_*_data.md")
    ):
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        ym = _earliest_listing_ym(txt)
        if ym:
            return ym, f"生データ {p.name}"

    # (2) screening_master の上場日列
    pq = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"
    if pq.exists():
        try:
            import pandas as pd

            head = pd.read_parquet(pq).head(0)
            col = next(
                (c for c in head.columns if ("上場" in c and "日" in c) or c in ("ListingDate", "IPODate")),
                None,
            )
            if col:
                df = pd.read_parquet(pq, columns=["Code", col])
                row = df[df["Code"].astype(str).str.upper() == str(code).upper()]
                if not row.empty and str(row.iloc[0][col]) not in ("nan", "NaT", ""):
                    return str(row.iloc[0][col])[:7], f"screening_master 列「{col}」"
        except Exception:  # noqa: BLE001
            pass

    # (3) レポート本文
    ym = _earliest_listing_ym(md)
    if ym:
        return ym, "レポート本文の「上場」記載"

    return None, "上場日を特定できず（検査スキップ）"


def _check_ipo_lockup(md: str, code: str) -> tuple[list[str], str]:
    """(errors, 判定に使った上場日の取得元メモ) を返す。"""
    ym, src = _listing_date(md, code)
    if not ym:
        return [], f"[IPO ロックアップ] {src}"

    from datetime import date

    y, mo = int(ym[:4]), int(ym[5:7])
    today = date.today()
    months = (today.year - y) * 12 + (today.month - mo)
    if months > 24:
        return [], f"[IPO ロックアップ] 上場 {ym}（{src}）→ 2 年超のため任意"

    note = f"[IPO ロックアップ] 上場 {ym}（{src}）→ 2 年以内のため必須"
    if not _LOCKUP_HEADING.search(md):
        return [
            f"[IPO ロックアップ節の欠落] 上場 {ym}（{src}）で 2 年以内だが "
            "§7-B「IPO ロックアップ」の見出しが無い。"
            f" → 対処: {LOCKUP_REMEDY}"
        ], note
    if not _LOCKUP_PCT.search(md):
        return [
            f"[IPO ロックアップの発行済比%欠落] 上場 {ym}（{src}）で 2 年以内。"
            "「ロックアップ」と % の共起が 1 件も無い。"
            f" → 対処: {LOCKUP_REMEDY}"
        ], note

    # --- §7-B 内の必須要素検査（PM 2026-09-06 指摘・_common_rules §42）---------
    errs: list[str] = []
    body = _lockup_section_body(md)
    if not body.strip():
        body = md  # 節の切り出しに失敗した場合は誌面全体を対象にして取りこぼしを防ぐ

    if not re.search(r"公募価格|公開価格", body):
        errs.append(
            f"[IPO 価格情報ブロックの欠落・公募価格] 上場 {ym}（{src}）で 2 年以内だが "
            "§7-B 内に「公募価格」「公開価格」のいずれの語も無い。"
            f" → 対処: {PRICE_BLOCK_REMEDY}"
        )
    if "初値" not in body:
        errs.append(
            f"[IPO 価格情報ブロックの欠落・初値] 上場 {ym}（{src}）で 2 年以内だが "
            "§7-B 内に「初値」の語が無い。公募価格と初値が揃わないと公募割れが読めない。"
            f" → 対処: {PRICE_BLOCK_REMEDY}"
        )
    if "仮条件" not in body:
        errs.append(
            f"[IPO 価格情報ブロックの欠落・仮条件] 上場 {ym}（{src}）で 2 年以内だが "
            "§7-B 内に「仮条件」の語が無い。"
            f" → 対処: {PRICE_BLOCK_REMEDY}"
        )
    if not _has_pipe_table(body):
        errs.append(
            f"[株主×ロックアップ対応表の欠落] 上場 {ym}（{src}）で 2 年以内だが "
            "§7-B 内にパイプ記法の表（行頭 | が 3 行以上連続）が 1 つも無い。"
            "株主ごとの条件を本文の羅列で済ませることを禁止する。"
            f" → 対処: {SHAREHOLDER_TABLE_REMEDY}"
        )
    return errs, note


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

    # 7b. 希薄化・資本異動セクションの記載形式（errors・PM 2026-09-05）
    #     回数列・分割前後の生株数列を禁止し、希薄化率（%）の記載を必須とする。
    dil_errs = _check_dilution(md)
    if dil_errs:
        errors.extend(dil_errs)
    else:
        if _DILUTION_HEADING.search(md):
            info.append("[希薄化の記載形式] 回数列なし・希薄化率（%）在籍")
        else:
            info.append("[希薄化の記載形式] 該当セクションなし → 検査スキップ")

    # 7c. IPO ロックアップ節の必須化（errors・PM 2026-09-05）
    lock_errs, lock_note = _check_ipo_lockup(md, code)
    if lock_errs:
        errors.extend(lock_errs)
    info.append(lock_note)

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
