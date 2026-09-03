#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""X 投稿 文体チェッカー（research/sns/style_rules_v1.md §6 の機械検査59項目）

59項目の内訳:
  §6-1 禁止語        32項目（NG_*・うち §1-8 の却下表現10項目・§1-9 の一人称過去/経歴語り2項目・
                     §1-10 の丸括弧禁止1項目＝NG_PAREN）
  §6-2 数値レンジ    19項目（LEN_* / CNT_* / RATIO_*）
  §6-3 文末分布       3項目（DIST_*・2026-08-10 改定＝全文です・ます統一）
  §6-4 構成テンプレ   5項目（TPL_A〜TPL_E・宣言した型のみ判定・他は SKIP）

使い方:
  python bi/pipelines/check_x_post_style.py path/to/post.txt --type D
  python bi/pipelines/check_x_post_style.py --text "本文..." --type A --frame short
  python bi/pipelines/check_x_post_style.py path/to/posts.json          # 一括
  python bi/pipelines/check_x_post_style.py path/to/posts.json --json   # 機械可読出力

一括入力（.json）の形式:
  [{"id": "DL-1", "type": "D", "frame": "short", "text": "本文..."}, ...]
  frame は省略可（省略時は総字数から自動判定: 280字未満=短文枠／280字以上=長文枠）

判定: FAIL=送信中止／WARN=PM へ提示／PASS／SKIP=宣言型以外の型チェック
終了コード: FAIL が1件でもあれば 1、なければ 0
字数は全て「URL 除去後・空白（改行含む）除去後」の文字数。

引用の扱い: 語調系の禁止語（NG_REVELATION 等）は「」『』内の引用を除いた本文に対して
判定する（書籍の引用句を改変せずに使えるようにするための実装上の判断）。
引用内にのみ検出された場合は INFO 行として併記する。--strict-quotes で引用も含めて判定。
文末判定（DIST_MASU / DIST_NON_MASU）も、引用のみで構成された文は対象外とする。

文体（PM 2026-08-10 指示）: 全文末をです・ます形終止に統一する。体言止め・常体終止
（〜だ／〜である／〜ない／動詞・形容詞の終止形）は1文でも FAIL。旧ルール（です・ます率
≥45%・非です・ます率 ≥20%）は廃止した。DIST_NON_MASU は「下限」から「上限0」へ意味が反転
している。丁寧形の問いかけ（ですか／ますか／でしょうか）はです・ます形として扱う。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 前処理

URL_RE = re.compile(r"https?://\S+")
WS_RE = re.compile(r"\s+")
QUOTE_RE = re.compile(r"「[^「」]*」|『[^『』]*』")


def strip_urls(text: str) -> str:
    return URL_RE.sub("", text)


def n_chars(text: str) -> int:
    """空白（改行・全角空白含む）を除いた文字数。"""
    return len(WS_RE.sub("", text.replace("　", " ")))


def strip_quotes(text: str) -> str:
    """「」『』の中身を除去（語調系禁止語の判定用）。"""
    prev = None
    cur = text
    while prev != cur:
        prev = cur
        cur = QUOTE_RE.sub("", cur)
    return cur


SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]*")


def split_sentences_spans(text: str) -> list[tuple[str, int, int]]:
    """(文, 開始オフセット, 終了オフセット) を返す。オフセットは引数 text 基準。"""
    out = []
    for m in SENT_RE.finditer(text):
        s = m.group(0).strip()
        if s and n_chars(s) > 0:
            out.append((s, m.start(), m.end()))
    return out


def split_sentences(text: str) -> list[str]:
    return [s for s, _, _ in split_sentences_spans(text)]


def quote_spans(text: str) -> list[tuple[int, int]]:
    """「」『』の引用範囲（最外側）を返す。複数行にまたがる引用にも対応する。"""
    spans = []
    for open_c, close_c in (("「", "」"), ("『", "』")):
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == open_c:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == close_c and depth > 0:
                depth -= 1
                if depth == 0:
                    spans.append((start, i + 1))
    return spans


