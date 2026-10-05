"""
動意レポート用「日別騰落表」の共通部品（2026-10-04 PM 承認・計画書 A-1〜A-3）
=========================================================================

各掲載銘柄に次の 2 表を付けるための事実（facts）を組み立て、markdown の表へ文字列化する。

  表 1: 当週 5 営業日（日次モードは直近 5 営業日）の日別表
  表 2: 直近 3 か月（暦の 3 か月。祝日で営業日が 63 より減ってもよい）のうち
        ★（2σ 超）の日・TDNet 開示があった日・決算発表日の表

列（両表共通）: 日付（曜）／終値／騰落率／出来高（20 日平均比）／印／
               その日の開示・ニュース（前営業日 15:30〜当日 15:30）／関連銘柄の同日騰落

判定の定義:
  - 2σ（★）: 当日騰落率の絶対値 ＞ 前 60 営業日の日次騰落率の標準偏差 × 2
    （deep_dive.py build_highlight_3m の `rolling(60).std().shift(1) * 2.0` と同じ・当日を含まない）
  - 出来高比: 当日出来高 ÷ 前 20 営業日平均（当日を含まない）
  - 材料の日割当: 15:30 以降の公表は翌営業日（deep_dive.py HL3M_TSE_CLOSE_MIN と同じ境界）
  - 場中開示（09:00 超〜15:30 未満・同日割当）は 5 分足で「開示前の高値（下落日は安値）と時刻・
    開示後の値幅・開示後に更新したか」を付ける。5 分足が取れない日（yfinance の 60 日より前）は
    日足の高安だけを書き「場中の時刻は取得できず」と印を付ける
  - 「影響軽微」: 該当開示の PDF 全文（2,000 字で切らない）に「影響…軽微」の記載がある

株価は price_history parquet（調整後 OHLC。出来高は調整係数で割り戻す）を使う。
本モジュールは make_mover_report.py から呼ぶ。deep_dive.py は本改修では変更しない。
"""

from __future__ import annotations

import bisect
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

SIGMA_WIN = 60
VOL_WIN = 20
CLOSE_MIN = 15 * 60 + 30      # 東証の大引け 15:30（以降の公表は翌営業日の材料）
OPEN_MIN = 9 * 60             # 寄り付き 09:00
MONTHS = 3
NEWS_PER_DAY = 3
M3_MAX_ROWS = 15              # 表 2 の行数上限（★・最大・決算日は必ず残す）
DISC_PER_ROW = 4              # 1 セルに並べる開示の上限（同時刻・同種はまとめる）
DISC_LIST_MAX = 15            # 開示一覧の行数上限
NEWS_LIST_MAX = 12            # 報道一覧の行数上限
PDF_PER_STOCK = 6             # 「軽微」検査で PDF を取る上限（1 銘柄あたり）
NEWS_TIME_PER_STOCK = 12      # 記事ページから配信時刻を取る上限（1 銘柄あたり）
NEWS_MAX_PAGES = 6            # ニュース一覧を遡る最大ページ数（3 か月の起点より古い記事が出たら打ち切り）
REQUEST_SLEEP = 0.5
WD = "月火水木金土日"

_HL_NOISE = re.compile(r"ランクイン|ランキング|値上がり率|値下がり率|S高|ストップ高|＝\s*\d+\s*銘柄|特集")
_MINOR_RE = re.compile(r"影響[^。]{0,30}軽微")
# 「軽微」の検査対象から外す開示（本文が長く注記に「軽微」が頻出するもの・訂正）
_MINOR_SKIP = re.compile(r"決算短信|決算説明|決算補足|有価証券報告書|四半期報告書|半期報告書|訂正")
_EARNINGS_RE = re.compile(r"決算短信")
_EARN_EXCL = re.compile(r"訂正")

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------------------
# 株価（price_history parquet）
# ---------------------------------------------------------------------------

def load_price_history(ph_dir: Path, codes: list[str], target: date,
                       lookback_days: int = 270) -> pd.DataFrame:
    """price_history/{YYYY}.parquet から codes の日足を読む（target 以前・lookback_days 暦日分）。

    3 か月表の 2σ 判定に 63 + 61 営業日が要るため既定 270 暦日。年をまたぐ場合は前年ファイルも読む。
    Returns: Code / Date(date) / open / high / low / close（調整後）/ volume（調整後）/ raw_close / raw_volume
    """
    codes = sorted({str(c)[:4] for c in codes if c})
    if not codes:
        return pd.DataFrame()
    start = target - timedelta(days=lookback_days)
    frames = []
    cols = ["Date", "Code", "Open", "High", "Low", "Close", "Volume",
            "AdjustmentOpen", "AdjustmentHigh", "AdjustmentLow", "AdjustmentClose"]
    for y in range(start.year, target.year + 1):
        p = Path(ph_dir) / f"{y}.parquet"
        if not p.exists():
            continue
        try:
            df = pd.read_parquet(p, columns=cols, filters=[("Code", "in", codes)])
        except Exception:
            df = pd.read_parquet(p, columns=cols)
            df = df[df["Code"].astype(str).str[:4].isin(codes)]
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["Code"] = df["Code"].astype(str).str[:4]
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    df = df[(df["Date"] >= start) & (df["Date"] <= target)]
    ratio = (df["AdjustmentClose"] / df["Close"]).where(df["Close"] > 0)
    out = pd.DataFrame({
        "Code": df["Code"], "Date": df["Date"],
        "open": df["AdjustmentOpen"].fillna(df["Open"]),
        "high": df["AdjustmentHigh"].fillna(df["High"]),
        "low": df["AdjustmentLow"].fillna(df["Low"]),
        "close": df["AdjustmentClose"].fillna(df["Close"]),
        "volume": (df["Volume"] / ratio).where(ratio > 0, df["Volume"]),
        "raw_close": df["Close"], "raw_volume": df["Volume"],
    })
    return (out.dropna(subset=["close"]).drop_duplicates(["Code", "Date"], keep="last")
            .sort_values(["Code", "Date"]).reset_index(drop=True))


