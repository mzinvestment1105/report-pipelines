"""
マクロ昼刊（前場引け時点）の生データを作るスクリプト（2026-10-05 新設・2026-10-06 作り直し）。

昼刊は朝刊・夕刊と同じ全 6 セクション構成の単独完結版で、数値だけを 11:30 の前場引け時点へ
差し替える。本スクリプトは朝刊・夕刊の生データ（generate_macro_report.py の出力）と同じブロック一式を、
前場引け時点の値で 1 本の raw（Claude が誌面を書くための材料）にまとめる。

  1. 市況スナップショット … 朝刊・夕刊と同じ 7 指標（VIX は誌面に出さないため作らない）。
       日経平均 = Yahoo の 11:30 の足（前引け）／日経先物・ドル円・金・BTC = 11:30 直前の足／
       S&P500・米10年債 = 11:30 時点で確定している直近の米国終値。前日比の基準は各指標の
       「対象日より前の最後の日足の終値」（朝刊・夕刊の get_latest_close と同じ基準日になる）。
  2. 市場別サマリー     … 朝刊・夕刊と同じ表。指数 = TradingView の 11:30 直前の足（東証プライム市場指数
       TSE:I0500・東証スタンダード市場指数 TSE:I0501・東証グロース市場250指数 TSE:MOS）。値上がり・値下がり・
       変わらず・中央値 = 個別株の前場の最後の約定値（Yahoo spark・5 分足）÷ 前営業日の終値（J-Quants）を
       screening_master の市場区分で集計（朝刊と同じ集計ロジック・ETF/REIT は結合で自然に除外）。
  3. セクター強弱       … 2 と同じ個別株の騰落率を generate_macro_report._build_sector_strength_block
       （朝刊・夕刊と同じ関数）で東証17業種へ単純平均し、見出しだけ「前場引け」にする。
  4. 日経225先物の数値関係 … 前営業日の現物引け・早朝・現物の取引開始直前・寄り付き・前場高値安値・前場引けの
       各時点の先物と日経平均・現物比（取引開始前の気配に対して実際にどう動いたかの材料）。
  5. ニュース           … 寄り付き前に作られた {date}_news_raw.md・{date}_finnhub_raw.md の全文（作成時刻が
       基準時刻以前のものだけ）と、前営業日 15:30〜基準時刻に配信された記事（RSS・yfinance・Finnhub・立花証券）。
  6. 直近の発行号の冒頭（背景参照用・朝刊・夕刊の raw の「前日レポート」と同じ扱い）。

J-Quants の前場四本値（/equities/bars/daily/am）と分足（/equities/bars/minute）は、現在の契約プランでは
HTTP 403（This API is not available on your subscription）のため使わない（2026-10-06 実測）。

使い方:
  python generate_macro_midday.py                                # 当日 11:30 時点（場中に実行）
  python generate_macro_midday.py --date 2026-10-05              # 過去日の再現（as-of）
  python generate_macro_midday.py --wait-until 12:05             # 11:30 の値が揃うまで待つ（GHA 用）
  python generate_macro_midday.py --date 2026-10-05 \\
      --news-raw scratchpad/.../morning_2026-10-05_news_raw.md --out scratchpad/.../raw.md

出力（既定）: market/daily/{date}_macro_midday_raw.md

exit codes:
  0  raw を書いた
  1  エラー（引数不正・書き込み失敗）
  2  対象日が東証の休場日のため作らない
  3  日経平均の前場の足が 1 本も取れなかった（誌面の中心の数値が無いため raw を書かない）
"""

from __future__ import annotations

import argparse
import math
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
MARKET_DIR = REPO_ROOT / "market" / "daily"
MACRO_DIR = MARKET_DIR / "macro"
ARCHIVE_MACRO_DIR = REPO_ROOT / "market" / "archive" / "macro"
JST = timezone(timedelta(hours=9))

sys.path.insert(0, str(SCRIPT_DIR))
from lib.snapshot_utils import (  # noqa: E402
    get_cnbc_session_quote,
    get_daily_closes_window,
    get_exchange_delay_minutes,
    get_intraday_bars,
    get_spark_morning_closes,
    get_tradingview_bars,
    previous_business_day,
)

EXIT_OK = 0
EXIT_ERR = 1
EXIT_HOLIDAY = 2
EXIT_NO_CASH = 3

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

# 市況スナップショット（朝刊・夕刊の generate_macro_report.SNAPSHOT_TICKERS と同じ並び・同じ名前。VIX 2 行は除く）
#   kind = "jp_cash": 東証の立会がある指数（asof の足＝前引けを含める）
#          "24h"    : ほぼ 24 時間値が付く（asof の足は asof より後の約定を含むため、その直前の足まで）
#          "us_cash": 米国の取引時間にしか値が付かない（11:30 時点で確定している直近の終値を使う）
SNAP = [
    {"name": "日経平均", "ticker": "^N225", "decimal": False, "kind": "jp_cash"},
    {"name": "日経先物", "ticker": "NIY=F", "decimal": False, "kind": "24h"},
    {"name": "S&P500", "ticker": "^GSPC", "decimal": False, "kind": "us_cash"},
    {"name": "ドル円", "ticker": "USDJPY=X", "decimal": True, "kind": "24h"},
    {"name": "金(Gold)", "ticker": "GC=F", "decimal": False, "kind": "24h"},
    {"name": "BTC", "ticker": "BTC-USD", "decimal": False, "kind": "24h"},
    {"name": "米10年債", "ticker": "^TNX", "decimal": True, "kind": "us_cash"},
]
# 前場の値動きの補足（30 分ごとの推移・5 分間の値動き）を出す 3 指標
MAIN_TICKERS = ["^N225", "NIY=F", "USDJPY=X"]

# 市場別サマリーの指数（J-Quants 指数コード → TradingView シンボル）。
# J-Quants のコードは generate_macro_report.SEGMENT_INDEX_CODES と同じ（0500 プライム／0501 スタンダード／0070 グロース250）。
TV_INDEX_SYMBOLS = {"0500": "TSE:I0500", "0501": "TSE:I0501", "0070": "TSE:MOS"}


# ---------------------------------------------------------------------------
# 時刻・数値の書式
# ---------------------------------------------------------------------------