# ---------------------------------------------------------------- §6-1 禁止語

# (検査ID, 正規表現, 重大度, 引用除外の対象か)
NG_CHECKS = [
    ("NG_REVELATION", r"(理解が変わ|初めて知っ|学んだ|学びました|気づかされ|目が覚め|衝撃を受け|教えられ|教わっ|勉強になっ|読んで変わ|人生が変わ|考えが変わ|知らなかっ)", "FAIL", True),
    ("NG_BENEFACTIVE", r"てくれています|てくれました|てくれている", "FAIL", True),
    ("NG_BOAST", r"(億トレーダー|億り人|再現しました|完成しました|構築しました|実現しました|解決しました|専業になりました)", "FAIL", True),
    ("NG_TEACH", r"(しよう(?!か)|しましょう|ましょう|してほしい|して欲しい|すべき|べきです|べきだ|覚えておこう|覚えておいて|してください|しなさい|やめよう|忘れないで|してみて|した方がいい|したほうがいい|おすすめします|意識して|心がけて)", "FAIL", True),
    ("NG_2ND_PERSON", r"(あなた|皆さん|みなさん|君たち)", "FAIL", True),
    ("NG_HASHTAG", r"[#＃][^\s#＃]+", "FAIL", False),
    # 絵文字は少量（目安1〜2個）可・装飾目的の多用は不可（PM 2026-08-09 決定）。検出は残し WARN で PM 提示。
    ("NG_EMOJI", "[\U0001f000-\U0001faff☀-➿\U0001f1e6-\U0001f1ff✅❌⭕⚠❗]", "WARN", False),
    ("NG_HEARSAY", r"(らしい|だそう|とのこと|みたいです|という噂)", "FAIL", True),
    ("NG_HEDGE", r"(かもしれ|だろう|と思われ|と考えられ|のはず|とみられ|可能性が高い|たぶん|おそらく|多分|気がします)", "FAIL", True),
    ("NG_BUFFER", r"という整理を自分はしています|という整理をしています", "FAIL", True),
    ("NG_JARGON_KIKU", r"効いて|効いた|効きます|効いている", "FAIL", True),
    ("NG_CLICHE_BOOK", r"戻ってくる本", "FAIL", True),
    ("NG_WEAK_END", r"というだけの話です|という話です", "FAIL", True),
    ("NG_FAVORITE", r"一番好き|個人的に一番", "FAIL", True),
    ("NG_NO_HOU", r"の方です|方でした", "FAIL", True),
    # §1-7 エビデンス紹介定型文（研究・論文を紹介する独立文）。数値は主張文へ畳み込む。
    ("NG_EVIDENCE_INTRO",
     r"(研究|解析|調査|報告|実験|論文|統計|データ|メタ分析|メタ解析)(結果)?が(あり|ある)"
     r"|(研究|解析|調査|実験|論文|統計|データ|メタ分析|メタ解析)(結果)?(によると|によれば)"
     r"|(が|と)(報告|示唆|実証)されて(い|お)"
     r"|という(研究|解析|調査|報告|実験|論文)(?!者)", "FAIL", True),
    ("NG_SELF_NAME", r"Mizuki Fund|noctra|mizuki_fund", "FAIL", False),
    ("NG_LOCAL_PATH", r"[A-Za-z]:[\\/]|/Users/|/home/", "FAIL", False),
    ("NG_TICKER", r"\b[0-9]{4}[A-Z]?\b", "WARN", False),
    # §1-8 PM が 2026-08-02 に却下した表現
    ("NG_DISCLAIMER",
     r"予想.{0,4}(と|や).{0,6}(推奨|推薦)"
     r"|銘柄.{0,4}(推奨|推薦).{0,6}(しません|しない)"
     r"|相場予想は(しません|しない)", "FAIL", True),
    ("NG_EUPHEMISM", r"と呼ばれる(会社|企業)", "FAIL", True),
    ("NG_META_INTRO", r"自己紹介|このアカウントで(は)?(何を)?(書く|発信)|だけ先に", "FAIL", True),
    # 専業・経歴の特別扱い（§1-2 に統合）
    ("NG_PRO_SPECIAL", r"専業に?なって(分かった|わかった|気づいた|気付いた)|専業の仕事は", "FAIL", True),
    ("NG_AI_DISCLOSURE",
     r"AI[\s　]*(に|へ)?[\s　]*(読ませ|読み込ませ|食わせ|流し込)"
     r"|(名著|論文|決算|本)[\s　]*を[\s　]*AI", "FAIL", True),
    ("NG_SELF_PRAISE",
     r"使える(話|部分|もの|ところ)だけ|要点だけ|有益な|(わかり|分かり)やすく(発信|解説|まとめ)", "FAIL", True),
    ("NG_SELF_DEPRECATION", r"やらかした|失敗の解剖|自分を信用していない", "FAIL", True),
    # §1-9 一人称の過去エピソード（失敗談・無知だった等の自分下げ）の創作禁止（PM 2026-08-09）
    ("NG_PAST_SELF",
     r"(会社員|サラリーマン|新人|駆け出し|入社したて|兼業|専業になる前)の頃"
     r"|会社員だった頃"
     r"|(昔|以前|かつて|当時|過去)の(自分|私|僕|俺)"
     r"|(昔|以前|かつて)は.{0,30}(ました|でした)"
     r"|(時期|頃)が(あり|ある)ました"
     r"|(できなかった|勝てなかった|知らなかった|分からなかった|見ていなかった)頃", "FAIL", True),
    # §1-9 一人称の経歴・転換・属性語り（肯定否定を問わず禁止）。三人称の観察は対象外（PM 2026-08-09 第2弾）
    ("NG_SELF_BIO",
     r"専業|兼業"
     r"|会社を辞め|独立した(今|いま)|独立して(から|以降)"
     r"|(会社員|サラリーマン|GAFA|外資|前職|大企業)[^。！？\n]{0,12}(自分|私|僕|俺)"
     r"|(GAFA|外資|前職|会社員)(で働いて|に勤めて|に在籍して)いた頃", "FAIL", True),
    ("NG_LITERARY", r"負け方[をの](設計|デザイン)|.{0,6}の解剖", "FAIL", True),
    ("NG_READER_PROMISE", r"フォローすると|が届きます|が学べます|必見", "FAIL", True),
    # X 投稿の本文に丸括弧を一切使わない（PM 2026-09-03 指示・X 投稿の本文限定）。
    # 全角（）・半角() のみを対象とし、【】「」『』は構造上の見出し・引用記号のため対象外。
    # レポート誌面（prompts/_common_rules.md の中学生レベル注釈等）には本ルールを適用しない。
    ("NG_PAREN", r"[（）()]", "FAIL", False),
]

