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
            # 認証・リクエスト不備（400/401/403/404）は再試行しても直らないため即時 raise。
            # 5xx・ネットワーク系（response 無し or 5xx）は従来どおりバックオフ再試行する。
            resp_obj = getattr(e, "response", None)
            status = getattr(resp_obj, "status_code", None)
            if status in (400, 401, 403, 404):
                raise
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
    prev_key: str | None = None
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
        # 同じ pagination_key が返り続けると無限ループになるため検知して打ち切る。
        if pagination_key == prev_key:
            raise RuntimeError(
                f"fetch_paginated_v2: pagination_key が変化せず無限ループの恐れ "
                f"(endpoint={endpoint_path}, key={pagination_key!r})"
            )
        prev_key = pagination_key

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
    raise RuntimeError(
        f"latest_trading_day_date_v2: {max_back_days} 日さかのぼっても bars/daily が見つかりません "
        f"(today={today})"
    )


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


def _calendar_business_days_v2(
    client: jquantsapi.ClientV2,
    *,
    date_from: date,
    date_to: date,
) -> list[date]:
    """
    /markets/calendar から HolDiv=="1"（営業日）の日付を昇順で返す。

    HolDiv: "1"=営業日, "0"=非営業日(土日祝), "3"=祝日(平日). 営業日は "1" のみ。
    /equities/bars/daily にデータがある日 と一致することを実機確認済み。
    """
    url = f"{client.JQUANTS_API_BASE}/markets/calendar"
    payload = get_json_with_429_backoff(
        client,
        url,
        {"from": date_from.strftime("%Y-%m-%d"), "to": date_to.strftime("%Y-%m-%d")},
    )
    rows = payload.get("data", [])
    out: list[date] = []
    for r in rows:
        if str(r.get("HolDiv")) != "1":
            continue
        ds = r.get("Date")
        if not ds:
            continue
        try:
            out.append(date.fromisoformat(str(ds)[:10]))
        except ValueError:
            continue
    return sorted(out)


def recent_trading_days_v2(
    client: jquantsapi.ClientV2,
    n_days: int,
    *,
    end: date | None = None,
    max_back_days: int = 14,
) -> list[date]:
    """
    直近 `n_days` 営業日を **新しい順**（[最新, 1日前, … , n-1日前]）で返す。

    /markets/calendar（営業日カレンダー）を1回引いて last N をスライスする高速版。
    旧 latest_trading_day_date_v2 + previous_trading_day_date_v2 のブルートフォース
    （日ごとに全市場 bars/daily を再取得）と **同一の日付リスト** を返す。

    `end` 省略時は今日。カレンダーは未来営業日（データ未公開）も含むため end 以下に絞り、
    さらに最新営業日が bars/daily に未反映（場中実行・公開前）の場合は旧 latest
    ロジックと同じく1営業日ずつさかのぼって最初にデータがある日を最新営業日とする。
    """
    if n_days < 1:
        return []
    if end is None:
        end = date.today()
    # 余裕を持って過去を引く: 営業日は週5日 ≒ 暦日の 7/5 倍 + 祝日/年末年始の余白。
    span_days = int(n_days * 7 / 5) + 30 + max_back_days
    date_from = end - timedelta(days=span_days)
    biz = [d for d in _calendar_business_days_v2(client, date_from=date_from, date_to=end) if d <= end]
    if not biz:
        # フォールバック: カレンダー取得失敗時は従来ロジック。
        latest = latest_trading_day_date_v2(client, max_back_days=max_back_days)
        days = [latest]
        prev = latest
        for _ in range(n_days - 1):
            prev = previous_trading_day_date_v2(client, before=prev, max_back_days=max_back_days)
            days.append(prev)
        return days
    biz.sort()
    # 最新営業日が bars/daily に未反映（公開前）なら旧 latest と一致するようさかのぼる。
    while biz:
        cand = biz[-1]
        rows = fetch_paginated_v2(
            client,
            "/equities/bars/daily",
            params={"date": cand.strftime("%Y-%m-%d")},
        )
        if rows:
            break
        biz.pop()
    if not biz:
        raise RuntimeError(
            f"recent_trading_days_v2: end={end} 付近で bars/daily がある営業日が見つかりません。"
        )
    if len(biz) < n_days:
        # カレンダー窓が足りない稀ケース: さらに過去へ拡張して再取得。
        biz = [
            d
            for d in _calendar_business_days_v2(
                client, date_from=end - timedelta(days=span_days * 3), date_to=biz[-1]
            )
            if d <= biz[-1]
        ]
        biz.sort()
    last_n = biz[-n_days:]
    return list(reversed(last_n))  # 新しい順 [最新, 1日前, …]


# 既存スクリプトとの互換用エイリアス
_normalize_code_4 = normalize_code_4
_get_json_with_429_backoff = get_json_with_429_backoff
_fetch_paginated_v2 = fetch_paginated_v2
_latest_trading_day_date_v2 = latest_trading_day_date_v2
_previous_trading_day_date_v2 = previous_trading_day_date_v2
_recent_trading_days_v2 = recent_trading_days_v2
