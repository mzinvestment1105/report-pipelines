"""市況スナップショット用 最新終値取得の共有ロジック（全レポート共通）。

PM 2026-06-27: マクロレポートが日経平均(^N225)の古い終値(6/25 の最高値 72,366.34)を
「最新」として発行する事故が発生。実際の 6/26(金) 東京終値 69,360.88(約 -4.2%)を取りこぼした。
原因は Yahoo 日足配列の最終要素(pairs[-1])だけを見ており、土曜早朝の生成時点では金曜分の
日足がまだ配列に載っていなかったこと。

本モジュールは Yahoo chart API の meta.regularMarketPrice(= 日足配列より遅延しない、直近
セッションの確定値)を併用し、(1) chart 日足 (2) chart meta (3) yfinance 日足 の中から
「最も新しい確定終値」を返す。全レポート(マクロ/セクター/銘柄)が本関数を共有することで、
stale 事故を一元的に防ぐ(同じロジックを各所にコピペしない)。

例外は内部で握りつぶし None を返す。1 銘柄の取得失敗が呼び出し側レポート全体を止めない。
"""

from __future__ import annotations

import json as _json
import urllib.parse as _up
import urllib.request as _ur
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from datetime import timezone as _timezone

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Quote:
    """1 銘柄の最新スナップショット。

    close        : 最新確定終値
    prev         : 前日(前セッション)終値。取れなければ None
    date         : close の取引日(取引所ローカル日付)
    source       : "yahoo_meta"(meta.regularMarketPrice) / "yahoo_chart"(日足) / "yfinance"
    market_state : close が meta 由来の時の市場状態("REGULAR"=場中速報 / "CLOSED" 等)。それ以外 None
    """

    close: float
    prev: float | None
    date: _date
    source: str
    market_state: str | None = None

    @property
    def change(self) -> float | None:
        if self.prev in (None, 0):
            return None
        return self.close - self.prev

    @property
    def pct(self) -> float | None:
        chg = self.change
        if chg is None or not self.prev:
            return None
        return chg / self.prev * 100