# NG_ACADEMIC_LABEL（§1-8・WARN）: 学問名ラベルが単体で置かれている場合のみ警告。
# 同一投稿内に中身を説明する語が1つでもあれば PASS とする。
ACADEMIC_LABEL_RE = re.compile(r"行動経済学|認知科学|認知心理学|脳科学|神経科学")
ACADEMIC_SUBSTANCE_RE = re.compile(
    r"(お金|判断|間違え|間違い|心理|感情|損|得|選ぶ|選択|記憶|習慣|思い込み|バイアス)")

# ---------------------------------------------------------------- §6-3 文末分類

Q_RE = re.compile(r"([？?]$)|((でしょうか|ますか|ですか|のか|かな)[。？?]*$)")
MASU_RE = re.compile(r"(です|ます|ました|ません|でした|ますね|ですね|でしょう|ください)[。、！？!?]*$")
DA_RE = re.compile(r"(だ|である|だった|ではない|じゃない|ない|た|る|う|い|よ|ね)[。！？!?]*$")


def classify_ending(sent: str) -> str:
    s = sent.strip()
    if Q_RE.search(s):
        return "question"
    if MASU_RE.search(s):
        return "masu"
    if DA_RE.search(s):
        return "da"
    return "taigen"


# --- 全文です・ます統一（§1-6・§2-6・PM 2026-08-10 指示） -----------------
# 旧ルール（です・ます率 ≥45% / 非です・ます率 ≥20%）は廃止。
# 全ての文が「です・ます形終止」でなければ FAIL とする二値判定を正本にする。
# 丁寧形の問いかけ（ですか/ますか/でしょうか 等）はです・ます形として扱う。