def jp_time(dt: datetime | None, with_date: bool = False) -> str:
    """JST の aware datetime を和文12時間制「午前9時5分」へ。分が 0 なら「午前9時」。"""
    if dt is None:
        return "―"
    dt = dt.astimezone(JST)
    h, m = dt.hour, dt.minute
    ampm = "午前" if h < 12 else "午後"
    h12 = h if h < 12 else h - 12
    s = f"{ampm}{h12}時" + (f"{m}分" if m else "")
    if with_date:
        s = f"{dt.month}/{dt.day} " + s
    return s


def md_label(d: date) -> str:
    return f"{d.month}/{d.day}（{WEEKDAY_JA[d.weekday()]}）"


def fmt_level(v: float | None, decimal: bool) -> str:
    if v is None:
        return "―"
    return f"{v:,.2f}" if decimal else f"{v:,.0f}"


def fmt_change(v: float | None, base: float | None, decimal: bool) -> str:
    """§21-A の前日比書式（指数等は「±整数 / ±%小数1桁」・為替金利は「±0.00 / ±0.00%」）。"""
    if v is None or base in (None, 0):
        return "―"
    d = v - base
    p = d / base * 100
    if decimal:
        return f"{d:+,.2f} / {p:+.2f}%"
    return f"{d:+,.0f} / {p:+.1f}%"


def _us_close_jst(d: date) -> datetime:
    """米国の取引日 d の引け（ニューヨーク 16:00）を JST で返す（米国の夏時間を考慮）。"""
    def nth_sunday(y: int, m: int, n: int) -> date:
        first = date(y, m, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))

    dst = nth_sunday(d.year, 3, 2) <= d < nth_sunday(d.year, 11, 1)
    offset = -4 if dst else -5
    ny = datetime(d.year, d.month, d.day, 16, 0, tzinfo=timezone(timedelta(hours=offset)))
    return ny.astimezone(JST)


# ---------------------------------------------------------------------------
# 場中の足
# ---------------------------------------------------------------------------

def session_bars(series, cash: bool, open_dt: datetime, asof: datetime) -> list:
    """前場の時間帯の足。立会のある指数は asof の足を含め、24 時間系は asof の直前の足までにする。"""
    if series is None:
        return []
    if cash:
        return [b for b in series.bars if open_dt <= b.ts <= asof]
    return [b for b in series.bars if open_dt <= b.ts < asof]


def summarize(bars: list) -> dict:
    """寄り付き・高値・安値（時刻つき）・前場引け・最後の足の時刻。"""
    if not bars:
        return {}
    first = bars[0]
    hi_v, hi_t, lo_v, lo_t = -math.inf, None, math.inf, None
    for b in bars:
        h = b.high if b.high is not None else b.close
        lo = b.low if b.low is not None else b.close
        if h > hi_v:
            hi_v, hi_t = h, b.ts
        if lo < lo_v:
            lo_v, lo_t = lo, b.ts
    open_v = first.open if first.open is not None else first.close
    if open_v > hi_v:
        hi_v, hi_t = open_v, first.ts
    if open_v < lo_v:
        lo_v, lo_t = open_v, first.ts
    return {
        "open": open_v, "open_ts": first.ts,
        "high": hi_v, "high_ts": hi_t,
        "low": lo_v, "low_ts": lo_t,
        "last": bars[-1].close, "last_ts": bars[-1].ts,
    }


def last_bar_at_or_before(series, t: datetime, after: datetime | None = None):
    """t 以前（t を含む）で最後の足。after を渡すとそれより後の足に限る。"""
    if series is None:
        return None
    bars = [b for b in series.bars if b.ts <= t and (after is None or b.ts > after)]
    return bars[-1] if bars else None


def value_before(series, t: datetime) -> float | None:
    """t より前（t を含まない）で最後の足の終値。"""
    if series is None:
        return None
    prev = [b for b in series.bars if b.ts < t]
    return prev[-1].close if prev else None


def top_moves(bars: list, minutes: int = 5, n: int = 5) -> list[tuple[datetime, datetime, float, float]]:
    """minutes 分ごとの区切りで終値の変化が大きかった区間（開始・終了・変化幅・変化率%）。"""
    if len(bars) < 2:
        return []
    buckets: dict[datetime, float] = {}
    for b in bars:
        key = b.ts.replace(minute=(b.ts.minute // minutes) * minutes, second=0, microsecond=0)
        buckets[key] = b.close
    keys = sorted(buckets)
    out = []
    prev_close = bars[0].open if bars[0].open is not None else bars[0].close
    for k in keys:
        c = buckets[k]
        d = c - prev_close
        out.append((k, k + timedelta(minutes=minutes), d, d / prev_close * 100 if prev_close else 0.0))
        prev_close = c
    out.sort(key=lambda x: abs(x[2]), reverse=True)
    return out[:n]


def fetch_intraday(target: date, asof: datetime, start: datetime) -> dict:
    """スナップショットのうち場中に値が付く指標の足（1 分足を優先）。"""
    end = asof + timedelta(minutes=1)
    out = {}
    for spec in SNAP:
        if spec["kind"] == "us_cash":
            continue
        out[spec["ticker"]] = get_intraday_bars(spec["ticker"], start, end)
    return out


# ---------------------------------------------------------------------------
# 市場別サマリー・セクター強弱（朝刊・夕刊と同じ集計を前場引けの値で）
# ---------------------------------------------------------------------------

def jquants_client():
    try:
        import jquantsapi
        from dotenv import load_dotenv

        load_dotenv(SCRIPT_DIR / ".env")
        key = os.environ.get("JQUANTS_API_KEY", "").strip()
        if not key:
            print("[WARN] J-Quants: JQUANTS_API_KEY 未設定", file=sys.stderr)
            return None
        return jquantsapi.ClientV2(api_key=key)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] J-Quants: クライアントを作れません: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def jq_business_days(client, target: date) -> list[date] | None:
    """J-Quants の営業日カレンダー（HolDiv=1）で target の前 40 日〜target の営業日を昇順で返す。"""
    try:
        from jq_client_utils import _calendar_business_days_v2

        return _calendar_business_days_v2(client, date_from=target - timedelta(days=40), date_to=target)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] J-Quants: 営業日カレンダーを取れません: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def jq_prev_closes(client, d: date) -> tuple[dict, dict]:
    """前営業日 d の確定値。({Code先頭4桁: 終値}, {指数コード: 終値})。朝刊の _equity_closes と同じ規則。"""
    from generate_macro_report import SEGMENT_INDEX_CODES
    from jq_client_utils import fetch_paginated_v2

    eq: dict[str, float] = {}
    for r in fetch_paginated_v2(client, "/equities/bars/daily", params={"date": d.strftime("%Y-%m-%d")}):
        if str(r.get("Date", ""))[:10] != d.isoformat():
            continue
        c = r.get("C")
        code5 = str(r.get("Code", ""))
        if c is None or not code5.endswith("0"):  # 優先株・種類株（5 桁目が 0 以外）は除く（朝刊と同じ）
            continue
        eq[code5[:4]] = float(c)
    idx: dict[str, float] = {}
    for r in fetch_paginated_v2(client, "/indices/bars/daily", params={"date": d.strftime("%Y-%m-%d")}):
        if str(r.get("Date", ""))[:10] != d.isoformat():
            continue
        code = str(r.get("Code", ""))
        if code in SEGMENT_INDEX_CODES.values() and r.get("C") is not None:
            idx[code] = float(r["C"])
    return eq, idx