def compute_metrics(px_one: pd.DataFrame) -> pd.DataFrame:
    """1 銘柄の日足から 騰落率・2σ 閾値・★・出来高 20 日比 を計算する（index = date）。"""
    d = px_one.sort_values("Date").set_index("Date")
    c = d["close"].astype(float)
    ret = c.pct_change() * 100.0
    thr = ret.rolling(SIGMA_WIN).std().shift(1) * 2.0           # 当日を含まない過去 60 営業日
    vavg = d["volume"].astype(float).rolling(VOL_WIN).mean().shift(1)  # 当日を含まない前 20 営業日
    m = pd.DataFrame({
        "close": c, "ret": ret, "thr": thr,
        "volume": d["volume"].astype(float), "raw_volume": d["raw_volume"].astype(float),
        "vol_ratio": d["volume"].astype(float) / vavg,
        "high": d["high"].astype(float), "low": d["low"].astype(float),
    })
    m["sig"] = (m["ret"].abs() > m["thr"]) & m["thr"].notna() & m["ret"].notna()
    return m


def expected_trading_days(start: date, end: date) -> list[date]:
    """東証の営業日（平日・祝日除外・12/31〜1/3 除外）。jpholiday が無ければ平日のみ。"""
    try:
        import jpholiday
        is_h = jpholiday.is_holiday
    except Exception:
        is_h = lambda d: False
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5 and not is_h(d) and not ((d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3)):
            out.append(d)
        d += timedelta(days=1)
    return out


def fill_gaps(px: pd.DataFrame, codes: list[str], target: date, fetch_day, log=print,
              lookback_days: int = 270, recent_days: int = 10) -> tuple[pd.DataFrame, list[str]]:
    """price_history に無い営業日（欠落日・対象日の未着）を fetch_day(date) で補う。

    fetch_day は J-Quants /equities/bars/daily の行（Code・O/H/L/C/Vo・AdjO/AdjH/AdjL/AdjC/AdjVo）
    のリストを返す callable。行が返らない日（休場）は補わない。Returns: (px, 補った日の一覧)
    """
    codes = {str(c)[:4] for c in codes}
    have = set(px["Date"].unique()) if not px.empty else set()
    want = expected_trading_days(target - timedelta(days=lookback_days), target)
    missing = [d for d in want if d not in have]
    # 直近 recent_days 営業日に行が無い銘柄（上場直後で price_history 未収録・対象日に売買なし等）は、
    # その日を J-Quants で取り直して銘柄単位で補う（売買の無い日は C が空で返り、補わない）。
    have_pairs = set(zip(px["Code"], px["Date"])) if not px.empty else set()
    for d in want[-recent_days:]:
        if d not in missing and any((c, d) not in have_pairs for c in codes):
            missing.append(d)
    missing = sorted(set(missing))
    filled: list[str] = []
    rows = []
    for d in missing:
        try:
            got = fetch_day(d) or []
        except Exception as e:
            log(f"  [WARN] 株価の欠落日補完 {d}: {type(e).__name__} {e}")
            continue
        n = 0
        for r in got:
            c = str(r.get("Code", r.get("code", "")))[:4]
            if c not in codes or (c, d) in have_pairs:
                continue
            g = lambda *ks: next((r.get(k) for k in ks if r.get(k) is not None), None)
            close, adjc = g("C", "Close"), g("AdjC", "C", "Close")
            if close is None or adjc is None:
                continue
            ratio = (float(adjc) / float(close)) if close else 1.0
            vol = g("Vo", "Volume")
            rows.append({"Code": c, "Date": d,
                         "open": g("AdjO", "O"), "high": g("AdjH", "H"), "low": g("AdjL", "L"),
                         "close": float(adjc),
                         "volume": g("AdjVo") if g("AdjVo") is not None else (float(vol) / ratio if vol is not None and ratio else vol),
                         "raw_close": float(close), "raw_volume": vol})
            n += 1
        if got:
            filled.append(f"{d}（{n}銘柄）")
    if rows:
        add = pd.DataFrame(rows)
        for col in ("open", "high", "low", "close", "volume", "raw_close", "raw_volume"):
            add[col] = pd.to_numeric(add[col], errors="coerce")
        px = (pd.concat([px, add], ignore_index=True).drop_duplicates(["Code", "Date"], keep="first")
              .sort_values(["Code", "Date"]).reset_index(drop=True))
    if filled:
        log(f"  price_history の欠落営業日を J-Quants で補完: {', '.join(filled)}")
    return px, filled


# ---------------------------------------------------------------------------
# 日割当（15:30 境界）
# ---------------------------------------------------------------------------

def parse_dt(s) -> datetime | None:
    """ISO 文字列を JST の naive datetime にする（日付だけなら 00:00 として返し has_time=False 扱いは呼び出し側）。"""
    if isinstance(s, datetime):
        return s.replace(tzinfo=None) if s.tzinfo is None else (s + (timedelta(hours=9) - s.utcoffset())).replace(tzinfo=None)
    t = str(s or "").strip()
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset() + timedelta(hours=9)).replace(tzinfo=None)
    return dt


def has_time(s) -> bool:
    return bool(re.search(r"T\d{2}:\d{2}", str(s or "")))


def assign_day(dt: datetime, cal: list[date], time_known: bool = True) -> date | None:
    """公表日時 → 材料として割り当てる営業日（前営業日 15:30〜当日 15:30 の窓）。

    15:30 以降の公表は翌暦日以降の最初の営業日へ。cal の最終日より後になる場合は None
    （＝対象日の 15:30 以降の公表。翌営業日の材料で、表には入れない）。
    """
    d0 = dt.date()
    if time_known and dt.hour * 60 + dt.minute >= CLOSE_MIN:
        d0 = d0 + timedelta(days=1)
    i = bisect.bisect_left(cal, d0)
    return cal[i] if i < len(cal) else None


