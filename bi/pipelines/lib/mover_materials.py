"""動意レポート「なぜ動いた」の一次情報を 5 層で集める（2026-10-28 PM 承認・理由品質の改修）。
================================================================================

背景: 「なぜ動いた」が (a) 値動きを報じた見出し（「ストップ高買い気配」等）をそのまま理由に置く循環説明、
(b) 売買代金・出来高の数値による文字数埋め、(c)「材料は確認できず」の逃げ、の 3 つで崩れていた。
原因は材料が「TDnet 表題 + Yahoo ファイナンス見出し」しか無かったこと。10/27 のグロース 1 位 3905 は
起点が 10/23 12:15 頃の会社 IR サイトのプレスリリース（TDnet 未掲載）だった。

層（基準日 D から 5 営業日前まで。起点日が窓より前なら起点日の前後 1 営業日まで窓を広げる）:
  層1 tdnet   TDnet 開示（yanoshin の一覧＋株探の開示行）の PDF 本文冒頭 600 字
  層2 ir      会社 IR サイトの新着（台帳 bi/data/ir_sites.csv で URL を再利用）と本文冒頭 400 字
  層3 kabutan 株探の銘柄ニュース（時刻・区分・表題）と材料記事の本文冒頭 400 字
  層4 yahoo   Yahoo!ファイナンス銘柄ニュースの見出しと本文冒頭 400 字
  層5 遡り    日別騰落から今回の上昇／下落の起点日を判定し、起点日が D より前なら起点日の前後 1 営業日を必ず取る

分類（kind）:
  primary       会社開示・IR リリース・決算・業績修正・受注・提携・大量保有・報道機関の材料記事
  price_report  値動きを報じた見出し（PRICE_REPORT_RE）。理由として使用不可・price_reports へ隔離
  strength（2026-10-30・primary のみ）: strong=決算・業績修正・配当修正・受注／契約／採用／提携・新製品・大量保有・
                自社株買い・株式分割・市場変更・M&A・希薄化・代表交代・独自報道・悪材料 / weak=説明会・セミナー・動画・
                株式譲渡報告書・行使状況・ガバナンス・定款・役員異動・再掲・英語版・非数値の訂正・区分外。weak は理由に使えない
  timing_fit   発信時刻の反映日（引け後→翌営業日・場中／寄り前→当日・15:00〜15:30→当日と翌営業日・日付のみ→当日と翌営業日）
               が今回の動き（起点日〜基準日）に入る
  （市場全体の一覧・テクニカル一覧・当該銘柄に触れない記事は材料ではないため捨て、件数だけ fetch_log に残す）

全層とも timeout（接続 10 秒・読み取り 20 秒）と失敗記録（fetch_log）を持ち、1 層の失敗で他層を止めない。
同一ホストへの同時接続は 2 本まで・リクエスト間隔は 0.5〜1.0 秒（株探は 1.0 秒以上）。

使い方（ライブラリ・make_mover_report.py から）:
    col = MaterialsCollector(base_date, ledger_path=..., names=..., state_writes=True)
    col.start_prefetch(codes, tdnet_entries)          # 層1〜3 を裏で開始（Yahoo 取得と並行）
    res = col.finalize(codes, facts_stocks, yahoo_news)  # 起点日判定・遡り・層4・分類 → {code: {...}}

使い方（単体）:
    python bi/pipelines/lib/mover_materials.py --code 3905 --date 2026-10-27 [--json-out x.json]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:  # パッケージ経由（make_mover_report.py）と単体実行の両対応
    from lib import move_days as _md
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib import move_days as _md

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
WINDOW_BDAYS = 5
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 20
PER_HOST_CONCURRENCY = 2
SLEEP_MIN, SLEEP_MAX = 0.5, 1.0
KABUTAN_MIN_INTERVAL = 1.0
LIST_MAX_BYTES = 200_000          # IR 一覧・会社トップ・記事ページの読み取り上限（1 ページ）
PDF_MAX_BYTES = 6_000_000
IR_RESOLVE_MAX_PAGES = 3
KABUTAN_MAX_PAGES = 3             # 窓の起点まで遡る株探ニュース一覧の最大ページ数（遡り時は 6）
TDNET_EXCERPT = 600
BODY_EXCERPT = 400
ARTICLES_PER_STOCK = 4            # 株探・Yahoo の本文を取る材料記事の上限（1 銘柄・層ごと）
IR_BODIES_PER_STOCK = 4
MAX_PRIMARY_LISTED = 14           # raw に並べる primary の上限（起点の項目は必ず残す）
LEDGER_RETRY_DAYS = 30            # unparsed / no_site を再解決するまでの日数
LEDGER_PARSER_SINCE = "2026-10-30"  # 一覧の読み取りを改良した日（eir-parts 対応）。これより前の unparsed は待たずに再解決する
CLOSE_MIN = _md.CLOSE_MIN         # 15:30

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# 値動きを報じた見出し（PM 承認の語彙・理由として使用不可）。reason_check.py も同じ語彙を使う。
PRICE_REPORT_PATTERN = (r"買い気配|ストップ高|S高|急騰|急伸|急落|急反発|急反落|続伸|続落|反発|反落|話題|注目|人気"
                        r"|前日に|年初来|上場来|大幅高|大幅安|物色|買われ|売られ")
PRICE_REPORT_RE = re.compile(PRICE_REPORT_PATTERN)

# 市場全体の一覧・順位表・テクニカル一覧（当該銘柄の材料ではない）
_MARKET_LIST_RE = re.compile(
    r"^本日の【|^前場の【|^本日のランキング|^前場のランキング|^週間ランキング|^今週の【|^先週の"
    r"|^《\d+日の|ランキング|ランクイン|＜動意株|<動意株|^＜|^<|^〔ブル＆ベア〕|^東証グロース[（(]|^東証スタンダード[（(]"
    r"|^東証プライム[（(]|市況|^5％ルール|^5%ルール|^５％ルール|銘柄一覧|^レーティング|^証金残|^信用残"
    r"|^話題株ピックアップ|^個別銘柄|^新興市場|^後場コメント|^前場コメント|^【本日の材料と銘柄】"
)


def is_market_list(title: str) -> bool:
    """市場全体の一覧・順位表・テクニカル一覧の見出し（原文と NFKC 正規化後の両方で判定）。"""
    return bool(_MARKET_LIST_RE.search(title or "") or _MARKET_LIST_RE.search(unicodedata.normalize("NFKC", title or "")))


_KABUTAN_PRIMARY_CTG = {"開示", "決算", "修正", "５％", "5％", "5%"}
_KABUTAN_ARTICLE_CTG = {"材料", "特集", "注目", "速報", "決算", "修正", "５％", "5％", "5%"}

# 材料記事の区分の手掛かり（primary の内訳表示用）
_TOPIC_RE = [
    ("決算", re.compile(r"決算|四半期|通期|業績")),
    ("業績修正", re.compile(r"修正|上方|下方|上振れ|下振れ")),
    ("受注", re.compile(r"受注|契約|大口")),
    ("提携", re.compile(r"提携|協業|資本業務|連携|共同")),
    ("大量保有", re.compile(r"大量保有|保有割合|変更報告書|主要株主")),
]

_DATE_FULL = re.compile(r"(20\d{2})\s*[./年-]\s*(\d{1,2})\s*[./月-]\s*(\d{1,2})\s*日?")
_DATE_SHORT_ONLY = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})\s*$")
_TIME_PHRASE = re.compile(r"(午前|午後)\s*(\d{1,2})時\s*(?:(\d{1,2})分|半)?\s*(ごろ|頃)?")
_TIME_HM = re.compile(r"(\d{1,2})時(\d{1,2})分")
_IR_LINK_RE = re.compile(r"news|ir|release|pressrelease|topics|press|info", re.I)
_IR_TEXT_RE = re.compile(r"ニュース|お知らせ|IR|ＩＲ|プレスリリース|リリース|トピックス|新着|投資家")
_NOISE_TOKENS = re.compile(r"picture_as_pdf|\bNEW\b|\bPDF\b|［PDF］|\[PDF\]|（PDF[^）]*）|\(PDF[^)]*\)|new!?", re.I)

_TDNET_ID_RE = re.compile(r"(\d{18})")


# ---------------------------------------------------------------------------
# 取得の共通部品（ホスト別の同時接続・間隔制御）
# ---------------------------------------------------------------------------
class _HostGate:
    def __init__(self) -> None:
        self._g = threading.Lock()
        self._sem: dict[str, threading.BoundedSemaphore] = {}
        self._next: dict[str, float] = {}

    def _interval(self, host: str) -> float:
        if "kabutan.jp" in host:
            return KABUTAN_MIN_INTERVAL + random.uniform(0.0, 0.2)
        return random.uniform(SLEEP_MIN, SLEEP_MAX)

    def __call__(self, host: str):
        gate = self

        class _Ctx:
            def __enter__(self_inner):
                with gate._g:
                    sem = gate._sem.setdefault(host, threading.BoundedSemaphore(PER_HOST_CONCURRENCY))
                sem.acquire()
                with gate._g:
                    now = time.monotonic()
                    start = max(now, gate._next.get(host, 0.0))
                    gate._next[host] = start + gate._interval(host)
                wait = start - now
                if wait > 0:
                    time.sleep(wait)
                return self_inner

            def __exit__(self_inner, *exc):
                gate._sem[host].release()
                return False
        return _Ctx()


GATE = _HostGate()
_STATS_LOCK = threading.Lock()
HOST_STATS: dict[str, dict[str, int]] = {}


def _stat(layer: str, key: str) -> None:
    with _STATS_LOCK:
        d = HOST_STATS.setdefault(layer, {})
        d[key] = d.get(key, 0) + 1


def _decode(raw: bytes, header_ct: str) -> str:
    enc = None
    m = re.search(r"charset=([\w-]+)", header_ct or "", re.I)
    if m:
        enc = m.group(1)
    if not enc:
        m2 = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4000], re.I)
        if m2:
            enc = m2.group(1).decode("ascii", "ignore")
    for e in ([enc] if enc else []) + ["utf-8", "cp932", "euc_jp"]:
        try:
            if e and e.lower() in ("shift_jis", "shift-jis", "sjis", "x-sjis"):
                e = "cp932"
            return raw.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def http_get(url: str, layer: str, max_bytes: int = LIST_MAX_BYTES, binary: bool = False,
             headers: dict | None = None) -> tuple[object | None, str, str]:
    """GET（ホスト別ゲート・timeout・読み取り上限つき）。戻り値 (本文 or None, status, detail)。

    status: ok / http_{code} / timeout / error / too_large（binary のみ。上限超過は切り詰めずに失敗扱い）
    """
    import requests
    host = urlparse(url).netloc
    try:
        with GATE(host):
            r = requests.get(url, headers=headers or UA, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                             stream=True, allow_redirects=True)
            try:
                if r.status_code != 200:
                    _stat(layer, f"http_{r.status_code}")
                    return None, f"http_{r.status_code}", url[:120]
                buf = io.BytesIO()
                for chunk in r.iter_content(65536):
                    buf.write(chunk)
                    if buf.tell() >= max_bytes:
                        break
                data = buf.getvalue()[:max_bytes]
                truncated = buf.tell() >= max_bytes
                ct = r.headers.get("Content-Type", "")
                final_url = r.url
            finally:
                r.close()
    except requests.exceptions.Timeout:
        _stat(layer, "timeout")
        return None, "timeout", url[:120]
    except Exception as e:  # 接続拒否・DNS・SSL 等
        _stat(layer, "error")
        return None, "error", f"{type(e).__name__} {url[:100]}"
    _stat(layer, "ok")
    if binary:
        if truncated:
            return None, "too_large", url[:120]
        return data, "ok", final_url
    return _decode(data, ct), "ok", final_url


# ---------------------------------------------------------------------------
# PDF（move_days の「影響軽微」判定と共通の取得キャッシュ）
# ---------------------------------------------------------------------------
def pdf_bytes(url: str, layer: str = "tdnet") -> tuple[bytes | None, str]:
    """PDF の中身。move_days の共通キャッシュを使い、同じ PDF を 2 度取りに行かない。"""
    cached = _md.pdf_cache_get(url)
    if cached is not None:
        return cached, "ok(cache)"
    data, st, _ = http_get(url, layer, max_bytes=PDF_MAX_BYTES, binary=True)
    if data is None and "release.tdnet.info" in url:
        # TDnet 側で 404 になる開示（2026-10-28 実測: 10/21 基準で TDnet PDF の約 5 割）は
        # 株探の PDF 複製（tdnet-pdf.kabutan.jp/{YYYYMMDD}/{ID}.pdf）で取り直す
        m = _TDNET_ID_RE.search(url)
        if m:
            mirror = f"https://tdnet-pdf.kabutan.jp/{m.group(1)[4:12]}/{m.group(1)}.pdf"
            data2, st2, _ = http_get(mirror, "tdnet_mirror", max_bytes=PDF_MAX_BYTES, binary=True)
            if data2 is not None:
                data, st = data2, "ok(kabutan_mirror)"
            else:
                # 2026-10-30 実測: 同じ PDF が実測時 404・後で 200（140120261015551719）。1 回だけ取り直す
                time.sleep(1.5)
                data3, st3, _ = http_get(url, layer, max_bytes=PDF_MAX_BYTES, binary=True)
                if data3 is not None:
                    data, st = data3, "ok(retry)"
    if data is not None:
        _md.pdf_cache_put(url, data)
    return data, st


_CJK = r"぀-ヿ一-鿿＀-￯"
_LETTERHEAD = re.compile(r"(各\s*位|会\s*社\s*名|代\s*表\s*者\s*名?|問\s*い?\s*合\s*わ?\s*せ\s*先|TEL)")


def _squash_cjk_spaces(s: str) -> str:
    """PDF 抽出で字間に入る空白（「デ ー タ セ ク」）を詰める。"""
    return re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", s)


def pdf_head_text(data: bytes, max_chars: int, maxpages: int = 2, title: str = "") -> str:
    """PDF 本文の冒頭。開示の定型の頭書き（各位・会社名・代表者・問合せ先）は表題の後ろから取って除く。"""
    try:
        from pdfminer.high_level import extract_text
        txt = extract_text(io.BytesIO(data), maxpages=maxpages) or ""
    except Exception:
        return ""
    t = _squash_cjk_spaces(_clean_text(_nfkc(txt)))
    if title:
        key = _squash_cjk_spaces(_nfkc(title)).replace(" ", "")
        flat = t.replace(" ", "")
        for k in (key, key[:20], key[:12]):
            if len(k) >= 6 and k in flat:
                # 空白を詰めた文字列上の位置を元の文字列の位置へ戻す
                pos, cnt = flat.index(k) + len(k), 0
                for i, ch in enumerate(t):
                    if ch != " ":
                        cnt += 1
                    if cnt >= pos:
                        rest = t[i + 1:]
                        # 表題の先頭部分だけ一致した場合は、その語の切れ目（次の空白）まで進める
                        if k != key:
                            sp = rest.find(" ")
                            if 0 <= sp <= 8:
                                rest = rest[sp:]
                        t = rest.strip(" 　")
                        break
                break
    if _LETTERHEAD.search(t[:120]):
        m = re.search(r"(当社|当行|当グループ|弊社)(は|の|では)", t)
        if m:
            t = t[m.start():]
    return t[:max_chars]


# ---------------------------------------------------------------------------
# 文字列処理
# ---------------------------------------------------------------------------
def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def _clean_text(s: str) -> str:
    s = re.sub(r"[　\s]+", " ", s or "")
    return s.strip()


def _soup(html: str):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def extract_body(html: str, title: str = "", max_chars: int = BODY_EXCERPT) -> str:
    """記事・リリースのページから本文冒頭を返す（サイト非依存の汎用抽出）。

    1) script/style/nav/header/footer/aside を除去 2) 記事らしい要素（article の Detail 系・main・本文 class）
    を選ぶ 3) その中の <p> を連結（50 字以上あれば採用）、無ければ行単位の文章（句点を含む行）を連結。
    表題の行より後ろから取る（表題の手前のパンくず・日付・区分を除く）。
    """
    s = _soup(html)
    for t in s(["script", "style", "nav", "header", "footer", "aside", "noscript", "form", "iframe"]):
        t.decompose()
    cands = []
    for a in s.find_all("article"):
        cls = " ".join(a.get("class") or [])
        cands.append((0 if re.search(r"detail|body|content|entry|post", cls, re.I) else 1, a))
    cands.sort(key=lambda x: x[0])
    nodes = [c for _, c in cands]
    for sel in ("div.body", "main", "[class*=entry-content]", "[class*=post-content]", "[class*=article]",
                "[class*=content]", "[id*=content]", "body"):
        try:
            nodes += s.select(sel)[:2]
        except Exception:
            continue
    tcore = _nfkc(title).strip()[:12]
    for node in nodes:
        if node is None:
            continue
        ps = [_clean_text(p.get_text("", strip=True)) for p in node.find_all("p")]
        ps = [p for p in ps if len(p) >= 15 and not _MARKET_LIST_RE.search(p)]
        text = " ".join(ps)
        if len(text) < 50:
            lines = [_clean_text(x) for x in node.get_text("\n", strip=True).split("\n")]
            if tcore:
                idx = next((i for i, ln in enumerate(lines) if tcore and tcore in _nfkc(ln)), None)
                if idx is not None:
                    lines = lines[idx + 1:]
            # インライン要素で割れた行（「データセクション<」「3905」「>が…」）をつなぎ直す
            joined, buf = [], ""
            for ln in lines:
                if not ln:
                    continue
                if buf and (buf.endswith(("<", "＜", "(", "（")) or ln.startswith((">", "＞", ")", "）"))
                            or re.fullmatch(r"[0-9A-Z]{4}", ln)):
                    buf += ln
                else:
                    if buf:
                        joined.append(buf)
                    buf = ln
            if buf:
                joined.append(buf)
            text = " ".join(x for x in joined if "。" in x and len(x) >= 15)
        if tcore and text.startswith(tcore):
            text = text[len(_nfkc(title).strip()):].strip()
        if len(text) >= 30:
            return text[:max_chars]
    return ""


def _parse_date_text(txt: str, ref: date) -> date | None:
    t = _nfkc(txt)
    m = _DATE_FULL.search(t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _DATE_SHORT_ONLY.match(t)
    if m:
        return _md._year_for(int(m.group(1)), int(m.group(2)), ref)
    return None


def _segments(node) -> list[str]:
    return [_clean_text(_NOISE_TOKENS.sub("", x)) for x in node.get_text("\n", strip=True).split("\n")
            if _clean_text(_NOISE_TOKENS.sub("", x))]


def parse_dated_links(html: str, base_url: str, ref: date) -> list[dict]:
    """一覧ページから「日付 + 表題 + リンク」を汎用に抽出する（会社 IR サイト用）。

    日付の表記は 2026.10.23 / 2026/10/23 / 2026年10月23日 / 2026-10-23 と、日付だけの要素の 10.23 を許容。
    リンク自身・親（3 階層まで・リンク 3 本以下・400 字以下）・直前の兄弟要素（dt 等）の順に日付を探す。
    """
    s = _soup(html)
    for t in s(["script", "style", "noscript", "header", "footer", "nav"]):
        t.decompose()
    out: list[dict] = []
    seen: set[tuple] = set()
    for a in s.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        d = None
        container = a
        segs = _segments(a)
        for seg in segs:
            d = _parse_date_text(seg, ref) if (len(seg) <= 40 or _DATE_FULL.search(_nfkc(seg))) else None
            if d:
                break
        if d is None:
            p = a
            for _ in range(3):
                p = p.parent
                if p is None or p.name in ("body", "html", "ul", "ol", "table", "tbody"):
                    break
                txt = p.get_text(" ", strip=True)
                if len(txt) > 400 or len(p.find_all("a", href=True)) > 3:
                    break
                ds = {x for x in (_parse_date_text(sg, ref) for sg in _segments(p)
                                  if len(sg) <= 40 or _DATE_FULL.search(_nfkc(sg))) if x}
                if len(ds) == 1:
                    d, container = ds.pop(), p
                    break
                prev = p.find_previous_sibling()
                if prev is not None:
                    pt = prev.get_text(" ", strip=True)
                    if len(pt) <= 40:
                        dd = _parse_date_text(pt, ref)
                        if dd:
                            d, container = dd, p
                            break
        if d is None:
            continue
        cand = [sg for sg in _segments(container)
                if not (_parse_date_text(sg, ref) and len(_DATE_FULL.sub("", _nfkc(sg)).strip()) <= 2)]
        cand = [_DATE_FULL.sub("", _nfkc(sg)).strip(" 　|｜-–—・:：") if _DATE_FULL.search(_nfkc(sg)) else sg
                for sg in cand]
        cand = [c for c in cand if len(c) >= 6]
        if not cand:
            continue
        title = max(cand, key=len)[:160]
        key = (d, title)
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": d, "title": title, "url": url})
    return out


# ---------------------------------------------------------------------------
# eir-parts（外部の IR 掲載部品・2026-10-30 追加）
# 会社 IR ページの新着欄を外部サービス eir-parts.net の JavaScript で描く会社は、HTML に日付つきの一覧が無く
# unparsed になっていた（2026-10-30 実測: 4588 オンコリス・3692 FFRI）。部品の設定ファイルから
# IR ニュースの JSONP（V4Public/EIR/{code}/ja/announcement/announcement_{N}.js）を求め、そこから読む。
# ---------------------------------------------------------------------------
EIR_HOST = "https://ssl4.eir-parts.net"
EIR_MAX_BYTES = 400_000            # JSONP は新しい順。全履歴（1MB 超）は要らないので先頭だけ読む
_EIR_JSON_RE = re.compile(r"V4Public/EIR/([0-9A-Za-z]+)/ja/announcement/(announcement_\d+)\.js")
_EIR_CODE_RE = re.compile(r"eirCode\s*=\s*['\"]([0-9A-Za-z]+)['\"]")
_EIR_UNI_RE = re.compile(r"uniCodeEir\s*=\s*['\"]([0-9A-Za-z]+)['\"]")
_EIR_PARTS_RE = re.compile(r"setParts\(\s*['\"]([A-Za-z0-9_]+)['\"]")
_EIR_SCRIPT_RE = re.compile(r"<script[^>]+src=[\"']([^\"']*eir[^\"'/]*\.js)[^\"']*[\"']", re.I)
_EIR_ITEM_SPLIT = re.compile(r'"guid"\s*:')


def is_eir_feed(url: str) -> bool:
    return "eir-parts.net/V4Public/EIR/" in (url or "")


def eir_feed_from_page(html: str, page_url: str) -> str:
    """eir-parts を使うページから IR ニュースの JSONP の URL を求める（使っていなければ ""）。"""
    if not html or "eir" not in html.lower():
        return ""
    m = _EIR_JSON_RE.search(html)
    if m:
        return f"{EIR_HOST}/V4Public/EIR/{m.group(1)}/ja/announcement/{m.group(2)}.js"
    parts = _EIR_PARTS_RE.search(html)
    if not parts:
        return ""
    code, uni = _EIR_CODE_RE.search(html), _EIR_UNI_RE.search(html)
    if not (code and uni):
        # eirCode・uniCodeEir は会社サイト側の eir.js / eir_n.js に書かれている
        for src in _EIR_SCRIPT_RE.findall(html)[:2]:
            js, _st, _ = http_get(urljoin(page_url, src), "ir_resolve", max_bytes=50_000)
            if js:
                code, uni = code or _EIR_CODE_RE.search(js), uni or _EIR_UNI_RE.search(js)
            if code and uni:
                break
    if not (code and uni):
        return ""
    conf_url = f"{EIR_HOST}/Custom/public/parts/{code.group(1)}/{uni.group(1)}/parts/conf/{parts.group(1)}.js"
    conf, _st, _ = http_get(conf_url, "ir_resolve", max_bytes=200_000)
    m = _EIR_JSON_RE.search(conf or "")
    return f"{EIR_HOST}/V4Public/EIR/{m.group(1)}/ja/announcement/{m.group(2)}.js" if m else ""


def _json_str(chunk: str, key: str) -> str:
    m = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
    if not m:
        return ""
    try:
        return json.loads('"' + m.group(1) + '"')
    except ValueError:
        return m.group(1)


def parse_eir_items(text: str, ref: date) -> list[dict]:
    """eir-parts の JSONP（先頭だけでも可）から「日時 + 表題 + リンク」を取る。予定の枠（リンク無し）は除く。"""
    out: list[dict] = []
    for chunk in _EIR_ITEM_SPLIT.split(text or "")[1:]:
        link = _json_str(chunk, "link")
        title = _clean_text(_json_str(chunk, "title"))
        dm = re.match(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", _json_str(chunk, "date"))
        if not (link and title and dm):
            continue
        try:
            dt = datetime(*(int(x) for x in dm.groups()))
        except ValueError:
            continue
        if dt.date() > ref + timedelta(days=1):
            continue
        tk = not (dt.hour == 0 and dt.minute <= 1)
        out.append({"date": dt.date(), "dt": dt, "time_known": tk, "title": title[:160],
                    "url": link if link.startswith("http") else urljoin(EIR_HOST, link)})
    return out


# ---------------------------------------------------------------------------
# 営業日
# ---------------------------------------------------------------------------
def _is_bday(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in ((1, 1), (1, 2), (1, 3), (12, 31)):
        return False
    try:
        import jpholiday
        return not jpholiday.is_holiday(d)
    except Exception:
        return True


def bday_shift(d: date, n: int) -> date:
    step = 1 if n > 0 else -1
    k = abs(n)
    x = d
    while k:
        x += timedelta(days=step)
        if _is_bday(x):
            k -= 1
    return x


def assign_bday(dt: datetime, time_known: bool) -> date:
    """前営業日 15:30〜当日 15:30 の窓で営業日に割り当てる（move_days.assign_day と同じ境界）。"""
    d0 = dt.date()
    if time_known and dt.hour * 60 + dt.minute >= CLOSE_MIN:
        d0 += timedelta(days=1)
    while not _is_bday(d0):
        d0 += timedelta(days=1)
    return d0


# ---------------------------------------------------------------------------
# IR サイト台帳
# ---------------------------------------------------------------------------
LEDGER_COLS = ["code", "company_url", "news_url", "resolved_at", "status"]


class IRLedger:
    def __init__(self, path: Path | None, writable: bool = True) -> None:
        self.path = Path(path) if path else None
        self.writable = writable and self.path is not None
        self.rows: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.dirty = False
        if self.path and self.path.exists():
            with open(self.path, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("code"):
                        self.rows[r["code"]] = {k: r.get(k, "") for k in LEDGER_COLS}

    def get(self, code: str) -> dict | None:
        with self.lock:
            r = self.rows.get(code)
            return dict(r) if r else None

    def put(self, code: str, **kw) -> None:
        with self.lock:
            r = self.rows.setdefault(code, {k: "" for k in LEDGER_COLS})
            r.update({k: ("" if v is None else str(v)) for k, v in kw.items() if k in LEDGER_COLS})
            r["code"] = code
            self.dirty = True

    def save(self) -> None:
        if not (self.writable and self.dirty):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".csv.tmp")
        with self.lock:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
                w.writeheader()
                for c in sorted(self.rows):
                    w.writerow(self.rows[c])
            os.replace(tmp, self.path)
            self.dirty = False


# ---------------------------------------------------------------------------
# 1 銘柄分の作業状態
# ---------------------------------------------------------------------------
class _Stock:
    def __init__(self, code: str, name: str) -> None:
        self.code = code
        self.name = name or code
        self.items: list[dict] = []
        self.log: list[dict] = []
        self.lock = threading.Lock()
        self.kabutan_ids: set[str] = set()
        self.done_ranges: list[tuple[date, date]] = []

    def add(self, it: dict) -> None:
        with self.lock:
            self.items.append(it)

    def flog(self, layer: str, status: str, detail: str = "") -> None:
        with self.lock:
            self.log.append({"layer": layer, "status": status, "detail": detail[:160]})


_MKT_PREFIX = re.compile(r"^[GSPTMJ東名福札]-")


def clean_name(name: str) -> str:
    """yanoshin の社名の市場接頭辞（「Ｇ－」等）と法人格を外す。"""
    return _MKT_PREFIX.sub("", _md._short_name(name or "")).strip()


def _keys_for(name: str, code: str) -> list[str]:
    """見出しが当該銘柄に触れているかの照合キー（コード・社名・先頭 4〜5 字の略称）。"""
    short = clean_name(name)
    ks = [code]
    if short:
        ks += [short, short[:5], short[:4]] if len(short) > 4 else [short]
    return [k for k in dict.fromkeys(ks) if k and (len(k) >= 2)]


def _mentions_stock(title: str, keys: list[str]) -> bool:
    """表題が当該銘柄に触れているか。銘柄コードは数字の一部（指数値「3692.583」等）では一致させない（2026-10-30）。"""
    t = _nfkc(title)
    for k in keys:
        kk = _nfkc(k)
        if re.fullmatch(r"[0-9][0-9A-Z]{3}", kk):
            if re.search(rf"(?<![0-9A-Za-z.,]){re.escape(kk)}(?![0-9A-Za-z.,])", t):
                return True
        elif kk in t:
            return True
    return False


def _item(dt: datetime, time_known: bool, source: str, publisher: str, title: str, url: str,
          excerpt: str = "", found_via: str = "", category: str = "") -> dict:
    return {"dt": dt, "time_known": time_known, "source": source, "publisher": publisher,
            "title": _clean_text(title), "url": url, "excerpt": _clean_text(excerpt),
            "found_via": found_via or source, "category": category}


# ---------------------------------------------------------------------------
# 層1 TDnet
# ---------------------------------------------------------------------------
def tdnet_pdf_url(raw: str) -> str:
    m = _TDNET_ID_RE.search(raw or "")
    return f"https://www.release.tdnet.info/inbs/{m.group(1)}.pdf" if m else (raw or "")


def layer_tdnet(st: _Stock, entries: list[dict], win: tuple[datetime, datetime]) -> None:
    n_in = n_pdf = n_fail = 0
    for e in entries or []:
        pub = str(e.get("published") or "")
        dt = _md.parse_dt(pub)
        if dt is None or not (win[0] <= dt <= win[1]):
            continue
        n_in += 1
        url = tdnet_pdf_url(e.get("pdf_url") or "")
        excerpt = ""
        if url:
            data, stt = pdf_bytes(url, "tdnet")
            if data is not None:
                excerpt = pdf_head_text(data, TDNET_EXCERPT, title=e.get("title") or "")
                n_pdf += 1
            else:
                n_fail += 1
                st.flog("tdnet", stt, f"PDF {url[-28:]}")
        st.add(_item(dt, _md.has_time(pub), "tdnet", f"{st.name}（適時開示）", e.get("title") or "", url,
                     excerpt, found_via="tdnet", category="開示"))
        m = _TDNET_ID_RE.search(url)
        if m:
            st.kabutan_ids.add(m.group(1))
    st.flog("tdnet", "ok" if not n_fail else "partial", f"窓内 {n_in} 件・本文 {n_pdf} 件・失敗 {n_fail} 件")


# ---------------------------------------------------------------------------
# 層3 株探
# ---------------------------------------------------------------------------
_KABUTAN_NEWS = "https://kabutan.jp/stock/news?code={code}"
_KABUTAN_STOCK = "https://kabutan.jp/stock/?code={code}"


def _kabutan_rows(html: str) -> list[dict]:
    s = _soup(html)
    t = s.find("table", class_="s_news_list")
    rows = []
    if t is None:
        return rows
    for tr in t.find_all("tr"):
        tm = tr.find("time")
        a = tr.find("a", href=True)
        if tm is None or a is None:
            continue
        dt = _md.parse_dt(tm.get("datetime") or "")
        if dt is None:
            continue
        ctg_el = tr.find("div", class_="newslist_ctg")
        rows.append({"dt": dt, "ctg": _nfkc(ctg_el.get_text(strip=True)) if ctg_el else "",
                     "title": _clean_text(a.get_text(" ", strip=True)),
                     "href": urljoin("https://kabutan.jp/", a["href"])})
    return rows


def layer_kabutan(st: _Stock, win: tuple[datetime, datetime], max_pages: int = KABUTAN_MAX_PAGES) -> None:
    keys = _keys_for(st.name, st.code)
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        url = _KABUTAN_NEWS.format(code=st.code) + (f"&nmode=0&page={page}" if page > 1 else "")
        html, stt, det = http_get(url, "kabutan", max_bytes=400_000)
        if html is None:
            st.flog("kabutan", stt, f"一覧 page{page}")
            break
        rs = _kabutan_rows(html)
        if not rs:
            st.flog("kabutan", "unparsed" if page == 1 else "ok", f"一覧 page{page} 行なし")
            break
        rows += rs
        if min(r["dt"] for r in rs) < win[0]:
            break
    n_win = n_art = n_drop = 0
    arts_left = ARTICLES_PER_STOCK
    body_cands: list[dict] = []
    for r in rows:
        if not (win[0] <= r["dt"] <= win[1]):
            continue
        n_win += 1
        ctg, title = r["ctg"], r["title"]
        if ctg == "開示":
            m = _TDNET_ID_RE.search(r["href"])
            tid = m.group(1) if m else ""
            if tid and tid in st.kabutan_ids:
                continue  # 層1 で取得済み
            url = tdnet_pdf_url(tid) if tid else r["href"]
            excerpt = ""
            data, stt = pdf_bytes(url, "tdnet") if tid else (None, "skip")   # TDnet 404 は株探の PDF 複製で取り直す
            if data is not None:
                excerpt = pdf_head_text(data, TDNET_EXCERPT, title=title)
            else:
                st.flog("tdnet", stt, f"PDF（株探の開示行）{tid}")
            if tid:
                st.kabutan_ids.add(tid)
            st.add(_item(r["dt"], True, "tdnet", f"{st.name}（適時開示）", title, url, excerpt,
                         found_via="kabutan", category="開示"))
            continue
        it = _item(r["dt"], True, "kabutan", "株探", title, r["href"], "", found_via="kabutan", category=ctg)
        primary_ctg = ctg in _KABUTAN_PRIMARY_CTG
        if not primary_ctg and (ctg not in _KABUTAN_ARTICLE_CTG or is_market_list(title)):
            n_drop += 1
            continue
        if not primary_ctg and not PRICE_REPORT_RE.search(_nfkc(title)) and not _mentions_stock(title, keys):
            n_drop += 1
            continue
        if (not PRICE_REPORT_RE.search(_nfkc(title))) and "/stock/news" in r["href"]:
            body_cands.append(it)
        st.add(it)
    # 本文を取る記事は「材料 → 決算・修正 → 特集・注目・速報 → 大量保有」の順・同順位は新しい順
    prio = {"材料": 0, "決算": 1, "修正": 1, "特集": 2, "注目": 2, "速報": 2}
    for it in sorted(body_cands, key=lambda x: (prio.get(x["category"], 3), -x["dt"].timestamp()))[:arts_left]:
        html, stt, _ = http_get(it["url"], "kabutan")
        if html is not None:
            it["excerpt"] = extract_body(html, it["title"])
            n_art += 1
            src = re.search(r"出所[:：]\s*([^\s<]{2,30})", html)
            if src:
                it["publisher"] = f"株探（{_clean_text(src.group(1))}）"
        else:
            st.flog("kabutan", stt, f"記事 {it['url'][-14:]}")
    st.flog("kabutan", "ok" if rows else "empty",
            f"一覧 {len(rows)} 行・窓内 {n_win} 行・本文 {n_art} 件・市場一覧等の除外 {n_drop} 件")


# ---------------------------------------------------------------------------
# 層2 会社 IR サイト
# ---------------------------------------------------------------------------
def _company_url_from_kabutan(code: str) -> tuple[str, str]:
    html, stt, _ = http_get(_KABUTAN_STOCK.format(code=code), "ir_resolve", max_bytes=400_000)
    if html is None:
        return "", stt
    m = re.search(r"会社サイト</th>\s*<td[^>]*>\s*<a[^>]+href=\"([^\"]+)\"", html)
    if not m:
        return "", "no_site"
    return m.group(1).strip(), "ok"


def _same_site(u: str, base: str) -> bool:
    a, b = urlparse(u).netloc.lower(), urlparse(base).netloc.lower()
    a, b = a[4:] if a.startswith("www.") else a, b[4:] if b.startswith("www.") else b
    return bool(a) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def resolve_news_url(company_url: str, ref: date) -> tuple[str, str, str]:
    """会社トップから新着一覧の URL を決める。戻り値 (news_url, status, detail)。"""
    html, stt, final = http_get(company_url, "ir_resolve")
    if html is None:
        return "", "fetch_error", f"トップ {stt}"
    base = final if isinstance(final, str) and final.startswith("http") else company_url
    s = _soup(html)
    cands: list[tuple[int, str]] = []
    for a in s.find_all("a", href=True):
        u = urljoin(base, a["href"].strip())
        if not u.startswith("http") or not _same_site(u, base) or u.lower().endswith(".pdf"):
            continue
        path = urlparse(u).path.lower()
        txt = _nfkc(a.get_text(" ", strip=True))
        score = 0
        if re.search(r"/(news|ir/news|irnews|release|pressrelease|press|topics|information|info)(/|$|\.)", path):
            score += 3
        elif _IR_LINK_RE.search(path):
            score += 1
        if _IR_TEXT_RE.search(txt) and len(txt) <= 20:
            score += 2
        if re.search(r"\d{4,}", path) and score < 3:
            score -= 2  # 個別記事への直リンク
        if score >= 2:
            cands.append((score, u.split("#")[0]))
    best: tuple[int, str] = (0, "")
    top_items = [x for x in parse_dated_links(html, base, ref) if x["date"] <= ref + timedelta(days=1)]
    if len(top_items) >= 3:
        best = (len(top_items), base)
    eir = eir_feed_from_page(html, base)
    tried = 0
    for _, u in sorted(dict.fromkeys(cands), key=lambda x: -x[0]):
        if tried >= IR_RESOLVE_MAX_PAGES:
            break
        if u.rstrip("/") == base.rstrip("/"):
            continue
        tried += 1
        h2, st2, f2 = http_get(u, "ir_resolve")
        if h2 is None:
            continue
        if not eir:
            eir = eir_feed_from_page(h2, f2 if isinstance(f2, str) else u)
        items = [x for x in parse_dated_links(h2, f2 if isinstance(f2, str) else u, ref)
                 if x["date"] <= ref + timedelta(days=1)]
        # 新しい日付が多い一覧を選ぶ（同点ならトップより一覧ページを優先）
        if len(items) >= 3 and len(items) + 1 > best[0]:
            best = (len(items) + 1, f2 if isinstance(f2, str) else u)
    if best[1]:
        return best[1], "ok", f"候補 {tried} 件"
    if eir:
        txt, st3, _ = http_get(eir, "ir_resolve", max_bytes=EIR_MAX_BYTES)
        if txt and len(parse_eir_items(txt, ref)) >= 3:
            return eir, "ok", f"eir-parts の IR ニュース（候補 {tried} 件）"
    return "", "unparsed", f"日付つきの一覧を検出できず（候補 {tried} 件）"


def layer_ir(st: _Stock, win: tuple[datetime, datetime], ledger: IRLedger, ref: date,
             kabutan_ok: bool = True) -> None:
    row = ledger.get(st.code)
    need = row is None or not row.get("news_url") or (
        row.get("status") == "unparsed" and (row.get("resolved_at") or "")[:10] < LEDGER_PARSER_SINCE)
    if row and not row.get("news_url"):
        try:
            age = (ref - date.fromisoformat((row.get("resolved_at") or "1970-01-01")[:10])).days
        except ValueError:
            age = 999
        if row.get("status") in ("unparsed", "no_site") and age < LEDGER_RETRY_DAYS \
                and (row.get("resolved_at") or "")[:10] >= LEDGER_PARSER_SINCE:
            st.flog("ir", row["status"], f"台帳の状態（{row.get('resolved_at')}）")
            return
    if need:
        cu = (row or {}).get("company_url") or ""
        if not cu:
            if not kabutan_ok:
                st.flog("ir", "skipped", "株探が遮断されており会社サイトの URL を解決できず")
                return
            cu, stt = _company_url_from_kabutan(st.code)
            if not cu:
                if stt == "no_site":
                    ledger.put(st.code, company_url="", news_url="", resolved_at=date.today().isoformat(), status="no_site")
                st.flog("ir", stt, "株探の銘柄ページに会社サイトなし" if stt == "no_site" else "株探 銘柄ページ")
                return
        nu, stt, det = resolve_news_url(cu, ref)
        ledger.put(st.code, company_url=cu, news_url=nu, resolved_at=date.today().isoformat(), status=stt)
        if not nu:
            st.flog("ir", stt, det)
            return
        row = ledger.get(st.code)
    news_url = row["news_url"]
    eir = is_eir_feed(news_url)
    html, stt, final = http_get(news_url, "ir", max_bytes=EIR_MAX_BYTES if eir else LIST_MAX_BYTES)
    if html is None:
        st.flog("ir", stt, f"一覧 {news_url[:80]}")
        return
    items = (parse_eir_items(html, ref) if eir
             else parse_dated_links(html, final if isinstance(final, str) else news_url, ref))
    if not items:
        st.flog("ir", "unparsed", f"一覧から日付つきの項目を抽出できず {news_url[:80]}")
        ledger.put(st.code, status="unparsed", resolved_at=date.today().isoformat())
        return
    in_win = [x for x in items if win[0].date() <= x["date"] <= win[1].date()]
    # 和文の無い表題（英語版の重複掲載）は除く（2026-10-30・3905 の 10/26 説明会資料の英語版で確認）
    in_win = [x for x in in_win if re.search(rf"[{_CJK}]", x["title"])]
    n_body = 0
    for x in in_win[:IR_BODIES_PER_STOCK * 2]:
        excerpt = ""
        if n_body < IR_BODIES_PER_STOCK:
            if x["url"].lower().split("?")[0].endswith(".pdf"):
                data, s2 = pdf_bytes(x["url"], "ir")
                excerpt = pdf_head_text(data, BODY_EXCERPT, title=x["title"]) if data else ""
            else:
                h2, s2, _ = http_get(x["url"], "ir")
                excerpt = extract_body(h2, x["title"]) if h2 else ""
            n_body += 1
            if not excerpt:
                st.flog("ir", "no_body" if s2.startswith("ok") else s2, x["url"][:80])
        st.add(_item(x.get("dt") or datetime.combine(x["date"], datetime.min.time()), bool(x.get("time_known")),
                     "ir", f"{st.name}（会社IR）", x["title"], x["url"], excerpt, found_via="ir", category="IR"))
    st.flog("ir", "ok", f"一覧 {len(items)} 件・窓内 {len(in_win)} 件・本文 {n_body} 件")


# ---------------------------------------------------------------------------
# 層4 Yahoo
# ---------------------------------------------------------------------------
_ARTICLE_CACHE: dict[str, str] = {}
_ARTICLE_LOCK = threading.Lock()


def _yahoo_article_hook(link: str, html: str) -> None:
    """move_days.fetch_article_time が取った記事ページの本文を控える（同じ記事を 2 度取らない）。"""
    try:
        ex = extract_body(html)
    except Exception:
        ex = ""
    with _ARTICLE_LOCK:
        _ARTICLE_CACHE[link] = ex


def install_article_hook() -> None:
    _md.ARTICLE_HTML_HOOK = _yahoo_article_hook


def _yahoo_link(link: str) -> str:
    return link if link.startswith("http") else f"https://finance.yahoo.co.jp{link}"


def layer_yahoo(st: _Stock, win: tuple[datetime, datetime], news: list[dict]) -> None:
    """news: [{title, media, link, dt(datetime|None), date(date)}]（呼び出し側で正規化済み）"""
    keys = _keys_for(st.name, st.code)
    n_win = n_body = n_drop = 0
    left = ARTICLES_PER_STOCK
    for n in news or []:
        dt, d = n.get("dt"), n.get("date")
        if dt is not None:
            if not (win[0] <= dt <= win[1]):
                continue
            tk = True
        elif d is not None:
            if not (win[0].date() <= d <= win[1].date()):
                continue
            dt, tk = datetime.combine(d, datetime.min.time()), False
        else:
            continue
        title = n.get("title") or ""
        if is_market_list(title) or (not _mentions_stock(title, keys)):
            n_drop += 1
            continue
        n_win += 1
        it = _item(dt, tk, "yahoo", n.get("media") or "Yahoo!ファイナンス", title, _yahoo_link(n.get("link") or ""),
                   "", found_via="yahoo", category="報道")
        link = n.get("link") or ""
        if link and not PRICE_REPORT_RE.search(_nfkc(title)):
            with _ARTICLE_LOCK:
                cached = _ARTICLE_CACHE.get(link)
            if cached is not None:
                it["excerpt"] = cached[:BODY_EXCERPT]
                n_body += 1 if cached else 0
            elif left > 0:
                left -= 1
                h, stt, _ = http_get(_yahoo_link(link), "yahoo")
                if h is not None:
                    it["excerpt"] = extract_body(h, title)
                    n_body += 1
                else:
                    st.flog("yahoo", stt, f"記事 {link[-16:]}")
        st.add(it)
    st.flog("yahoo", "ok" if news else "empty", f"見出し {len(news or [])} 件・窓内 {n_win} 件・本文 {n_body} 件・除外 {n_drop} 件")


def yahoo_news_from_list(items: list[dict]) -> list[dict]:
    """move_days.fetch_news_list の戻り値（list_date・time）を layer_yahoo の形へ。"""
    out = []
    for n in items or []:
        d = n.get("list_date")
        dt = None
        if n.get("time") and d:
            hh, mm = (int(v) for v in n["time"].split(":"))
            dt = datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm)
        out.append({"title": n.get("title"), "media": n.get("media"), "link": n.get("link"), "dt": dt, "date": d})
    return out


def yahoo_news_from_facts(news_3m: list[dict]) -> list[dict]:
    """movers_facts の news_3m（published・link）を layer_yahoo の形へ。"""
    out = []
    for n in news_3m or []:
        pub = str(n.get("published") or "")
        dt = _md.parse_dt(pub)
        if dt is None:
            continue
        tk = bool(n.get("time_known"))
        out.append({"title": n.get("title"), "media": n.get("source"), "link": n.get("link") or "",
                    "dt": dt if tk else None, "date": dt.date()})
    return out


# ---------------------------------------------------------------------------
# 分類・時刻の補完・起点
# ---------------------------------------------------------------------------
def classify(it: dict) -> str:
    if it["source"] in ("tdnet", "ir"):
        return "primary"
    if PRICE_REPORT_RE.search(_nfkc(it["title"])):
        return "price_report"
    return "primary"


def _topic(title: str) -> str:
    for lab, rx in _TOPIC_RE:
        if rx.search(title):
            return lab
    return ""


# ---------------------------------------------------------------------------
# 説明力（strength）と時刻整合（timing_fit）（2026-10-30 PM 承認・理由品質の改修 第 2 弾）
# ---------------------------------------------------------------------------
# 背景: 10/27 グロースの機械差し替えで、値動きと関係の薄い開示（593A「個人投資家向け通期決算説明会の
# お知らせ」・2160「第三者割当に係る株式譲渡報告書」・4493「【緊急開催セミナー】…」）が理由に選ばれた。
# 一次情報でも値動きを説明しない告知・事務系の開示は weak として理由に使わせない。
#
# 判定順: (1) 英語だけの表題 → weak (2) WEAK 表（表題）→ weak (3) STRONG 表（表題）→ strong
#         (4) 報道機関の独自記事（発信主体＝報道機関・「によると」型）→ strong
#         (5) 会社開示・会社 IR に限り本文冒頭 150 字を STRONG 表で判定 → strong (6) どれにも当たらない → weak（区分外）
# 語彙は下の表に足せば効く（1 行 = (区分名, 正規表現, 除外の正規表現 or "")。表題は NFKC 正規化して判定する）。
# 事務系の表題でも、同じ表題に事業・資本の本題（分割・合併・買収・TOB・市場変更）があれば weak にしない
_CORE_EVENT = r"株式分割|株式の分割|併合|合併|買収|子会社化|公開買付|TOB|ＴＯＢ|市場変更|上場廃止|経営統合"
STRENGTH_WEAK_RULES: list[tuple[str, str, str]] = [
    ("説明会・IRイベント", r"説明会|セミナー|ウェビナー|webinar|Webinar|IR ?イベント|IR ?Day|個人投資家向け|投資家向けイベント"
     r"|ライブ配信|オンデマンド配信|登壇|講演|カンファレンス|フォーラム|質疑応答|Q&A|書き起こし|ログミー", r"資料"),
    ("動画", r"動画|YouTube|ポッドキャスト|配信のお知らせ", ""),
    ("出展・協賛・メディア", r"出展|展示会|展示スポンサー|協賛|出演|掲載|取り上げ|寄稿|取材|放送|コメントが|受賞|アワード|Award"
     r"|表彰|選出されました|ランクイン", ""),
    ("調査レポート・注意喚起", r"調査レポート|レポート公開|ホワイトペーパー|コラム|ブログ|noteを|導入事例|事例公開|注意喚起|脆弱性", ""),
    ("株式事務", r"株式譲渡報告書|譲渡報告|行使状況|大量行使|行使期間満了|行使完了|取得状況|取得結果|取得終了"
     r"|自己株式.{0,6}(処分|消却)|譲渡制限付株式|ストック・?オプション|株式報酬|役員報酬|報酬制度"
     r"|剰余金の配当(?!.*(修正|増配|減配|予想|記念|特別))|基準日設定|株主名簿|単元株|投資単位"
     r"|法定事前開示|事前開示書類|事後開示|決算公告|招集通知|株主総会|議決権"
     r"|決算発表(日|予定|の予定)|発表予定日|開示予定", _CORE_EVENT),
    ("報告書類の提出", r"臨時報告書|有価証券報告書|半期報告書|四半期報告書|内部統制報告書|確認書|年次報告書|Fact ?Sheet", r"遅延|延期|不提出"),
    ("ガバナンス・定款", r"コーポレート・?ガバナンス|ガバナンス報告|独立役員|定款.{0,6}変更|統合報告書|サステナビリティ|ESG|CSR"
     r"|再発防止|コンプライアンス委員会|内部統制", _CORE_EVENT),
    ("役員異動", r"(役員|取締役|執行役員|監査役|人事).{0,8}(異動|人事|選任|退任|就任)|人事異動|組織変更|機構改革", r"代表"),
    ("再掲・英語版", r"再掲|再送|英語版|英文開示|English|英訳", ""),
    ("訂正", r"訂正", r"数値|業績|予想|配当|決算短信"),
    ("報道への見解", r"一部報道|報道について|報道に関する|報道の件", r"TOB|ＴＯＢ|公開買付|買収|経営統合|合併|提携|子会社化"),
    ("事務・お知らせ", r"メンテナンス|休業|年末年始|営業日|夏季休暇|ウェブサイト|ホームページ|サイト.{0,4}(開設|再開)|リニューアル"
     r"|移転|ロゴ|採用情報|求人|価格改定|一時停止|障害|復旧", ""),
    ("市場一覧・複数銘柄", r"動いた株|出来た株|ランキング|一覧|まとめ|関連株|銘柄群|^★|【イチオシ|上海総合|NYダウ|日経平均|TOPIX|為替",
     r"採用|組み入れ|組入|除外|入れ替え"),
]
STRENGTH_STRONG_RULES: list[tuple[str, str]] = [
    ("決算", r"決算|四半期|中間期|上期|下期|業績|月次|売上高|営業利益|営業損益|経常|最終利益|最終損益|純利益|増益|減益|黒字|赤字|最高益"),
    ("業績修正", r"修正|上方|下方|上振れ|下振れ|特別利益|特別損失|特損|減損|引当金|営業外"),
    ("配当修正・株主還元", r"増配|減配|無配|復配|記念配当|特別配当|配当予想|配当.{0,4}修正|株主還元|配当方針"
     r"|株主優待.{0,6}(新設|導入|変更|拡充|廃止)"),
    ("受注・契約・採用・提携", r"受注|契約|採用|提携|協業|共創|合意|覚書|MOU|共同開発|共同研究|連携|協定|パートナー|納入|納品|選定"
     r"|搭載|導入決定|導入が決定|獲得|落札"),
    ("新製品・サービス開始", r"新製品|新サービス|発売|販売開始|提供開始|サービス開始|を開始|開始へ|稼働開始|ローンチ|出荷|量産|リリース決定"
     r"|配信開始|サービスイン|を提供|提供へ|無償提供|開発に成功|開発完了|特許|承認取得|承認を取得|製造販売承認|治験|臨床試験|FDA|PMDA|薬事"
     r"|アップデート|新機能|機能追加|機能を追加"),
    ("大量保有", r"大量保有|変更報告書|保有割合|主要株主|筆頭株主|支配株主の異動|親会社の異動"),
    ("自社株買い", r"自己株式.{0,4}取得|自社株買い|自己株買い|自己株式.{0,6}公開買付"),
    ("株式分割", r"株式分割|株式の分割|株式併合|株式の併合"),
    ("上場市場変更・指数", r"市場変更|市場区分|上場市場|指定替え|プライム市場への|スタンダード市場への|上場承認|新規上場"
     r"|指数.{0,6}(採用|組み入れ|組入|除外)|TOPIX.{0,6}(採用|組入|除外)|日経平均.{0,8}(採用|入れ替え|除外)|MSCI"),
    ("M&A・子会社化", r"買収|子会社化|子会社の異動|株式取得|株式の取得|TOB|ＴＯＢ|公開買付|合併|経営統合|事業譲渡|事業譲受|事業承継"
     r"|吸収分割|会社分割|株式交換|株式移転|完全子会社|非公開化|MBO|スクイーズ|持分法|持分譲渡|資本参加|出資"),
    ("資本政策（希薄化）", r"第三者割当|公募|売出し|募集株式|新株式発行|新株予約権.{0,12}発行|行使価額修正|MSワラント|増資|資金調達"
     r"|転換社債|種類株"),
    ("代表交代", r"代表取締役.{0,10}(異動|交代|辞任|就任|退任|選定)|社長交代|社長.{0,4}(就任|辞任|退任)|CEO.{0,4}(就任|交代)"),
    ("悪材料", r"監理銘柄|整理銘柄|上場廃止|特別注意|特設注意|改善報告|改善計画|業務停止|行政処分|業務改善命令|課徴金|勧告|不正|不適切"
     r"|不祥事|調査委員会|第三者委員会|延期|遅延|提出期限|訴訟|提訴|判決|和解|賠償|損害|リコール|事故|火災|爆発|サイバー攻撃"
     r"|ランサム|情報漏えい|情報漏洩|漏えい|破産|民事再生|会社更生|債務超過|継続企業|疑義|取引停止|売買停止|資金繰り|債務不履行"),
]
# 報道機関の独自記事（「〜によると」型）。「〜と報じた」型の二次報道は含めない。
MEDIA_PUBLISHER_RE = re.compile(r"日本経済新聞|日経|NHK|ブルームバーグ|Bloomberg|ロイター|Reuters|共同通信|時事通信|読売|朝日新聞"
                                r"|毎日新聞|産経|日刊工業|テレビ東京|TBS|化学工業日報|電波新聞")
EXCLUSIVE_RE = re.compile(r"によると|関係者|分かった|わかった|判明|方針を固め|明らかになった|検討している|調整している|最終調整|固めた")
SECONDARY_REPORT_RE = re.compile(r"と報じ|報道を受け|と伝わ|報道に関する|一部報道")
LATE_SESSION_MIN = 15 * 60        # 15:00〜15:30（大引け前の 30 分）の公表は翌営業日の動きにも整合とみなす
_GENERIC_TITLE_RE = re.compile(r"^(お知らせ|ニュース|ニュースリリース|プレスリリース|リリース|IRニュース|IR情報|ＩＲ情報|適時開示"
                               r"|新着情報|最新情報|TOPICS|Topics|トピックス|NEWS|News)?$")

_WEAK_RX = [(lab, re.compile(p), re.compile(u) if u else None) for lab, p, u in STRENGTH_WEAK_RULES]
_STRONG_RX = [(lab, re.compile(p)) for lab, p in STRENGTH_STRONG_RULES]


def _is_english_title(t: str) -> bool:
    cjk = len(re.findall(r"[぀-ヿ一-鿿]", t))
    latin = len(re.findall(r"[A-Za-z]", t))
    return cjk == 0 and latin >= 10


def classify_strength(title: str, excerpt: str = "", publisher: str = "", source: str = "") -> tuple[str, str]:
    """材料の説明力を (strong|weak, 区分名) で返す（表題・本文冒頭の語彙で判定）。"""
    t = _nfkc(title)
    if _is_english_title(t):
        return "weak", "英語版"
    for lab, rx, unless in _WEAK_RX:
        if rx.search(t) and not (unless and unless.search(t)):
            return "weak", lab
    for lab, rx in _STRONG_RX:
        if rx.search(t):
            return "strong", lab
    head = _nfkc(excerpt)[:200]
    if MEDIA_PUBLISHER_RE.search(_nfkc(publisher)) and EXCLUSIVE_RE.search(t + head) \
            and not SECONDARY_REPORT_RE.search(t + head):
        return "strong", "独自報道"
    # 本文冒頭は、表題が中身を表さない会社開示・会社 IR（「お知らせ」「プレスリリース」だけ等）に限って見る。
    # 表題のある開示の本文冒頭は定型の書き出し（代表者名・報道の引用）で誤判定するため使わない（2026-10-30 実測）
    if source in ("tdnet", "ir") and head and _GENERIC_TITLE_RE.match(re.sub(r"[\s　【】\[\]「」『』:：|｜]", "", t)):
        h = re.sub(r"[（(][^（）()]{0,80}[）)]", "", head)[:150]
        if not any(rx.search(h) and not (unless and unless.search(h)) for _, rx, unless in _WEAK_RX):
            for lab, rx in _STRONG_RX:
                if rx.search(h):
                    return "strong", lab + "（本文冒頭）"
    return "weak", "区分外"


def reaction_days(dt_str: str, time_known: bool) -> set[date]:
    """材料が値動きに表れる営業日（日単位）。

    時刻あり: 前営業日 15:30〜当日 15:30 の公表 → 当日（寄り前の公表は当日の寄りから・場中は同日の公表以降）。
              15:00〜15:30（大引け前 30 分）の公表は翌営業日も含める。15:30 以降は翌営業日（assign_bday）。
    日付だけ: その日と翌営業日（引け後の公表を含めうるため）。
    """
    try:
        x = datetime.fromisoformat(str(dt_str)[:16])
    except ValueError:
        return set()
    if not time_known or len(str(dt_str)) <= 10:
        d0 = assign_bday(datetime.combine(x.date(), datetime.min.time()), False)
        return {d0, bday_shift(d0, 1)}
    d0 = assign_bday(x, True)
    out = {d0}
    mins = x.hour * 60 + x.minute
    if x.date() == d0 and LATE_SESSION_MIN <= mins < CLOSE_MIN:
        out.add(bday_shift(d0, 1))
    return out


def streak_dates(origin: date, base: date) -> set[date]:
    out, d = set(), origin
    for _ in range(80):
        if d > base:
            break
        if _is_bday(d):
            out.add(d)
        d += timedelta(days=1)
    return out or {base}


def annotate_strength(mats: list[dict], origin: date | str, base: date | str) -> list[dict]:
    """primary 材料に strength・strength_label・timing_fit を付ける（その場で更新し、同じリストを返す）。

    timing_fit = 材料の反映日（reaction_days）が今回の動き（起点日〜基準日の営業日）に入る。
    """
    o = origin if isinstance(origin, date) else date.fromisoformat(str(origin)[:10])
    b = base if isinstance(base, date) else date.fromisoformat(str(base)[:10])
    days = streak_dates(o, b)
    for m in mats:
        s, lab = classify_strength(m.get("title") or "", m.get("excerpt") or "", m.get("publisher") or "",
                                   m.get("source") or "")
        m["strength"], m["strength_label"] = s, lab
        m["timing_fit"] = bool(reaction_days(m.get("dt") or "", bool(m.get("time_known"))) & days)
    return mats



def _bigram_ratio(a: str, b: str) -> float:
    ca = re.sub(r"[^0-9A-Za-z぀-ヿ一-鿿]", "", _nfkc(a))
    cb = re.sub(r"[^0-9A-Za-z぀-ヿ一-鿿]", "", _nfkc(b))
    ca = re.sub(r"(に関するお知らせ|についてのお知らせ|のお知らせ|お知らせ)$", "", ca)
    if len(ca) < 4:
        return 0.0
    g = {ca[i:i + 2] for i in range(len(ca) - 1)}
    return sum(1 for x in g if x in cb) / len(g)


def infer_ir_times(items: list[dict]) -> None:
    """日付だけの IR 項目に、同日の材料記事の本文にある「午後0時15分ごろ…発表」の時刻を付ける。"""
    arts = [x for x in items if x["source"] in ("kabutan", "yahoo") and x["excerpt"]]
    for it in items:
        if it["source"] != "ir" or it["time_known"]:
            continue
        for a in arts:
            if a["dt"].date() != it["dt"].date():
                continue
            if _bigram_ratio(it["title"], a["title"] + a["excerpt"]) < 0.5:
                continue
            ex = _nfkc(a["excerpt"])
            m = _TIME_PHRASE.search(ex)
            if m:
                h = int(m.group(2)) + (12 if m.group(1) == "午後" and int(m.group(2)) < 12 else 0)
                mi = 30 if (m.group(3) is None and "半" in m.group(0)) else int(m.group(3) or 0)
                cand = it["dt"].replace(hour=h, minute=mi)
                if cand <= a["dt"]:
                    it["dt"], it["time_known"], it["time_approx"] = cand, True, True
                    it["time_from"] = a["publisher"]
                    break


def find_origin(rows: list[dict], base: date) -> tuple[date, int, int]:
    """日別騰落の行から今回の上昇／下落の起点日を返す。戻り値 (起点日, 連続日数 N, 基準日が何日目 M)。

    rows: [{date, ret_pct, sigma2, sigma2_threshold_pct}]（week_days と month3_days を合わせたもの）。
    「大きく動いた」= 2σ 超（閾値が無い日は前日比 ±5% 以上）。基準日と同じ向きで、営業日として連続する日を遡る。
    基準日が大きく動いていなければ基準日を起点とする。
    """
    by = {}
    for r in rows or []:
        try:
            by[date.fromisoformat(str(r["date"])[:10])] = r
        except (KeyError, ValueError):
            continue

    def big(r: dict, sign: int) -> bool:
        v = r.get("ret_pct")
        if not isinstance(v, (int, float)) or v == 0 or (1 if v > 0 else -1) != sign:
            return False
        if r.get("sigma2_threshold_pct") is not None:
            return bool(r.get("sigma2"))
        return abs(v) >= 5.0

    r0 = by.get(base)
    if r0 is None or not isinstance(r0.get("ret_pct"), (int, float)) or r0["ret_pct"] == 0:
        return base, 1, 1
    sign = 1 if r0["ret_pct"] > 0 else -1
    if not big(r0, sign):
        return base, 1, 1
    origin, n = base, 1
    d = base
    for _ in range(60):
        p = bday_shift(d, -1)
        r = by.get(p)
        if r is None or not big(r, sign):
            break
        origin, d, n = p, p, n + 1
    return origin, n, n


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------
class MaterialsCollector:
    def __init__(self, base: date, *, names: dict[str, str] | None = None, ledger_path: Path | None = None,
                 state_writes: bool = True, max_workers: int = 6, asof: datetime | None = None,
                 log=print) -> None:
        self.base = base
        self.names = names or {}
        self.ledger = IRLedger(ledger_path, writable=state_writes)
        self.max_workers = max_workers
        self.log = log
        end = asof if asof is not None else datetime.combine(base, datetime.min.time()).replace(hour=15, minute=29)
        self.win_end = min(end, datetime.combine(base, datetime.min.time()).replace(hour=23, minute=59))
        start_d = bday_shift(base, -WINDOW_BDAYS)
        # 窓の始まりは「5 営業日前の前営業日 15:30」（その日に割り当たる材料を含める）
        self.win_start = datetime.combine(bday_shift(start_d, -1), datetime.min.time()).replace(hour=15, minute=30)
        self.stocks: dict[str, _Stock] = {}
        self._thread: threading.Thread | None = None
        self.timing: dict[str, float] = {}
        self.kabutan_blocked = False
        install_article_hook()

    def _st(self, code: str) -> _Stock:
        if code not in self.stocks:
            self.stocks[code] = _Stock(code, self.names.get(code, code))
        return self.stocks[code]

    # 層1〜3（窓全体）
    def _prefetch_one(self, code: str, entries: list[dict]) -> None:
        st = self._st(code)
        win = (self.win_start, self.win_end)
        for fn, args in ((layer_tdnet, (st, entries, win)),
                         (layer_kabutan, (st, win)),
                         (layer_ir, (st, win, self.ledger, self.base, not self.kabutan_blocked))):
            try:
                fn(*args)
            except Exception as e:  # 1 層の失敗で他層を止めない
                st.flog(fn.__name__.replace("layer_", ""), "error", f"{type(e).__name__} {e}")
        st.done_ranges.append((self.win_start.date(), self.win_end.date()))
        ks = HOST_STATS.get("kabutan", {})
        if ks.get("http_403", 0) + ks.get("http_405", 0) >= 5 and ks.get("ok", 0) == 0:
            self.kabutan_blocked = True

    def prefetch(self, codes: list[str], tdnet_entries: dict[str, list[dict]]) -> None:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            list(ex.map(lambda c: self._prefetch_one(c, tdnet_entries.get(c) or []), codes))
        self.timing["prefetch_sec"] = round(time.time() - t0, 1)

    def start_prefetch(self, codes: list[str], tdnet_entries: dict[str, list[dict]]) -> None:
        self._thread = threading.Thread(target=self._safe_prefetch, args=(codes, tdnet_entries), daemon=True)
        self._thread.start()

    def _safe_prefetch(self, codes, tdnet_entries) -> None:
        try:
            self.prefetch(codes, tdnet_entries)
        except Exception as e:  # pragma: no cover
            self.log(f"  [WARN] 一次材料の先行取得: {type(e).__name__} {e}")

    def wait(self) -> None:
        if self._thread is not None:
            t0 = time.time()
            self._thread.join()
            self.timing["wait_after_main_sec"] = round(time.time() - t0, 1)
            self._thread = None

    def _extend(self, code: str, origin: date, entries: list[dict], news: list[dict]) -> None:
        """層5: 起点日が窓より前なら、起点日の前後 1 営業日について層1〜4 を取る。"""
        st = self._st(code)
        s = datetime.combine(bday_shift(origin, -2), datetime.min.time()).replace(hour=15, minute=30)
        e = min(self.win_start, datetime.combine(bday_shift(origin, 1), datetime.min.time()).replace(hour=15, minute=29))
        if e <= s:
            return
        win = (s, e)
        for fn, args in ((layer_tdnet, (st, entries, win)),
                         (layer_kabutan, (st, win, 6)),
                         (layer_ir, (st, win, self.ledger, self.base, not self.kabutan_blocked)),
                         (layer_yahoo, (st, win, news))):
            try:
                fn(*args)
            except Exception as ex:
                st.flog("origin_" + fn.__name__.replace("layer_", ""), "error", f"{type(ex).__name__} {ex}")
        st.flog("origin", "extended", f"起点日 {origin} の前後を追加取得（{s:%m/%d %H:%M}〜{e:%m/%d %H:%M}）")

    def finalize(self, codes: list[str], day_rows: dict[str, list[dict]], yahoo_news: dict[str, list[dict]],
                 tdnet_entries: dict[str, list[dict]] | None = None) -> dict[str, dict]:
        """起点日判定・遡り・層4・分類を行い、{code: {materials, price_reports, origin_date, ...}} を返す。"""
        self.wait()
        t0 = time.time()
        tdnet_entries = tdnet_entries or {}
        origins: dict[str, tuple[date, int, int]] = {}
        for c in codes:
            origins[c] = find_origin(day_rows.get(c) or [], self.base)

        def _one(c: str) -> None:
            st = self._st(c)
            news = yahoo_news.get(c) or []
            try:
                layer_yahoo(st, (self.win_start, self.win_end), news)
            except Exception as e:
                st.flog("yahoo", "error", f"{type(e).__name__} {e}")
            o = origins[c][0]
            if o < bday_shift(self.base, -WINDOW_BDAYS):   # 起点日が窓（5 営業日）より前
                self._extend(c, o, tdnet_entries.get(c) or [], news)

        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            list(ex.map(_one, codes))
        out = {c: self._build(c, *origins[c]) for c in codes}
        self.timing["finalize_sec"] = round(time.time() - t0, 1)
        try:
            self.ledger.save()
        except Exception as e:
            self.log(f"  [WARN] IR 台帳の保存: {type(e).__name__} {e}")
        return out

    def _build(self, code: str, origin: date, n_streak: int, m_pos: int) -> dict:
        st = self._st(code)
        items = st.items
        infer_ir_times(items)
        # 重複（同じ表題・同じ日付）を除く。優先: tdnet > ir > kabutan > yahoo
        rank = {"tdnet": 0, "ir": 1, "kabutan": 2, "yahoo": 3}
        uniq: dict[tuple, dict] = {}
        for it in sorted(items, key=lambda x: (rank.get(x["source"], 9), -len(x["excerpt"]))):
            k = (re.sub(r"\s+", "", _nfkc(it["title"]))[:40], it["dt"].date())
            if k not in uniq:
                uniq[k] = it
        mats, prs = [], []
        for it in sorted(uniq.values(), key=lambda x: x["dt"], reverse=True):
            ad = assign_bday(it["dt"], it["time_known"])
            if ad > self.base:
                continue
            kind = classify(it)
            rec = {
                "dt": it["dt"].strftime("%Y-%m-%dT%H:%M") if it["time_known"] else it["dt"].strftime("%Y-%m-%d"),
                "time_known": it["time_known"], "assigned_date": ad.isoformat(), "kind": kind,
                "source": it["source"], "publisher": it["publisher"], "title": it["title"],
                "excerpt": it["excerpt"], "url": it["url"], "found_via": it["found_via"],
                "is_origin": bool(kind == "primary" and ad == origin),
                "topic": _topic(it["title"]),
            }
            if it.get("time_approx"):
                rec["time_approx"] = True
                rec["time_from"] = it.get("time_from", "")
            (mats if kind == "primary" else prs).append(rec)
        # 説明力（strong / weak）と時刻整合（2026-10-30）。weak は理由に使えない（raw では [weak] 印で参考扱い）
        annotate_strength(mats, origin, self.base)
        if len(mats) > MAX_PRIMARY_LISTED:
            # 残す順: 起点 → strong かつ時刻整合 → strong → weak（同順位は新しい順）。表示は元の新しい順のまま
            def _keep_key(m: dict) -> tuple:
                return (0 if m["is_origin"] else 1, 0 if (m["strength"] == "strong" and m["timing_fit"]) else 1,
                        0 if m["strength"] == "strong" else 1, -datetime.fromisoformat(m["dt"][:16]).timestamp())
            keep_ids = {id(m) for m in sorted(mats, key=_keep_key)[:MAX_PRIMARY_LISTED]}
            mats = [m for m in mats if id(m) in keep_ids]
        return {
            "origin_date": origin.isoformat(), "streak_days": n_streak, "streak_pos": m_pos,
            "window": [self.win_start.strftime("%Y-%m-%dT%H:%M"), self.win_end.strftime("%Y-%m-%dT%H:%M")],
            "materials": mats, "price_reports": prs[:12], "fetch_log": st.log,
        }


# ---------------------------------------------------------------------------
# raw の「材料」欄
# ---------------------------------------------------------------------------
def _fmt_dt(m: dict) -> str:
    d = m["dt"]
    try:
        x = datetime.fromisoformat(d)
    except ValueError:
        return d
    s = f"{x.month}/{x.day}"
    if m.get("time_known"):
        s += f" {x.hour}:{x.minute:02d}" + ("頃" if m.get("time_approx") else "")
    return s


def render_materials(res: dict | None) -> list[str]:
    """raw の銘柄ブロックへ差し込む「材料」欄（1 行 1 項目・数値の市場データは入れない）。

    書式はルール側（prompts/mover-report.md・prompts/mover-weekly.md の「材料欄」）と一致させる:
    「材料（primary）」見出し → 起点日の行 → 1 行 1 項目「[印]日時｜発信主体｜表題｜本文冒頭」→
    「参考（理由に使用不可）」小見出しの下に値動きを報じた見出し（price_report）。
    印（2026-10-30）: [strong]=値動きを説明しうる材料（理由に使える）・[weak]=告知・事務系（理由に使えない）・
    [参考]=値動きを報じた見出し（理由に使えない）。【起点】=起点日に割り当たる・【時刻整合】=発信時刻が今回の動きと整合。
    並びは [strong] → [weak]（各々新しい順）。
    """
    if not res:
        return ["**材料（primary・一次情報）:** 取得処理を実行できず", ""]
    lines = ["**材料（primary・一次情報＝会社開示／会社IR／株探／Yahoo の本文冒頭つき・[印]日時｜発信主体｜表題｜本文冒頭。"
             "理由に使えるのは [strong] だけ・[weak] は告知／事務系で理由に使えない）:**", ""]
    o = res.get("origin_date")
    if o:
        od = date.fromisoformat(o)
        sd, sp = res.get("streak_days"), res.get("streak_pos")
        tail = (f"（今回の動きは {sd} 日連続・基準日は {sp or sd} 日目）" if isinstance(sd, int) and sd >= 2
                else "（今回の動きは基準日に始まった）")
        lines.append(f"- 起点日: {od.month}/{od.day}{tail}")
    mats = res.get("materials") or []
    if not mats:
        bad = "・".join(f"{x['layer']} {x['status']}" for x in res.get("fetch_log") or []
                        if x.get("status") not in ("ok",))[:200]
        lines.append("- primary 0 件（確認範囲: TDnet／会社 IR／株探／Yahoo" + (f"・取得状態 {bad}" if bad else "") + "）")
    elif not any(m.get("strength", "strong") == "strong" for m in mats):
        lines.append("- [strong] 0 件（理由に使える材料なし。理由は検査側が定型文を入れる）")
    for m in sorted(mats, key=lambda x: 0 if x.get("strength", "strong") == "strong" else 1):
        mark = "[strong]" if m.get("strength", "strong") == "strong" else "[weak]"
        tag = "【起点】" if m.get("is_origin") else ("【時刻整合】" if m.get("timing_fit") else "")
        ex = (m.get("excerpt") or "").replace("|", "／").replace("｜", "／")
        lines.append(f"- {mark}{tag}{_fmt_dt(m)}｜{m['publisher']}｜{m['title']}｜{ex[:200] if ex else '本文取得できず'}")
    lines.append("")
    prs = res.get("price_reports") or []
    if prs:
        lines += ["**参考（理由に使用不可）:** 値動きを報じた見出し", ""]
        for m in prs[:8]:
            lines.append(f"- [参考]{_fmt_dt(m)}｜{m['publisher']}｜{m['title']}")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 単体実行（1 銘柄の動作確認）
# ---------------------------------------------------------------------------
def _standalone(code: str, base: date, name: str = "", json_out: str = "") -> dict:
    """単体実行: TDnet 一覧（yanoshin）・Yahoo 一覧を自前で取り、日別騰落は J-Quants の日足から作る。"""
    import requests
    from xml.etree import ElementTree as ET
    entries = []
    try:
        r = requests.get(f"https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.atom", timeout=15)
        root = ET.fromstring(r.content)
        ns = {"a": "http://purl.org/atom/ns#"}
        for e in root.findall("a:entry", ns):
            def _t(tag):
                el = e.find(tag, ns)
                return el.text.strip() if el is not None and el.text else ""
            link = e.find("a:link[@rel='alternate']", ns)
            href = link.get("href", "") if link is not None else ""
            raw_title = _t("a:title")
            if ":" in raw_title and not name:
                name = raw_title.split(":", 1)[0].strip()
            entries.append({"title": raw_title.split(":", 1)[1].strip() if ":" in raw_title else raw_title,
                            "published": _t("a:issued") or _t("a:created"),
                            "pdf_url": href.split("rd.php?", 1)[1] if "rd.php?" in href else href})
    except Exception as e:
        print(f"[WARN] TDnet 一覧: {e}")
    name = clean_name(name) or code
    col = MaterialsCollector(base, names={code: name}, ledger_path=None, state_writes=False)
    col.prefetch([code], {code: entries})
    ws = bday_shift(base, -(WINDOW_BDAYS + 2))
    nl, err = _md.fetch_news_list(code, base, stop_before=ws)
    rows = daily_rows_jquants([code], base).get(code, [])
    res = col.finalize([code], {code: rows}, {code: yahoo_news_from_list(nl)}, {code: entries})[code]
    if json_out:
        Path(json_out).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return res


def daily_rows_jquants(codes: list[str], base: date, lookback_days: int = 130) -> dict[str, list[dict]]:
    """J-Quants の日足から起点判定用の行（ret_pct・2σ）を作る（単体実行・実測用）。"""
    import pandas as pd
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not key:
        return {}
    import jquantsapi
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from jq_client_utils import fetch_paginated_v2
    cli = jquantsapi.ClientV2(api_key=key)
    out: dict[str, list[dict]] = {}
    for c in codes:
        try:
            rows = fetch_paginated_v2(cli, "/equities/bars/daily",
                                      params={"code": c, "from": (base - timedelta(days=lookback_days)).isoformat(),
                                              "to": base.isoformat()})
        except Exception as e:
            print(f"[WARN] J-Quants {c}: {e}")
            continue
        df = pd.DataFrame(rows or [])
        if df.empty:
            continue
        ccol = "AdjC" if "AdjC" in df.columns else ("C" if "C" in df.columns else None)
        if ccol is None:
            continue
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
        df = df.sort_values("Date")
        ret = df[ccol].astype(float).pct_change() * 100.0
        thr = ret.rolling(60).std().shift(1) * 2.0
        out[c] = [{"date": d.isoformat(), "ret_pct": (None if r != r else round(float(r), 2)),
                   "sigma2_threshold_pct": (None if t != t else round(float(t), 2)),
                   "sigma2": bool(t == t and r == r and abs(r) > t)}
                  for d, r, t in zip(df["Date"], ret, thr)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="動意の一次材料（5 層）を 1 銘柄分取得して表示する")
    ap.add_argument("--code", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()
    t0 = time.time()
    res = _standalone(a.code, date.fromisoformat(a.date), a.name, a.json_out)
    print("\n".join(render_materials(res)))
    print(f"起点日 {res['origin_date']}・連続 {res['streak_days']} 日・{time.time() - t0:.1f} 秒")
    for x in res["fetch_log"]:
        print(f"  [{x['layer']}] {x['status']} {x['detail']}")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