def tv_index_morning(target: date, open_dt: datetime, asof: datetime) -> tuple[dict, dict]:
    """東証の市場別指数の前場の最後の足（1 分足・asof より前）を TradingView から取る。

    戻り値: ({指数コード: (値, 足の時刻)}, {指数コード: 取得情報の文字列})
    """
    days_back = max(1, (datetime.now(JST).date() - target).days + 1)
    n_bars = min(5000, 340 * days_back + 200)
    out, info = {}, {}
    for code, sym in TV_INDEX_SYMBOLS.items():
        got = get_tradingview_bars(sym, "1", n_bars)
        if got is None:
            info[code] = f"{sym}: 取得できず"
            continue
        bars, meta = got
        mb = [b for b in bars if open_dt <= b.ts < asof]
        if not mb:
            info[code] = f"{sym}: 前場の足なし（取得 {len(bars)} 本・遅延申告 {meta.get('delay')} 秒）"
            continue
        out[code] = (mb[-1].close, mb[-1].ts)
        info[code] = f"{sym}: 前場の最後の足 {mb[-1].ts.strftime('%H:%M')}（1 分足）・遅延申告 {meta.get('delay')} 秒"
    return out, info


def build_segment_blocks(target: date, morning: dict, prev_eq: dict, idx_now: dict, prev_idx: dict) -> tuple[str | None, str | None]:
    """朝刊・夕刊の get_market_segment_summary と同じ集計で (市場別サマリー, セクター強弱) の md を返す。

    morning : {Code先頭4桁: 前場の最後の約定値}
    prev_eq : {Code先頭4桁: 前営業日終値}（J-Quants）
    idx_now : {指数コード: 前場の最後の値}（TradingView）
    prev_idx: {指数コード: 前営業日終値}（J-Quants）
    """
    import pandas as pd

    import generate_macro_report as gmr

    sm = pd.read_parquet(gmr.SCREENING_MASTER_PATH, columns=["Code", "MarketCodeName", "Sector17CodeName"])
    label = f"{target.month}/{target.day} 前場引け"

    sector_block = gmr._build_sector_strength_block(morning, prev_eq, sm, target)
    if sector_block:
        sector_block = sector_block.replace(f"{target.month}/{target.day} 終値）", f"{label}）", 1)

    rows_out: list[str] = []
    for mkt, idx_code in gmr.SEGMENT_INDEX_CODES.items():
        codes = sm.loc[sm["MarketCodeName"] == mkt, "Code"].astype(str)
        changes: list[float] = []
        for c4 in codes:
            t_close, p_close = morning.get(c4), prev_eq.get(c4)
            if t_close is not None and p_close:
                changes.append((t_close / p_close - 1.0) * 100.0)
        it, ip = idx_now.get(idx_code), prev_idx.get(idx_code)
        if not changes or it is None or ip is None or ip == 0:
            print(f"[WARN] 市場別サマリー: {mkt} のデータ欠落のため行を省略します", file=sys.stderr)
            continue
        ups = [x for x in changes if x > 0]
        downs = [x for x in changes if x < 0]
        flat = len(changes) - len(ups) - len(downs)
        med_up = f"{statistics.median(ups):+.2f}%" if ups else "─"
        med_dn = f"{statistics.median(downs):+.2f}%" if downs else "─"
        chg_txt = f"{it - ip:+,.2f}pt / {(it / ip - 1.0) * 100.0:+.2f}%"
        rows_out.append(
            f"| {mkt} | {it:,.2f} | {chg_txt} | {len(ups)} | {len(downs)} | {flat} "
            f"| {med_up} | {med_dn} |"
        )
    segment_block = None
    if rows_out:
        segment_block = "\n".join(
            [
                f"### 市場別サマリー（{label}）",
                "",
                "| 市場 | 指数 | 前日比 | 値上がり | 値下がり | 変わらず | 上昇側中央値 | 下落側中央値 |",
                "|------|------|--------|--------|--------|--------|--------------|--------------|",
                *rows_out,
            ]
        )
    return segment_block, sector_block


# ---------------------------------------------------------------------------
# ニュース
# ---------------------------------------------------------------------------

def _rss_items(window_start: datetime, asof: datetime) -> list[dict]:
    """fetch_rss.py の rss_config 全フィードを同じ関数で取得し、配信時刻つきで返す。"""
    try:
        import yaml

        import fetch_rss
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] RSS: fetch_rss を読み込めません: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    try:
        config = yaml.safe_load(fetch_rss.CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] RSS: 設定を読めません: {e}", file=sys.stderr)
        return []
    days = max(1, math.ceil((datetime.now(timezone.utc) - window_start).total_seconds() / 86400) + 1)
    out = []
    for feed_name, cfg in (config.get("feeds") or {}).items():
        for it in fetch_rss.fetch_feed(cfg.get("url", ""), days):
            dt = it.get("sort_dt")
            if dt is None:
                continue
            dt = dt.astimezone(JST)
            if window_start < dt <= asof:
                out.append({"dt": dt, "src": feed_name, "title": it.get("title", ""),
                            "summary": it.get("summary", ""), "url": it.get("link", "")})
    return out