# ---------------------------------------------------------------------------
# 外部取得（5 分足・PDF 本文・ニュース一覧と配信時刻）
# ---------------------------------------------------------------------------

_BARS_CACHE: dict[str, pd.DataFrame | None] = {}
_PDF_CACHE: dict[str, bool | None] = {}


def fetch_5m_bars(code4: str) -> pd.DataFrame | None:
    """yfinance の 5 分足（直近 60 日・JST naive index・High/Low）。取れなければ None。"""
    if code4 in _BARS_CACHE:
        return _BARS_CACHE[code4]
    bars = None
    try:
        import yfinance as yf
        m = yf.download(f"{code4}.T", period="60d", interval="5m", progress=False, auto_adjust=False)
        if m is not None and not m.empty:
            if isinstance(m.columns, pd.MultiIndex):
                m.columns = m.columns.get_level_values(0)
            idx = m.index
            if idx.tz is not None:
                idx = idx.tz_convert("Asia/Tokyo").tz_localize(None)
            m.index = idx
            bars = m[["High", "Low"]].dropna()
    except Exception as e:
        print(f"  [WARN] 5分足 {code4}: {type(e).__name__} {e}")
        bars = None
    _BARS_CACHE[code4] = bars
    return bars


def pdf_has_minor(pdf_url: str) -> bool | None:
    """開示 PDF の全文に「影響…軽微」があれば True、無ければ False、取れなければ None。"""
    if not pdf_url:
        return None
    if pdf_url in _PDF_CACHE:
        return _PDF_CACHE[pdf_url]
    res = None
    try:
        import requests
        from io import BytesIO
        from pdfminer.high_level import extract_text
        r = requests.get(pdf_url, headers=_UA, timeout=25)
        r.raise_for_status()
        text = extract_text(BytesIO(r.content), maxpages=30)
        flat = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
        res = bool(_MINOR_RE.search(flat)) if flat else None
    except Exception as e:
        print(f"  [WARN] 開示PDF取得 {pdf_url[-30:]}: {type(e).__name__}")
        res = None
    time.sleep(REQUEST_SLEEP)
    _PDF_CACHE[pdf_url] = res
    return res


def _year_for(month: int, day: int, ref: date) -> date | None:
    try:
        d = date(ref.year, month, day)
    except ValueError:
        return None
    if d > ref + timedelta(days=1):
        try:
            d = date(ref.year - 1, month, day)
        except ValueError:
            return None
    return d


def fetch_news_list(code4: str, ref: date, max_pages: int = NEWS_MAX_PAGES, session=None,
                    stop_before: date | None = None) -> tuple[list[dict], str]:
    """Yahoo!ファイナンス 銘柄ニュース一覧（?page=N）を埋め込み JSON から取る。

    一覧の createTime は「M/D」（当日分は「HH:MM」）で年が無いため ref から補う。
    Returns: ([{title, media, link, list_date(date), time('HH:MM' or '')}], エラー理由)
    """
    import json
    import requests
    _flight_re = re.compile(r'self\.__next_f\.push\(\s*\[\s*\d+\s*,\s*("(?:[^"\\]|\\.)*")')
    s = session or requests.Session()
    out: list[dict] = []
    seen: set[str] = set()
    err = ""
    for page in range(1, max_pages + 1):
        url = f"https://finance.yahoo.co.jp/quote/{code4}.T/news" + (f"?page={page}" if page > 1 else "")
        r, err = None, ""
        for k in range(3):   # 一覧ページは一時的に 500 を返すことがあるため再試行する（2 秒→4 秒）
            if k:
                time.sleep(2 * k)
            try:
                r = s.get(url, headers=_UA, timeout=20)
            except Exception as e:
                r, err = None, f"page{page} 通信例外 {type(e).__name__}"
                continue
            if r.status_code == 200:
                err = ""
                break
            err = f"page{page} HTTP {r.status_code}（3 回試行）"
            if r.status_code < 500 and r.status_code != 429:
                break
        if err or r is None:
            break
        chunks = []
        for m in _flight_re.finditer(r.text):
            try:
                chunks.append(json.loads(m.group(1)))
            except Exception:
                continue
        flight = "".join(chunks)
        arts: list = []
        has_next = False
        for m in re.finditer(r'"newsTopics"\s*:\s*', flight):
            b = flight.find("{", m.end())
            if b < 0:
                continue
            blob = _extract_obj(flight, b)
            try:
                d = json.loads(blob)
            except Exception:
                continue
            arts = d.get("articles", []) or []
            has_next = bool((d.get("paging") or {}).get("hasNext"))
            break
        for a in arts:
            if not isinstance(a, dict) or not a.get("headline"):
                continue
            link = str(a.get("link") or "")
            if link in seen:
                continue
            seen.add(link)
            ct = str(a.get("createTime") or "")
            md = re.fullmatch(r"(\d{1,2})/(\d{1,2})", ct)
            hm = re.fullmatch(r"(\d{1,2}):(\d{2})", ct)
            if md:
                ld, tm = _year_for(int(md.group(1)), int(md.group(2)), ref), ""
            elif hm:
                ld, tm = date.today(), f"{int(hm.group(1)):02d}:{hm.group(2)}"
            else:
                continue
            if ld is None:
                continue
            out.append({"title": re.sub(r"\s+", " ", str(a["headline"])).strip(),
                        "media": str(a.get("mediaName") or ""), "link": link,
                        "list_date": ld, "time": tm})
        if not has_next:
            break
        if stop_before is not None and out and min(x["list_date"] for x in out) < stop_before:
            break
        time.sleep(REQUEST_SLEEP)
    return out, err