MASU_FORM_RE = re.compile(
    r"("
    r"ですか|ますか|でしょうか|ませんか|ましたか|でしたか|ませんでしたか"      # 丁寧形の問いかけ
    r"|ませんでした|ました|ません|でした|でしょう|ですね|ますね|ですよ|ますよ"
    r"|です|ます|ください|くださいませ|ませ"
    r")$"
)
# 文末から落とす記号（句読点・三点リーダ・閉じ括弧・引用符）
TAIL_TRIM_RE = re.compile(r"[。、．，！？!?…‥・〜~\s」』）\)】〉》\"'’”]+$")


def normalize_ending(sent: str) -> str:
    """文末判定用に、末尾の記号・閉じ括弧を落とした文字列を返す。"""
    s = sent.strip()
    prev = None
    while prev != s:
        prev = s
        s = TAIL_TRIM_RE.sub("", s)
    return s


def is_quote_only(sent: str) -> bool:
    """「」『』の引用だけで構成された文か（原文改変禁止のため判定対象外）。"""
    residue = strip_quotes(sent)
    residue = re.sub(r"[「」『』\s。、．，！？!?…‥・〜~（）\(\)]", "", residue)
    return residue == ""


# 文脈ブリッジ見出し（PM 2026-08-09 決定）: ビジネス心理系の本文を投資アカウントの
# タイムラインに接続するため、本文の前に【...】だけの見出し行を1本置く。見出しは
# 名詞句のラベルであって文ではないため、文末分布（DIST_MASU / DIST_NON_MASU）の
# 判定対象から外す（引用のみの文と同じ扱い）。禁止語判定は従来どおり見出しにも効く。
FRAME_HEADING_RE = re.compile(r"^[【\[][^【】\[\]\n]{1,40}[】\]]$")


def is_frame_heading(sent: str) -> bool:
    """文脈ブリッジ見出し（【...】だけで構成された行）か。"""
    return bool(FRAME_HEADING_RE.match(sent.strip()))


def is_masu_form(sent: str) -> bool:
    """文がです・ます形終止か。§6-3 の DIST_MASU / DIST_NON_MASU の正本。"""
    return bool(MASU_FORM_RE.search(normalize_ending(sent)))


# ---------------------------------------------------------------- 計測

DIGIT_RE = re.compile(r"[0-9０-９][0-9０-９,，.]*")
BOOK_RE = re.compile(r"『[^』]*』")
PERSON_RE = re.compile(r"[ァ-ヴー]{2,10}・[ァ-ヴー]{2,12}")
FIRST_PERSON_RE = re.compile(r"(自分|私|僕|俺|わたし|自身)")