def _yf_news_items(window_start: datetime, asof: datetime) -> list[dict]:
    """fetch_rss.MACRO_TICKERS と同じティッカーの yfinance ニュース（配信時刻を分まで残す）。"""
    try:
        import yfinance as yf

        import fetch_rss
        tickers = fetch_rss.MACRO_TICKERS
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] yfinance ニュース: 読み込み失敗: {e}", file=sys.stderr)
        return []
    out, seen = [], set()
    for label, tk in tickers.items():
        try:
            news = yf.Ticker(tk).news or []
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] yfinance ニュース {tk}: {e}", file=sys.stderr)
            continue
        for n in news:
            c = n.get("content", {}) or {}
            title = (c.get("title") or "").strip()
            pub = c.get("pubDate") or ""
            url = ((c.get("canonicalUrl") or {}).get("url")) or ""
            if not title or not pub or url in seen:
                continue
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(JST)
            except ValueError:
                continue
            if window_start < dt <= asof:
                seen.add(url)
                prov = ((c.get("provider") or {}).get("displayName")) or ""
                out.append({"dt": dt, "src": f"{label}{('・' + prov) if prov else ''}", "title": title,
                            "summary": (c.get("summary") or "").replace("\xa0", " ").strip()[:200], "url": url})
    return out


def _finnhub_items(window_start: datetime, asof: datetime) -> list[dict]:
    """fetch_finnhub.py と同じ API・同じカテゴリから窓内の記事を返す。"""
    try:
        from dotenv import load_dotenv

        import fetch_finnhub
        load_dotenv(SCRIPT_DIR / ".env")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Finnhub: 読み込み失敗: {e}", file=sys.stderr)
        return []
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        print("[WARN] Finnhub: FINNHUB_API_KEY 未設定のため省略します", file=sys.stderr)
        return []
    out, seen = [], set()
    for cat, label in fetch_finnhub.NEWS_CATEGORIES:
        data = fetch_finnhub.get("/news", {"category": cat}, key)
        if not isinstance(data, list):
            continue
        for item in data:
            ts = item.get("datetime") or 0
            url = item.get("url", "")
            if not ts or url in seen:
                continue
            dt = datetime.fromtimestamp(int(ts), tz=JST)
            if window_start < dt <= asof:
                seen.add(url)
                out.append({"dt": dt, "src": f"Finnhub {label}・{item.get('source', '')}",
                            "title": (item.get("headline") or "").strip(),
                            "summary": (item.get("summary") or "").strip()[:200], "url": url})
        time.sleep(0.5)
    return out


def _tachibana_items(path: Path, window_start: datetime, asof: datetime) -> list[dict]:
    """fetch_tachibana_news.py の出力 raw（行頭「- **{日付} {時刻}**」）から窓内の行を返す。"""
    if not path.exists():
        return []
    out = []
    section = ""
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("### "):
            section = re.sub(r"\s*\(\d+ 件\)\s*$", "", ln[4:]).strip()
            continue
        m = re.match(r"^- \*\*(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})\s+(\d{1,2}):?(\d{2}):?(\d{2})?\*\*\s*(.*)$", ln)
        if not m:
            continue
        y, mo, d, hh, mm = (int(m.group(i)) for i in range(1, 6))
        try:
            dt = datetime(y, mo, d, hh, mm, tzinfo=JST)
        except ValueError:
            continue
        if window_start < dt <= asof:
            out.append({"dt": dt, "src": f"立花証券・{section}", "title": m.group(7).strip(), "summary": "", "url": ""})
    return out


def premarket_file(path: Path, target: date, asof: datetime, pattern: str) -> tuple[str | None, datetime | None]:
    """寄り付き前に作られた raw（news_raw・finnhub_raw）を、作成時刻が対象日の基準時刻以前の時だけ返す。"""
    if not path.exists():
        return None, None
    txt = path.read_text(encoding="utf-8")
    m = re.search(pattern, txt)
    if not m:
        return None, None
    made = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    if made.date() != target or made > asof:
        print(f"[WARN] {path.name} の作成時刻 {made:%Y-%m-%d %H:%M} が対象日の基準時刻以前ではないため使いません",
              file=sys.stderr)
        return None, None
    return txt, made


def prev_report_preview(target: date) -> tuple[str | None, str]:
    """前日（暦日）の最新の発行号の冒頭 2,500 字（H1 を除く）。朝刊・夕刊の raw の「前日レポート」と同じ扱い。"""
    d = target - timedelta(days=1)
    for base in (MACRO_DIR, ARCHIVE_MACRO_DIR):
        for name in (f"{d.isoformat()}_evening.md", f"{d.isoformat()}.md"):
            p = base / name
            if p.exists():
                body = p.read_text(encoding="utf-8").lstrip()
                if body.startswith("# "):
                    nl = body.find("\n")
                    body = body[nl + 1:].lstrip() if nl != -1 else ""
                return body[:2500].rstrip(), name
    return None, ""


# ---------------------------------------------------------------------------
# raw の組み立て
# ---------------------------------------------------------------------------

