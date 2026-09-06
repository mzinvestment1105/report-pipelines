#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_stock_brief.py

Pre-compile a writing brief for the single-stock deep-dive writer agent.

Reads three source rule files, extracts only the heading blocks that the
writing agent actually needs to produce the report body, and emits a single
compact file at agents/_compiled/stock_brief.md.

Extraction is purely mechanical (heading-block allowlist). No LLM involved.

Usage:
    python bi/pipelines/ops/build_stock_brief.py            # unconditional rebuild
    python bi/pipelines/ops/build_stock_brief.py --check    # rebuild only when sources changed

Notes:
    - Never prints Japanese (cp932 consoles break). ASCII only on stdout.
    - Writes a detailed log to bi/outputs/logs/build_stock_brief.log (append).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]

SRC_ANALYST = REPO_ROOT / "agents" / "stock_analyst.md"
SRC_COMMON = REPO_ROOT / "prompts" / "_common_rules.md"
SRC_ENTRY = REPO_ROOT / "playbook" / "entry_exit_rules.md"

OUT_PATH = REPO_ROOT / "agents" / "_compiled" / "stock_brief.md"
LOG_PATH = REPO_ROOT / "bi" / "outputs" / "logs" / "build_stock_brief.log"

SOURCES: List[Tuple[str, Path]] = [
    ("stock_analyst", SRC_ANALYST),
    ("common_rules", SRC_COMMON),
    ("entry_exit_rules", SRC_ENTRY),
]

# --------------------------------------------------------------------------
# Allowlists
#
# Each entry is the exact heading line text (without the leading "#" marks and
# without surrounding whitespace) of a block to keep. A kept block carries its
# heading line plus every line until the next heading of ANY level -- child
# headings must therefore be listed explicitly when they are wanted.
#
# Selection principle: keep what the writer needs to compose the report body
# (section list, per-section required elements, prose discipline, prohibitions,
# rating/target-price rules, table discipline, jargon annotation).
# Drop execution procedure, ETL, Discord, other report types, PM-thesis mode.
# --------------------------------------------------------------------------

ALLOW_ANALYST: List[str] = [
    # --- cross-section writing discipline ---
    "専門用語ルール（全セクション共通）",
    "未取得の告白を誌面に書くことの全面禁止（全セクション共通・絶対遵守・PM 2026-08-27 明示指示）",
    "財務データの鮮度（全セクション共通・絶対遵守・PM 2026-08-30 明示指示）",
    "誌面の書き方（全セクション共通・絶対遵守・PM 2026-08-30 明示指示）",
    "A. 結論先行（最重要・PM 2026-08-30）",
    "B. 一度だけの原則（PM 2026-08-30）",
    "数値の住所表（各事実の所有セクション・重複禁止の運用基準）",
    "C. 表の規律（PM 2026-08-30）",
    "D. 説明義務と因果の規律（PM 2026-08-30）",
    "E. 読みやすさ（PM 2026-08-30）",
    "F. 表記の統一（PM 2026-09-06 指摘・機械検査 error）",
    # --- report structure: every section the writer must emit ---
    "レポート構成",
    "事業モデル（200〜250文字・必須）",
    "直近材料・カタリスト（必須・事実 + 解釈）",
    "直近 1 週間のハイライト",
    "株価が反応した上位 3 件",
    "「株価の反応」の記載禁止事項（PM 2026-09-06 明示指示）",
    "「なぜそう動いたか」の根拠の限定（PM 2026-09-06 明示指示）",
    "同義反復の禁止（PM 2026-09-06 明示指示）",
    "自社の開示が無い日の必須確認手順（PM 2026-09-06 明示指示）",
    "分量の上限（PM 2026-09-06 明示指示）",
    "順位付けと選定（従前どおり）",
    "1. 基本情報",
    "2. 3C分析・事業理解",
    "Customer（市場）",
    "Competitor（競合）",
    "Company（自社）",
    "3. 投資テーシス（なぜ今この銘柄か）",
    "一次情報ベースの判断（TDNet・EDINET・決算資料）",
    "期待 vs 実態ギャップ",
    "4. 業績トレンド",
    "4-B. 会社が公表した将来目標（必須・全銘柄）",
    "5. バリュエーション比較",
    "6. 財務健全性",
    "7. 大株主・増資傾向",
    "大株主のデータソースと基準日（必須・PM 2026-08-30 明示指示）",
    "会社との関係の記載必須（PM 2026-08-30 明示指示）",
    "資本異動・希薄化フルヒストリー（必須・PM 2026-06-14 明示指示）",
    "希薄化要因の記載必須項目（新株予約権・ストックオプション・転換社債）",
    "8. 需給分析",
    "統合テーブル（1 枚集約・全項目必須・以下の表のみ・他に需給表を追加しない）",
    "9. カタリスト",
    "ポジティブ（株価を上げるイベント・最大 5 件）",
    "ネガティブ（株価を下げるリスク・最大 5 件）",
    "10. テクニカル",
    "出力フォーマット（散文 + 必要数値のみ）",
    "11. リスク（必須・最低3項目）",
    "12. 目標株価・結論",
    "廃止セクション（絶対出力禁止・PM 2026-05-30 確定）",
    # --- quality gate ---
    # NOTE: 「完了条件（Write 直前・簡易ゲート）」は上記各節の再掲であり、
    #       行数予算のため意図的に除外している（正本は agents/stock_analyst.md）。
    "❌ やってはいけないこと",
    "✅ 必ず守ること",
]