def _extract_obj(text: str, start: int) -> str:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


_TIME_TAG = re.compile(r'<time[^>]*dateTime="([^"]+)"', re.I)


def fetch_article_time(link: str, session=None) -> datetime | None:
    """Yahoo ニュース記事ページの最初の <time dateTime="…"> から配信日時（JST）を返す。"""
    if not link:
        return None
    import requests
    url = link if link.startswith("http") else f"https://finance.yahoo.co.jp{link}"
    s = session or requests.Session()
    try:
        r = s.get(url, headers=_UA, timeout=20)
        time.sleep(REQUEST_SLEEP)
        if r.status_code != 200:
            return None
        m = _TIME_TAG.search(r.text)
        return parse_dt(m.group(1)) if m else None
    except Exception:
        time.sleep(REQUEST_SLEEP)
        return None


# ---------------------------------------------------------------------------
# 本体: 1 銘柄分の facts を組む
# ---------------------------------------------------------------------------

def _week_days(cal: list[date], target: date, weekly: bool) -> list[date]:
    upto = [d for d in cal if d <= target]
    if weekly:
        mon = target - timedelta(days=target.weekday())
        return [d for d in upto if d >= mon]
    return upto[-5:]


def _m3_days(cal: list[date], target: date) -> list[date]:
    start = (pd.Timestamp(target) - pd.DateOffset(months=MONTHS)).date()
    return [d for d in cal if start <= d <= target]


def _short_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = re.sub(r"^(株式会社|\(株\))|(株式会社|ホールディングス|HD|\(株\))$", "", s).strip()
    return s


def _intraday(code4: str, day: date, dt: datetime, up: bool, mrow, bars_fetcher) -> dict:
    """場中開示の前後の値動き。5 分足が無い日は日足の高安のみ。"""
    res = {"intraday": True, "pre_extreme": None, "pre_extreme_time": None,
           "post_range": None, "post_new_extreme": None, "intraday_note": ""}
    bars = bars_fetcher(code4) if bars_fetcher else None
    dayb = None
    if bars is not None and not bars.empty:
        dayb = bars[bars.index.date == day]
    if dayb is None or dayb.empty:
        res["intraday_note"] = "場中の時刻は取得できず"
        res["day_high"] = float(mrow["high"]) if mrow is not None else None
        res["day_low"] = float(mrow["low"]) if mrow is not None else None
        return res
    t0 = datetime.combine(day, dt.time())
    pre = dayb[dayb.index < t0]
    post = dayb[dayb.index >= t0]
    if pre.empty or post.empty:
        res["intraday_note"] = "5分足に開示前後の足が無い"
        return res
    if up:
        ext = float(pre["High"].max())
        ts = pre["High"].idxmax()
        res["post_new_extreme"] = bool(float(post["High"].max()) > ext)
    else:
        ext = float(pre["Low"].min())
        ts = pre["Low"].idxmin()
        res["post_new_extreme"] = bool(float(post["Low"].min()) < ext)
    res["pre_extreme"] = ext
    res["pre_extreme_time"] = ts.strftime("%H:%M")
    res["post_range"] = [float(post["Low"].min()), float(post["High"].max())]
    return res


