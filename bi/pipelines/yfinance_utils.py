"""
Yahoo Finance (yfinance) から東証銘柄の時価総額・発行済株式数を取得する補助。

銘柄コードは J-Quants の 4 桁に対し、Yahoo の表記 ``{code}.T`` を用いる。
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd


def jpx_code_to_yahoo_symbol(code4: str) -> str:
    c = str(code4).strip()
    if len(c) < 4 and c.isdigit():
        c = c.zfill(4)
    return f"{c}.T"


def fetch_yfinance_market_snapshot(
    codes: list[str],
    *,
    sleep_seconds: float = 0.35,
) -> pd.DataFrame:
    """
    各銘柄の ticker.info から marketCap / sharesOutstanding を取得。

    Returns:
        DataFrame columns: Code, YFinanceMarketCap, YFinanceSharesOutstanding
    """
    try:
        import yfinance as yf
    except ImportError as e:
        raise ImportError(
            "yfinance がインストールされていません。例: pip install yfinance"
        ) from e

    rows: list[dict[str, Any]] = []
    n = len(codes)
    for i, raw in enumerate(codes):
        code = str(raw).strip()
        sym = jpx_code_to_yahoo_symbol(code)
        mcap: Any = pd.NA
        sh: Any = pd.NA
        try:
            t = yf.Ticker(sym)
            inf = t.info
            if isinstance(inf, dict):
                raw_m = inf.get("marketCap")
                raw_s = inf.get("sharesOutstanding")
                if raw_m is not None:
                    mcap = pd.to_numeric(raw_m, errors="coerce")
                if raw_s is not None:
                    sh = pd.to_numeric(raw_s, errors="coerce")
        except Exception:
            pass
        rows.append(
            {
                "Code": code,
                "YFinanceMarketCap": mcap,
                "YFinanceSharesOutstanding": sh,
            }
        )
        if sleep_seconds > 0 and i + 1 < n:
            time.sleep(sleep_seconds)

    return pd.DataFrame(rows)


def fetch_yfinance_shares_fast(
    codes: list[str],
    *,
    sleep_seconds: float = 0.2,
) -> pd.DataFrame:
    """
    fast_info を使って株数のみを高速取得（株式分割検出専用）。

    ticker.info より大幅に軽量。全銘柄スキャン用途向け。
    Returns:
        DataFrame columns: Code, _yf_sh_split
    """
    try:
        import yfinance as yf
    except ImportError as e:
        raise ImportError(
            "yfinance がインストールされていません。例: pip install yfinance"
        ) from e

    rows: list[dict[str, Any]] = []
    n = len(codes)
    for i, raw in enumerate(codes):
        code = str(raw).strip()
        sym = jpx_code_to_yahoo_symbol(code)
        sh: Any = pd.NA
        try:
            t = yf.Ticker(sym)
            fi = t.fast_info
            s = getattr(fi, "shares", None)
            if s is not None:
                sh = pd.to_numeric(s, errors="coerce")
        except Exception:
            pass
        rows.append({"Code": code, "_yf_sh_split": sh})
        if sleep_seconds > 0 and i + 1 < n:
            time.sleep(sleep_seconds)

    return pd.DataFrame(rows)