# NOTE: 意図的に除外した節（他レポート種別専用・執筆に不要・上位節と重複）:
#   §2/§2-C（動意の銘柄行）・§6（VIX）・§10（動意 raw）・§12（不可逆操作）・
#   §13/§14（Deep Research・WebSearch の手順）・§15・§17・§19・§21/§21-A（マクロ市況）・
#   §22（マクロ）・§23/§23-A（イベントカレンダー）・§31（動意）・§33（送信形式）・
#   §35（夕方レポート）・§37（ファイル命名）・§38（テーマ2部）・付録A（grep 手順）
ALLOW_COMMON: List[str] = [
    # Kept: rules with no equivalent in Part 1. Sections whose content is
    # already stated by the stock_analyst blocks above are dropped to stay
    # inside the line budget (they remain binding via prompts/_common_rules.md).
    "1. ETF・REIT・上場投信は全レポート全セクションから完全除外",
    "3. 時刻は和文12時間制・米国時間の時計表記は禁止",
    "4. 英語原文の転記は完全禁止・全レポート日本語完結",
    "5. 専門用語に中学生レベル注釈必須・投資用語は注釈完全禁止",
    "7. 銘柄名は必ず銘柄コードとセット",
    "8. 記憶ベース・推測ベース発言の全面禁止（全発話）",
    "9. ローソク足の判定は OHLC 4 点全部を確認",
    "18. 信用残データの読み方",
    "20. 外部要因の業績影響は四半期粒度で検証",
    "24. 太字（`**`）使用節度ルール・全レポート横断",
    "26. 事業モデルは「中学生が読んで何の会社かわかる」が絶対基準（全レポート横断）",
    "27. 材料は「事実」と「解釈」の両方記載必須（全レポート横断）",
    "32. 信用倍率の出力禁止・信用残は割合表記（全レポート横断）",
    "39. 表のスカスカ禁止（全レポート横断）",
    "40. レポート内相互参照の全面禁止（全レポート種別・全セクション横断）",
    "41. 見出しの装飾禁止・素のタイトル表記（全レポート種別・全見出しレベル横断）",
    "45. 固有名詞の表記統一・本文への内部タグ残存の禁止（全レポート横断・PM 2026-09-06 指摘）",
]

