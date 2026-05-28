"""全銘柄 × 過去 10 年の OHLCV + 株価派生指標を年別 partition で保存。

使用方法:
    # 初回フル取得（全銘柄 × 過去 10 年・数時間規模）
    python make_price_history_master.py initial

    # 初回フル取得（先頭 N 銘柄のみ・動作確認用）
    python make_price_history_master.py initial --limit 50

    # 日次差分更新（最新営業日 1 日分を追記・数分規模）
    python make_price_history_master.py daily

    # テスト（指定銘柄のみ）
    python make_price_history_master.py test --codes 4180,3905,3778

出力:
    bi/outputs/price_history/{YYYY}.parquet (年別 partition)

スキーマ（株価本体メイン・テクニカル最小限）:
    キー: Date, Code
    生 OHLCV: Open, High, Low, Close, Volume, Value
    調整済株価: AdjustmentFactor, AdjustmentOpen/High/Low/Close
    節目: AllTimeHigh, AllTimeLow, High52w, Low52w
    期間別: YearHigh/Low, QuarterHigh/Low, MonthHigh/Low, YTDHigh/Low
    乖離率: DistFromHigh52w, DistFromLow52w
    値動き: PrevDayClose, GapPct, IntradayRangePct
    リターン: Return_1d/5d/20d/60d/120d/252d
    出来高: Volume_vs_SMA20Ratio
    節目テクニカル: SMA20/50/200, BB_Upper2σ, BB_Lower2σ
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import jquantsapi

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jq_client_utils import (
    normalize_code_4,
    fetch_paginated_v2,
    latest_trading_day_date_v2,
)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "bi" / "outputs" / "price_history"
SCREENING_MASTER = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"

REQUEST_SLEEP = 1.0
MAX_RETRIES = 6


def _is_4digit_code(code4: str) -> bool:
    s = str(code4).strip()
    return len(s) == 4 and s.isdigit()


def _fetch_daily_with_backoff(client, code4: str, from_date: str | None = None) -> pd.DataFrame:
    backoff = 2.0
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_SLEEP)
            kwargs = {"code": code4}
            if from_date:
                kwargs["from_yyyymmdd"] = from_date.replace("-", "")
            return client.get_eq_bars_daily(**kwargs)
        except TypeError:
            # 引数仕様が異なる場合のフォールバック
            try:
                return client.get_eq_bars_daily(code=code4)
            except Exception as e:
                last_err = e
                msg = str(e)
                if (" 429 " in msg) or ("too many 429" in msg.lower()):
                    wait = min(120.0, backoff)
                    print(f"  rate limited (429): {code4} attempt {attempt}/{MAX_RETRIES} wait {wait:.1f}s")
                    time.sleep(wait)
                    backoff *= 2.0
                    continue
                raise
        except Exception as e:
            last_err = e
            msg = str(e)
            if (" 429 " in msg) or ("too many 429" in msg.lower()):
                wait = min(120.0, backoff)
                print(f"  rate limited (429): {code4} attempt {attempt}/{MAX_RETRIES} wait {wait:.1f}s")
                time.sleep(wait)
                backoff *= 2.0
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError(f"_fetch_daily_with_backoff: max retries exceeded for {code4}")


def load_universe() -> list[str]:
    if not SCREENING_MASTER.exists():
        raise FileNotFoundError(
            f"{SCREENING_MASTER} が存在しません。screening_master を先に実行してください。"
        )
    df = pd.read_parquet(SCREENING_MASTER, columns=["Code"])
    codes = df["Code"].astype(str).map(normalize_code_4).drop_duplicates().tolist()
    return [c for c in codes if _is_4digit_code(c)]


def _detect_columns(df: pd.DataFrame) -> dict[str, str]:
    """JQuants v2 のカラム名は短縮形（O/H/L/C/Vo）と長形（Open/High/Low/Close/Volume）の両方ある。"""
    candidates = {
        "Open": ["Open", "O", "open"],
        "High": ["High", "H", "high"],
        "Low": ["Low", "L", "low"],
        "Close": ["Close", "C", "close"],
        "Volume": ["Volume", "Vo", "volume"],
        "Value": ["TurnoverValue", "Val", "Value", "Va"],
        "AdjFactor": ["AdjustmentFactor", "AdjFactor"],
        "AdjOpen": ["AdjustmentOpen", "AdjOpen"],
        "AdjHigh": ["AdjustmentHigh", "AdjHigh"],
        "AdjLow": ["AdjustmentLow", "AdjLow"],
        "AdjClose": ["AdjustmentClose", "AdjClose"],
    }
    found = {}
    for canonical, options in candidates.items():
        for opt in options:
            if opt in df.columns:
                found[canonical] = opt
                break
    return found


def enrich_price_data(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV から株価派生指標を計算する。"""
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df["Code"] = df["Code"].astype(str).map(normalize_code_4)
    df = df.sort_values(["Code", "Date"]).reset_index(drop=True)

    col_map = _detect_columns(df)
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in col_map]
    if missing:
        raise RuntimeError(f"必須カラム不足: {missing} (実カラム: {list(df.columns)})")

    # 標準カラム名にリネーム
    rename_dict = {col_map[k]: k for k in col_map}
    df = df.rename(columns=rename_dict)

    # 売買代金が無ければ Close × Volume で近似
    if "Value" not in df.columns:
        df["Value"] = (df["Close"] * df["Volume"]).round(0)

    # 調整済株価が無ければ生 OHLC を複製
    if "AdjFactor" not in df.columns:
        df["AdjustmentFactor"] = 1.0
        df["AdjustmentOpen"] = df["Open"]
        df["AdjustmentHigh"] = df["High"]
        df["AdjustmentLow"] = df["Low"]
        df["AdjustmentClose"] = df["Close"]
    else:
        df = df.rename(columns={
            "AdjFactor": "AdjustmentFactor",
            "AdjOpen": "AdjustmentOpen",
            "AdjHigh": "AdjustmentHigh",
            "AdjLow": "AdjustmentLow",
            "AdjClose": "AdjustmentClose",
        })

    out_list = []
    total_codes = df["Code"].nunique()
    for idx, (code, group) in enumerate(df.groupby("Code"), start=1):
        g = group.copy().reset_index(drop=True)

        # 前日終値・ギャップ・日中レンジ
        g["PrevDayClose"] = g["Close"].shift(1)
        g["GapPct"] = ((g["Open"] - g["PrevDayClose"]) / g["PrevDayClose"] * 100).round(3)
        g["IntradayRangePct"] = ((g["High"] / g["Low"] - 1) * 100).round(3)

        # リターン期間別
        for n_days, col in [(1, "Return_1d"), (5, "Return_5d"), (20, "Return_20d"),
                            (60, "Return_60d"), (120, "Return_120d"), (252, "Return_252d")]:
            g[col] = (g["Close"].pct_change(n_days, fill_method=None) * 100).round(3)

        # 期間別 High/Low
        g["Year"] = g["Date"].dt.year
        quarter_key = g["Date"].dt.to_period("Q").astype(str)
        month_key = g["Date"].dt.to_period("M").astype(str)

        g["YearHigh"] = g.groupby("Year")["High"].transform("cummax")
        g["YearLow"] = g.groupby("Year")["Low"].transform("cummin")
        g["QuarterHigh"] = g.groupby(quarter_key)["High"].transform("cummax")
        g["QuarterLow"] = g.groupby(quarter_key)["Low"].transform("cummin")
        g["MonthHigh"] = g.groupby(month_key)["High"].transform("cummax")
        g["MonthLow"] = g.groupby(month_key)["Low"].transform("cummin")
        g["YTDHigh"] = g.groupby("Year")["High"].transform("cummax")
        g["YTDLow"] = g.groupby("Year")["Low"].transform("cummin")

        # 52 週 High/Low
        g["High52w"] = g["High"].rolling(window=252, min_periods=20).max()
        g["Low52w"] = g["Low"].rolling(window=252, min_periods=20).min()
        g["DistFromHigh52w"] = ((g["Close"] / g["High52w"] - 1) * 100).round(3)
        g["DistFromLow52w"] = ((g["Close"] / g["Low52w"] - 1) * 100).round(3)

        # 全期間 High/Low（取得期間内）
        g["AllTimeHigh"] = g["High"].cummax()
        g["AllTimeLow"] = g["Low"].cummin()

        # 出来高 vs 20 日平均
        vol_sma20 = g["Volume"].rolling(window=20, min_periods=5).mean()
        g["Volume_vs_SMA20Ratio"] = (g["Volume"] / vol_sma20).round(3)

        # 節目テクニカル（最小限）
        g["SMA20"] = g["Close"].rolling(window=20, min_periods=5).mean().round(2)
        g["SMA50"] = g["Close"].rolling(window=50, min_periods=10).mean().round(2)
        g["SMA200"] = g["Close"].rolling(window=200, min_periods=50).mean().round(2)

        # BB ±2σ
        ma20 = g["Close"].rolling(window=20, min_periods=5).mean()
        std20 = g["Close"].rolling(window=20, min_periods=5).std()
        g["BB_Upper2sigma"] = (ma20 + 2 * std20).round(2)
        g["BB_Lower2sigma"] = (ma20 - 2 * std20).round(2)

        out_list.append(g)

        if idx == 1 or idx % 100 == 0 or idx == total_codes:
            print(f"  enrich progress: {idx}/{total_codes}")

    return pd.concat(out_list, ignore_index=True)