def measure(text: str) -> dict:
    t = strip_urls(text).strip("\n")
    lines = t.split("\n")
    nonempty = [ln for ln in lines if ln.strip()]
    blank = [ln for ln in lines if not ln.strip()]
    paras = [p for p in re.split(r"\n[ \t　]*\n", t) if p.strip()]
    sent_spans = split_sentences_spans(t)
    sents = [s for s, _, _ in sent_spans]
    sent_lens = [n_chars(s) for s in sents] or [0]
    qspans = quote_spans(t)

    bullet_flags = [ln.strip().startswith("・") for ln in lines if ln.strip()]
    bullet_lines = [ln.strip() for ln in lines if ln.strip().startswith("・")]
    blocks = 0
    prev = False
    for f in bullet_flags:
        if f and not prev:
            blocks += 1
        prev = f

    endings = [classify_ending(s) for s in sents]

    # 全文です・ます統一（PM 2026-08-10）: 引用は原文改変禁止のため判定対象外にする。
    # 除外するのは (a) 引用だけで構成された文 (b) 引用範囲の内側に完全に収まる文
    # （複数行にわたる逐語引用の中の各文）(c) 文脈ブリッジ見出し行（【...】のみ）。
    # 引用を受ける地の文は対象。
    def _in_quote(a: int, b: int) -> bool:
        return any(qs <= a and b <= qe for qs, qe in qspans)

    judged = [s for s, a, b in sent_spans
              if not is_quote_only(s) and not _in_quote(a, b) and not is_frame_heading(s)]
    n_judged = len(judged) or 1
    non_masu_sents = [s for s in judged if not is_masu_form(s)]
    masu = n_judged - len(non_masu_sents)

    total = n_chars(t)
    return {
        "text": t,
        "total": total,
        "lines": lines,
        "nonempty": nonempty,
        "n_nonempty": len(nonempty),
        "n_blank": len(blank),
        "paras": paras,
        "n_para": len(paras),
        "para_max": max((n_chars(p) for p in paras), default=0),
        "first_line": nonempty[0] if nonempty else "",
        "last_line": nonempty[-1] if nonempty else "",
        "sents": sents,
        "sent_avg": round(sum(sent_lens) / len(sent_lens), 1),
        "sent_max": max(sent_lens),
        "chars_per_line": round(total / len(nonempty), 1) if nonempty else total,
        "n_digit": len(DIGIT_RE.findall(t)),
        "n_book": len(BOOK_RE.findall(t)),
        "n_person": len(PERSON_RE.findall(t)),
        "n_first_person": len(FIRST_PERSON_RE.findall(t)),
        "bullet_blocks": blocks,
        "bullet_lines": bullet_lines,
        "bullet_max": max((n_chars(b) for b in bullet_lines), default=0),
        "masu_ratio": round(masu * 100 / n_judged, 1),
        "non_masu_ratio": round(len(non_masu_sents) * 100 / n_judged, 1),
        "n_judged": n_judged,
        "n_non_masu": len(non_masu_sents),
        "non_masu_sents": non_masu_sents,
        "n_question": endings.count("question"),
        "endings": endings,
    }


def count_sent_marks(line: str) -> int:
    return len(re.findall(r"[。！？!?]", line))


# ---------------------------------------------------------------- 判定