ALLOW_ENTRY: List[str] = [
    "3. エントリー基準",
    "3-1. 必須条件（全て満たすこと）",
    "3-4. 飛びつき禁止",
    "3-5. ボリンジャーバンドのエントリー禁止帯",
    "3-6. 銘柄選定の禁止リスト",
    "3-7. 分割エントリーシステム（必須）",
    "3-8. 加点条件（満たすほど確信度UP）",
    "3-9. 信用需給の実戦ルール（信用倍率は使わない・**5軸評価**）",
    "4. イグジット基準",
    "4-1. チャート崩れたら即売り（PM最大の苦手領域）",
    "4-2. ボリンジャーバンドの利確ルール",
    "4-3. 損切りルール",
    "4-4. 利確で迷ったら半分売り",
    "4-6. ファンダメンタルズ前提崩壊時",
    "6. ポジションサイズ・分散ルール",
    "ポジションサイズ",
]

ALLOWLISTS: Dict[str, List[str]] = {
    "stock_analyst": ALLOW_ANALYST,
    "common_rules": ALLOW_COMMON,
    "entry_exit_rules": ALLOW_ENTRY,
}

SECTION_TITLES: Dict[str, str] = {
    "stock_analyst": "第1部 誌面規定（出典: agents/stock_analyst.md）",
    "common_rules": "第2部 全レポート共通規定（出典: prompts/_common_rules.md）",
    "entry_exit_rules": "第3部 売買規律（出典: playbook/entry_exit_rules.md）",
}

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

# --------------------------------------------------------------------------
# Text rewrites
#
# The /stock-report skill was restructured (Step 1-B..1-G folded into Step 1;
# Step 4.3/4.4/4.4b folded into the writer's input conditions). Source files
# still carry the old Step numbers, and this compiler must not edit them.
# So the brief generalises every concrete Step reference on the way out.
# --------------------------------------------------------------------------

STEP_REWRITES: List[Tuple[str, str]] = [
    (r"Step\s*1-[A-G]\s*〜\s*(?:Step\s*)?1-[A-G]", "の生データ収集工程"),
    (r"Step\s*1-[A-G]\s*・\s*Step\s*1-[A-G]", "の生データ収集工程"),
    (r"Step\s*1-[A-G]", "の生データ収集工程"),
    (r"Step\s*4\.4b", "の執筆入力条件"),
    (r"Step\s*4\.[0-9]+(?:\s*の[^\s。、]*)?", "の執筆入力条件"),
    (r"Step\s*5\.5\s*の項目\s*[0-9]+\s*・\s*[0-9]+", "の送信前品質ゲート"),
    (r"Step\s*5\.5\s*の[^\s。、]*", "の送信前品質ゲート"),
    (r"Step\s*5\.5", "の送信前品質ゲート"),
    (r"Step\s*[0-9]+(?:\.[0-9]+)?(?:-[0-9A-Za-z]+)?", "の該当工程"),
]
STEP_REWRITES_C = [(re.compile(p), r) for p, r in STEP_REWRITES]

# Tidy the artefacts the substitutions can leave behind.
PHRASE = r"(?:生データ収集工程|執筆入力条件|送信前品質ゲート|該当工程)"
CLEANUP_REWRITES = [
    # ") <phrase>" -> ") の<phrase>"  (restore the particle eaten with "Step")
    (re.compile(r"\)\s+(" + PHRASE + r")"), r") の\g<1>"),
    # "<phrase> で" -> "<phrase>で"   (no ASCII space before a Japanese particle)
    (re.compile(r"(" + PHRASE + r")\s+([でにをはがのと])"), r"\g<1>\g<2>"),
    (re.compile(r"[ \t]{2,}"), " "),
]

STEP_DETECT = re.compile(r"Step\s*[0-9]")


def rewrite_steps(line: str) -> str:
    out = line
    for pat, repl in STEP_REWRITES_C:
        out = pat.sub(repl, out)
    if out != line:
        for pat, repl in CLEANUP_REWRITES:
            out = pat.sub(repl, out)
    return out.rstrip()


# --------------------------------------------------------------------------
# Line-level drops
#
# Lines that describe execution procedure, ETL, gate execution or Discord
# sending are not needed by the writer. Dropping them at line granularity (as
# opposed to heading granularity) keeps every required element while cutting
# the parts of a kept block that belong to the operator, not the author.
# --------------------------------------------------------------------------