def save_year_partition(df: pd.DataFrame, merge_existing: bool = True) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for year, year_df in df.groupby("Year"):
        path = OUT_DIR / f"{int(year)}.parquet"
        if merge_existing and path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, year_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["Code", "Date"], keep="last")
            combined = combined.sort_values(["Code", "Date"]).reset_index(drop=True)
        else:
            combined = year_df.sort_values(["Code", "Date"]).reset_index(drop=True)
        combined.to_parquet(path, index=False)
        size_mb = path.stat().st_size / 1024 / 1024
        print(f"  saved: {path.name} ({len(combined):,} rows, {size_mb:.1f} MB)")


def cmd_initial(args) -> int:
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY 未設定")

    client = jquantsapi.ClientV2(api_key=api_key)
    codes = load_universe()
    if args.limit and args.limit > 0:
        codes = codes[: args.limit]

    print(f"=== 初回フル取得モード: {len(codes)} 銘柄 × 過去 10 年 ===")
    ten_years_ago = (date.today() - timedelta(days=365 * 10)).strftime("%Y-%m-%d")

    frames: list[pd.DataFrame] = []
    failures: list[tuple[str, str]] = []

    for i, code4 in enumerate(codes, start=1):
        try:
            df = _fetch_daily_with_backoff(client, code4, from_date=ten_years_ago)
            if df is None or df.empty:
                continue
            frames.append(df)
        except Exception as e:
            failures.append((code4, f"{type(e).__name__}: {e}"))

        if i == 1 or i % 50 == 0 or i == len(codes):
            ok = len(frames)
            fail = len(failures)
            print(f"  fetch progress: {i}/{len(codes)} (ok={ok} fail={fail})")

    if not frames:
        print("ERROR: 1 銘柄も取得できませんでした")
        return 1

    all_data = pd.concat(frames, ignore_index=True)
    print(f"\n=== 派生指標計算開始: {len(all_data):,} 行 ===")
    enriched = enrich_price_data(all_data)

    print(f"\n=== 年別 partition 保存開始 ===")
    save_year_partition(enriched, merge_existing=False)

    if failures:
        print(f"\n失敗: {len(failures)} 件")
        for c, m in failures[:20]:
            print(f"  - {c}: {m}")

    return 0


