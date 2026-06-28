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
