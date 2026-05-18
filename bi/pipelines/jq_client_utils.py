"""
J-Quants API v2 向けの共通ヘルパー（429 対策付きページング・営業日判定など）。
"""

from __future__ import annotations

import os
import platform
import time
from datetime import date, timedelta
from typing import Any

import jquantsapi
from jquantsapi import __version__ as _JQ_CLIENT_VERSION
import requests


def normalize_code_4(code: object) -> str:
    s = str(code).strip()
    return s[:4] if len(s) >= 4 else s


def get_json_with_429_backoff(
    client: jquantsapi.ClientV2,
    url: str,
    query: dict[str, Any],
    *,
    max_attempts: int = 15,
) -> dict[str, Any]:
    """
    J-Quants は短時間に連続アクセスすると 429 を返す。

    jquantsapi.ClientV2 の Session は 429 のたびに urllib3 側で最大3回まで
    自動リトライするため、こちらの「待ってから1回だけ叩く」と相性が悪い。
    そのため **リトライなしの requests.get** で1回ずつ投げ、429 のときだけ
    長めに sleep してから再試行する。
    """
    api_key = getattr(client, "_api_key", "") or os.environ.get("JQUANTS_API_KEY", "")
    headers = {
        "x-api-key": api_key,
        "User-Agent": f"jqapi-python-v2/{_JQ_CLIENT_VERSION} p/{platform.python_version()}",
    }
    delays_sec = (
        60,
        120,
        180,
        300,
        300,
        600,
        600,
        900,
        900,
        1200,
        1200,
        1800,
        1800,
        3600,
    )

    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            resp = requests.get(url, params=query, headers=headers, timeout=90)
            if resp.status_code == 429:
                last_err = requests.exceptions.HTTPError("429 Too Many Requests", response=resp)
                if attempt >= max_attempts - 1:
                    break
                d = delays_sec[min(attempt, len(delays_sec) - 1)]
                print(f"HTTP 429: {d}s 待って再試行 ({attempt + 1}/{max_attempts}) …")
                time.sleep(d)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt >= max_attempts - 1:
                break
            d = delays_sec[min(attempt, len(delays_sec) - 1)]
            print(f"API エラー ({type(e).__name__}): {d}s 待って再試行 ({attempt + 1}/{max_attempts}) …")
            time.sleep(d)

    assert last_err is not None
    raise last_err


def fetch_paginated_v2(
    client: jquantsapi.ClientV2,
    endpoint_path: str,
    params: dict[str, Any],
    data_key: str = "data",
    sleep_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """
    pagination_key に対応して全ページ取得する（呼び出しごとに sleep するため自前 while）。
    sleep_seconds: 省略時は 1.0 秒（429 対策）。/fins/summary はさらに長めに指定推奨。
    """
    url = f"{client.JQUANTS_API_BASE}{endpoint_path}"
    all_data: list[dict[str, Any]] = []
    pagination_key = ""
    query = dict(params or {})
    wait = 1.0 if sleep_seconds is None else float(sleep_seconds)

    while True:
        time.sleep(wait)
        if pagination_key:
            query["pagination_key"] = pagination_key
        else:
            query.pop("pagination_key", None)

        payload = get_json_with_429_backoff(client, url, query)

        batch = payload.get(data_key, [])
        if isinstance(batch, list):
            all_data.extend(batch)

        pagination_key = payload.get("pagination_key") or ""
        if not pagination_key:
            break

    return all_data


def latest_trading_day_date_v2(client: jquantsapi.ClientV2, max_back_days: int = 14) -> date:
    today = date.today()
    for i in range(0, max_back_days + 1):
        d = today - timedelta(days=i)
        rows = fetch_paginated_v2(
            client,
            "/equities/bars/daily",
            params={"date": d.strftime("%Y-%m-%d")},
        )
        if rows:
            return d
    return today


def previous_trading_day_date_v2(
    client: jquantsapi.ClientV2,
    *,
    before: date | None = None,
    max_back_days: int = 30,
) -> date:
    """
    `before` より前で、/equities/bars/daily にデータがある最も新しい日付（直前の営業日想定）。
    `before` 省略時はまず latest_trading_day_date_v2 で最新営業日を求め、その前を探す。
    """
    if before is None:
        before = latest_trading_day_date_v2(client, max_back_days=max_back_days)
    for i in range(1, max_back_days + 1):
        d = before - timedelta(days=i)
        rows = fetch_paginated_v2(
            client,
            "/equities/bars/daily",
            params={"date": d.strftime("%Y-%m-%d")},
        )
        if rows:
            return d
    raise RuntimeError(
        f"previous_trading_day_date_v2: {max_back_days} 日さかのぼっても bars/daily が見つかりません "
        f"(before={before})"
    )


# 既存スクリプトとの互換用エイリアス
_normalize_code_4 = normalize_code_4
_get_json_with_429_backoff = get_json_with_429_backoff
_fetch_paginated_v2 = fetch_paginated_v2
_latest_trading_day_date_v2 = latest_trading_day_date_v2
