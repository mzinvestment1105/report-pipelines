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
  (c) 推測語が出所付き引用 `{媒体}は「…」` の外にある                        … 不合格
  (d) 「材料は確認できず」を含む文（と直後の 1 文）で、創作理由の典型語が
      出所付き引用の外に出る                                              … 不合格
  (e) 最大変動日の材料欄に開示・報道があるのに本文がどれにも触れていない      … 警告のみ
  (f) facts に当該銘柄が無い（ETL 失敗）                                  … 警告のみ・検査を飛ばす
facts に必要なキーが無い場合は、その検査だけを飛ばして警告を出す（ETL の形の変化に寛容）。

不合格時の扱い（PM 判断事項 3）: workflow が該当銘柄だけ 1 回書き直させ、それでも不合格なら
`--fallback-fix` で facts だけから組んだ 1 文へ機械差し替えして配信する（絶対配信原則 _cr §36）。

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
NO_MATERIAL = "材料は確認できず"

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
    check: str           # "a"〜"f" / "facts"
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

    sents = _sentences(body)
    max_day = st.get("max_move_day")
    sigma2 = {str(d)[:10] for d in (st.get("sigma2_days") or [])}
    week, m3 = _day_rows(st)
    week_dates = {str(r["date"])[:10] for r in week}
    lead_segs = _lead_segments(body, sents, max_day)

    # (a) 最大変動日の M/D
    if not max_day:
        add("a", "warn", "facts に max_move_day が無い（(a)(b-1)(e) を飛ばす）")
    elif not _date_in_text(max_day, body):
        add("a", "fail", f"最大変動日 {_md(max_day)} が本文に無い")

    # (b) 本文に出る開示の妥当性
    if "sigma2_days" not in st:
        add("b", "warn", "facts に sigma2_days が無い（2σ 日は最大変動日のみで判定）")
    if not week and not m3:
        add("b", "warn", "facts に week_days / month3_days が無い（(b)(e) を飛ばす）")
    seen: set[tuple[str, str]] = set()
    for scope, rows in (("week", week), ("month3", m3)):
        for r in rows:
            for d in (r.get("disclosures") or []):
                title = d.get("title") or ""
                adate = str(d.get("assigned_date") or r.get("date"))[:10]
                if not title or (title, adate) in seen:
                    continue
                seen.add((title, adate))
                hit_sents = [s for s in sents if _mentions_title(title, s)]
                if not hit_sents:
                    continue
                in_lead = any(_mentions_title(title, s) for s in lead_segs)
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

    # (e) 最大変動日に材料があるのに本文が触れていない（警告のみ）
    if max_day:
        row = _row_for(st, max_day)
        if row is None:
            if week or m3:
                add("e", "warn", f"facts に最大変動日 {_md(max_day)} の行が無い（(e) を飛ばす）")
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
                    add("e", "warn", f"最大変動日 {_md(max_day)} の材料 {len(mats)} 件に本文が触れていない: "
                        + "／".join(t[:20] for _, t in mats[:3]))


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
                              "f", "warn", "facts に当該銘柄が無い（ETL 失敗・検査を飛ばす）"))
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


def build_fallback_sentence(st: dict) -> str | None:
    """facts だけで「なぜ動いた」の 1 文を組む（計画書 D-2 の書式）。max_move_day が無ければ None。"""
    day = st.get("max_move_day")
    if not day:
        return None
    row = _row_for(st, day) or {}
    head = _md_wd(day)
    if isinstance(row.get("ret_pct"), (int, float)):
        head += _pct(float(row["ret_pct"]))
    discs = _qualifying_disclosures(st, day)
    if discs:
        d = discs[0]
        when = _published_phrase(d.get("published") or "", day)
        return f"{head}：{when}「{d['title']}」を開示。"
    rel = [r for r in (row.get("related") or []) if r.get("name") and isinstance(r.get("ret_pct"), (int, float))]
    rel = sorted(rel, key=lambda r: -abs(float(r["ret_pct"])))[:2]
    tail = ""
    if rel:
        tail = "同日、" + "、".join(f"{r['name']}{_pct(float(r['ret_pct']))}" for r in rel) + "。"
    return f"{head}：{NO_MATERIAL}。{tail}"


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