def build_move_days(
    code4: str,
    name: str,
    metrics: pd.DataFrame,
    cal: list[date],
    target: date,
    *,
    weekly: bool,
    tdnet_entries: list[dict] | None = None,
    news_items: list[dict] | None = None,
    related: list[dict] | None = None,
    related_metrics: dict[str, pd.DataFrame] | None = None,
    index_metrics: dict[str, pd.DataFrame] | None = None,
    shikiho: dict | None = None,
    asof: datetime | None = None,
    bars_fetcher=fetch_5m_bars,
    minor_checker=pdf_has_minor,
    news_time_fetcher=fetch_article_time,
    news_noise=None,
) -> dict:
    """1 銘柄の facts（表 1・表 2・四季報・開示一覧・報道）を組む。

    tdnet_entries: [{"title","published"(ISO),"pdf_url"}]（全期間。ここで 3 か月・asof に絞る）
    news_items: fetch_news_list の戻り値（配信時刻は必要な日の分だけ news_time_fetcher で補う）
    related: [{"code","name","relation"}]（最大 3 社）
    index_metrics: {"日経平均": DataFrame(index=date, ret[%], chg[円]), "TOPIX": DataFrame(ret)}
      （全銘柄共通・make_mover_report が 1 回だけ取得。日の行の "indices" に入れ、関連銘柄の列の末尾に出す）
    """
    errors: list[str] = []
    facts: dict = {"name": name, "errors": errors}
    cal = sorted(d for d in cal if d <= target)
    if metrics is None or metrics.empty:
        errors.append("price_history・J-Quants とも株価が無い")
        facts.update({"week_days": [], "month3_days": [], "sigma2_days": [], "max_move_day": None})
        return facts
    if target not in metrics.index:
        last = max(d for d in metrics.index if d <= target) if any(d <= target for d in metrics.index) else None
        errors.append(f"対象日 {target} の売買なし（最終売買日 {last}）")
        facts["no_trade_on_target"] = True
    wk = [d for d in _week_days(cal, target, weekly) if d in metrics.index]
    m3 = [d for d in _m3_days(cal, target) if d in metrics.index]
    if metrics["thr"].reindex(m3).isna().any():
        errors.append("株価日足が不足し一部の日の 2σ 閾値を算出できず")
    m3_start = m3[0] if m3 else target

    # ---- 開示（3 か月・asof） ----
    discs: list[dict] = []
    for e in (tdnet_entries or []):
        pub = str(e.get("published") or "")
        dt = parse_dt(pub)
        if dt is None:
            continue
        if asof is not None and dt > asof:
            continue
        if dt.date() < m3_start - timedelta(days=5):
            continue
        tk = has_time(pub)
        ad = assign_day(dt, cal, tk)
        title = str(e.get("title") or "").strip()
        discs.append({
            "title": title, "published": dt.strftime("%Y-%m-%dT%H:%M") if tk else dt.strftime("%Y-%m-%d"),
            "time_known": tk, "assigned_date": ad.isoformat() if ad else None,
            "pdf_url": e.get("pdf_url") or "",
            "earnings": bool(_EARNINGS_RE.search(title) and not _EARN_EXCL.search(title)),
            "_dt": dt, "_ad": ad,
        })
    discs = [x for x in discs if x["_ad"] is None or x["_ad"] >= m3_start]
    by_day_d: dict[date, list[dict]] = {}
    for x in discs:
        if x["_ad"] is not None:
            by_day_d.setdefault(x["_ad"], []).append(x)

    # ---- 行の選定 ----
    def _absret(d):
        v = metrics.at[d, "ret"]
        return abs(v) if v == v else -1.0
    wk_max = max(wk, key=_absret) if wk else None
    m3_max = max(m3, key=_absret) if m3 else None
    earn_days = {d for d, xs in by_day_d.items() if any(x["earnings"] for x in xs)}
    m3_rows = [d for d in m3 if bool(metrics.at[d, "sig"]) or d in by_day_d]
    if len(m3_rows) > M3_MAX_ROWS:
        keep = {d for d in m3_rows if bool(metrics.at[d, "sig"]) or d in earn_days or d == m3_max}
        rest = [d for d in reversed(m3_rows) if d not in keep]
        while len(keep) < M3_MAX_ROWS and rest:
            keep.add(rest.pop(0))
        dropped = len(m3_rows) - len(keep)
        m3_rows = [d for d in m3_rows if d in keep]
        errors.append(f"表2の行数上限 {M3_MAX_ROWS} のため開示日 {dropped} 日を省略（開示一覧には全件）")

    # ---- ニュース（必要な日だけ配信時刻を取る） ----
    need: set[date] = set()
    for d in set(wk) | set(m3_rows):
        i = cal.index(d)
        prev = cal[i - 1] if i > 0 else d - timedelta(days=4)
        k = prev
        while k <= d:
            need.add(k)
            k += timedelta(days=1)
    short = _short_name(name)
    keys = [k for k in (code4, short[:3] if len(short) >= 3 else short) if k]
    noise = news_noise or (lambda t: False)
    news: list[dict] = []
    n_time = 0
    ref_today = (asof.date() if asof else target)
    for n in (news_items or []):
        # 市場全体の順位表・まとめ記事は落とす。ただし見出しに当該銘柄名・コードがあるもの
        # （例「動いた株・出来た株（前場）part3：ヴィッツ、河西工業など15社」）は残す。
        if noise(n["title"]) and not any(k in n["title"] for k in keys):
            continue
        ld = n["list_date"]
        dt = None
        if n.get("time"):
            hh, mm = (int(v) for v in n["time"].split(":"))
            dt = datetime.combine(ld, datetime.min.time()).replace(hour=hh, minute=mm)
        elif ld in need or ld == ref_today:
            if n_time < NEWS_TIME_PER_STOCK and news_time_fetcher is not None:
                dt = news_time_fetcher(n.get("link", ""))
                n_time += 1
        if asof is not None:
            if dt is not None and dt > asof:
                continue
            if dt is None and ld > asof.date():
                continue
        if dt is not None:
            ad = assign_day(dt, cal, True)
            pub = dt.strftime("%Y-%m-%dT%H:%M")
        else:
            ad = assign_day(datetime.combine(ld, datetime.min.time()), cal, False)
            pub = ld.isoformat()
        news.append({"title": n["title"], "source": n.get("media", ""), "published": pub,
                     "time_known": dt is not None,
                     "assigned_date": ad.isoformat() if ad else None,
                     "own": any(k in n["title"] for k in keys),
                     "_ad": ad, "_d": dt.date() if dt else ld})
    by_day_n: dict[date, list[dict]] = {}
    for x in news:
        if x["_ad"] is not None:
            by_day_n.setdefault(x["_ad"], []).append(x)

    # ---- 四季報 ----
    rel_date = None
    if shikiho and shikiho.get("release_date"):
        try:
            rel_date = date.fromisoformat(shikiho["release_date"])
        except ValueError:
            rel_date = None

    pdf_used = 0

    def _day(d: date, is_max: bool, kinds: list[str] | None) -> dict:
        nonlocal pdf_used
        r = metrics.loc[d]
        up = bool(r["ret"] >= 0) if r["ret"] == r["ret"] else True
        ds_out = []
        for x in sorted(by_day_d.get(d, []), key=lambda z: z["_dt"]):
            item = {k: v for k, v in x.items() if not k.startswith("_")}
            dt = x["_dt"]
            mins = dt.hour * 60 + dt.minute
            intraday = bool(x["time_known"] and dt.date() == d and OPEN_MIN < mins < CLOSE_MIN)
            item["intraday"] = intraday
            if intraday:
                item.update(_intraday(code4, d, dt, up, r, bars_fetcher))
            if _MINOR_SKIP.search(x["title"]) or not x["pdf_url"]:
                item["minor_impact"] = None
                item["minor_checked"] = False
            elif pdf_used < PDF_PER_STOCK and minor_checker is not None:
                item["minor_impact"] = minor_checker(x["pdf_url"])
                item["minor_checked"] = item["minor_impact"] is not None
                pdf_used += 1
            else:
                item["minor_impact"] = None
                item["minor_checked"] = False
            ds_out.append(item)
        nws = sorted(by_day_n.get(d, []),
                     key=lambda z: (0 if z["own"] else 1, 1 if _HL_NOISE.search(z["title"]) else 0,
                                    z["published"]))[:NEWS_PER_DAY]
        nws = sorted(nws, key=lambda z: z["published"])
        rel_out = []
        for rc in (related or []):
            rm = (related_metrics or {}).get(rc["code"])
            v = rm.at[d, "ret"] if (rm is not None and d in rm.index) else None
            rel_out.append({"code": rc["code"], "name": rc["name"], "relation": rc.get("relation", ""),
                            "ret_pct": round(float(v), 2) if v is not None and v == v else None})
        idx_out = []
        for lbl, im in (index_metrics or {}).items():
            if im is None or d not in im.index:
                continue
            iv = im.at[d, "ret"]
            if iv is None or iv != iv:
                continue
            item = {"name": lbl, "ret_pct": round(float(iv), 2)}
            if "chg" in im.columns:
                cv = im.at[d, "chg"]
                if cv is not None and cv == cv:
                    item["chg"] = round(float(cv), 2)
            idx_out.append(item)
        thr = r["thr"]
        vr = r["vol_ratio"]
        out = {
            "date": d.isoformat(), "close": float(r["close"]),
            "ret_pct": round(float(r["ret"]), 2) if r["ret"] == r["ret"] else None,
            "volume": float(r["raw_volume"]) if r["raw_volume"] == r["raw_volume"] else None,
            "vol_ratio_20d": round(float(vr), 2) if vr == vr else None,
            "sigma2": bool(r["sig"]), "sigma2_threshold_pct": round(float(thr), 2) if thr == thr else None,
            "is_max": is_max, "high": float(r["high"]), "low": float(r["low"]),
            "disclosures": ds_out,
            "news": [{k: v for k, v in z.items() if not k.startswith("_")} for z in nws],
            "related": rel_out,
            "indices": idx_out,
            "shikiho_release": bool(rel_date == d),
        }
        if kinds is not None:
            out["kind"] = kinds
        return out

    week_days = [_day(d, d == wk_max, None) for d in wk]
    month3 = []
    for d in m3_rows:
        kinds = []
        if bool(metrics.at[d, "sig"]):
            kinds.append("sigma2")
        if d in earn_days:
            kinds.append("earnings")
        if d in by_day_d:
            kinds.append("disclosure")
        if d == m3_max:
            kinds.append("max")
        month3.append(_day(d, d == m3_max, kinds))

    facts.update({
        "max_move_day": (wk_max.isoformat() if weekly else target.isoformat()) if wk_max else None,
        "table_max_day": wk_max.isoformat() if wk_max else None,
        "month3_max_day": m3_max.isoformat() if m3_max else None,
        "sigma2_days": [d.isoformat() for d in m3 if bool(metrics.at[d, "sig"])],
        "week_range": [wk[0].isoformat(), wk[-1].isoformat()] if wk else None,
        "month3_range": [m3[0].isoformat(), m3[-1].isoformat(), len(m3)] if m3 else None,
        "week_days": week_days,
        "month3_days": month3,
        "related": related or [],
        "disclosures_3m": [{k: v for k, v in x.items() if not k.startswith("_")}
                           for x in sorted(discs, key=lambda z: z["_dt"], reverse=True)],
        "news_3m": [{k: v for k, v in x.items() if not k.startswith("_")}
                    for x in sorted(news, key=lambda z: z["published"], reverse=True)
                    if x["_d"] >= m3_start],
        "news_oldest": min((x["_d"] for x in news), default=None),
    })
    if facts["news_oldest"]:
        facts["news_oldest"] = facts["news_oldest"].isoformat()

    # 四季報
    if shikiho is None:
        facts["shikiho"] = None
    elif not shikiho.get("ok"):
        facts["shikiho"] = {"error": shikiho.get("error") or "取得失敗"}
    else:
        sh = {k: shikiho.get(k) for k in (
            "issue", "release_date", "forecast_updated", "per", "price", "price_time", "shares_adj",
            "company_forecast_label", "company_forecast_disclosed", "fiscal_year_end", "next_disclosure")}
        sni, cni, gap = shikiho.get("shikiho_net_income_mn"), shikiho.get("company_net_income_mn"), shikiho.get("gap_pct")
        sh["shikiho_net_income_mn"] = round(sni) if sni is not None else None
        sh["company_net_income_mn"] = round(cni) if cni is not None else None
        sh["gap_pct"] = round(gap, 1) if gap is not None else None
        sh["notes"] = shikiho.get("notes") or []
        if rel_date is not None:
            sh["days_since_release"] = sum(1 for d in cal if rel_date < d <= target)
            if rel_date in metrics.index:
                rr = metrics.loc[rel_date]
                sh["release_day_ret_pct"] = round(float(rr["ret"]), 2) if rr["ret"] == rr["ret"] else None
                sh["release_day_sigma2"] = bool(rr["sig"])
        facts["shikiho"] = sh
    return facts


