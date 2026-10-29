"""動意レポート「なぜ動いた」の事実照合ゲート（日次・週次共通）。

PM 2026-10-04 承認（dev/drafts/2026-10-04_dev_mover_reason_fix_plan.md §3-D）:
10/2 週次号の 7256 で、最大変動日（10/2 ▼13.7%）と無関係な 9/28 の開示（影響軽微・
場中開示後に高値更新なし）を冒頭の理由に置いた誤記の再発防止。ETL が出力する
`{date}_movers_facts.json`（各銘柄の最大変動日・2σ 日・日別の開示/報道/関連銘柄）と
誌面の「なぜ動いた」を機械照合する。

検査項目（「なぜ動いた」1 件ごと。誌面の `### N位 {コード} …` 見出しで facts と結び付ける）:
  (a) 最大変動日の M/D が本文に含まれる                                  … 不合格
  (b) 本文に出る開示が (b-1) 割当日=最大変動日 or 2σ 日 (b-2) 影響軽微でない
      (b-3) 場中開示なら開示後に当日高値（上昇日）／安値（下落日）を更新、を満たす … 不合格
      ※ 冒頭の 1 文または `{最大変動日}（曜）±X%：` に続く材料欄（＝最大変動日の理由）に出た開示は
        (b-1)(b-2)(b-3) を全て検査する。
        当週（日次は直近 5 営業日）の表 1 にある開示がそれ以外の文に出た場合は、因果の語（を受け・
        きっかけ・好感・嫌気 等。出所付き引用の中は除く）で値動きの原因に据えた文だけ (b-1) を検査し、
        日付付きの事実としての言及は検査しない（2026-10-05 追加）。
        直近 3 か月の表 2 にだけある過去の開示は、最大変動日の理由に流用した場合だけ検査する（経緯としての
        言及は許す。PM 承認済みサンプル mover_sample_7256_v2.md §4 の 8/14 決算の言及がこれに当たる）。
      ※ 割当日の 2σ 閾値が facts に無い日（上場から日が浅い銘柄等）は (b-1) を判定せず警告にする（2026-10-05 追加）。
      ※ 場中開示の post_new_extreme が null（5 分足が取れず判定不能）の開示は (b-3) を判定せず警告にする（2026-10-05 追加）。
      ※ 似た表題の開示（同じ案件の続報・前期の決算短信等）が同じ文に当たる場合は、全文が出た方、または
        一致度が高い方の開示だけを「触れた」とする（2026-10-05 追加・`_mentions_disclosure`）。
  (c) 推測語が出所付き引用 `{媒体}は「…」` の外にある                        … 不合格
  (d) 「材料は確認できず」を含む文（と直後の 1 文）で、創作理由の典型語が
      出所付き引用の外に出る                                              … 不合格
  (e) 循環説明（2026-10-28 追加）: 本文（先頭ラベル `M/D（曜）±X.X%：` を除く）が値動きを報じた語彙
      （PRICE_REPORT_RE・lib/mover_materials.py と同一）を使い、facts の primary 材料（会社開示・IR・
      材料記事）への言及（内容＝表題の一致、または日付＋発信主体／公表の語）が無い             … 不合格
      primary が 1 件以上あるのに「取得結果なし」の定型文（NO_PRIMARY）を書いた場合も不合格
  (f) 数値埋め（2026-10-28 追加）: 本文（先頭ラベルを除く）に `売買代金|出来高|時価総額|[0-9.]+倍`
      （primary 材料の中身の金額・株数は対象外＝「億円」単体では落とさない）                  … 不合格
  (g) 未特定の逃げ（2026-10-28 追加）: `確認できず|未特定|材料不明|不明`                      … 不合格
  (h) 説明力のない開示（2026-10-30 追加）: 本文が weak 材料（説明会・セミナー・動画・株式譲渡報告書・行使状況・
      ガバナンス・役員異動・再掲・英語版・非数値の訂正 等の告知／事務系。lib/mover_materials.py の
      classify_strength）の表題・内容だけで構成され、strong 材料に触れていない                      … 不合格
      ※ (e)(h) と機械差し替えの「使える材料」は strong だけ（strong かつ timing_fit を最優先）。
      ※ strong が 0 件の銘柄は固定文 NO_STRONG（確認した開示＝weak の表題を最大 2 件）だけが合格。strong が
        あるのに固定文を書いた場合・旧定型文（NO_PRIMARY_OLD）を書いた場合は (e) で不合格。
  (m) 最大変動日の材料欄に開示・報道があるのに本文がどれにも触れていない（旧 (e)）      … 警告のみ
  (x) facts に当該銘柄が無い（ETL 失敗・旧 (f)）                            … 警告のみ・検査を飛ばす
  ※ facts に materials が無い（一次材料の取得を止めた日・旧形式の facts）は (e) を飛ばして警告にする。
facts に必要なキーが無い場合は、その検査だけを飛ばして警告を出す（ETL の形の変化に寛容）。

不合格時の扱い（PM 判断事項 3）: workflow が該当銘柄だけ 1 回書き直させ、それでも不合格なら
`--fallback-fix` で facts だけから組んだ 1 文へ機械差し替えして配信する（絶対配信原則 _cr §36）。
差し替え文（2026-10-30 改定）: strong の材料（strong かつ timing_fit → 起点 → 会社開示の順・timing_fit が無ければ
最も新しい strong）から「{M/D} {時刻}、{発信主体}が『{表題}』を公表（{要点}）。これを起点に{基準日}は N 日連続の
上昇の M 日目。」を組む（後半は起点の材料の時だけ）。使える strong が 0 件なら固定文 NO_STRONG を書く。
weak（告知・事務系）は理由に使わない。数値（売買代金・出来高等）は入れない。旧「材料は確認できず」は使わない。

使い方（ライブラリ）:
    from lib.reason_check import check_reasons, load_facts
    failures = check_reasons(md_text, load_facts(path))   # [Failure, ...]

使い方（単体）:
    python bi/pipelines/lib/reason_check.py --file market/daily/movers/2026-10-02_weekly.md \
        --facts market/daily/2026-10-02_movers_facts.json [--json-out result.json]
    python bi/pipelines/lib/reason_check.py --file X.md --facts F.json --fallback-fix [--out Y.md]
  exit code: 0=合格（警告のみ含む） / 1=インフラ失敗（読込不可） / 4=不合格あり
  （--fallback-fix 時も「元の md に不合格があったか」で 0/4 を返す。差し替え後の md は常に書き出す）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

PREFIX = "[理由ゲート]"

# --- 検査語彙 ---------------------------------------------------------------
# (c) 推測語: _cr §8 L166 と mover-weekly.md L369 の列挙（計画書 D-1）。
GUESS_WORDS: tuple[str, ...] = (
    "可能性が高い", "と思われる", "と考えられる", "だろう", "のはず", "と推測される",
    "公算", "とみるのが自然", "とみられる", "見込み", "likely", "probably",
)
# 「見込み」は会社公表値の文脈（_cr §43 の除外）では推測語に当たらない。
_COMPANY_CONTEXT_RE = re.compile(r"会社|同社|当社|決算短信|業績予想|通期見込み|期末見込み")

# (d) 「材料は確認できず」の文で単独で出てはならない創作理由の典型語（計画書 D-1）。
INVENTED_WORDS: tuple[str, ...] = (
    "思惑", "期待", "警戒", "懸念", "失望", "連想", "観測", "利益確定", "買い戻し", "需給",
)
NO_MATERIAL = "材料は確認できず"   # 旧正規語（2026-10-28 廃止）。(d) の検出と (g) の不合格にだけ使う
# 使える strong 材料が 0 件の時だけ使う固定文（2026-10-30 改定。ルール側 prompts/_common_rules.md §2-C・§44・
# prompts/mover-report.md・prompts/mover-weekly.md と完全一致させる・変更不可）。{weak} = 確認した weak の表題を
# 最大 2 件（『表題』を「・」で連結）、無ければ「なし」。書き手が書いても検査側（機械差し替え）が facts から入れ直す。
NO_STRONG_HEAD = "当日・起点日に値動きを説明する会社開示・IR・材料記事なし（確認範囲: TDnet／会社 IR／株探／Yahoo。確認した開示: "
NO_STRONG_TMPL = NO_STRONG_HEAD + "{weak}）"
# 旧名（GHA の workflow が参照する）。確認した開示が無い時の固定文と同じ
NO_PRIMARY = NO_STRONG_TMPL.format(weak="なし")
# 旧定型文（2026-10-28〜10-30）。書いたら (e) で不合格
NO_PRIMARY_OLD = "当日の TDnet 開示・IR 更新・材料記事のいずれも無し（確認範囲: TDnet／会社 IR／株探／Yahoo）"

# (e) 値動きを報じた語彙（lib/mover_materials.py の PRICE_REPORT_PATTERN と同一。GHA ではこのファイルを
# 単体実行するため import せず複製している。テストで一致を確かめる）
PRICE_REPORT_PATTERN = (r"買い気配|ストップ高|S高|急騰|急伸|急落|急反発|急反落|続伸|続落|反発|反落|話題|注目|人気"
                        r"|前日に|年初来|上場来|大幅高|大幅安|物色|買われ|売られ")
PRICE_REPORT_RE = re.compile(PRICE_REPORT_PATTERN)
# (f) 数値埋め（PM 確定 2026-10-28: 「億円」単体では落とさない。primary の契約額・修正幅・受注額・取得株数は合格）
PADDING_RE = re.compile(r"売買代金|出来高|時価総額|[0-9.]+倍")
# (g) 未特定の逃げ
UNIDENTIFIED_RE = re.compile(r"確認できず|未特定|材料不明|不明")
# 先頭ラベル `10/27（火）＋20.7%：`（ルール側で残している・(e)(f) の対象外）
_LABEL_RE = re.compile(r"^\s*[0-9]{1,2}/[0-9]{1,2}\s*[（(][月火水木金土日][）)]\s*[＋+▼▲\-−±]?\s*[0-9.]+\s*[%％]\s*[：:]\s*")
_DISCLOSE_VERB_RE = re.compile(r"開示|IR|ＩＲ|リリース|発表|公表|報道|報じ|説明会|提出")

# 開示を値動きの原因に据える因果の語（2026-10-05 追加。冒頭・最大変動日の材料欄の外の文で使う）。
_CAUSAL_RE = re.compile(
    r"を受け|きっかけ|契機|好感|嫌気|が響|材料視|を材料|手掛かり|手がかり|が理由|が要因|要因は|理由は|"
    r"背景に|を背景|につながっ|で買われ|で売られ|が重し|が原因|による(?:上昇|下落|急騰|急落|買い|売り)"
)

# 出所付き引用 `{媒体名}は「{原文}」`（C-2 の「報道の推量の引用」）。引用部分は (c)(d) の対象外。
_QUOTE_RE = re.compile(r"は「[^」]*」")

# 誌面の銘柄見出し `### 3位 7256 河西工業　-17.2%（…）`
_ENTRY_RE = re.compile(r"^###\s*[0-9０-９]+\s*位\s+([0-9A-Z]{4})\b")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_REASON_RE = re.compile(r"^\*\*なぜ動いた\*\*\s*[：:]\s*")
_FIELD_RE = re.compile(r"^\*\*[^*]+\*\*\s*[：:]")

# 開示表題の定型部分（照合キーから外す）
_TITLE_BOILERPLATE = (
    "に関するお知らせ", "についてのお知らせ", "のお知らせ", "お知らせ", "について",
    "〔日本基準〕", "〔IFRS〕", "〔米国基準〕", "(連結)", "(非連結)",
)
_WEEKDAYS = "月火水木金土日"


@dataclass
class Failure:
    code: str
    name: str
    line: int            # 誌面の見出し行（1 始まり）。facts 全体の問題は 0
    check: str           # "a"〜"h" / "m" / "x" / "facts"
    severity: str        # "fail" / "warn"
    message: str
    extra: dict = field(default_factory=dict)


# --- 文字列ユーティリティ ---------------------------------------------------
def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def _md(date_str: str) -> str:
    """'2026-10-02' -> '10/2'"""
    d = _dt.date.fromisoformat(date_str[:10])
    return f"{d.month}/{d.day}"


def _md_wd(date_str: str) -> str:
    d = _dt.date.fromisoformat(date_str[:10])
    return f"{d.month}/{d.day}（{_WEEKDAYS[d.weekday()]}）"


def _date_in_text(date_str: str, text: str) -> bool:
    d = _dt.date.fromisoformat(date_str[:10])
    t = _norm(text)
    pats = (rf"(?<![0-9/]){d.month}/{d.day}(?![0-9])", rf"(?<![0-9]){d.month}月{d.day}日")
    return any(re.search(p, t) for p in pats)


def _pct(v: float) -> str:
    """騰落率を誌面書式へ（上昇 ＋X.X% / 下落 ▼X.X%・四捨五入）。"""
    q = Decimal(str(abs(v))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{'▼' if v < 0 else '＋'}{q}%"


def _sentences(text: str) -> list[str]:
    """「。」で文に分ける。引用「…」の中の「。」では分けない（出所付き引用を壊さないため）。"""
    out: list[str] = []
    buf, depth = [], 0
    for ch in text:
        buf.append(ch)
        if ch == "「":
            depth += 1
        elif ch == "」" and depth:
            depth -= 1
        elif ch == "。" and depth == 0:
            out.append("".join(buf))
            buf = []
    if "".join(buf).strip():
        out.append("".join(buf))
    return [s for s in out if s.strip()]


def _strip_quotes(text: str) -> str:
    return _QUOTE_RE.sub("は「」", text)


def _title_core(title: str) -> str:
    t = _norm(title)
    t = re.sub(r"^\s*[\(（][^)）]*訂正[^)）]*[\)）]", "", t)   # (訂正・数値データ訂正)
    t = re.sub(r"^\s*\d{4}年\d{1,2}月期", "", t)               # 2027年3月期
    for b in _TITLE_BOILERPLATE:
        t = t.replace(_norm(b), "")
    # 漢字・カナ・英数だけを残す（助詞・記号の言い換えに強くする）
    return "".join(ch for ch in t if re.match(r"[0-9A-Za-z぀-ヿ一-鿿]", ch)
                   and ch not in "のとおよびにをはがでへや及並")


def _mentions_title(title: str, text: str) -> bool:
    """本文（1 文）が開示・報道の表題に触れているか。

    照合キー = 表題の先頭 12 字（計画書 D-1）。言い換え（「資金使途及び…」→「資金の使途と…」）
    に備え、表題の中核語の 2 文字連なり（bigram）の 6 割以上が同じ文に出る場合も「触れた」とする。
    """
    t = _norm(text)
    head = _norm(title).strip()[:12]
    if len(head) >= 6 and head in t:
        return True
    core = _title_core(title)
    if len(core) < 4:
        return bool(core) and core in t
    t_core = "".join(ch for ch in t if ch not in "のとおよびにをはがでへや及並")
    grams = {core[i:i + 2] for i in range(len(core) - 1)}
    hit = sum(1 for g in grams if g in t_core)
    return hit / len(grams) >= 0.6


def _verbatim_title(title: str, text: str) -> bool:
    """表題の全文（NFKC・空白除去）がそのまま本文に出ているか。"""
    tt = re.sub(r"\s+", "", _norm(title))
    return len(tt) >= 6 and tt in re.sub(r"\s+", "", _norm(text))


def _mentions_disclosure(title: str, text: str, all_titles: list[str]) -> bool:
    """`_mentions_title` に、似た表題の別開示との取り違えの除外を足したもの（2026-10-05 追加）。

    本表題と似た表題の別開示（同じ案件の続報・前期の決算短信など）について、文がその表題を全文そのまま
    引いている、または言い換えでもそちらの方が一致度（中核語の 2 文字連なりの一致割合）が高い場合は、
    文が指しているのはそちらの開示とみなし、本表題には触れていない扱いにする
    （4004 の「パーシャル・スピンオフの実行完了」・3549 の第 1 四半期決算短信の誤検知対策）。
    """
    if _verbatim_title(title, text):
        return True
    if not _mentions_title(title, text):
        return False
    my_score = _title_score(title, text)
    for other in all_titles:
        if other == title or not _mentions_title(title, other):
            continue
        # 似た表題の別開示が全文で出ている、または言い換えでもそちらの方が一致度が高い
        if _verbatim_title(other, text) or (
                _mentions_title(other, text) and _title_score(other, text) > my_score):
            return False
    return True


def _title_score(title: str, text: str) -> float:
    """表題の中核語の 2 文字連なりのうち本文に出る割合（似た表題どうしの比較用）。"""
    core = _title_core(title)
    if len(core) < 2:
        return 0.0
    t_core = "".join(ch for ch in _norm(text) if ch not in "のとおよびにをはがでへや及並")
    grams = {core[i:i + 2] for i in range(len(core) - 1)}
    return sum(1 for g in grams if g in t_core) / len(grams)


# --- facts の読込・参照 -----------------------------------------------------
def load_facts(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _stocks(facts: dict) -> dict:
    if isinstance(facts, dict) and isinstance(facts.get("stocks"), dict):
        return facts["stocks"]
    return {}


def _day_rows(st: dict) -> tuple[list[dict], list[dict]]:
    week = [r for r in (st.get("week_days") or []) if isinstance(r, dict) and r.get("date")]
    m3 = [r for r in (st.get("month3_days") or []) if isinstance(r, dict) and r.get("date")]
    return week, m3


def _row_for(st: dict, date_str: str) -> dict | None:
    week, m3 = _day_rows(st)
    for r in week + m3:
        if str(r["date"])[:10] == date_str[:10]:
            return r
    return None


# --- 誌面のパース -----------------------------------------------------------
def parse_entries(md_text: str) -> list[dict]:
    """[{code, heading, line, reason, reason_start, reason_end}] を返す（行番号 1 始まり・end は排他）。"""
    lines = md_text.splitlines()
    entries: list[dict] = []
    i = 0
    while i < len(lines):
        m = _ENTRY_RE.match(lines[i])
        if not m:
            i += 1
            continue
        ent = {"code": m.group(1), "heading": lines[i].strip(), "line": i + 1,
               "reason": None, "reason_start": None, "reason_end": None}
        j = i + 1
        while j < len(lines) and not _HEADING_RE.match(lines[j]):
            if _REASON_RE.match(lines[j]):
                k = j + 1
                while (k < len(lines) and lines[k].strip() and not _HEADING_RE.match(lines[k])
                       and not _FIELD_RE.match(lines[k])):
                    k += 1
                ent["reason"] = "\n".join([_REASON_RE.sub("", lines[j])] + lines[j + 1:k]).strip()
                ent["reason_start"], ent["reason_end"] = j, k
                j = k
                continue
            j += 1
        entries.append(ent)
        i = j
    return entries


def _name_from_heading(heading: str, code: str) -> str:
    rest = heading.split(code, 1)[-1].strip()
    return re.split(r"[\s　]", rest, maxsplit=1)[0] if rest else ""


# --- 検査本体 ---------------------------------------------------------------
def _lead_segments(body: str, sents: list[str], max_day: str | None) -> list[str]:
    """最大変動日の理由として書かれた部分（C-1 の 1）: 冒頭の 1 文と、
    `{M/D}（曜）{±X.X}%：` の書式に続く材料欄（次の「。」まで）。"""
    segs = sents[:1]
    if max_day:
        d = _dt.date.fromisoformat(max_day[:10])
        rx = re.compile(rf"(?<![0-9/]){d.month}/{d.day}(?![0-9])[^：:。]{{0,16}}[：:]([^。]*)")
        segs += [m.group(1) for m in rx.finditer(_norm(body))]
    return segs


def _check_entry(ent: dict, st: dict, fs: list[Failure]) -> None:
    code, line = ent["code"], ent["line"]
    name = st.get("name") or _name_from_heading(ent["heading"], code)
    body = ent["reason"] or ""

    def add(check: str, sev: str, msg: str, **extra) -> None:
        fs.append(Failure(code, name, line, check, sev, msg, extra))

    if not body:
        add("a", "warn", "「なぜ動いた」行が見つからない（検査を飛ばす）")
        return
    # 固定文 NO_STRONG（中に開示の表題を含む）は検査の対象から外し、使い方だけ (e) で見る（2026-10-30）
    body, fixed_used = _strip_fixed(body)

    sents = _sentences(body)
    max_day = st.get("max_move_day")
    sigma2 = {str(d)[:10] for d in (st.get("sigma2_days") or [])}
    week, m3 = _day_rows(st)
    week_dates = {str(r["date"])[:10] for r in week}
    lead_segs = _lead_segments(body, sents, max_day)

    # (a) 最大変動日の M/D
    if not max_day:
        add("a", "warn", "facts に max_move_day が無い（(a)(b-1)(m) を飛ばす）")
    elif not _date_in_text(max_day, body):
        add("a", "fail", f"最大変動日 {_md(max_day)} が本文に無い")

    # (b) 本文に出る開示の妥当性
    if "sigma2_days" not in st:
        add("b", "warn", "facts に sigma2_days が無い（2σ 日は最大変動日のみで判定）")
    if not week and not m3:
        add("b", "warn", "facts に week_days / month3_days が無い（(b)(m) を飛ばす）")
    seen: set[tuple[str, str]] = set()
    all_titles = sorted({d.get("title") or "" for rr in (week, m3) for r in rr
                         for d in (r.get("disclosures") or []) if d.get("title")})
    for scope, rows in (("week", week), ("month3", m3)):
        for r in rows:
            for d in (r.get("disclosures") or []):
                title = d.get("title") or ""
                adate = str(d.get("assigned_date") or r.get("date"))[:10]
                if not title or (title, adate) in seen:
                    continue
                seen.add((title, adate))
                hit_sents = [s for s in sents if _mentions_disclosure(title, s, all_titles)]
                if not hit_sents:
                    continue
                in_lead = any(_mentions_disclosure(title, s, all_titles) for s in lead_segs)
                if scope == "month3" and adate not in week_dates:
                    # 過去の開示は「最大変動日の理由」に流用した場合だけ検査する
                    # （冒頭の 1 文、または `{最大変動日}（曜）±X%：` に続く材料欄に出た場合）
                    if not in_lead:
                        continue
                lead_use = in_lead
                if not in_lead:
                    # 2026-10-05: 冒頭・最大変動日の材料欄の外で、日付付きの事実として開示に触れた文
                    # （例「9/29 は前日に大口受注を開示し、報道は…と伝えた」）は主因の扱いと区別して検査しない。
                    # 外の文でも因果の語（を受け・きっかけ・好感 等。引用の中は除く）で値動きの原因に据えた
                    # 場合は、(b-1) だけを検査する（10/2 号 7256 の型を 2 文目以降へ移した書き方を見逃さない）。
                    if not any(_CAUSAL_RE.search(_strip_quotes(_norm(s))) for s in hit_sents):
                        continue
                reasons: list[str] = []
                ok_days = sigma2 | ({max_day[:10]} if max_day else set())
                if adate not in ok_days:
                    arow = _row_for(st, adate)
                    if arow is not None and "sigma2_threshold_pct" in arow and arow.get("sigma2_threshold_pct") is None:
                        # 2026-10-05: 2σ 閾値が算出できない日（上場から日が浅い銘柄等）は ★ を判定できないため
                        # 不合格にせず警告に回す（476A の誤検知対策）。
                        add("b", "warn", f"開示「{title[:30]}」（{_md(adate)}）: 割当日の 2σ 閾値が無く (b-1) を判定できず",
                            title=title, assigned_date=adate)
                    else:
                        reasons.append(f"(b-1) 割当日 {_md(adate)} が最大変動日・2σ 日でない")
                if not lead_use:
                    if reasons:
                        add("b", "fail", f"開示「{title[:30]}」（{_md(adate)}）を値動きの原因として記述: "
                            + "／".join(reasons), title=title, assigned_date=adate)
                    continue
                if "minor_impact" not in d:
                    add("b", "warn", f"開示「{title[:20]}」に minor_impact が無い（(b-2) を飛ばす）")
                elif d.get("minor_impact"):
                    reasons.append("(b-2) 開示本文に影響軽微の記載")
                if d.get("intraday"):
                    if "post_new_extreme" not in d:
                        add("b", "warn", f"開示「{title[:20]}」に post_new_extreme が無い（(b-3) を飛ばす）")
                    elif d.get("post_new_extreme") is None:
                        # 2026-10-05: 場中の 5 分足が取れず ETL が更新の有無を判定できなかった開示（値 null。
                        # 終日値幅制限に張り付いた日など）は「更新していない」と扱わず警告に回す（402A の誤検知対策）。
                        add("b", "warn", f"開示「{title[:20]}」（{_md(adate)}）: 場中の値動きが取れず (b-3) を判定できず",
                            title=title, assigned_date=adate)
                    elif not d.get("post_new_extreme"):
                        pe = d.get("pre_extreme")
                        pt = d.get("pre_extreme_time")
                        tail = f"（開示前の高値/安値 {pe}・{pt}）" if pe is not None else ""
                        reasons.append(f"(b-3) 場中開示後に高値/安値を更新していない{tail}")
                if reasons:
                    add("b", "fail", f"開示「{title[:30]}」（{_md(adate)}）を本文で使用: "
                        + "／".join(reasons), title=title, assigned_date=adate)

    # (c) 推測語（出所付き引用の外）
    for s in sents:
        bare = _strip_quotes(_norm(s))
        for w in GUESS_WORDS:
            if w not in bare:
                continue
            if w == "見込み" and _COMPANY_CONTEXT_RE.search(bare):
                continue
            add("c", "fail", f"推測語「{w}」が引用の外にある: {s.strip()[:60]}", word=w)

    # (d) 「材料は確認できず」の文（と直後の 1 文）に創作理由の典型語
    for idx, s in enumerate(sents):
        if NO_MATERIAL not in s:
            continue
        scope_text = "".join(sents[idx:idx + 2])
        bare = _strip_quotes(_norm(scope_text))
        for w in INVENTED_WORDS:
            if w in bare:
                add("d", "fail", f"「{NO_MATERIAL}」の文に創作理由の語「{w}」: {scope_text.strip()[:60]}",
                    word=w)

    # (e)(f)(g) 循環説明・数値埋め・未特定の逃げ（2026-10-28 追加）
    _check_quality(body, sents, st, add, fixed_used)

    # (m) 最大変動日に材料があるのに本文が触れていない（警告のみ・旧 (e)）
    if max_day:
        row = _row_for(st, max_day)
        if row is None:
            if week or m3:
                add("m", "warn", f"facts に最大変動日 {_md(max_day)} の行が無い（(m) を飛ばす）")
        else:
            mats = [("開示", d.get("title") or "") for d in (row.get("disclosures") or [])]
            mats += [("報道", n.get("title") or "") for n in (row.get("news") or [])]
            mats = [(k, t) for k, t in mats if t]
            srcs = [n.get("source") for n in (row.get("news") or []) if n.get("source")]
            if row.get("shikiho_release"):
                mats.append(("四季報", "四季報"))
            if mats:
                touched = any(_mentions_title(t, s) for _, t in mats for s in sents) \
                    or any(_norm(src) in _norm(body) for src in srcs) \
                    or (row.get("shikiho_release") and "四季報" in body)
                if not touched:
                    add("m", "warn", f"最大変動日 {_md(max_day)} の材料 {len(mats)} 件に本文が触れていない: "
                        + "／".join(t[:20] for _, t in mats[:3]))


def _strip_label(body: str) -> str:
    return _LABEL_RE.sub("", _norm(body).strip(), count=1)


def _primaries(st: dict) -> list[dict]:
    prim = [m for m in (st.get("materials") or []) if isinstance(m, dict) and m.get("kind", "primary") == "primary"
            and m.get("title")]
    _ensure_strength(st, prim)
    return prim


_MM = None


def _mm():
    """lib/mover_materials.py の判定関数（strength が無い旧 facts の後付け判定用）。読めなければ None。"""
    global _MM
    if _MM is None:
        try:
            p = str(Path(__file__).resolve().parent.parent)
            if p not in sys.path:
                sys.path.insert(0, p)
            from lib import mover_materials as m  # noqa: WPS433
            _MM = m
        except Exception:  # pragma: no cover - GHA でも private-repo に同梱されている
            _MM = False
    return _MM or None


def _ensure_strength(st: dict, prim: list[dict]) -> None:
    """strength / timing_fit が無い材料（2026-10-30 より前の facts）へ後付けする。判定関数が読めなければ
    strong・timing_fit=is_origin とみなす（旧挙動）。"""
    todo = [m for m in prim if "strength" not in m]
    if not todo:
        return
    mm = _mm()
    base = str((st.get("window") or ["", ""])[-1] or st.get("max_move_day") or "")[:10]
    origin = str(st.get("origin_date") or base)[:10]
    if mm is not None and base and origin:
        try:
            mm.annotate_strength(todo, origin, base)
            return
        except Exception:
            pass
    for m in todo:
        m.setdefault("strength", "strong")
        m.setdefault("timing_fit", bool(m.get("is_origin")))


def _is_strong(m: dict) -> bool:
    return m.get("strength", "strong") == "strong"


def _strip_fixed(body: str) -> tuple[str, str]:
    """本文から固定文 NO_STRONG（『』「」の中の括弧は数えない）を除いた本文と、除いた固定文（無ければ ""）を返す。"""
    t = _norm(body)
    head = _norm(NO_STRONG_HEAD)
    i = t.find(head)
    if i < 0:
        return body, ""
    depth, j = 0, i + len(head)
    while j < len(t):
        ch = t[j]
        if ch in "『「":
            depth += 1
        elif ch in "』」" and depth:
            depth -= 1
        elif ch == ")" and depth == 0:   # NFKC で「）」は「)」になる
            break
        j += 1
    return t[:i] + t[j + 1:], t[i:j + 1]


def _pub_name(m: dict) -> str:
    """発信主体の照合用の短い名前（「データセクション（会社IR）」→「データセクション」・「株探（MINKABU PRESS）」→「株探」）。"""
    return re.split(r"[（(]", _norm(m.get("publisher") or ""), maxsplit=1)[0].strip()


def _mentions_primary(m: dict, body: str, sents: list[str]) -> bool:
    """本文が primary 材料に触れているか（内容＝表題の一致、または日付＋発信主体／公表の語）。"""
    title = m.get("title") or ""
    if any(_mentions_title(title, s) for s in sents):
        return True
    dt = str(m.get("dt") or "")[:10]
    has_date = bool(dt) and _date_in_text(dt, body)
    if has_date and _title_score(title, body) >= 0.4:
        return True
    pub = _pub_name(m)
    if has_date and ((len(pub) >= 2 and pub in _norm(body)) or _DISCLOSE_VERB_RE.search(_norm(body))):
        return True
    return False


def _mentions_content(m: dict, body: str, sents: list[str]) -> bool:
    """本文が材料の内容（表題）に触れているか（(h) 用の厳しい照合。日付＋発信主体だけでは触れた扱いにしない）。"""
    title = m.get("title") or ""
    if any(_mentions_title(title, s) for s in sents):
        return True
    dt = str(m.get("dt") or "")[:10]
    return bool(dt) and _date_in_text(dt, body) and _title_score(title, body) >= 0.4


def _usable_strong(st: dict) -> list[dict]:
    """理由に使える strong 材料（影響軽微の開示・値動きの見出しを除く）を使う順に並べて返す。

    順: strong かつ timing_fit（起点 → 会社開示 → 時刻あり → 早い順）→ strong（新しい日付順・同日は会社開示を先に）。
    """
    minor = {(_norm(d.get("title") or "")) for r in (st.get("week_days") or []) + (st.get("month3_days") or [])
             for d in (r.get("disclosures") or []) if d.get("minor_impact")}
    # 影響軽微の開示と同じ案件（表題が同じ・その開示を報じた記事）も除く（(b-2) と同じ扱い）
    strong = [m for m in _primaries(st) if _is_strong(m)
              and not any(_norm(m.get("title") or "") == t or _mentions_title(t, m.get("title") or "") for t in minor)
              and not PRICE_REPORT_RE.search(_norm(m.get("title") or ""))]
    fit = sorted([m for m in strong if m.get("timing_fit")],
                 key=lambda m: (0 if m.get("is_origin") else 1, _SRC_RANK.get(m.get("source"), 9),
                                0 if m.get("time_known") else 1, str(m.get("dt") or "")))
    rest = [m for m in strong if not m.get("timing_fit")]
    rest = sorted(sorted(rest, key=lambda m: (_SRC_RANK.get(m.get("source"), 9), 0 if m.get("time_known") else 1)),
                  key=lambda m: str(m.get("dt") or "")[:10], reverse=True)
    return fit + rest


def _unusable(st: dict) -> list[dict]:
    """理由に使えない primary（weak・影響軽微）を、時刻整合 → 新しい順に並べて返す（固定文の「確認した開示」用）。

    「確認した開示」なので、市場全体・複数銘柄の記事（strength_label=市場一覧・複数銘柄）と株探／Yahoo の
    区分外の記事は入れない（コード番号の数字が一致しただけの記事等を並べないため）。
    """
    use = {id(m) for m in _usable_strong(st)}
    out = [m for m in _primaries(st) if id(m) not in use
           and m.get("strength_label") not in ("市場一覧・複数銘柄", "英語版")
           and not (m.get("source") in ("kabutan", "yahoo") and m.get("strength_label") == "区分外")]
    return sorted(sorted(out, key=lambda m: str(m.get("dt") or ""), reverse=True),
                  key=lambda m: 0 if m.get("timing_fit") else 1)


def _material_list(st: dict, n: int = 4) -> str:
    """書き直し指示へ渡す「使える strong 材料」の一覧（strong かつ時刻整合 → strong の順）。"""
    out = []
    for m in _usable_strong(st)[:n]:
        tag = "【起点】" if m.get("is_origin") else ("【時刻整合】" if m.get("timing_fit") else "")
        out.append(f"[strong]{tag}{_when_label(m)} {m.get('publisher')}『{m.get('title')}』")
    if out:
        return "／".join(out)
    return ("使える strong 材料なし（weak＝告知・事務系の開示は理由に使えない。理由の欄は固定文「"
            + NO_STRONG_HEAD + "…）」で、確認した開示の部分は検査側が facts から入れる）")


def _check_quality(body: str, sents: list[str], st: dict, add, fixed_used: str = "") -> None:
    core = _strip_label(body)
    bare = _strip_quotes(core)
    has_mat = "materials" in st
    prim = _primaries(st) if has_mat else []
    strong = _usable_strong(st) if has_mat else []
    mats_hint = _material_list(st) if has_mat else ""
    # (g) 未特定の逃げ
    m = UNIDENTIFIED_RE.search(core)
    if m:
        add("g", "fail", f"未特定の語「{m.group(0)}」で理由を逃げている。使える材料: {mats_hint or 'facts に materials なし'}",
            word=m.group(0), materials=mats_hint)
    # (f) 数値埋め（先頭ラベルの騰落率は対象外）
    mm = PADDING_RE.search(core)
    if mm:
        add("f", "fail", f"市場データの数値で文字数を埋めている（「{mm.group(0)}」）。売買代金・出来高・時価総額・倍率は書かない",
            word=mm.group(0))
    # (e) 循環説明・定型文の誤用
    if not has_mat:
        add("e", "warn", "facts に materials が無い（(e)(h) を飛ばす）")
        return
    if _norm(NO_PRIMARY_OLD) in _norm(body):
        add("e", "fail", "旧定型文（2026-10-30 廃止）を書いている。使える材料: " + mats_hint, materials=mats_hint)
        return
    if fixed_used:
        if strong:
            add("e", "fail", f"使える strong 材料が {len(strong)} 件あるのに固定文（説明する材料なし）を書いている。"
                f"使える材料: {mats_hint}", materials=mats_hint)
        elif _norm(fixed_used) != _norm(no_strong_sentence(st)):
            # 固定文の「確認した開示」は検査側が facts から入れる（書き手の書いた版は機械差し替えで入れ直す）
            add("e", "fail", "固定文の「確認した開示」が facts と一致しない（検査側が facts から入れ直す）: "
                + no_strong_sentence(st))
        return
    c_sents = _sentences(core)
    pm = PRICE_REPORT_RE.search(bare)
    if pm and not any(_mentions_primary(x, core, c_sents) for x in prim):
        add("e", "fail", f"値動きの報道（「{pm.group(0)}」）を理由に置き、一次材料（会社開示・IR・材料記事）に触れていない。"
            f"使える材料: {mats_hint}", word=pm.group(0), materials=mats_hint)
    # (h) 説明力のない開示（weak だけで構成・strong に触れない）
    sid = {id(x) for x in strong}
    weak_hit = [x for x in prim if id(x) not in sid and _mentions_content(x, core, c_sents)]
    if weak_hit and not any(_mentions_content(x, core, c_sents) for x in strong):
        titles = "／".join(f"『{(x.get('title') or '')[:30]}』" for x in weak_hit[:2])
        add("h", "fail", f"説明力のない開示（告知・事務系の weak）{titles} だけで理由を書いている。"
            f"使える材料: {mats_hint}", materials=mats_hint, weak_titles=[x.get("title") for x in weak_hit[:3]])


def check_reasons(md_text: str, facts: dict) -> list[Failure]:
    """誌面 md と facts を照合し、不合格（severity=fail）・警告（warn）の一覧を返す。"""
    fs: list[Failure] = []
    stocks = _stocks(facts)
    if not stocks:
        fs.append(Failure("", "", 0, "facts", "warn", "facts に stocks が無い（全検査を飛ばす）"))
        return fs
    for ent in parse_entries(md_text):
        st = stocks.get(ent["code"])
        if not isinstance(st, dict):
            fs.append(Failure(ent["code"], _name_from_heading(ent["heading"], ent["code"]), ent["line"],
                              "x", "warn", "facts に当該銘柄が無い（ETL 失敗・検査を飛ばす）"))
            continue
        _check_entry(ent, st, fs)
    return fs


# --- 機械差し替え -----------------------------------------------------------
def _qualifying_disclosures(st: dict, day: str) -> list[dict]:
    row = _row_for(st, day) or {}
    out = []
    for d in row.get("disclosures") or []:
        if str(d.get("assigned_date") or row.get("date"))[:10] != day[:10]:
            continue
        if d.get("minor_impact"):
            continue
        if d.get("intraday") and not d.get("post_new_extreme"):
            continue
        if d.get("title"):
            out.append(d)
    return out


def _published_phrase(published: str, day: str) -> str:
    try:
        p = _dt.datetime.fromisoformat(str(published)[:16])
    except ValueError:
        return ""
    d = _dt.date.fromisoformat(day[:10])
    hm = f"{p.hour}:{p.minute:02d}"
    gap = (d - p.date()).days
    if gap == 0:
        return f"{hm} に"
    if gap == 1 or (d.weekday() == 0 and 1 <= gap <= 3):   # 前営業日（月曜は金〜日の公表分）
        return f"前日 {hm} に"
    return f"{_md(p.date().isoformat())} {hm} に"


def _when_label(m: dict) -> str:
    """'2026-10-23T12:15' → '10/23 12:15'（推定時刻は「頃」）・日付だけなら '10/23'。"""
    dt = str(m.get("dt") or "")
    try:
        p = _dt.datetime.fromisoformat(dt[:16])
    except ValueError:
        return dt[:10]
    s = f"{p.month}/{p.day}"
    if m.get("time_known") and len(dt) > 10:
        s += f" {p.hour}:{p.minute:02d}" + (" 頃" if m.get("time_approx") else "")
    return s


_TAIL_NOTICE_RE = re.compile(r"(ので|こと|旨)?[、,]?\s*(下記の(とおり|通り))?\s*(お知らせ|ご案内|ご報告)(いた)?(します|申し上げます)。?$")
_BOILER_HEAD_RE = re.compile(r"^[^、。]{0,40}?(株式会社|当社|同社)[^、。]{0,6}は[、,]\s*")


def _point(excerpt: str, title: str) -> str:
    """本文冒頭から要点を 40 字以内で取る（定型の書き出し・括弧書き・「お知らせします」を除く）。"""
    t = _norm(excerpt or "")
    for _ in range(3):
        t = re.sub(r"[（(][^（）()]{0,80}[）)]", "", t)
    sents = [x.strip() for x in re.split(r"(?<=。)", t) if x.strip()]
    cands = []   # (完結した文か, 文)
    for s in sents:
        s2 = _TAIL_NOTICE_RE.sub("", s.rstrip("。")).strip()
        s2 = _BOILER_HEAD_RE.sub("", s2).strip()
        if len(s2) >= 10:
            cands.append((s.endswith("。"), s2))
    if not cands:
        return ""
    full = [c for ok, c in cands if ok]
    pick = next((c for c in full if _title_score(title, c) < 0.6 and len(c) <= 40), None)
    if pick is None and full:
        # 表題の言い換えに近い文は、最後の読点の後ろ（述部の節）を要点にする
        last = full[0].rsplit("、", 1)[-1]
        pick = last if 8 <= len(last) <= 40 else full[0]
    if pick is None:
        pick = cands[0][1]
    if len(pick) > 40:
        cut = pick[:40]
        k = cut.rfind("、")
        pick = cut[:k] if k >= 15 else cut
    pick = pick.rstrip("、,。 ")
    # 主語だけの切れ端（「サイバートラスト<4498>は」）・銘柄コードの括弧書きは要点にしない（2026-10-30）
    # 助詞で始まる切れ端（「は10月23日受付で…」）・空白の崩れ（PDF の段組み）も要点にしない
    if (pick.endswith("は") or re.search(r"[<＜][0-9][0-9A-Z]{3}[>＞]", pick) or len(pick) < 8
            or re.match(r"^(は|が|を|に|の|へ|と|及び|並びに|及|並)", pick) or re.search(r"\s{2,}", pick)):
        return ""
    # 要点が他の検査（推測語・数値埋め・未特定・循環）に掛かる場合は付けない
    if PADDING_RE.search(pick) or UNIDENTIFIED_RE.search(pick) or any(w in pick for w in GUESS_WORDS) \
            or NO_MATERIAL in pick or PRICE_REPORT_RE.search(pick):
        return ""
    return pick


_SRC_RANK = {"tdnet": 0, "ir": 1, "kabutan": 2, "yahoo": 3}


def _pick_material(st: dict, day: str) -> dict | None:
    """機械差し替えに使う strong 材料（_usable_strong の先頭）。使える strong が無ければ None（固定文へ）。"""
    use = _usable_strong(st)
    return use[0] if use else None


def _safe_title(t: str) -> str:
    """固定文の「確認した開示」に入れる表題（文の区切り・引用符・括弧を崩す文字を除き 40 字まで）。"""
    t = re.sub(r"[。『』「」]", " ", _norm(t or "")).strip()
    return t[:40] + ("…" if len(t) > 40 else "")


def no_strong_sentence(st: dict) -> str:
    """固定文 NO_STRONG（確認した開示＝理由に使えない primary の表題を最大 2 件・同じ表題は 1 件）。"""
    seen, ts = set(), []
    for m in _unusable(st):
        k = re.sub(r"\s+", "", _norm(m.get("title") or ""))
        if not k or k in seen:
            continue
        seen.add(k)
        ts.append(f"{_when_label(m)}『{_safe_title(m.get('title') or '')}』")
        if len(ts) >= 2:
            break
    return NO_STRONG_TMPL.format(weak="・".join(ts) if ts else "なし")


def build_fallback_sentence(st: dict) -> str | None:
    """facts だけで「なぜ動いた」の 1 文を組む。max_move_day が無ければ None。

    2026-10-30 改定: facts に materials（lib/mover_materials.py）があれば strong の材料（_usable_strong の先頭）
    から組み、使える strong が 0 件なら固定文 NO_STRONG を書く。旧形式の facts（materials なし）は当日の開示だけで
    組み、開示も無ければ差し替えない（None）。数値（売買代金・出来高等）は入れない。
    """
    day = st.get("max_move_day")
    if not day:
        return None
    row = _row_for(st, day) or {}
    head = _md_wd(day)
    ret = row.get("ret_pct")
    if isinstance(ret, (int, float)):
        head += _pct(float(ret))
    if "materials" in st:
        m = _pick_material(st, day)
        if m is None:
            return f"{head}：{no_strong_sentence(st)}。"
        pt = _point(m.get("excerpt") or "", m.get("title") or "")
        sent = f"{_when_label(m)}、{m.get('publisher')}が『{m.get('title')}』を公表" + (f"（{pt}）" if pt else "") + "。"
        origin = str(st.get("origin_date") or "")[:10]
        tail = ""
        from_origin = bool(m.get("is_origin")) or (bool(m.get("timing_fit"))
                                                     and str(m.get("assigned_date") or "")[:10] <= origin)
        if from_origin and origin and origin < day[:10] and isinstance(st.get("streak_days"), int) \
                and st["streak_days"] >= 2:
            way = "下落" if isinstance(ret, (int, float)) and ret < 0 else "上昇"
            tail = (f"これを起点に{_md(day)}は{st['streak_days']}日連続の{way}の"
                    f"{st.get('streak_pos') or st['streak_days']}日目。")
        return f"{head}：{sent}{tail}"
    discs = _qualifying_disclosures(st, day)
    if discs:
        d = discs[0]
        when = _published_phrase(d.get("published") or "", day)
        return f"{head}：{when}「{d['title']}」を開示。"
    return None


def apply_fallback(md_text: str, facts: dict, failures: list[Failure]) -> tuple[str, list[str]]:
    """不合格銘柄の「なぜ動いた」を facts の 1 文へ差し替えた md と、差し替えた銘柄コードを返す。"""
    stocks = _stocks(facts)
    bad_lines = {f.line for f in failures if f.severity == "fail" and f.line}
    lines = md_text.splitlines()
    replaced: list[str] = []
    # 後ろから置換して行番号のずれを防ぐ
    for ent in sorted(parse_entries(md_text), key=lambda e: -e["line"]):
        if ent["line"] not in bad_lines or ent["reason_start"] is None:
            continue
        sent = build_fallback_sentence(stocks.get(ent["code"]) or {})
        if not sent:
            continue
        lines[ent["reason_start"]:ent["reason_end"]] = [f"**なぜ動いた**：{sent}"]
        replaced.append(ent["code"])
    out = "\n".join(lines)
    if md_text.endswith("\n"):
        out += "\n"
    return out, list(reversed(replaced))


# --- 出力 -------------------------------------------------------------------
def report_reasons(failures: list[Failure], prefix: str = PREFIX) -> None:
    fails = [f for f in failures if f.severity == "fail"]
    warns = [f for f in failures if f.severity == "warn"]
    if not fails:
        print(f"{prefix} [OK] 「なぜ動いた」の事実照合で不合格なし（警告 {len(warns)} 件）。")
    else:
        codes = sorted({f.code for f in fails})
        print(f"{prefix} [NG] 不合格 {len(fails)} 件（{len(codes)} 銘柄: {', '.join(codes)}）。",
              file=sys.stderr)
    for f in fails + warns:
        tag = "不合格" if f.severity == "fail" else "警告"
        print(f"{prefix}   [{tag}({f.check})] L{f.line} {f.code} {f.name}: {f.message}",
              file=sys.stderr if f.severity == "fail" else sys.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description="動意「なぜ動いた」の事実照合ゲート（不合格で exit 4）")
    ap.add_argument("--file", required=True, help="検査対象の誌面 md")
    ap.add_argument("--facts", required=True, help="ETL が出力した {date}_movers_facts.json")
    ap.add_argument("--fallback-fix", action="store_true",
                    help="不合格銘柄の「なぜ動いた」を facts だけの 1 文へ差し替えた md を別ファイルへ書く")
    ap.add_argument("--out", default="", help="--fallback-fix の出力先（既定: {入力名}_reasonfixed.md）")
    ap.add_argument("--json-out", default="", help="結果（不合格・警告・差し替え銘柄）を JSON で保存")
    args = ap.parse_args()

    try:
        text = Path(args.file).read_text(encoding="utf-8")
        facts = load_facts(args.facts)
    except (OSError, json.JSONDecodeError) as e:
        print(f"{PREFIX} 読込失敗: {e}", file=sys.stderr)
        return 1

    failures = check_reasons(text, facts)
    report_reasons(failures)
    has_fail = any(f.severity == "fail" for f in failures)

    result: dict = {"file": args.file, "facts": args.facts,
                    "fail_count": sum(f.severity == "fail" for f in failures),
                    "warn_count": sum(f.severity == "warn" for f in failures),
                    "failed_codes": sorted({f.code for f in failures if f.severity == "fail"}),
                    "failures": [asdict(f) for f in failures]}

    if args.fallback_fix:
        fixed, replaced = apply_fallback(text, facts, failures)
        out = Path(args.out) if args.out else Path(args.file).with_name(Path(args.file).stem + "_reasonfixed.md")
        try:
            out.write_text(fixed, encoding="utf-8")
        except OSError as e:
            print(f"{PREFIX} 差し替え md の書込失敗: {e}", file=sys.stderr)
            return 1
        after = [f for f in check_reasons(fixed, facts) if f.severity == "fail"]
        print(f"REPLACED={','.join(replaced)}")
        print(f"{PREFIX} 機械差し替え {len(replaced)} 銘柄 → {out}（差し替え後の不合格 {len(after)} 件）")
        result.update({"replaced": replaced, "out_file": str(out), "after_fix_fail_count": len(after),
                       "after_fix_failures": [asdict(f) for f in after]})

    if args.json_out:
        try:
            Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"{PREFIX} JSON 書込失敗: {e}", file=sys.stderr)
            return 1
    return 4 if has_fail else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