def _chart_payload(ticker: str) -> tuple[list[tuple], tuple | None]:
    """Yahoo chart API を直叩きして (daily_pairs, meta_point) を返す。失敗時 ([], None)。

    daily_pairs : [(取引所ローカル日付, close)] 昇順。日付は meta.gmtoffset(取引所TZ)で確定する
                  ため、UTC 丸めによる為替等の 1 日ズレが起きない。
    meta_point  : (date, regularMarketPrice, marketState) または None。日足配列が遅延していても
                  meta は直近セッションの確定値を保持するため、最新値の主ソースとして使う。
    """
    sym = _up.quote(ticker, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=14d&interval=1d"
    try:
        req = _ur.Request(url, headers={"User-Agent": _UA})
        with _ur.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        res = data["chart"]["result"][0]
        meta = res.get("meta", {}) or {}
        gmt = int(meta.get("gmtoffset") or 0)
        ex_tz = _timezone(_timedelta(seconds=gmt))

        ts = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        pairs = [
            (_datetime.fromtimestamp(t, tz=ex_tz).date(), float(c))
            for t, c in zip(ts, closes)
            if c is not None
        ]
        pairs.sort(key=lambda p: p[0])

        meta_pt = None
        rmp = meta.get("regularMarketPrice")
        rmt = meta.get("regularMarketTime")
        if rmp is not None and rmt:
            md = _datetime.fromtimestamp(int(rmt), tz=ex_tz).date()
            meta_pt = (md, float(rmp), meta.get("marketState"))
        return pairs, meta_pt
    except Exception:
        return [], None


def _yf_pairs(ticker: str) -> list[tuple]:
    """yfinance 14 日履歴から (date, close) 昇順。chart API が全滅した時のみ使う backup。失敗時 []。"""
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period="14d", auto_adjust=False)
        if hist is None or hist.empty:
            return []
        hist = hist.dropna(subset=["Close"])
        return [(idx.date(), float(c)) for idx, c in zip(hist.index, hist["Close"])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 独立ベンダー（CNBC）: Yahoo がズレた時に実値を取得する第二ソース（無料・キー不要・トークンゼロ）
# ---------------------------------------------------------------------------

# Yahoo ティッカー → CNBC シンボル。CNBC は last / previous_day_closing / last_time を返すため
# Yahoo の日足配列が遅延していても最新営業日の確定値を独立に取得できる。
# 日経先物(NIY=F)は CNBC の @NK.1（CME Nikkei Futures 先限）で代替取得する。日経VI(^N225VI)
# のみ Yahoo・CNBC とも無料 API に存在せず（取得不可表示）、別途スクレイピングが必要。
_CNBC_MAP = {
    "^N225": ".N225",
    "NIY=F": "@NK.1",
    "^GSPC": ".SPX",
    "USDJPY=X": "JPY=",
    "^VIX": ".VIX",
    "GC=F": "@GC.1",
    "BTC-USD": "BTC.CM=",
    "^TNX": "US10Y",
}


def _to_float(v) -> float | None:
    """CNBC の "69,360.88" / "4.376%" / "N/A" 等を float へ。失敗時 None。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _cnbc_point(yahoo_ticker: str):
    """CNBC quote API から (date, last, prev) を返す。マッピングなし/失敗時 None。

    last_time は指数で "YYYY-MM-DD"、為替等で ISO("...T..-0400") のため先頭 10 文字を取引日とする。
    """
    sym = _CNBC_MAP.get(yahoo_ticker)
    if not sym:
        return None
    base = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    q = _up.urlencode(
        {
            "symbols": sym,
            "requestMethod": "itv",
            "noform": "1",
            "partnerId": "2",
            "fund": "1",
            "exthrs": "1",
            "output": "json",
            "events": "1",
        }
    )
    try:
        req = _ur.Request(f"{base}?{q}", headers={"User-Agent": _UA})
        with _ur.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        quotes = (data.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
        if not quotes:
            return None
        d = quotes[0]
        last = _to_float(d.get("last"))
        prev = _to_float(d.get("previous_day_closing"))
        lt = d.get("last_time") or d.get("last_time_msec")
        if last is None or not lt:
            return None
        trade_date = _date.fromisoformat(str(lt)[:10])
        return trade_date, last, prev
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 営業日ベースの陳腐化判定（カレンダー日数ではなく営業日で判定する）
# ---------------------------------------------------------------------------

def _is_jp_holiday(d: _date) -> bool:
    try:
        import jpholiday

        return bool(jpholiday.is_holiday(d))
    except Exception:
        return False


def business_days_after(data_date: _date, ref_date: _date) -> int:
    """data_date(排他) 〜 ref_date(包含) の営業日数（土日 + 日本の祝日を除外）。ref<=data なら 0。"""
    if ref_date <= data_date:
        return 0
    n = 0
    d = data_date + _timedelta(days=1)
    while d <= ref_date:
        if d.weekday() < 5 and not _is_jp_holiday(d):
            n += 1
        d += _timedelta(days=1)
    return n


def previous_business_day(ref_date: _date) -> _date:
    """ref_date の直前営業日（土日 + 日本の祝日を除外）。"""
    d = ref_date - _timedelta(days=1)
    while d.weekday() >= 5 or _is_jp_holiday(d):
        d -= _timedelta(days=1)
    return d


def is_stale_close(data_date: _date, ref_date: _date) -> bool:
    """data_date が ref_date 基準で陳腐化しているか（営業日判定・カレンダー日数では判定しない）。

    期待する最新確定日 = ref_date の「直前営業日」。data_date がそれより古ければ陳腐化とみなす。
      - 朝刊（寄り付き前）が直前営業日の確定終値を出す → 直前営業日 = data → 陳腐化なし
      - 土曜版が金曜終値を出す → 直前営業日=金曜 = data → 陳腐化なし
      - 水曜に月曜値（火曜が抜け） → data=月曜 < 直前営業日=火曜 → 陳腐化
      - 土曜に木曜値（金曜が抜け） → data=木曜 < 直前営業日=金曜 → 陳腐化（元事故を検知）
    祝日は jpholiday で除外するため 3 連休明け等で誤検知しない。
    """
    return data_date < previous_business_day(ref_date)


_NIKKEI_VI_URL = "https://indexes.nikkei.co.jp/nkave/index?type=vi"
_NIKKEI_VI_NAME = "日経平均ボラティリティー・インデックス"


def get_nikkei_vi(target_date=None) -> Quote | None:
    """日経VI(^N225VI) を日経公式指数ページからスクレイプして返す（無料 API に存在しないため）。

    日経公式の指数一覧（type=vi）から「日経平均ボラティリティー・インデックス」の最新値・前日比・
    取引日を抽出する。先物指数（同名＋「先物指数」）とは `</a>` 直後で区別する。WebFetch が GHA で
    404 になる代替として、生 HTML を urllib で取得し正規表現で抽出する。取得不能時は None。
    """
    import re as _re

    try:
        req = _ur.Request(_NIKKEI_VI_URL, headers={"User-Agent": _UA})
        with _ur.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = _re.search(
        _re.escape(_NIKKEI_VI_NAME) + r"</a>"
        r'.*?<div class="value">\s*([\d,\.]+)'
        r'.*?<div class="daily-change[^"]*">.*?([+\-][\d.,]+)'
        r'.*?<div class="date">\s*(\d{2})\.(\d{2})',
        html,
        _re.S,
    )
    if not m:
        return None
    try:
        close = float(m.group(1).replace(",", ""))
        chg = float(m.group(2).replace(",", ""))
        mon, day = int(m.group(3)), int(m.group(4))
    except ValueError:
        return None
    today = _datetime.now(_timezone(_timedelta(hours=9))).date()
    try:
        d = _date(today.year, mon, day)
        if d > today:  # 年跨ぎ（1月実行で 12 月の値を見る等）への保険
            d = _date(today.year - 1, mon, day)
    except ValueError:
        return None
    return Quote(close=close, prev=close - chg, date=d, source="nikkei_official")


def get_latest_close(ticker: str, target_date=None) -> Quote | None:
    """ticker の「当日終値・前日終値・取得日」を最も新しい確定値で返す。取得不能なら None。

    優先順:
      1. Yahoo（chart 日足 + meta.regularMarketPrice / chart 全滅時のみ yfinance backup）を主とする。
         meta は日足配列が遅延しても直近セッションの確定値を持つため、通常はこれで最新営業日に届く。
      2. Yahoo が取得不能、または「直前営業日より古い（営業日基準で陳腐化）」場合のみ、独立ベンダー
         CNBC から実値を取得してフォールバックする（＝ズレたら必ず別ソースで取りに行く・送付は止めない）。
    通常時は Yahoo を主に保つことで、24h 取引銘柄（為替・金・先物等）の前日比を CNBC の週末基準 prev で
    薄めない。target_date 指定時は target_date 以下の最新営業日を「当日」とする。
    例外は内部で握りつぶし None を返す(呼び出し側レポートを 1 銘柄の失敗で止めない)。
    """
    if isinstance(target_date, str):
        try:
            target_date = _date.fromisoformat(target_date)
        except ValueError:
            target_date = None

    # 日経VI は Yahoo/CNBC など無料 API に存在しないため日経公式ページからスクレイプする。
    if ticker == "^N225VI":
        return get_nikkei_vi(target_date)

    chart_pairs, meta_pt = _chart_payload(ticker)

    merged: dict = {}
    chart_dates: set = set()
    if chart_pairs:
        for d, c in chart_pairs:
            merged[d] = c
            chart_dates.add(d)
    else:
        # chart API が日足ゼロの時のみ yfinance を backup として使う
        for d, c in _yf_pairs(ticker):
            merged[d] = c

    meta_date = None
    if meta_pt is not None:
        md, mp, _state = meta_pt
        if target_date is None or md <= target_date:
            merged[md] = mp  # meta は自セッションの確定値として採用
            meta_date = md

    # --- 主ソース: Yahoo 側の最新確定値を確定 ---
    yahoo_quote: Quote | None = None
    if merged:
        pairs = sorted(merged.items(), key=lambda p: p[0])
        if target_date is not None:
            filtered = [p for p in pairs if p[0] <= target_date]
            if filtered:
                pairs = filtered
        if pairs:
            close_date, close = pairs[-1]
            prev = pairs[-2][1] if len(pairs) >= 2 else None
            if close_date == meta_date:
                source = "yahoo_meta"
            elif close_date in chart_dates:
                source = "yahoo_chart"
            else:
                source = "yfinance"
            market_state = meta_pt[2] if (source == "yahoo_meta" and meta_pt) else None
            yahoo_quote = Quote(
                close=close, prev=prev, date=close_date, source=source, market_state=market_state
            )

    # --- フォールバック: Yahoo が取得不能 or 直前営業日より古い時だけ CNBC で実値を取りに行く ---
    need_fallback = (yahoo_quote is None) or (
        target_date is not None and is_stale_close(yahoo_quote.date, target_date)
    )
    if need_fallback:
        cnbc = _cnbc_point(ticker)
        if cnbc is not None:
            cd, c_last, c_prev = cnbc
            if (target_date is None or cd <= target_date) and (
                yahoo_quote is None or cd > yahoo_quote.date
            ):
                return Quote(close=c_last, prev=c_prev, date=cd, source="cnbc", market_state=None)

    return yahoo_quote


# ---------------------------------------------------------------------------
# 場中の足（マクロ昼刊・2026-10-05 追加）
#
# 以下は追加のみ。上の既存関数（get_latest_close 等）の挙動・引数・戻り値は一切変えない。
# 昼刊は「11:30 の前場引け時点で、日経平均・日経先物・ドル円が朝の気配に対してどう動いたか」を
# 載せるため、日足ではなく場中の足（1 分足など）が要る。取得経路は既存と同じ Yahoo chart API を
# 主とし、Yahoo が落ちた時だけ yfinance（同じ Yahoo の別経路）を使う。
# 取得元の遅延（Yahoo の quoteSummary が返す exchangeDataDelayedBy・分）は別関数で取り、
# 呼び出し側が「最後の足の時刻」と並べて記録できるようにする（遅延の実測に使う）。
# ---------------------------------------------------------------------------

_JST = _timezone(_timedelta(hours=9))


@dataclass
class IntradayBar:
    """場中の足 1 本。ts は足の開始時刻（JST の aware datetime）。"""

    ts: _datetime
    open: float | None
    high: float | None
    low: float | None
    close: float


@dataclass
class IntradaySeries:
    """場中の足の系列と、取得時の付帯情報。

    bars        : 足の開始時刻の昇順。close が欠けた足は含めない
    interval    : 実際に取れた足の長さ（"1m" / "2m" / "5m"）
    fetched_at  : 取得した時刻（JST）
    source      : "yahoo_chart" / "yfinance"
    meta        : 取得元の付帯情報（exchangeName・previousClose・regularMarketTime(JST)・
                  regularMarketPrice・取引時間帯 等。取れたものだけ）
    """

    ticker: str
    interval: str
    bars: list
    fetched_at: _datetime
    source: str
    meta: dict


def _interval_minutes(interval: str) -> int:
    try:
        return int(interval.rstrip("m"))
    except ValueError:
        return 1


def _chart_intraday(ticker: str, start: _datetime, end: _datetime, interval: str, host: str):
    """Yahoo chart API を period1/period2 指定で叩き (bars, meta) を返す。失敗時 None。"""
    sym = _up.quote(ticker, safe="")
    p1 = int(start.timestamp())
    p2 = int(end.timestamp())
    url = (
        f"https://{host}/v8/finance/chart/{sym}"
        f"?period1={p1}&period2={p2}&interval={interval}&includePrePost=false"
    )
    try:
        req = _ur.Request(url, headers={"User-Agent": _UA})
        with _ur.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        res = data["chart"]["result"][0]
    except Exception:
        return None
    meta_raw = res.get("meta", {}) or {}
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens = q.get("open") or [None] * len(ts)
    highs = q.get("high") or [None] * len(ts)
    lows = q.get("low") or [None] * len(ts)
    closes = q.get("close") or [None] * len(ts)
    bars = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        bars.append(
            IntradayBar(
                ts=_datetime.fromtimestamp(int(t), tz=_JST),
                open=float(opens[i]) if i < len(opens) and opens[i] is not None else None,
                high=float(highs[i]) if i < len(highs) and highs[i] is not None else None,
                low=float(lows[i]) if i < len(lows) and lows[i] is not None else None,
                close=float(c),
            )
        )
    bars.sort(key=lambda b: b.ts)
    meta: dict = {}
    for k in (
        "exchangeName", "fullExchangeName", "instrumentType", "exchangeTimezoneName",
        "dataGranularity", "regularMarketPrice", "previousClose", "chartPreviousClose",
    ):
        if k in meta_raw:
            meta[k] = meta_raw[k]
    rmt = meta_raw.get("regularMarketTime")
    if rmt:
        try:
            meta["regularMarketTime_jst"] = _datetime.fromtimestamp(int(rmt), tz=_JST).isoformat()
        except Exception:
            pass
    reg = ((meta_raw.get("currentTradingPeriod") or {}).get("regular") or {})
    if reg.get("start") and reg.get("end"):
        try:
            meta["regular_period_jst"] = (
                _datetime.fromtimestamp(int(reg["start"]), tz=_JST).isoformat()
                + " -> "
                + _datetime.fromtimestamp(int(reg["end"]), tz=_JST).isoformat()
            )
        except Exception:
            pass
    return bars, meta


def _yf_intraday(ticker: str, start: _datetime, end: _datetime, interval: str):
    """yfinance で場中の足を取る（Yahoo chart API 直叩きが全滅した時だけ使う）。失敗時 None。"""
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(
            start=start, end=end, interval=interval, auto_adjust=False, prepost=False
        )
        if hist is None or hist.empty:
            return None
        hist = hist.dropna(subset=["Close"])
        bars = []
        for idx, row in hist.iterrows():
            ts = idx.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_timezone.utc)
            bars.append(
                IntradayBar(
                    ts=ts.astimezone(_JST),
                    open=float(row["Open"]) if row.get("Open") == row.get("Open") else None,
                    high=float(row["High"]) if row.get("High") == row.get("High") else None,
                    low=float(row["Low"]) if row.get("Low") == row.get("Low") else None,
                    close=float(row["Close"]),
                )
            )
        bars.sort(key=lambda b: b.ts)
        return bars, {}
    except Exception:
        return None


def get_intraday_bars(
    ticker: str,
    start: _datetime,
    end: _datetime,
    intervals: tuple = ("1m", "2m", "5m"),
    use_yfinance: bool = True,
) -> IntradaySeries | None:
    """ticker の場中の足を [start, end) で返す（start / end は aware datetime）。取得不能なら None。

    取得順: Yahoo chart API（query1 → query2）を intervals の短い足から順に試し、
    足が 1 本以上取れた最初の組み合わせを採用する。全滅した時だけ yfinance で同じ順に試す。
    足の時刻は足の開始時刻（JST）。end より後の足は返さない（as-of 再現で未来の足を混ぜない）。
    例外は内部で握りつぶす（呼び出し側レポートを 1 銘柄の失敗で止めない）。
    """
    fetched_at = _datetime.now(_JST)
    for interval in intervals:
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            got = _chart_intraday(ticker, start, end, interval, host)
            if got is None:
                continue
            bars, meta = got
            bars = [b for b in bars if start <= b.ts < end]
            if bars:
                return IntradaySeries(
                    ticker=ticker, interval=interval, bars=bars,
                    fetched_at=fetched_at, source="yahoo_chart", meta=meta,
                )
    if not use_yfinance:
        return None
    for interval in intervals:
        got = _yf_intraday(ticker, start, end, interval)
        if got is None:
            continue
        bars, meta = got
        bars = [b for b in bars if start <= b.ts < end]
        if bars:
            return IntradaySeries(
                ticker=ticker, interval=interval, bars=bars,
                fetched_at=fetched_at, source="yfinance", meta=meta,
            )
    return None


def get_exchange_delay_minutes(ticker: str) -> int | None:
    """取得元（Yahoo）が自ら申告する配信遅延（分）を返す。取れなければ None。

    yfinance の Ticker.info（Yahoo の quoteSummary）に含まれる exchangeDataDelayedBy を読む。
    chart API の meta にはこの項目が無い（2026-10-05 実測）ため、別経路で取る。
    """
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info or {}
        v = info.get("exchangeDataDelayedBy")
        return int(v) if v is not None else None
    except Exception:
        return None


def get_cnbc_session_quote(yahoo_ticker: str) -> dict | None:
    """CNBC quote API から当日の始値・高値・安値・現在値・最終時刻・リアルタイム区分を返す。

    Yahoo の場中の足が全滅した時の第二ソース（既存の _CNBC_MAP を使う・マップは変えない）。
    戻り値: {"open","high","low","last","prev","last_time"(ISO),"realtime"(bool|None)}。失敗時 None。
    CNBC は「今この瞬間」の値しか返さないため、過去時点の再現には使えない（呼び出し側で判定する）。
    """
    sym = _CNBC_MAP.get(yahoo_ticker)
    if not sym:
        return None
    base = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    q = _up.urlencode(
        {
            "symbols": sym, "requestMethod": "itv", "noform": "1", "partnerId": "2",
            "fund": "1", "exthrs": "1", "output": "json", "events": "1",
        }
    )
    try:
        req = _ur.Request(f"{base}?{q}", headers={"User-Agent": _UA})
        with _ur.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        quotes = (data.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
        if not quotes:
            return None
        d = quotes[0]
        rt = str(d.get("realTime", "")).lower()
        return {
            "open": _to_float(d.get("open")),
            "high": _to_float(d.get("high")),
            "low": _to_float(d.get("low")),
            "last": _to_float(d.get("last")),
            "prev": _to_float(d.get("previous_day_closing")),
            "last_time": d.get("last_time"),
            "realtime": True if rt == "true" else False if rt == "false" else None,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# マクロ昼刊の市況ブロック用（2026-10-06 追加・追加のみ・上の既存関数は変えない）
#
# 昼刊は朝刊・夕刊と同じ「市況スナップショット・市場別サマリー・セクター強弱」を
# 11:30 の前場引け時点の値で作る。そのために次の 3 つの取得口を足す。
#   get_daily_closes_window : 日足の (取引所ローカル日付, 終値) を期間指定で返す（前日比の基準・米国指標の直近終値）
#   get_spark_morning_closes: 個別株の場中の足を Yahoo spark API で 20 銘柄ずつ一括取得し、基準時刻までの最後の約定値を返す
#   get_tradingview_bars    : TradingView の公開チャート用 websocket（未ログイン＝遅延配信）から場中の足を返す
#                             （東証の市場別指数 TSE:I0500 等は Yahoo に無いため・2026-10-06 実測で取得可）
# ---------------------------------------------------------------------------


def get_daily_closes_window(ticker: str, start: _datetime, end: _datetime) -> list[tuple]:
    """Yahoo chart API の日足を [start, end] で取り、[(取引所ローカル日付, 終値)] を日付昇順で返す。

    日付は meta.gmtoffset（取引所の時差）で確定する。Yahoo は period2 より後でも当日の途中の足を
    返すことがある（2026-10-06 実測: BTC・ドル円）ため、呼び出し側が「基準日より前の日付」で絞ること。
    失敗時は []。
    """
    sym = _up.quote(ticker, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        f"?period1={int(start.timestamp())}&period2={int(end.timestamp())}&interval=1d"
    )
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            req = _ur.Request(url.replace("query1.finance.yahoo.com", host), headers={"User-Agent": _UA})
            with _ur.urlopen(req, timeout=20) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
            res = data["chart"]["result"][0]
            gmt = int((res.get("meta") or {}).get("gmtoffset") or 0)
            ex_tz = _timezone(_timedelta(seconds=gmt))
            ts = res.get("timestamp") or []
            closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
            pairs = [
                (_datetime.fromtimestamp(int(t), tz=ex_tz).date(), float(c))
                for t, c in zip(ts, closes)
                if c is not None
            ]
            pairs.sort(key=lambda p: p[0])
            if pairs:
                return pairs
        except Exception:
            continue
    return []


def get_spark_morning_closes(
    symbols: list,
    start: _datetime,
    asof: _datetime,
    interval: str = "5m",
    chunk: int = 20,
    workers: int = 4,
    retries: int = 3,
) -> tuple[dict, dict]:
    """Yahoo spark API で複数銘柄の場中の足を一括取得し、start〜asof（asof の足を含む）の最後の約定値を返す。

    spark は 1 回 20 銘柄まで（2026-10-06 実測: 50 銘柄で HTTP 400）。約定の無い足は close が null で
    返るため、null を除いた最後の足を「基準時刻までの最後の約定値」とする。期間内に約定が 1 本も無い
    銘柄は結果に含めない（朝刊の集計で終値の無い銘柄を除くのと同じ扱い）。

    戻り値: (closes, stats)
      closes: {symbol: (最後の約定値, その足の時刻 JST)}
      stats : {"requested", "returned", "with_price", "at_asof"(asof の足に約定がある銘柄数),
               "failed_chunks", "seconds"}
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    t0 = _time.time()
    syms = list(dict.fromkeys(symbols))
    chunks = [syms[i:i + chunk] for i in range(0, len(syms), chunk)]
    p1 = int(start.timestamp())
    p2 = int((asof + _timedelta(minutes=1)).timestamp())

    def _one(batch: list):
        q = _up.urlencode({"symbols": ",".join(batch), "period1": p1, "period2": p2, "interval": interval})
        for attempt in range(retries):
            for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
                try:
                    req = _ur.Request(f"https://{host}/v8/finance/spark?{q}", headers={"User-Agent": _UA})
                    with _ur.urlopen(req, timeout=30) as resp:
                        return _json.loads(resp.read().decode("utf-8", "replace"))
                except Exception:
                    continue
            _time.sleep(2 * (attempt + 1))
        return None

    closes: dict = {}
    returned = 0
    failed = 0
    at_asof = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for res in ex.map(_one, chunks):
            if not isinstance(res, dict):
                failed += 1
                continue
            for sym, v in res.items():
                if not isinstance(v, dict):
                    continue
                returned += 1
                ts = v.get("timestamp") or []
                cl = v.get("close") or []
                last = None
                for t, c in zip(ts, cl):
                    if c is None:
                        continue
                    dt = _datetime.fromtimestamp(int(t), tz=_JST)
                    if start <= dt <= asof:
                        if last is None or dt >= last[1]:
                            last = (float(c), dt)
                if last is not None:
                    closes[sym] = last
                    if last[1] >= asof:
                        at_asof += 1
    stats = {
        "requested": len(syms), "returned": returned, "with_price": len(closes), "at_asof": at_asof,
        "failed_chunks": failed, "chunks": len(chunks), "seconds": round(_time.time() - t0, 1),
    }
    return closes, stats


def get_tradingview_bars(symbol: str, resolution: str = "1", n_bars: int = 1000, timeout: float = 25.0):
    """TradingView の公開チャート用 websocket から足を取り (bars, meta) を返す。失敗時 None。

    未ログインの接続（unauthorized_user_token）は遅延配信で、東証の指数は meta["delay"]=1200 秒
    （20 分）と申告される（2026-10-06 実測）。bars は IntradayBar（ts は足の開始時刻 JST）の昇順。
    websocket-client（import websocket）が無い環境では None を返す。
    """
    try:
        import random as _random
        import string as _string

        import websocket as _websocket
    except Exception:
        return None

    def _frame(obj: dict) -> str:
        s = _json.dumps(obj, separators=(",", ":"))
        return f"~m~{len(s)}~m~{s}"

    def _split(raw: str) -> list:
        out, i = [], 0
        while raw.startswith("~m~", i):
            j = raw.index("~m~", i + 3)
            n = int(raw[i + 3:j])
            out.append(raw[j + 3:j + 3 + n])
            i = j + 3 + n
        return out

    import time as _time

    try:
        ws = _websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F&type=chart",
            header=["Origin: https://jp.tradingview.com"], timeout=timeout,
        )
    except Exception:
        return None
    bars: dict = {}
    meta: dict = {}
    ok = False
    try:
        cs = "cs_" + "".join(_random.choices(_string.ascii_lowercase, k=12))
        sym_json = _json.dumps({"symbol": symbol, "adjustment": "splits", "session": "regular"})
        for m, p in (
            ("set_auth_token", ["unauthorized_user_token"]),
            ("chart_create_session", [cs, ""]),
            ("resolve_symbol", [cs, "sds_sym_1", "=" + sym_json]),
            ("create_series", [cs, "sds_1", "s1", "sds_sym_1", resolution, n_bars, ""]),
        ):
            ws.send(_frame({"m": m, "p": p}))
        deadline = _time.time() + timeout
        while _time.time() < deadline and not ok:
            raw = ws.recv()
            for body in _split(raw):
                if body.startswith("~h~"):
                    ws.send(f"~m~{len(body)}~m~{body}")
                    continue
                try:
                    js = _json.loads(body)
                except ValueError:
                    continue
                m = js.get("m")
                if m == "symbol_resolved":
                    p = js["p"][2] or {}
                    meta = {k: p.get(k) for k in ("name", "description", "exchange", "timezone", "session", "delay", "type")}
                elif m in ("timescale_update", "du"):
                    for x in ((js["p"][1].get("sds_1") or {}).get("s") or []):
                        v = x.get("v") or []
                        if len(v) >= 5 and v[4] is not None:
                            bars[int(v[0])] = v
                elif m == "series_completed":
                    ok = True
                elif m in ("symbol_error", "series_error", "critical_error", "protocol_error"):
                    return None
    except Exception:
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    if not bars:
        return None
    out = [
        IntradayBar(
            ts=_datetime.fromtimestamp(t, tz=_JST),
            open=float(v[1]) if v[1] is not None else None,
            high=float(v[2]) if v[2] is not None else None,
            low=float(v[3]) if v[3] is not None else None,
            close=float(v[4]),
        )
        for t, v in sorted(bars.items())
    ]
    return out, meta