# ---------------------------------------------------------------------------
# markdown 文字列化（書式は mover_sample_7256_v2.md の表 1・表 2・§3）
# ---------------------------------------------------------------------------

def _md(d: str) -> str:
    x = date.fromisoformat(d[:10])
    return f"{x.month}/{x.day}"


def _mdw(d: str) -> str:
    x = date.fromisoformat(d[:10])
    return f"{x.month}/{x.day}（{WD[x.weekday()]}）"


def fmt_pct(v) -> str:
    if v is None:
        return "−"
    if abs(v) < 0.005:
        return "±0.00%"
    return f"＋{v:.2f}%" if v > 0 else f"▼{abs(v):.2f}%"


def _price(v) -> str:
    if v is None:
        return "−"
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-6 else f"{v:,.1f}"


def _esc(s: str) -> str:
    return str(s).replace("|", "｜").replace("\n", " ")


def _tlabel(pub: str, row_date: str, time_known: bool) -> str:
    if not time_known:
        return f"{_md(pub)}（時刻不明）" if pub[:10] != row_date else "時刻不明"
    hm = pub[11:16]
    return hm if pub[:10] == row_date else f"{_md(pub)} {hm}"


def _disc_cell(x: dict, row_date: str, extra_n: int = 0) -> str:
    s = f"TDnet {_tlabel(x['published'], row_date, x.get('time_known', True))}「{_esc(x['title'])}」"
    if extra_n:
        s += f"ほか同時刻 {extra_n} 件"
    if x.get("intraday"):
        if x.get("pre_extreme") is not None:
            lab = "高値" if not x.get("_down") else "安値"
            pr = x.get("post_range") or [None, None]
            s += (f"［場中・開示前{lab} {_price(x['pre_extreme'])}（{x['pre_extreme_time']}）・"
                  f"開示後 {_price(pr[0])}〜{_price(pr[1])}"
                  + ("・開示後に" + lab + "更新" if x.get("post_new_extreme") else "") + "］")
        elif x.get("intraday_note") == "場中の時刻は取得できず":
            s += (f"［場中・日足 高値 {_price(x.get('day_high'))}／安値 {_price(x.get('day_low'))}・"
                  "場中の時刻は取得できず］")
        elif x.get("intraday_note"):
            s += f"［場中・{x['intraday_note']}］"
    if x.get("minor_impact"):
        s += "［影響軽微］"
    return f"**{s}**"