def build_raw(target: date, asof: datetime, prev_bd: date, live: bool, series: dict, daily: dict,
              polls: list[str], delays: dict, seg: dict, news: dict, prev_rep: tuple[str | None, str]) -> tuple[str, bool]:
    open_dt = datetime(target.year, target.month, target.day, 9, 0, tzinfo=JST)
    now = datetime.now(JST)
    wd = WEEKDAY_JA[target.weekday()]
    L: list[str] = []
    L.append(f"# マクロ昼刊 生データ（{target.isoformat()}・前場引け時点）")
    L.append("")
    L.append(f"- 対象日: {target.isoformat()}（{wd}）")
    L.append(f"- 基準時刻: {target.month}/{target.day} {jp_time(asof)}（前場引け）")
    mode = "場中の取得" if live else "過去時点の再現（基準時刻より後に取得し、基準時刻以前の足と記事だけを使用）"
    L.append(f"- 取得時刻: {now.strftime('%Y-%m-%d %H:%M:%S')}（{mode}）")
    L.append(f"- 前営業日: {prev_bd.isoformat()}（{WEEKDAY_JA[prev_bd.weekday()]}）")
    L.append("")

    stats = {}
    for spec in SNAP:
        if spec["kind"] == "us_cash":
            continue
        stats[spec["ticker"]] = summarize(session_bars(series.get(spec["ticker"]), spec["kind"] == "jp_cash", open_dt, asof))
    cash = stats.get("^N225") or {}
    fut = stats.get("NIY=F") or {}
    cash_closed = bool(cash) and cash["last_ts"] >= asof

    # 前日比の基準（対象日より前の最後の日足）と米国指標の直近終値
    def ref_pairs(ticker: str) -> list[tuple]:
        return [p for p in daily.get(ticker, []) if p[0] < target]

    # ---- 取得状況 ----
    L.append("## 取得状況（足の最終時刻・取得元の遅延申告）")
    L.append("")
    L.append("| 指標 | 足 | 前場の最後の足 | 取得元の最新足 | 遅延申告 | 取得元 |")
    L.append("|---|---|---|---|---|---|")
    for spec in SNAP:
        if spec["kind"] == "us_cash":
            rp = ref_pairs(spec["ticker"])
            L.append(f"| {spec['name']}（{spec['ticker']}） | 日足 | 米国の取引時間外 | "
                     f"{rp[-1][0].isoformat() if rp else '―'} | ― | yahoo_chart |")
            continue
        s = series.get(spec["ticker"])
        st = stats.get(spec["ticker"]) or {}
        if s is None:
            L.append(f"| {spec['name']}（{spec['ticker']}） | ― | 取得できず | ― | ― | ― |")
            continue
        latest = s.bars[-1].ts if s.bars else None
        dly = delays.get(spec["ticker"])
        L.append(
            f"| {spec['name']}（{spec['ticker']}） | {s.interval} | "
            f"{st.get('last_ts').strftime('%H:%M') if st else '前場の足なし'} | "
            f"{latest.strftime('%m/%d %H:%M') if latest else '―'} | "
            f"{(str(dly) + '分') if dly is not None else '―'} | {s.source} |"
        )
    L.append("")
    if polls:
        L.append("取得の試行（時刻 → 日経平均の最後の足）: " + " / ".join(polls))
        L.append("")
    if cash_closed:
        L.append(f"- 前場引けの足: 取得済み（日経平均の {jp_time(cash['last_ts'])} の足）")
    elif cash:
        L.append(f"- 前場引けの足: 未着（日経平均の最後の足は {jp_time(cash['last_ts'])}）。"
                 f"誌面の「前場引け」は {jp_time(cash['last_ts'])} 時点の値として扱うこと")
    for line in seg.get("status", []):
        L.append(f"- {line}")
    L.append("")

    # ---- 市況スナップショット ----
    L.append("## 本日の市況スナップショット（前場引け時点）")
    L.append("| 指標 | 水準 | 前日比 | 取得日 / 備考 |")
    L.append("|------|------|--------|------|")
    for spec in SNAP:
        tk, dec = spec["ticker"], spec["decimal"]
        rp = ref_pairs(tk)
        if spec["kind"] == "us_cash":
            if not rp:
                continue
            d0, lvl = rp[-1]
            prev = rp[-2][1] if len(rp) >= 2 else None
            cj = _us_close_jst(d0)
            L.append(f"| {spec['name']} | {fmt_level(lvl, dec)} | {fmt_change(lvl, prev, dec)} | "
                     f"close={d0.isoformat()}（米国の取引日・日本時間 {jp_time(cj, with_date=True)}引け）/ src=yahoo_chart |")
            continue
        st = stats.get(tk) or {}
        if not st:
            continue
        lvl = st["last"]
        if tk == "NIY=F":
            cv = cash.get("last")
            if cv:
                chg = f"{lvl - cv:+,.0f} / {(lvl / cv - 1) * 100:+.1f}%（現物比）"
            else:
                chg = "─"
            L.append(f"| {spec['name']} | {fmt_level(lvl, dec)} | {chg} | "
                     f"{target.isoformat()} {st['last_ts'].strftime('%H:%M')} の足の終値（前場引けの直前）"
                     f"・現物比の基準=日経平均の {jp_time(cash.get('last_ts'))} の値 / src={series[tk].source} {series[tk].interval} |")
            continue
        prev_d, prev = (rp[-1] if rp else (None, None))
        when = (f"{target.isoformat()} {st['last_ts'].strftime('%H:%M')} の足の終値（前場引け）" if spec["kind"] == "jp_cash"
                else f"{target.isoformat()} {st['last_ts'].strftime('%H:%M')} の足の終値（前場引けの直前）")
        base_txt = f"前日比の基準={prev_d.isoformat()} の終値" if prev_d else "前日比の基準なし"
        L.append(f"| {spec['name']} | {fmt_level(lvl, dec)} | {fmt_change(lvl, prev, dec)} | "
                 f"{when}・{base_txt} / src={series[tk].source} {series[tk].interval} |")
    L.append("")
    if seg.get("segment"):
        L.append(seg["segment"])
        L.append("")
    if seg.get("sector"):
        L.append(seg["sector"])
        L.append("")

    # ---- 日経225先物の数値関係 ----
    prev_cash = ref_pairs("^N225")[-1][1] if ref_pairs("^N225") else None
    fs = series.get("NIY=F")
    pc_dt = datetime(prev_bd.year, prev_bd.month, prev_bd.day, 15, 30, tzinfo=JST)
    early = datetime(target.year, target.month, target.day, 6, 30, tzinfo=JST)
    pts = []
    b = last_bar_at_or_before(fs, pc_dt)
    if b:
        pts.append((f"前営業日の現物の引け（{jp_time(b.ts, with_date=True)}）", b.close, prev_cash, "前営業日終値"))
    b = last_bar_at_or_before(fs, early, after=pc_dt)
    if b:
        pts.append((f"早朝（{jp_time(b.ts, with_date=True)} の足）", b.close, prev_cash, "前営業日終値"))
    b = last_bar_at_or_before(fs, open_dt - timedelta(seconds=1), after=pc_dt)
    if b:
        pts.append((f"現物の取引開始直前（{jp_time(b.ts, with_date=True)} の足）", b.close, prev_cash, "前営業日終値"))
    if fut and cash:
        pts.append((f"寄り付き（{jp_time(open_dt)}）", fut.get("open"), cash.get("open"), "日経平均の寄り付き"))
        pts.append((f"前場引け（日経平均 {jp_time(cash['last_ts'])}・先物 {jp_time(fut['last_ts'])} の足）",
                    fut.get("last"), cash.get("last"), "日経平均の前場引け"))
    L.append("## 日経225先物の数値関係（材料・各時点の先物と日経平均）")
    L.append("")
    L.append("| 時点 | 日経先物 | 日経平均 | 現物比 | 現物比の基準 |")
    L.append("|------|------|------|------|------|")
    for label, fv, cv, basis in pts:
        L.append(f"| {label} | {fmt_level(fv, False)} | {fmt_level(cv, False)} | {fmt_change(fv, cv, False)} | {basis} |")
    L.append("")
    if cash and prev_cash:
        L.append("日経平均の前場（前営業日終値 "
                 f"{fmt_level(prev_cash, False)} 比）: 寄り付き {fmt_level(cash['open'], False)}（{fmt_change(cash['open'], prev_cash, False)}）"
                 f" ／ 高値 {fmt_level(cash['high'], False)}（{jp_time(cash['high_ts'])}・{fmt_change(cash['high'], prev_cash, False)}）"
                 f" ／ 安値 {fmt_level(cash['low'], False)}（{jp_time(cash['low_ts'])}・{fmt_change(cash['low'], prev_cash, False)}）"
                 f" ／ 前場引け {fmt_level(cash['last'], False)}（{fmt_change(cash['last'], prev_cash, False)}）")
    if fut:
        L.append(f"日経先物の前場: 寄り付き（{jp_time(fut['open_ts'])}の足の始値） {fmt_level(fut['open'], False)} ／ "
                 f"高値 {fmt_level(fut['high'], False)}（{jp_time(fut['high_ts'])}） ／ 安値 {fmt_level(fut['low'], False)}"
                 f"（{jp_time(fut['low_ts'])}） ／ 前場引けの直前 {fmt_level(fut['last'], False)}（{jp_time(fut['last_ts'])}の足）")
    L.append("先物は CME の円建て日経225先物（ほぼ 24 時間取引）。日経平均の前日比は前営業日終値が基準、先物の現物比は同じ時点の日経平均が基準で、基準が異なる。")
    L.append("")

    # ---- 前場の値動き（補足）----
    L.append("## 前場の値動き（補足データ・誌面に表として転記しない・本文の根拠用）")
    L.append("")
    L.append("### 30分ごとの推移")
    L.append("")
    L.append("| 時刻 | 日経平均 | 日経先物 | ドル円 |")
    L.append("|------|------|------|------|")
    checkpoints = [open_dt + timedelta(minutes=30 * i) for i in range(0, 6)] + [asof]
    seen_cp = set()
    for cp in checkpoints:
        if cp > asof or cp in seen_cp:
            continue
        seen_cp.add(cp)
        cells = []
        for tk in MAIN_TICKERS:
            st = stats.get(tk) or {}
            dec = tk == "USDJPY=X"
            if cp == open_dt:
                v = st.get("open")
            elif cp == asof:
                v = st.get("last")
            else:
                v = value_before(series.get(tk), cp)
            cells.append(fmt_level(v, dec))
        L.append(f"| {jp_time(cp)} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("### 5分間の値動きが大きかった時間帯（上位5件・前場）")
    L.append("")
    for tk, name in zip(MAIN_TICKERS, ("日経平均", "日経先物", "ドル円")):
        bars = session_bars(series.get(tk), tk == "^N225", open_dt, asof)
        mv = top_moves(bars)
        if not mv:
            continue
        dec = tk == "USDJPY=X"
        parts = []
        for a, b2, d, p in mv:
            dtxt = f"{d:+,.2f}" if dec else f"{d:+,.0f}"
            parts.append(f"{jp_time(a)}〜{jp_time(b2)} {dtxt}（{p:+.2f}%）")
        L.append(f"- {name}: " + " ／ ".join(parts))
    usd = stats.get("USDJPY=X") or {}
    if usd:
        L.append(f"- ドル円の前場: 寄り付き {fmt_level(usd['open'], True)} ／ 高値 {fmt_level(usd['high'], True)}（{jp_time(usd['high_ts'])}）"
                 f" ／ 安値 {fmt_level(usd['low'], True)}（{jp_time(usd['low_ts'])}） ／ 前場引けの直前 {fmt_level(usd['last'], True)}")
    L.append("")

    # ---- 直近の発行号（背景参照用）----
    prev_txt, prev_name = prev_rep
    if prev_txt:
        L.append("---")
        L.append(f"## 直近の発行号の冒頭（{prev_name}・背景参照用）")
        L.append("前日までの経緯を把握するためだけに使う。誌面でこの号に言及しない・数値を本日の値として使わない。")
        L.append("")
        L.append(prev_txt)
        L.append("---")
        L.append("")

    # ---- ニュース ----
    fh_txt, fh_made = news.get("finnhub_raw") or (None, None)
    if fh_txt:
        L.append(f"## グローバルニュース・経済カレンダー（Finnhub・{jp_time(fh_made, with_date=True)}取得）")
        L.append("英語のニュース見出し・要約は内容を理解した上で日本語で分析に反映する。")
        L.append("")
        L.append(fh_txt.rstrip())
        L.append("")
    nr_txt, nr_made = news.get("news_raw") or (None, None)
    if nr_txt:
        L.append(f"## 本日のニュース生データ（{target.isoformat()}_news_raw.md 全文・{jp_time(nr_made, with_date=True)}作成）")
        L.append("")
        L.append(nr_txt.rstrip())
        L.append("")
    window_start = news.get("_window_start")
    L.append("## 前営業日の引け後〜前場のニュース（基準時刻以前に配信されたもの・時刻つき・新しい順・上の 2 つと重複する記事は除く）")
    L.append("")
    if window_start:
        L.append(f"窓: {jp_time(window_start, with_date=True)} 〜 {jp_time(asof, with_date=True)}")
        L.append("")
    for key, title in (("finnhub", "Finnhub（海外ニュース・英語）"), ("yf", "yfinance ニュース（株価指数・為替・英語）"),
                       ("rss", "RSS（日銀・大和総研・Yahoo ビジネス）"), ("tachibana", "立花証券 e支店 API（QUICK NQN 等）")):
        items = news.get(key)
        if items is None:
            continue
        L.append(f"### {title}（{len(items)} 件）")
        L.append("")
        if not items:
            L.append("（窓内の新しい記事なし）")
            L.append("")
            continue
        for it in sorted(items, key=lambda x: x["dt"], reverse=True):
            L.append(f"- [{jp_time(it['dt'], with_date=True)}] {it['title']} — {it['src']}")
            if it.get("summary"):
                L.append(f"  > {it['summary']}")
            if it.get("url"):
                L.append(f"  {it['url']}")
        L.append("")
    return "\n".join(L).rstrip() + "\n", cash_closed


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def _hm(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m:
        raise ValueError(f"時刻は HH:MM で指定してください: {s}")
    return int(m.group(1)), int(m.group(2))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="マクロ昼刊（前場引け時点）の生データを作る")
    ap.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"), help="対象日 YYYY-MM-DD（JST）")
    ap.add_argument("--asof", default="11:30", help="基準時刻 HH:MM（JST・既定 11:30＝前場引け）")
    ap.add_argument("--wait-until", default="", help="11:30 の値が揃うまで待つ上限 HH:MM（JST）")
    ap.add_argument("--poll-sec", type=int, default=60, help="待つ間の再取得間隔（秒）")
    ap.add_argument("--news-raw", default="", help="寄り付き前の news raw（既定 market/daily/{date}_news_raw.md）")
    ap.add_argument("--finnhub-raw", default="", help="寄り付き前の Finnhub raw（既定 market/daily/{date}_finnhub_raw.md）")
    ap.add_argument("--tachibana-raw", default="", help="立花証券ニュース raw（既定 market/daily/{date}_tachibana_news_raw.md）")
    ap.add_argument("--out", default="", help="出力先（既定 market/daily/{date}_macro_midday_raw.md）")
    ap.add_argument("--no-news", action="store_true", help="ニュースを取得しない")
    ap.add_argument("--no-delay-meta", action="store_true", help="取得元の遅延申告（yfinance info）を取らない")
    args = ap.parse_args()

    try:
        target = date.fromisoformat(args.date)
        ah, am = _hm(args.asof)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return EXIT_ERR
    asof = datetime(target.year, target.month, target.day, ah, am, tzinfo=JST)
    open_dt = datetime(target.year, target.month, target.day, 9, 0, tzinfo=JST)
    t_start = time.time()

    # ---- 休場日の判定（J-Quants の営業日カレンダー → 取れなければ土日・jpholiday）----
    client = jquants_client()
    biz = jq_business_days(client, target) if client else None
    if biz:
        if target not in biz:
            print(f"[SKIP] {target} は東証の休場日です（J-Quants 営業日カレンダー）")
            return EXIT_HOLIDAY
        prev_bd = [d for d in biz if d < target][-1]
    else:
        try:
            import jpholiday

            holiday = bool(jpholiday.is_holiday(target))
        except Exception:  # noqa: BLE001
            holiday = False
        if target.weekday() >= 5 or holiday:
            print(f"[SKIP] {target} は東証の休場日です（土日・祝日）")
            return EXIT_HOLIDAY
        prev_bd = previous_business_day(target)

    now = datetime.now(JST)
    live = now < asof + timedelta(minutes=90)
    deadline = None
    if args.wait_until:
        wh, wm = _hm(args.wait_until)
        deadline = datetime(target.year, target.month, target.day, wh, wm, tzinfo=JST)

    def can_wait() -> bool:
        return live and deadline is not None and datetime.now(JST) + timedelta(seconds=args.poll_sec) <= deadline

    # ---- 場中の足（日経平均の 11:30 の足が出るまで待つ）----
    bar_start = max(datetime(prev_bd.year, prev_bd.month, prev_bd.day, 15, 0, tzinfo=JST), asof - timedelta(days=6))
    polls: list[str] = []
    while True:
        series = fetch_intraday(target, asof, bar_start)
        cash_bars = session_bars(series.get("^N225"), True, open_dt, asof)
        last_ts = cash_bars[-1].ts if cash_bars else None
        t_now = datetime.now(JST)
        polls.append(f"{t_now.strftime('%H:%M:%S')} → {last_ts.strftime('%H:%M') if last_ts else 'なし'}")
        print(f"[INFO] 取得 {t_now.strftime('%H:%M:%S')}: 日経平均の前場の最後の足 = {last_ts.strftime('%H:%M') if last_ts else 'なし'}")
        if last_ts is not None and last_ts >= asof:
            break
        if not can_wait():
            break
        time.sleep(max(5, args.poll_sec))

    # Yahoo が全滅した時だけ、場中に限り CNBC の当日値で日経平均の前場を補う（過去時点の再現には使わない）
    cash_bars = session_bars(series.get("^N225"), True, open_dt, asof)
    if not cash_bars and live and datetime.now(JST) < asof + timedelta(minutes=55):
        q = get_cnbc_session_quote("^N225")
        if q and q.get("last") is not None and str(q.get("last_time", ""))[:10] == target.isoformat():
            print("[WARN] Yahoo の場中の足が取れないため CNBC の当日値を使います", file=sys.stderr)
            from lib.snapshot_utils import IntradayBar, IntradaySeries  # noqa: PLC0415

            lt = datetime.fromisoformat(str(q["last_time"]).replace("Z", "+00:00")).astimezone(JST)
            bars = [IntradayBar(ts=open_dt, open=q["open"], high=q["high"], low=q["low"], close=q["open"] or q["last"]),
                    IntradayBar(ts=min(lt, asof), open=None, high=q["high"], low=q["low"], close=q["last"])]
            series["^N225"] = IntradaySeries(ticker="^N225", interval="cnbc", bars=bars, fetched_at=datetime.now(JST),
                                             source="cnbc", meta={"previousClose": q.get("prev")})
            cash_bars = session_bars(series["^N225"], True, open_dt, asof)
    if not cash_bars:
        print("[ERROR] 日経平均の前場の足が 1 本も取れませんでした（raw を書きません）", file=sys.stderr)
        return EXIT_NO_CASH
    t_bars = time.time()

    # ---- 日足（前日比の基準・米国指標の直近終値）----
    daily = {}
    for spec in SNAP:
        daily[spec["ticker"]] = get_daily_closes_window(spec["ticker"], asof - timedelta(days=25), asof)

    delays = {}
    if not args.no_delay_meta:
        for tk in MAIN_TICKERS:
            delays[tk] = get_exchange_delay_minutes(tk)

    # ---- 市場別サマリー・セクター強弱 ----
    seg: dict = {"status": []}
    t_seg0 = time.time()
    try:
        import pandas as pd

        import generate_macro_report as gmr

        sm_codes = pd.read_parquet(gmr.SCREENING_MASTER_PATH, columns=["Code", "MarketCodeName"])
        codes4 = sm_codes["Code"].astype(str).tolist()
        prime = set(sm_codes.loc[sm_codes["MarketCodeName"] == "プライム", "Code"].astype(str))
        prev_eq, prev_idx = ({}, {})
        if client:
            prev_eq, prev_idx = jq_prev_closes(client, prev_bd)
        seg["status"].append(f"前営業日の確定値（J-Quants）: {prev_bd.isoformat()} の株価 {len(prev_eq):,} 銘柄・市場別指数 {len(prev_idx)} 本")
        while True:
            t0 = time.time()
            spark, sst = get_spark_morning_closes([c + ".T" for c in codes4], open_dt, asof)
            morning = {sym[:-2]: v[0] for sym, v in spark.items()}
            prime_at = sum(1 for sym, v in spark.items() if sym[:-2] in prime and v[1] >= asof)
            share = prime_at / len(prime) if prime else 0.0
            print(f"[INFO] 個別株の前場: 対象 {sst['requested']} / 約定あり {sst['with_price']} / 11:30 の足あり {sst['at_asof']}"
                  f"（プライム {prime_at}/{len(prime)}）/ {time.time() - t0:.1f} 秒")
            if share >= 0.5 or not can_wait():
                break
            time.sleep(max(5, args.poll_sec))
        seg["status"].append(
            f"個別株の前場の最後の約定値（Yahoo spark・{sst.get('chunks')} 回×20 銘柄・5 分足）: 対象 {sst['requested']:,} 銘柄 / "
            f"前場に約定あり {sst['with_price']:,} / {jp_time(asof)} の足（前引け）に約定あり {sst['at_asof']:,}"
            f"（うちプライム {prime_at:,}/{len(prime):,}）/ 取得失敗 {sst['failed_chunks']} 回 / 所要 {sst['seconds']} 秒")
        while True:
            idx_now, tv_info = tv_index_morning(target, open_dt, asof)
            ok = all(code in idx_now and idx_now[code][1] >= asof - timedelta(minutes=1) for code in TV_INDEX_SYMBOLS)
            if ok or not can_wait():
                break
            time.sleep(max(5, args.poll_sec))
        seg["status"].append("市場別指数（TradingView・未ログインの遅延配信）: " + " ／ ".join(tv_info.values()))
        if prev_eq and morning:
            segment, sector = build_segment_blocks(target, morning, prev_eq, {k: v[0] for k, v in idx_now.items()}, prev_idx)
            seg["segment"], seg["sector"] = segment, sector
    except Exception as e:  # noqa: BLE001 — 補助ブロックの失敗で raw 全体を止めない（朝刊と同じ流儀）
        print(f"[WARN] 市場別サマリー・セクター強弱を作れません（ブロック省略）: {type(e).__name__}: {e}", file=sys.stderr)
        seg["status"].append(f"市場別サマリー・セクター強弱: 作成失敗（{type(e).__name__}）")
    t_seg = time.time() - t_seg0

    # ---- ニュース ----
    news: dict = {}
    t_news0 = time.time()
    if not args.no_news:
        nr_path = Path(args.news_raw) if args.news_raw else MARKET_DIR / f"{target.isoformat()}_news_raw.md"
        fh_path = Path(args.finnhub_raw) if args.finnhub_raw else MARKET_DIR / f"{target.isoformat()}_finnhub_raw.md"
        news["news_raw"] = premarket_file(nr_path, target, asof, r"生成日時\*\*:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
        news["finnhub_raw"] = premarket_file(fh_path, target, asof, r"取得日時\*\*:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
        def _norm_url(u: str) -> str:
            return re.sub(r"^https?://", "", u.strip()).rstrip("/")

        known_urls: set[str] = set()
        for txt, _ in (news["news_raw"], news["finnhub_raw"]):
            if txt:
                known_urls |= {_norm_url(u) for u in re.findall(r"https?://[^\s\)\]]+", txt)}
        window_start = datetime(prev_bd.year, prev_bd.month, prev_bd.day, 15, 30, tzinfo=JST)
        news["_window_start"] = window_start

        def _new(items: list[dict]) -> list[dict]:
            return [it for it in items if not it.get("url") or _norm_url(it["url"]) not in known_urls]

        news["finnhub"] = _new(_finnhub_items(window_start, asof))
        news["yf"] = _new(_yf_news_items(window_start, asof))
        news["rss"] = _new(_rss_items(window_start, asof))
        tpath = Path(args.tachibana_raw) if args.tachibana_raw else MARKET_DIR / f"{target.isoformat()}_tachibana_news_raw.md"
        if tpath.exists():
            news["tachibana"] = _tachibana_items(tpath, window_start, asof)
        print("[INFO] ニュース件数（窓内・重複除外後）: " + " / ".join(
            f"{k}={len(v)}" for k, v in news.items() if isinstance(v, list)))
    t_news = time.time() - t_news0

    md, closed = build_raw(target, asof, prev_bd, live, series, daily, polls, delays, seg, news, prev_report_preview(target))
    out = Path(args.out) if args.out else MARKET_DIR / f"{target.isoformat()}_macro_midday_raw.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
    except OSError as e:
        print(f"[ERROR] 書き込み失敗: {out}: {e}", file=sys.stderr)
        return EXIT_ERR
    print(f"[TIME] 場中の足 {t_bars - t_start:.1f} 秒 / 市場別サマリー・セクター強弱 {t_seg:.1f} 秒 / "
          f"ニュース {t_news:.1f} 秒 / 合計 {time.time() - t_start:.1f} 秒")
    print(f"[OK] raw 保存: {out}（{len(md):,} 字・前場引けの足: {'取得済み' if closed else '未着'}・"
          f"市場別サマリー: {'あり' if seg.get('segment') else 'なし'}・セクター強弱: {'あり' if seg.get('sector') else 'なし'}）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