def build_results(text: str, tpl_type: str | None, frame: str | None, strict_quotes: bool) -> tuple[list[dict], dict]:
    m = measure(text)
    body = strip_urls(text)
    body_nq = body if strict_quotes else strip_quotes(body)
    res: list[dict] = []

    def add(cid, status, value, rule, info=""):
        res.append({"id": cid, "status": status, "value": value, "rule": rule, "info": info})

    # ---- §6-1
    for cid, pat, sev, quote_exempt in NG_CHECKS:
        target = body_nq if (quote_exempt and not strict_quotes) else body
        hits = re.findall(pat, target)
        all_hits = re.findall(pat, body)
        info = ""
        if quote_exempt and not strict_quotes and len(all_hits) > len(hits):
            info = "引用内に%d件（判定除外）" % (len(all_hits) - len(hits))
        add(cid, "PASS" if not hits else sev, "%d件" % len(hits), "検出0件", info)

    # ---- §6-1 NG_ACADEMIC_LABEL（条件付き WARN）
    labels = ACADEMIC_LABEL_RE.findall(body if strict_quotes else body_nq)
    has_substance = bool(ACADEMIC_SUBSTANCE_RE.search(body))
    add("NG_ACADEMIC_LABEL",
        "WARN" if (labels and not has_substance) else "PASS",
        "%d件" % len(labels),
        "学問名の単体使用0件（中身の説明語があれば可）",
        "中身の説明語あり" if (labels and has_substance) else "")

    # ---- 枠の決定
    if frame not in ("short", "long"):
        frame = "short" if m["total"] < 280 else "long"

    # ---- §6-2
    if frame == "short":
        ok = 150 <= m["total"] <= 270
        rule = "短文枠 150〜270"
    else:
        ok = 280 <= m["total"] <= 560
        rule = "長文枠 280〜560"
    add("LEN_TOTAL", "PASS" if ok else "FAIL", "%d字" % m["total"], rule)

    fl = n_chars(m["first_line"])
    add("LEN_FIRST_LINE", "PASS" if fl <= 40 else ("WARN" if fl <= 60 else "FAIL"), "%d字" % fl, "≤40（41〜60=WARN）")

    fls = count_sent_marks(m["first_line"])
    add("CNT_FIRST_LINE_SENT", "PASS" if fls <= 1 else "FAIL", "%d文" % fls, "≤1")

    add("RATIO_CHARS_PER_LINE", "PASS" if m["chars_per_line"] <= 60 else "FAIL", "%.1f字/行" % m["chars_per_line"], "≤60")

    lim_lines = 5 if frame == "long" else 2
    add("CNT_LINES", "PASS" if m["n_nonempty"] >= lim_lines else "FAIL", "%d行" % m["n_nonempty"], "≥%d" % lim_lines)

    lim_para = 4 if frame == "long" else 2
    add("CNT_PARA", "PASS" if m["n_para"] >= lim_para else "FAIL", "%d段落" % m["n_para"], "≥%d" % lim_para)

    add("CNT_BLANK", "PASS" if m["n_blank"] >= 1 else "FAIL", "%d個" % m["n_blank"], "≥1")
    add("LEN_PARA_MAX", "PASS" if m["para_max"] <= 150 else "FAIL", "%d字" % m["para_max"], "≤150")
    add("LEN_SENT_AVG", "PASS" if m["sent_avg"] <= 45 else "FAIL", "%.1f字" % m["sent_avg"], "≤45")
    add("LEN_SENT_MAX", "PASS" if m["sent_max"] <= 70 else ("WARN" if m["sent_max"] <= 90 else "FAIL"), "%d字" % m["sent_max"], "≤90（71〜90=WARN）")

    ll = n_chars(m["last_line"])
    add("LEN_LAST_LINE", "PASS" if ll <= 60 else "FAIL", "%d字" % ll, "≤60")
    lls = count_sent_marks(m["last_line"])
    add("CNT_LAST_LINE_SENT", "PASS" if lls <= 2 else "FAIL", "%d文" % lls, "≤2")

    d = m["n_digit"]
    add("CNT_DIGIT", "PASS" if d >= 2 else ("WARN" if d == 1 else "FAIL"), "%d個" % d, "≥2（1個=WARN・0=FAIL）")
    add("CNT_BOOK", "PASS" if m["n_book"] <= 1 else "FAIL", "%d冊" % m["n_book"], "≤1")
    add("CNT_PERSON", "PASS" if m["n_person"] <= 2 else "FAIL", "%d名" % m["n_person"], "≤2")
    # 一人称は0回可（PM 2026-08-22 指示・主語を立てない所感文を許容。上限3は維持）
    add("CNT_1ST_PERSON", "PASS" if 0 <= m["n_first_person"] <= 3 else "FAIL", "%d回" % m["n_first_person"], "0〜3")
    add("CNT_BULLET_BLOCK", "PASS" if m["bullet_blocks"] <= 1 else "FAIL", "%dブロック" % m["bullet_blocks"], "≤1")
    nb = len(m["bullet_lines"])
    add("CNT_BULLET_LINES", "PASS" if (nb == 0 or 3 <= nb <= 5) else "FAIL", "%d行" % nb, "0 または 3〜5")
    add("LEN_BULLET_LINE", "PASS" if m["bullet_max"] <= 50 else "FAIL", "最長%d字" % m["bullet_max"], "≤50")

    # ---- §6-3 全文です・ます統一（PM 2026-08-10 指示・旧 ≥45% / ≥20% は廃止）
    bad = m["non_masu_sents"]
    bad_info = "違反文: " + " / ".join("「%s」" % s.strip() for s in bad[:8]) if bad else ""
    if len(bad) > 8:
        bad_info += " ほか%d文" % (len(bad) - 8)
    add("DIST_MASU", "PASS" if m["masu_ratio"] >= 100 else "FAIL",
        "%.1f%%（%d/%d文）" % (m["masu_ratio"], m["n_judged"] - m["n_non_masu"], m["n_judged"]),
        "100%（全文です・ます形終止）", bad_info)
    add("DIST_NON_MASU", "PASS" if m["n_non_masu"] == 0 else "FAIL",
        "%d文" % m["n_non_masu"], "0文（体言止め・常体終止は禁止）", bad_info)
    add("DIST_QUESTION", "PASS" if m["n_question"] <= 1 else "FAIL", "%d文" % m["n_question"], "≤1")

    # ---- §6-4（宣言型のみ判定・他は SKIP）
    tpl = (tpl_type or "").strip().upper()
    fl_first = m["first_line"].strip()
    para_lens = [n_chars(p) for p in m["paras"]]
    tpl_eval = {
        "A": (
            19 <= fl <= 40 and m["n_para"] >= 4 and all(30 <= x <= 60 for x in para_lens),
            "1行目19〜40字・段落4以上・各段落30〜60字",
            "1行目%d字／段落%d／各段落%s" % (fl, m["n_para"], para_lens),
        ),
        "B": (
            fl_first.startswith(("「", "『")) and 29 <= fl <= 53 and m["n_para"] >= 2,
            "1行目が「か『で始まる・29〜53字・2段落以上",
            "先頭記号%s／1行目%d字／段落%d" % (fl_first[:1], fl, m["n_para"]),
        ),
        "C": (
            bool(re.search(r"[0-9０-９]|[ァ-ヴー]{3,}|『[^』]*』|[A-Za-z]{2,}", fl_first)) and 30 <= ll <= 60,
            "1行目に数字または固有名詞・締め30〜60字",
            "1行目固有性%s／締め%d字" % ("有" if re.search(r"[0-9０-９]|[ァ-ヴー]{3,}|『[^』]*』|[A-Za-z]{2,}", fl_first) else "無", ll),
        ),
        "D": (
            bool(re.search(r"じゃない|ではない|ウソ|間違え|違います", fl_first)),
            "1行目に「じゃない/ではない/ウソ/間違え/違います」を含む",
            "1行目=%s" % fl_first[:24],
        ),
        "E": (
            18 <= fl <= 23 and "。" not in fl_first and 3 <= nb <= 5,
            "1行目18〜23字・1行目に。なし・箇条書き3〜5行",
            "1行目%d字／。%s／箇条書き%d行" % (fl, "無" if "。" not in fl_first else "有", nb),
        ),
    }
    for key in ("A", "B", "C", "D", "E"):
        ok_, rule_, val_ = tpl_eval[key]
        if tpl != key:
            add("TPL_%s" % key, "SKIP", "―", rule_, "宣言型は%s" % (tpl or "未宣言"))
        else:
            add("TPL_%s" % key, "PASS" if ok_ else "WARN", val_, rule_)

    m["frame"] = frame
    m["tpl"] = tpl
    return res, m