DROP_LINE_PATTERNS = [
    # --- operator-facing procedure (the writer does not run these) ---
    r"保存前の機械検査は",
    r"保存前の検査は",
    r"保存前の注釈自己検証は",
    r"送信前の品質ゲート実行手順",
    r"詳細 memory:",
    r"詳細な品質ゲートは",
    r"^\s*[-*]\s*※誌面共通ルールは",
    r"実行手順は\s*\[/stock-report\]",
    r"取得手順の正本は",
    r"列名・取得コードは",
    r"\*\*機械検査\*\*",
    r"\*\*検証\*\*:",
    r"bi/pipelines/lib/",
    r"check_mover_counts\.py",
    r"レンダラが",
    r"@media print",
    r"break-inside",
    r"break-after",
    r"PDF 生成後に各ページ",
    # --- rules that only bind other report types ---
    r"動意日次・動意週次・夜間PTS",
    r"テーマ2部と同じブロック構成",
    r"本日のテーマ・単独材料・初動候補テーマ",
    r"改ページ由来",
    r"適用範囲.*(マクロ|セクター週次|動意)",
    r"^\s*-\s*\*\*適用範囲\*\*",
    # --- rationale-only prose that repeats a rule already stated ---
    r"^\s*-\s*(注意|補足|例外（誤検出)",
]
DROP_LINE_C = [re.compile(p) for p in DROP_LINE_PATTERNS]


def should_drop(line: str) -> bool:
    return any(p.search(line) for p in DROP_LINE_C)


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


PAREN_SUFFIX = re.compile(r"[（(][^（()）]*[）)]\s*$")


def norm_heading(title: str) -> str:
    """Heading text with any trailing parenthetical annotation removed.

    Source headings carry editable suffixes like "（PM 2026-08-30・2026-09-05
    改定）". Those change whenever a rule is revised, so they must not take
    part in matching.
    """
    prev = None
    out = title.strip()
    while out != prev:
        prev = out
        out = PAREN_SUFFIX.sub("", out).strip()
    return out


def match_allow(title: str, allow: List[str], allow_set: set) -> str:
    """Return the allowlist entry this heading satisfies, else None."""
    if title in allow_set:
        return title
    n = norm_heading(title)
    for entry in allow:
        if n == norm_heading(entry):
            return entry
    return None


def split_blocks(text: str) -> List[Tuple[int, str, List[str]]]:
    """Split markdown into (level, title, body_lines) blocks at any heading."""
    blocks: List[Tuple[int, str, List[str]]] = []
    cur_level = 0
    cur_title = ""
    cur_body: List[str] = []
    started = False
    in_fence = False

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            if started:
                blocks.append((cur_level, cur_title, cur_body))
            cur_level = len(m.group(1))
            cur_title = m.group(2)
            cur_body = []
            started = True
        else:
            if started:
                cur_body.append(line)
    if started:
        blocks.append((cur_level, cur_title, cur_body))
    return blocks


def trim(body: List[str]) -> List[str]:
    """Drop leading/trailing blank lines and trailing horizontal rules."""
    out = list(body)
    while out and not out[-1].strip():
        out.pop()
    while out and out[-1].strip() in ("---", "***", "___"):
        out.pop()
        while out and not out[-1].strip():
            out.pop()
    while out and not out[0].strip():
        out.pop(0)
    return out


BULLET_RE = re.compile(r"^\s*([-*+]\s|\d+[.)]\s)")