def _material_cell(day: dict, shikiho_issue: str) -> str:
    parts = []
    if day.get("shikiho_release"):
        parts.append(f"**会社四季報 {shikiho_issue} 発売日**")
    ds = day.get("disclosures") or []
    down = (day.get("ret_pct") or 0) < 0
    groups: list[list[dict]] = []
    for x in ds:
        x = dict(x, _down=down)
        if groups and groups[-1][0]["published"] == x["published"] and groups[-1][0]["title"][:8] == x["title"][:8]:
            groups[-1].append(x)
        else:
            groups.append([x])
    for g in groups[:DISC_PER_ROW]:
        parts.append(_disc_cell(g[0], day["date"], len(g) - 1))
    rest = sum(len(g) for g in groups[DISC_PER_ROW:])
    if rest:
        parts.append(f"ほか開示 {rest} 件")
    for n in day.get("news") or []:
        parts.append(f"ニュース {_tlabel(n['published'], day['date'], n.get('time_known', False))} "
                     f"{_esc(n.get('source') or '')}「{_esc(n['title'])}」")
    return "／".join(parts) if parts else "−"


def _related_cell(day: dict) -> str:
    xs = [f"{r['name']} {fmt_pct(r['ret_pct'])}" for r in (day.get("related") or []) if r.get("ret_pct") is not None]
    # 指数（2026-10-05 追加・承認済みサンプルの「日経平均は 647 円安」の形。日経平均は円の値幅も付ける）
    ix = []
    for r in (day.get("indices") or []):
        if r.get("ret_pct") is None:
            continue
        t = f"{r['name']} {fmt_pct(r['ret_pct'])}"
        c = r.get("chg")
        if c is not None:
            t += f"（{abs(c):,.0f} 円{'安' if c < 0 else ('高' if c > 0 else '')}）" if round(abs(c)) else "（変わらず）"
        ix.append(t)
    parts = (["・".join(xs)] if xs else []) + (["・".join(ix)] if ix else [])
    return "／".join(parts) if parts else "−"


def _mark(day: dict, table: str) -> str:
    thr = day.get("sigma2_threshold_pct")
    lab = ("★" if day.get("sigma2") else "") + ("最大" if day.get("is_max") else "")
    if not lab:
        lab = "−"
    if table == "m3" and not day.get("sigma2"):
        k = day.get("kind") or []
        pre = "決算日・" if "earnings" in k else ("開示日・" if "disclosure" in k else "")
        lab = pre + lab
    return f"{lab}（{thr:.2f}%）" if thr is not None else f"{lab}（閾値算出不能）"


def _row(day: dict, table: str, shikiho_issue: str) -> str:
    vol = day.get("volume")
    vr = day.get("vol_ratio_20d")
    vs = (f"{vol:,.0f} 株" if vol is not None else "−") + (f"（{vr:.2f} 倍）" if vr is not None else "")
    return (f"| {_mdw(day['date'])} | {_price(day['close'])} | {fmt_pct(day.get('ret_pct'))} | {vs} "
            f"| {_mark(day, table)} | {_material_cell(day, shikiho_issue)} | {_related_cell(day)} |")


_TABLE_HEAD = ("| 日付 | 終値 | 騰落率 | 出来高（20 日平均比） | 印 | その日の開示・ニュース（前営業日 15:30〜当日 15:30） "
               "| 関連銘柄の同日騰落 |")
_TABLE_SEP = "|---|---|---|---|---|---|---|"