# ---------------------------------------------------------------- 出力

def report(post_id: str, text: str, tpl: str | None, frame: str | None, strict: bool, verbose: bool) -> tuple[int, int, dict]:
    res, m = build_results(text, tpl, frame, strict)
    fails = [r for r in res if r["status"] == "FAIL"]
    warns = [r for r in res if r["status"] == "WARN"]
    print("=" * 78)
    print("[%s] 型%s／%s枠／総%d字／非空行%d／段落%d／1行あたり%.1f字／1行目%d字／"
          "です・ます%.1f%%（以外%.1f%%）"
          % (post_id, m["tpl"] or "未宣言", "短文" if m["frame"] == "short" else "長文",
             m["total"], m["n_nonempty"], m["n_para"], m["chars_per_line"],
             n_chars(m["first_line"]), m["masu_ratio"], m["non_masu_ratio"]))
    print("  判定: 検査%d項目 → PASS %d / WARN %d / FAIL %d / SKIP %d"
          % (len(res), len([r for r in res if r["status"] == "PASS"]), len(warns), len(fails),
             len([r for r in res if r["status"] == "SKIP"])))
    for r in res:
        if r["status"] in ("FAIL", "WARN") or verbose:
            print("  %-6s %-22s %-14s (基準 %s) %s"
                  % (r["status"], r["id"], r["value"], r["rule"], r["info"]))
        elif r["info"]:
            print("  INFO   %-22s %s" % (r["id"], r["info"]))
    return len(fails), len(warns), {"id": post_id, "checks": res,
                                    "metrics": {k: m[k] for k in
                                                ("total", "n_nonempty", "n_para", "n_blank",
                                                 "chars_per_line", "sent_avg", "sent_max",
                                                 "para_max", "n_digit", "masu_ratio",
                                                 "non_masu_ratio", "frame", "tpl")}}