def compact(body: List[str]) -> List[str]:
    """Remove blank lines that carry no markdown meaning.

    Markdown only needs a blank line to separate a paragraph from a preceding
    paragraph, a list from a preceding paragraph, or a table from a preceding
    paragraph. Blank lines between consecutive bullets, between consecutive
    table rows, or right after a heading are pure line-count overhead.
    """
    out: List[str] = []
    in_fence = False
    for line in body:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if stripped:
            out.append(line)
            continue
        # blank line: keep only when it actually separates two blocks of
        # different kinds (previous is prose/table, next is prose/table).
        out.append(line)

    # second pass: drop a blank line when the surrounding lines are of the
    # same kind (bullet/bullet, table/table) or when it leads/trails a block.
    def kind(s: str) -> str:
        t = s.strip()
        if not t:
            return "blank"
        if t.startswith("|"):
            return "table"
        if BULLET_RE.match(s):
            return "bullet"
        return "prose"

    squeezed: List[str] = []
    in_fence = False
    for i, line in enumerate(out):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            squeezed.append(line)
            continue
        if in_fence or stripped:
            squeezed.append(line)
            continue
        prev_kind = kind(squeezed[-1]) if squeezed else "blank"
        nxt = ""
        for j in range(i + 1, len(out)):
            if out[j].strip():
                nxt = out[j]
                break
        next_kind = kind(nxt) if nxt else "blank"
        if prev_kind == "blank" or next_kind == "blank":
            continue  # leading/trailing/duplicate blank
        if prev_kind == next_kind and prev_kind in ("bullet", "table"):
            continue  # same-kind neighbours need no separator
        if next_kind == "bullet":
            # A list directly after a paragraph still renders as a list;
            # only a table genuinely needs the separating blank line.
            continue
        if prev_kind == "bullet" and next_kind == "prose":
            continue
        squeezed.append(line)

    while squeezed and not squeezed[0].strip():
        squeezed.pop(0)
    while squeezed and not squeezed[-1].strip():
        squeezed.pop()
    return squeezed


def filter_body(body: List[str]) -> List[str]:
    """Apply line drops and Step rewrites, at whole-bullet granularity.

    A dropped bullet takes its wrapped continuation lines, its nested
    sub-bullets and any fenced example with it, so the brief never keeps an
    orphaned fragment of a rule it decided to omit.
    """
    out: List[str] = []
    i = 0
    n = len(body)
    while i < n:
        line = body[i]
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = s[:3]
            out.append(line)
            i += 1
            while i < n:
                out.append(body[i])
                if body[i].strip().startswith(fence):
                    i += 1
                    break
                i += 1
            continue
        if should_drop(line):
            indent = len(line) - len(line.lstrip())
            i += 1
            # swallow the dropped bullet's continuation / nested block
            while i < n:
                nxt = body[i]
                ns = nxt.strip()
                if not ns:
                    break
                nindent = len(nxt) - len(nxt.lstrip())
                is_new_bullet = bool(BULLET_RE.match(nxt)) and nindent <= indent
                if is_new_bullet or ns.startswith("|") or ns.startswith("#"):
                    break
                if ns.startswith("```") or ns.startswith("~~~"):
                    fence = ns[:3]
                    i += 1
                    while i < n:
                        if body[i].strip().startswith(fence):
                            i += 1
                            break
                        i += 1
                    continue
                i += 1
            continue
        out.append(rewrite_steps(line))
        i += 1
    return out


def extract(key: str, path: Path) -> Tuple[List[str], List[str], List[str]]:
    """Return (rendered_lines, kept_titles, missing_titles)."""
    allow = ALLOWLISTS[key]
    allow_set = set(allow)
    blocks = split_blocks(path.read_text(encoding="utf-8"))

    found: Dict[str, Tuple[int, List[str]]] = {}
    for level, title, body in blocks:
        key_title = match_allow(title, allow, allow_set)
        if key_title is not None and key_title not in found:
            found[key_title] = (level, trim(body))

    # Normalize heading depth: top-level source headings become "###" in the
    # brief so that the brief's own "##" part headers stay above them.
    rendered: List[str] = []
    kept: List[str] = []
    for title in allow:  # preserve allowlist order == source order
        if title not in found:
            continue
        level, body = found[title]
        depth = 3 + max(0, level - 2)
        depth = min(depth, 6)
        rendered.append("#" * depth + " " + title)
        if body:
            rendered.extend(compact(filter_body(body)))
        rendered.append("")
        kept.append(title)

    missing = [t for t in allow if t not in found]
    return rendered, kept, missing