def cmd_daily(args) -> int:
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY 未設定")

    client = jquantsapi.ClientV2(api_key=api_key)
    latest = latest_trading_day_date_v2(client)
    print(f"=== 日次差分更新: {latest.isoformat()} ===")

    rows = fetch_paginated_v2(
        client, "/equities/bars/daily",
        params={"date": latest.strftime("%Y-%m-%d")},
    )
    if not rows:
        print(f"  最新営業日 {latest.isoformat()} のデータ取得失敗")
        return 1

    new_df = pd.DataFrame(rows)
    new_df["Code"] = new_df["Code"].map(normalize_code_4)
    new_df = new_df[new_df["Code"].map(_is_4digit_code)].copy()
    print(f"  取得: {len(new_df):,} 銘柄 × 1 日")

    year = latest.year
    existing_paths = [
        OUT_DIR / f"{year}.parquet",
        OUT_DIR / f"{year - 1}.parquet",
    ]

    base_cols = ["Date", "Code", "Open", "High", "Low", "Close", "Volume"]
    existing_frames = []
    for p in existing_paths:
        if p.exists():
            df = pd.read_parquet(p)
            cols_present = [c for c in base_cols if c in df.columns]
            if len(cols_present) == len(base_cols):
                existing_frames.append(df[base_cols])

    if existing_frames:
        existing_base = pd.concat(existing_frames, ignore_index=True)
    else:
        existing_base = pd.DataFrame(columns=base_cols)

    col_map_new = _detect_columns(new_df)
    rename_new = {col_map_new[k]: k for k in col_map_new if k in base_cols[2:]}
    new_raw = new_df.rename(columns=rename_new)
    for c in base_cols:
        if c not in new_raw.columns and c != "Date" and c != "Code":
            new_raw[c] = None
    new_raw = new_raw[base_cols]
    new_raw["Date"] = pd.to_datetime(new_raw["Date"])

    combined_raw = pd.concat([existing_base, new_raw], ignore_index=True)
    combined_raw["Date"] = pd.to_datetime(combined_raw["Date"])
    combined_raw = combined_raw.drop_duplicates(subset=["Code", "Date"], keep="last")

    print(f"\n=== 派生指標計算 ===")
    enriched = enrich_price_data(combined_raw)
    new_year_df = enriched[enriched["Year"] == year]
    print(f"\n=== 年別 partition 保存 ===")
    save_year_partition(new_year_df, merge_existing=False)
    return 0