def render_move_days(facts: dict, weekly: bool) -> list[str]:
    """facts を raw 用の markdown 行へ。見出しは `**日別騰落` で始める（build_market_raw の需給除外に掛からない）。"""
    lines: list[str] = []
    if not facts or not facts.get("week_days"):
        errs = "・".join((facts or {}).get("errors") or []) or "株価を取得できず"
        return [f"**日別騰落:** 作成できず（{errs}）", ""]
    sh = facts.get("shikiho") or {}
    issue = sh.get("issue") or ""
    wr = facts["week_range"]
    span = "週内" if weekly else "5 営業日内"
    lines += [
        f"**日別騰落（{_md(wr[0])}〜{_md(wr[1])}・{'当週' if weekly else '直近 5 営業日'}"
        f"・★＝当日騰落率の絶対値が前 60 営業日の日次騰落率の標準偏差×2 超／最大＝{span}で絶対値最大の日。"
        f"括弧内は 2σ 閾値）:**",
        "",
        _TABLE_HEAD, _TABLE_SEP,
    ]
    lines += [_row(d, "wk", issue) for d in facts["week_days"]]
    lines.append("")
    mr = facts.get("month3_range")
    if mr:
        lines += [
            f"**直近3か月の大きく動いた日（{_md(mr[0])}〜{_md(mr[1])}・{mr[2]} 営業日。★の日・開示日・決算日のみ。"
            f"★最大＝3 か月内で絶対値最大の日。前営業日 15:30 以降〜当日 15:30 前の公表をその日に割当）:**",
            "",
        ]
        if facts["month3_days"]:
            lines += [_TABLE_HEAD, _TABLE_SEP]
            lines += [_row(d, "m3", issue) for d in facts["month3_days"]]
        else:
            lines.append("（該当日なし: ★の日・開示日・決算日がいずれも無い）")
        lines.append("")

    # 四季報
    if facts.get("shikiho") is None:
        pass
    elif "error" in sh:
        lines.append(f"- 四季報: 取得失敗（{sh['error']}）")
    else:
        parts = []
        if sh.get("release_date"):
            rd = date.fromisoformat(sh["release_date"])
            parts.append(f"{issue}（発売日 {rd.month}/{rd.day}〈{WD[rd.weekday()]}〉・{_md(wr[1])} まで "
                         f"{sh.get('days_since_release', '−')} 営業日）")
        if sh.get("forecast_updated"):
            parts.append(f"四季報予想の最終更新日 {sh['forecast_updated']}")
        if sh.get("shikiho_net_income_mn") is not None:
            parts.append(f"四季報予想 純利益 {sh['shikiho_net_income_mn']:,} 百万円（四季報予想 PER "
                         f"{sh['per']:.4f}・株価 {_price(sh['price'])} 円・調整後株数 {sh['shares_adj']:,.0f} 株から逆算）")
        else:
            parts.append("四季報予想 純利益: 逆算できず（" + "・".join(sh.get("notes") or ["理由不明"]) + "）")
        if sh.get("company_net_income_mn") is not None:
            fy = sh.get("fiscal_year_end") or ""
            fyl = f"{fy[:4]}年{int(fy[5:7])}月期" if re.fullmatch(r"\d{4}-\d{2}", fy) else fy
            parts.append(f"会社予想 純利益 {sh['company_net_income_mn']:,} 百万円（{fyl}"
                         + (f"・会社予想の開示日 {sh['company_forecast_disclosed']}" if sh.get("company_forecast_disclosed") else "")
                         + "）")
        if sh.get("gap_pct") is not None:
            parts.append(f"乖離率 {_gap(sh['gap_pct'])}")
        if sh.get("release_day_ret_pct") is not None:
            parts.append(f"発売日の騰落 {fmt_pct(sh['release_day_ret_pct'])}"
                         + ("（★）" if sh.get("release_day_sigma2") else ""))
        lines.append("- 四季報: " + "／".join(parts))

    # 関連銘柄（表の右端列の銘柄が当社とどういう関係か。2026-10-05 追加。売上構成比は出所ごとに値が違うため載せない）
    rels = facts.get("related") or []
    if rels:
        def _rel_txt(r: dict) -> str:
            rel = re.sub(r"（売上の[^）]*）", "", str(r.get("relation") or "関係不明"))
            if str(r.get("code", "")).startswith("S33:"):   # 業種中央値の疑似コード（make_mover_report.SECTOR_PSEUDO_PREFIX）
                return f"{r.get('name', '')}＝{rel}"
            return f"{r.get('name', '')}（{r.get('code', '')}・{rel}）"
        lines.append("- 関連銘柄（表の右端列）: " + "／".join(_rel_txt(r) for r in rels))
    else:
        lines.append("- 関連銘柄（表の右端列）: なし（取得できず）")
    if any(dd.get("indices") for dd in facts.get("week_days") or []):
        lines.append("- 指数（表の右端列の「／」の後）: 日経平均（括弧内は前日比の値幅）・TOPIX の同日騰落率")

    # 開示一覧（3 か月）
    ds = facts.get("disclosures_3m") or []
    if mr:
        lines.append(f"- 開示一覧（直近 3 か月・{_md(mr[0])}〜{_md(mr[1])}）: {len(ds)} 件")
        groups: list[list[dict]] = []
        for x in ds:
            if groups and groups[-1][0]["published"] == x["published"] and groups[-1][0]["title"][:8] == x["title"][:8]:
                groups[-1].append(x)
            else:
                groups.append([x])
        for g in groups[:DISC_LIST_MAX]:
            x = g[0]
            ts = x["published"].replace("T", " ")
            lines.append(f"  - {ts}「{_esc(x['title'])}」" + (f"ほか同時刻 {len(g) - 1} 件" if len(g) > 1 else ""))
        if len(groups) > DISC_LIST_MAX:
            lines.append(f"  - ほか {sum(len(g) for g in groups[DISC_LIST_MAX:])} 件（古い分）")
    # 報道
    ns = facts.get("news_3m") or []
    oldest = facts.get("news_oldest")
    rng = f"取得範囲 {_md(oldest)}〜" if oldest else "取得できず"
    lines.append(f"- 報道（Yahoo!ファイナンス 銘柄ニュース・{rng}・新しい順・市場全体の順位表は除外）: {len(ns)} 件"
                 + (f"（うち {NEWS_LIST_MAX} 件を表示）" if len(ns) > NEWS_LIST_MAX else ""))
    for n in ns[:NEWS_LIST_MAX]:
        ts = n["published"].replace("T", " ")
        note = "" if n.get("assigned_date") else "（対象日 15:30 以降の公表＝翌営業日の材料）"
        lines.append(f"  - {ts} {_esc(n.get('source') or '')}「{_esc(n['title'])}」{note}")
    lines.append("")
    return lines


def _gap(v: float) -> str:
    return f"＋{v:.1f}%" if v > 0 else (f"▼{abs(v):.1f}%" if v < 0 else "±0.0%")