def build() -> Tuple[str, List[str], List[str]]:
    """Return (content, kept_titles_flat, missing_titles_flat)."""
    hashes = {key: sha256_of(path) for key, path in SOURCES}

    head: List[str] = []
    head.append("<!-- AUTO-GENERATED by bi/pipelines/ops/build_stock_brief.py"
                " -- DO NOT EDIT BY HAND -->")
    for key, path in SOURCES:
        rel = path.relative_to(REPO_ROOT).as_posix()
        head.append("<!-- source-sha256: %s = %s -->" % (rel, hashes[key]))
    head.append("<!-- generated-at: %s -->"
                % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    head.append("")
    head.append("# 個別銘柄 Deep Dive 執筆ブリーフ（自動生成）")
    head.append("")
    head.append("本ファイルは執筆に必要な規定のみを機械抽出した集約版である。"
                "執筆時は本ファイルのみを読めばよく、"
                "agents/stock_analyst.md・prompts/_common_rules.md・"
                "playbook/entry_exit_rules.md を個別に読む必要はない。"
                "実行手順・ETL・送信手順は本ファイルの対象外であり、"
                "各スキル定義が正本となる。")
    head.append("")

    body: List[str] = []
    kept_all: List[str] = []
    missing_all: List[str] = []
    for key, path in SOURCES:
        rendered, kept, missing = extract(key, path)
        kept_all.extend("%s :: %s" % (key, t) for t in kept)
        missing_all.extend("%s :: %s" % (key, t) for t in missing)
        body.append("## " + SECTION_TITLES[key])
        body.extend(rendered)

    # collapse runs of >1 blank line, and drop blanks that sit immediately
    # before a heading (markdown does not need them).
    raw = head + body
    out: List[str] = []
    blank = 0
    for i, line in enumerate(raw):
        if line.strip():
            blank = 0
            out.append(line.rstrip())
        else:
            blank += 1
            if blank > 1:
                continue
            nxt = ""
            for j in range(i + 1, len(raw)):
                if raw[j].strip():
                    nxt = raw[j]
                    break
            if nxt.startswith("#"):
                continue
            out.append("")
    while out and not out[-1]:
        out.pop()

    return "\n".join(out) + "\n", kept_all, missing_all


def read_existing_hashes() -> Dict[str, str]:
    if not OUT_PATH.exists():
        return {}
    hashes: Dict[str, str] = {}
    with OUT_PATH.open("r", encoding="utf-8") as fh:
        for _ in range(20):
            line = fh.readline()
            if not line:
                break
            m = re.match(r"<!--\s*source-sha256:\s*(\S+)\s*=\s*([0-9a-f]{64})\s*-->", line.strip())
            if m:
                hashes[m.group(1)] = m.group(2)
    return hashes


def current_hashes() -> Dict[str, str]:
    return {path.relative_to(REPO_ROOT).as_posix(): sha256_of(path)
            for _key, path in SOURCES}


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def main() -> int:
    ap = argparse.ArgumentParser(description="Compile the stock deep-dive writing brief.")
    ap.add_argument("--check", action="store_true",
                    help="rebuild only when source SHA256 differs from the emitted brief")
    args = ap.parse_args()

    for _key, path in SOURCES:
        if not path.exists():
            print("ERROR: missing source: %s" % path.as_posix())
            return 1

    if args.check:
        if read_existing_hashes() == current_hashes():
            print("no change")
            log("check: no change")
            return 0
        print("sources changed; rebuilding")

    content, kept, missing = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(content, encoding="utf-8")

    n_lines = content.count("\n")
    n_chars = len(content)
    print("wrote %s" % OUT_PATH.relative_to(REPO_ROOT).as_posix())
    print("lines=%d chars=%d kept_sections=%d missing_sections=%d"
          % (n_lines, n_chars, len(kept), len(missing)))
    if missing:
        print("WARNING: %d allowlisted heading(s) not found in sources"
              " (see log)" % len(missing))

    log("build: lines=%d chars=%d kept=%d missing=%d" % (n_lines, n_chars, len(kept), len(missing)))
    for t in kept:
        log("  kept   : %s" % t)
    for t in missing:
        log("  MISSING: %s" % t)

    return 0


if __name__ == "__main__":
    sys.exit(main())
