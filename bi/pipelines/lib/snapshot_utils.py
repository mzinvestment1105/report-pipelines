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


def get_latest_close(ticker: str, target_date=None) -> Quote | None:
    """ticker の「当日終値・前日終値・取得日」を最も新しい確定値で返す。取得不能なら None。

    優先順:
      1. Yahoo chart API の日足 + meta.regularMarketPrice(遅延しない最新セッション確定値)
      2. chart API が全滅した場合のみ yfinance 日足(backup)
    target_date 指定時は target_date 以下の最新営業日を「当日」とする。
    例外は内部で握りつぶし None を返す(呼び出し側レポートを 1 銘柄の失敗で止めない)。
    """
    if isinstance(target_date, str):
        try:
            target_date = _date.fromisoformat(target_date)
        except ValueError:
            target_date = None

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
            merged[md] = mp  # meta は自セッションの確定値として最優先で採用
            meta_date = md

    if not merged:
        return None

    pairs = sorted(merged.items(), key=lambda p: p[0])
    if target_date is not None:
        filtered = [p for p in pairs if p[0] <= target_date]
        if filtered:
            pairs = filtered
    if not pairs:
        return None

    close_date, close = pairs[-1]
    prev = pairs[-2][1] if len(pairs) >= 2 else None

    if meta_date is not None and close_date == meta_date:
        source = "yahoo_meta"
    elif close_date in chart_dates:
        source = "yahoo_chart"
    else:
        source = "yfinance"
    market_state = meta_pt[2] if (meta_pt and close_date == meta_pt[0]) else None

    return Quote(close=close, prev=prev, date=close_date, source=source, market_state=market_state)