def main() -> int:
    ap = argparse.ArgumentParser(description="X 投稿 文体チェッカー（style_rules_v1.md §6・59項目）")
    ap.add_argument("path", nargs="?", help="投稿本文の .txt、または一括入力の .json")
    ap.add_argument("--text", help="本文を直接渡す")
    ap.add_argument("--type", dest="tpl", help="構成テンプレート A〜E（§6-4 の照合対象）")
    ap.add_argument("--frame", choices=["short", "long"], help="短文枠／長文枠（省略時は自動）")
    ap.add_argument("--strict-quotes", action="store_true", help="「」『』内も禁止語判定の対象にする")
    ap.add_argument("--json", action="store_true", help="機械可読な JSON を最後に出力")
    ap.add_argument("-v", "--verbose", action="store_true", help="PASS 項目も全件表示")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    posts = []
    if a.text:
        posts.append({"id": "text", "type": a.tpl, "frame": a.frame, "text": a.text})
    elif a.path:
        p = Path(a.path)
        if not p.exists():
            print("ファイルがありません: %s" % p)
            return 2
        raw = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".json":
            for item in json.loads(raw):
                posts.append({"id": item.get("id", "?"), "type": item.get("type", a.tpl),
                              "frame": item.get("frame", a.frame), "text": item["text"]})
        else:
            posts.append({"id": p.stem, "type": a.tpl, "frame": a.frame, "text": raw})
    else:
        ap.print_help()
        return 2

    tf = tw = 0
    out = []
    for post in posts:
        f, w, d = report(post["id"], post["text"], post.get("type"), post.get("frame"),
                         a.strict_quotes, a.verbose)
        tf += f
        tw += w
        out.append(d)
    print("=" * 78)
    print("合計: 投稿%d本 / FAIL %d件 / WARN %d件" % (len(posts), tf, tw))
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 1 if tf else 0


if __name__ == "__main__":
    sys.exit(main())