def cmd_test(args) -> int:
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY 未設定")

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    print(f"=== テストモード: {codes} ===")

    client = jquantsapi.ClientV2(api_key=api_key)
    ten_years_ago = (date.today() - timedelta(days=365 * 10)).strftime("%Y-%m-%d")

    frames = []
    for code4 in codes:
        df = _fetch_daily_with_backoff(client, code4, from_date=ten_years_ago)
        if df is not None and not df.empty:
            frames.append(df)
            print(f"  {code4}: {len(df)} rows fetched")

    if not frames:
        print("ERROR: 1 銘柄も取得できませんでした")
        return 1

    all_data = pd.concat(frames, ignore_index=True)
    enriched = enrich_price_data(all_data)

    test_out = OUT_DIR / "_test.parquet"
    test_out.parent.mkdir(parents=True, exist_ok=True)
    enriched.to_parquet(test_out, index=False)
    size_kb = test_out.stat().st_size / 1024
    print(f"\n保存: {test_out} ({size_kb:.0f} KB)")
    print(f"カラム数: {len(enriched.columns)}")
    print(f"カラム: {list(enriched.columns)}")
    print(f"\n直近 5 行サンプル（最初の銘柄）:")
    first_code = enriched["Code"].iloc[0]
    sample = enriched[enriched["Code"] == first_code].tail(5)
    print(sample[["Date", "Code", "Open", "High", "Low", "Close", "Volume",
                  "Return_1d", "SMA20", "SMA200", "High52w", "Low52w",
                  "DistFromHigh52w"]].to_string(index=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="全銘柄 OHLCV + 派生指標 ETL")
    sub = parser.add_subparsers(dest="mode")

    p_init = sub.add_parser("initial", help="初回フル取得（全銘柄 × 過去 10 年）")
    p_init.add_argument("--limit", type=int, default=0,
                        help="先頭 N 銘柄のみ取得（動作確認用・0 で全銘柄）")

    sub.add_parser("daily", help="日次差分更新（最新営業日のみ）")

    p_test = sub.add_parser("test", help="テスト（指定銘柄のみ）")
    p_test.add_argument("--codes", type=str, required=True,
                        help="カンマ区切り銘柄コード（例: 4180,3905,3778）")

    args = parser.parse_args()
    if args.mode == "initial":
        return cmd_initial(args)
    elif args.mode == "daily":
        return cmd_daily(args)
    elif args.mode == "test":
        return cmd_test(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
