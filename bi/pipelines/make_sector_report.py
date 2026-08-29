"""
セクター週次レポート ETL
========================
出力:
  bi/outputs/sector_stock_weekly.parquet  … 銘柄別生データ（deep dive 用）
  bi/outputs/sector_weekly.parquet        … Sector17 集計

価格キャッシュ:
  bi/data/raw/sector_prices.parquet       … 全銘柄日次OHLCV（増分更新）

投資主体別売買:
  bi/data/raw/tse_investor_trading.parquet … 東証全体の週次データ

実行:
  cd bi/pipelines
  python make_sector_report.py [--limit-codes N] [--skip-price-fetch] [--skip-investor-fetch]

環境変数:
  JQUANTS_API_KEY  … 必須
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from jq_client_utils import (
    fetch_paginated_v2,
    normalize_code_4,
)

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / ".." / "outputs"
DATA_RAW_DIR = BASE_DIR / ".." / "data" / "raw"
SCREENING_MASTER_PATH = OUTPUTS_DIR / "screening_master.parquet"
PRICE_CACHE_PATH = DATA_RAW_DIR / "sector_prices.parquet"
INVESTOR_CACHE_PATH = DATA_RAW_DIR / "tse_investor_trading.parquet"
OUT_STOCK_PATH = OUTPUTS_DIR / "sector_stock_weekly.parquet"
OUT_SECTOR_PATH = OUTPUTS_DIR / "sector_weekly.parquet"

# 営業日ベースの陳腐化判定は全レポート共通ロジックを共有（カレンダー日数では判定しない）
sys.path.insert(0, str(BASE_DIR.resolve()))
from lib.snapshot_utils import is_stale_close  # noqa: E402

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
LOOKBACK_YEARS = 3
WEEKLY_SLOTS = 8
SNAPSHOT_LABELS = ["3M", "6M", "1Y", "2Y", "3Y"]
SNAPSHOT_DAYS   = [63,   126,  252,  504,  756]
REQUEST_SLEEP   = 1.2

# 東証投資主体別売買データURL（週次CSV、東証公開）
TSE_INVESTOR_URL = "https://www.jpx.co.jp/markets/statistics-equities/investor-type/b7gje6000000p9ov-att/investors.csv"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _last_friday(d: date) -> date:
    offset = (d.weekday() - 4) % 7
    return d - timedelta(days=offset)


def _prior_friday(d: date, n: int) -> date:
    return _last_friday(d) - timedelta(weeks=n)


# ---------------------------------------------------------------------------
# Step 1: 価格キャッシュ（OHLCV、増分更新）
# ---------------------------------------------------------------------------

def _load_price_cache() -> pd.DataFrame:
    if PRICE_CACHE_PATH.exists():
        df = pd.read_parquet(PRICE_CACHE_PATH)
        df["Date"] = pd.to_datetime(df["Date"])
        df["Code"] = df["Code"].astype("string")
        return df
    return pd.DataFrame(columns=["Date", "Code", "O", "H", "L", "C", "V"])


def _save_price_cache(df: pd.DataFrame) -> None:
    PRICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PRICE_CACHE_PATH, index=False)


def load_price_from_history(*, lookback_years: int = LOOKBACK_YEARS) -> pd.DataFrame:
    """price_history/{YYYY}.parquet（price_history.yml が毎日更新・commit する OHLCV マスタ）から
    直近 lookback_years 年分を読み込み、セクター ETL が期待する [Date, Code, O, H, L, C, V] で返す。

    旧実装は JQuants から 3 年分を日付逐次でコールド取得していたが、gitignore 済みの
    sector_prices.parquet が clone 毎に空のため毎回 full backfill（約89分）→ 90分タイムアウトで
    cancelled する慢性障害だった。既に daily で温まっている price_history を入力源にすることで
    数分に短縮する（PM 2026-06-14 確定）。OHLCV は旧コールドフェッチと同じ raw 値（調整前）を用い、
    レポート数値の意味を変えない。
    """
    hist_dir = OUTPUTS_DIR / "price_history"
    today = date.today()
    # AdjustmentFactor（株式分割等の調整係数・権利落ち日の行のみ非 1.0）は週次・期間リターンの
    # 分割調整に必須（PM 2026-07-12 確定・compute_stock_metrics 参照）。
    cols = ["Date", "Code", "Open", "High", "Low", "Close", "Volume", "AdjustmentFactor"]
    frames: list[pd.DataFrame] = []
    for year in range(today.year - lookback_years, today.year + 1):
        p = hist_dir / f"{year}.parquet"
        if p.exists():
            try:
                frames.append(pd.read_parquet(p, columns=cols))
            except Exception:
                # 旧スキーマ（AdjustmentFactor 列なし）の年ファイルは基本列のみ読み係数 1.0 扱い
                df_y = pd.read_parquet(p, columns=cols[:-1])
                df_y["AdjustmentFactor"] = 1.0
                frames.append(df_y)
    if not frames:
        return pd.DataFrame(columns=["Date", "Code", "O", "H", "L", "C", "V", "AdjFactor"])
    out = pd.concat(frames, ignore_index=True).rename(
        columns={"Open": "O", "High": "H", "Low": "L", "Close": "C", "Volume": "V",
                 "AdjustmentFactor": "AdjFactor"}
    )
    out["Date"] = pd.to_datetime(out["Date"])
    out["Code"] = out["Code"].astype("string").str.strip().str[:4]
    cutoff = pd.Timestamp(today - timedelta(days=lookback_years * 365 + 30))
    out = out[out["Date"] >= cutoff]
    keep = ["Date", "Code", "O", "H", "L", "C", "V", "AdjFactor"]
    return out[keep].sort_values(["Code", "Date"]).reset_index(drop=True)


def fetch_price_history(codes: list[str], *, limit_codes: int = 0, lookback_days: int = 0) -> pd.DataFrame:
    """
    全銘柄の価格履歴を取得する。

    jquantsapi.ClientV2.get_eq_bars_daily_range は MAX_WORKERS=5 で日付単位を
    並列リクエストするが、内部の get_eq_bars_daily に 429 リトライがなく
    urllib3 の MaxRetryError で落ちる。そのため日付単位の逐次ループ +
    jq_client_utils.fetch_paginated_v2（指数バックオフ 429 リトライ実装済み）
    に置き換える。並列度 1 + リクエスト間 sleep で 429 を構造的に回避する。

    lookback_days > 0 の場合はコールドフェッチ範囲を直近 lookback_days 日に絞る
    （PM 2026-07-12 確定・週次動意 GHA 用。price_history parquet は 2026-07-07 に git 管理外化
    され GHA clone に存在しないため、3 年分のコールドフェッチ（約 90 分・timeout 事故源）を
    W01〜W08 計算に必要な直近数週間へ短縮する。0 = 従来どおり LOOKBACK_YEARS 年分）。
    """
    import jquantsapi

    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY が未設定です")
    client = jquantsapi.ClientV2(api_key=api_key)

    cache = _load_price_cache()
    if lookback_days > 0:
        fetch_from = date.today() - timedelta(days=lookback_days)
    else:
        fetch_from = date.today() - timedelta(days=LOOKBACK_YEARS * 365 + 30)
    if not cache.empty and "Date" in cache.columns:
        cached_max = cache["Date"].max().date()
        if cached_max >= fetch_from:
            fetch_from = cached_max + timedelta(days=1)

    today = date.today()
    if fetch_from > today:
        print(f"価格キャッシュは最新（{fetch_from} > {today}）、スキップ")
        return cache

    print(f"価格一括取得: {fetch_from} 〜 {today}（全銘柄・日付逐次・429バックオフ）")
    # 日付単位で逐次取得（並列度 1）。土日祝は空レスポンスで即終了するため軽量。
    all_rows: list[dict] = []
    fetch_dates = pd.date_range(fetch_from, today, freq="D")
    total_dates = len(fetch_dates)
    fetched_days = 0
    empty_days = 0
    for idx, d in enumerate(fetch_dates, 1):
        d_str = d.strftime("%Y-%m-%d")
        try:
            rows = fetch_paginated_v2(
                client,
                "/equities/bars/daily",
                params={"date": d_str},
                sleep_seconds=REQUEST_SLEEP,
            )
        except Exception as e:
            print(f"  {d_str}: 取得失敗（スキップ）: {type(e).__name__}: {e}")
            continue

        if rows:
            all_rows.extend(rows)
            fetched_days += 1
        else:
            empty_days += 1

        if idx % 30 == 0 or idx == total_dates:
            print(
                f"  価格取得: {idx}/{total_dates} 日処理 "
                f"(データあり {fetched_days} 日 / 空 {empty_days} 日 / 累計 {len(all_rows)} 行)"
            )

    print(f"取得完了: {len(all_rows)} 行（{fetched_days} 営業日）")

    if not all_rows:
        print("新規価格データなし、キャッシュをそのまま使用")
        return cache

    df = pd.DataFrame(all_rows)

    # カラム正規化
    col_map = {}
    for col in df.columns:
        cl = col.lower()
        if cl in ("open", "o"):           col_map[col] = "O"
        elif cl in ("high", "h"):         col_map[col] = "H"
        elif cl in ("low", "l"):          col_map[col] = "L"
        elif cl in ("close", "c"):        col_map[col] = "C"
        elif cl in ("volume", "vo", "v"): col_map[col] = "V"
        elif cl == "adjfactor":           col_map[col] = "AdjFactor"  # 分割調整係数（PM 2026-07-12）
        elif cl == "date":                col_map[col] = "Date"
        elif cl == "code":                col_map[col] = "Code"
    df = df.rename(columns=col_map)

    for col in ["O", "H", "L", "C", "V", "AdjFactor"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = float("nan")

    df["Code"] = df["Code"].astype("string").str.strip().str[:4]
    df["Date"] = pd.to_datetime(df["Date"])

    keep = [c for c in ["Date", "Code", "O", "H", "L", "C", "V", "AdjFactor"] if c in df.columns]
    new_data = df[keep].copy()

    combined = pd.concat([cache, new_data], ignore_index=True)
    combined = combined.drop_duplicates(subset=["Date", "Code"]).sort_values(["Code", "Date"]).reset_index(drop=True)
    _save_price_cache(combined)
    print(f"価格キャッシュ保存: {PRICE_CACHE_PATH} ({len(combined)} 行、{combined['Code'].nunique()} 銘柄)")
    return combined


# ---------------------------------------------------------------------------
# Step 2: 投資主体別売買（東証全体、週次）
# ---------------------------------------------------------------------------

def fetch_investor_trading(*, skip: bool = False) -> pd.DataFrame:
    """
    東証公開の投資主体別売買データを取得・キャッシュ。
    カラム: Week（週末日）, 外国人_買, 外国人_売, 外国人_差引, 個人_買, 個人_売, 個人_差引,
            信託銀行_買, 信託銀行_売, 信託銀行_差引, 事業法人_買, 事業法人_売, 事業法人_差引
    """
    if skip and INVESTOR_CACHE_PATH.exists():
        df = pd.read_parquet(INVESTOR_CACHE_PATH)
        df["Week"] = pd.to_datetime(df["Week"])
        print(f"投資主体データ: キャッシュ読み込み ({len(df)} 行)")
        return df

    print("投資主体別売買データ取得中...")
    try:
        resp = requests.get(TSE_INVESTOR_URL, timeout=30)
        resp.raise_for_status()
        # 東証CSVはShift-JIS
        raw = resp.content.decode("shift-jis", errors="replace")
        df_raw = pd.read_csv(io.StringIO(raw), header=None, skiprows=1)

        # 東証CSVの列構造を解析して整形
        # 実際のCSVフォーマットに合わせてパース（列数・構造が変わる場合あり）
        # ここでは汎用的にカラムを割り当て
        if df_raw.empty:
            raise ValueError("投資主体データが空です")

        # 週列（最初の列）を Week として扱う
        df_raw.columns = [f"col{i}" for i in range(len(df_raw.columns))]
        df_raw["Week"] = pd.to_datetime(df_raw["col0"], errors="coerce")
        df_raw = df_raw.dropna(subset=["Week"])

        # 数値列を抽出（外国人・個人・信託・事業法人の買/売/差引）
        # 列インデックスは東証のフォーマット次第なので、取得できた列をそのまま保存
        numeric_cols = [c for c in df_raw.columns if c != "Week" and c != "col0"]
        for c in numeric_cols:
            df_raw[c] = pd.to_numeric(df_raw[c], errors="coerce")

        df_raw = df_raw.sort_values("Week").reset_index(drop=True)
        INVESTOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        df_raw.to_parquet(INVESTOR_CACHE_PATH, index=False)
        print(f"投資主体データ保存: {len(df_raw)} 行")
        return df_raw

    except Exception as e:
        print(f"投資主体データ取得失敗（スキップ）: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Step 3: 銘柄別指標計算（OHLCV → リターン・ボラ・MA・出来高等）
# ---------------------------------------------------------------------------

def compute_stock_metrics(prices: pd.DataFrame, today: date, anchor: str = "friday") -> pd.DataFrame:
    """
    銘柄ごとにOHLCVから全指標を計算。

    anchor:
      "friday" — W01 を「直近金曜終値 vs 1週前金曜終値」で算出（デフォルト・週末実行向け）
      "today"  — W01 を「today 終値 vs 7日前終値」で算出（平日実行で当日まで含めたい場合）
    """
    if anchor not in ("friday", "today"):
        raise ValueError(f"anchor は 'friday' または 'today' を指定: {anchor}")

    def _week_target(n: int) -> date:
        # n=0 が「最新スロット」、n=1 が「1週前スロット」...
        if anchor == "friday":
            return _prior_friday(today, n)
        else:
            return today - timedelta(weeks=n)

    prices = prices.copy()
    prices["Date"] = pd.to_datetime(prices["Date"])
    prices = prices.sort_values(["Code", "Date"])

    results: list[dict] = []
    codes = prices["Code"].unique()
    total = len(codes)

    for i, code in enumerate(codes, 1):
        sub = prices[prices["Code"] == code].copy().set_index("Date")
        if sub.empty or "C" not in sub.columns:
            continue

        close = sub["C"]
        volume = sub["V"] if "V" in sub.columns else pd.Series(dtype=float)
        high = sub["H"] if "H" in sub.columns else pd.Series(dtype=float)
        low = sub["L"] if "L" in sub.columns else pd.Series(dtype=float)

        # 株式分割調整（PM 2026-07-12 確定）: Return 系はすべて調整後終値 adj_close で計算する。
        # 生 C の比較では権利落ち日（AdjFactor != 1.0）を跨ぐ週に「1:3 分割 = -66%」型の虚偽リターンが
        # 出る（2026-07-10 週次動意で 2986 を -66.3% と誤配信・分割調整後は +1.1%）。日次
        # make_mover_report.py の「過去終値 × AdjFactor」方式を期間へ一般化し、各日の終値に
        # 「その日より後の AdjFactor の累積積」を掛けた後方調整終値で比較する（最新終値は生値と一致）。
        # Close_Wxx / Close_Latest / MA 乖離 / 52W 高安は表示・水準系のため従来どおり生値のまま。
        if "AdjFactor" in sub.columns:
            _adj = pd.to_numeric(sub["AdjFactor"], errors="coerce").fillna(1.0)
        else:
            _adj = pd.Series(1.0, index=close.index)
        _factor_after = _adj[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
        adj_close = close * _factor_after

        row: dict = {"Code": str(code)}

        # 最新終値
        latest_close = close.iloc[-1]
        row["Close_Latest"] = latest_close

        # 週次スロット終値 W01〜W08
        for w in range(1, WEEKLY_SLOTS + 1):
            target = _week_target(w - 1)
            sub_before = close[close.index <= pd.Timestamp(target)]
            row[f"Close_W{w:02d}"] = sub_before.iloc[-1] if not sub_before.empty else float("nan")

        # 週次リターン（W01〜W08）:
        # Wxx は「x週前のスロット終値 ÷ (x+1)週前のスロット終値 - 1」で直接計算する。
        # スロットは anchor=friday なら金曜終値、anchor=today なら today から週単位で逆算した日付。
        # 分割調整のため adj_close で比較する（PM 2026-07-12・スロット選定日付は close と同一）。
        for w in range(1, WEEKLY_SLOTS + 1):
            cur_target = _week_target(w - 1)
            prev_target = _week_target(w)
            cur_series = adj_close[adj_close.index <= pd.Timestamp(cur_target)]
            prev_series = adj_close[adj_close.index <= pd.Timestamp(prev_target)]
            c_cur = cur_series.iloc[-1] if not cur_series.empty else float("nan")
            c_prev = prev_series.iloc[-1] if not prev_series.empty else float("nan")
            if pd.notna(c_cur) and pd.notna(c_prev) and c_prev != 0:
                row[f"Return_W{w:02d}"] = c_cur / c_prev - 1
            else:
                row[f"Return_W{w:02d}"] = float("nan")

        # スナップショットリターン（3M/6M/1Y/2Y/3Y）
        # Close_{label} は表示用の生値・Return_{label} は分割調整後で計算（PM 2026-07-12）
        for label, bdays in zip(SNAPSHOT_LABELS, SNAPSHOT_DAYS):
            target = today - timedelta(days=int(bdays * 365 / 252))
            sub_before = close[close.index <= pd.Timestamp(target)]
            adj_before = adj_close[adj_close.index <= pd.Timestamp(target)]
            if sub_before.empty:
                row[f"Close_{label}"] = float("nan")
                row[f"Return_{label}"] = float("nan")
            else:
                snap_close = sub_before.iloc[-1]
                adj_snap = adj_before.iloc[-1]
                row[f"Close_{label}"] = snap_close
                row[f"Return_{label}"] = (adj_close.iloc[-1] / adj_snap - 1) if adj_snap != 0 and pd.notna(adj_snap) else float("nan")

        # --- ボラティリティ（過去20営業日の日次リターンσ、年率換算） ---
        # 権利落ち日の「-66%」型の虚偽日次リターンを除くため adj_close で計算（PM 2026-07-12）
        daily_ret = adj_close.pct_change().dropna()
        if len(daily_ret) >= 5:
            row["Volatility_20d"] = daily_ret.tail(20).std() * (252 ** 0.5)
        else:
            row["Volatility_20d"] = float("nan")

        # --- 移動平均乖離率（25日・75日） ---
        if len(close) >= 25:
            ma25 = close.tail(25).mean()
            row["MA25_Deviation"] = (latest_close / ma25 - 1) if ma25 != 0 else float("nan")
        else:
            row["MA25_Deviation"] = float("nan")

        if len(close) >= 75:
            ma75 = close.tail(75).mean()
            row["MA75_Deviation"] = (latest_close / ma75 - 1) if ma75 != 0 else float("nan")
        else:
            row["MA75_Deviation"] = float("nan")

        # --- 52週高値・安値比 ---
        last_252 = close.tail(252)
        if not last_252.empty:
            w52_high = last_252.max()
            w52_low = last_252.min()
            row["52W_High"] = w52_high
            row["52W_Low"] = w52_low
            row["52W_High_Ratio"] = (latest_close / w52_high - 1) if w52_high != 0 else float("nan")
            row["52W_Low_Ratio"] = (latest_close / w52_low - 1) if w52_low != 0 else float("nan")
        else:
            row["52W_High"] = row["52W_Low"] = row["52W_High_Ratio"] = row["52W_Low_Ratio"] = float("nan")

        # --- 出来高変化率（直近1週 vs 4週前） ---
        if not volume.empty:
            fri_latest = _prior_friday(today, 0)
            fri_4w = _prior_friday(today, 4)

            vol_w1 = volume[(volume.index > pd.Timestamp(_prior_friday(today, 1))) &
                            (volume.index <= pd.Timestamp(fri_latest))].mean()
            vol_w4 = volume[(volume.index > pd.Timestamp(_prior_friday(today, 5))) &
                            (volume.index <= pd.Timestamp(fri_4w))].mean()

            row["Volume_W01_Avg"] = vol_w1
            row["Volume_Change_W1vsW4"] = (vol_w1 / vol_w4 - 1) if pd.notna(vol_w4) and vol_w4 != 0 else float("nan")

            # 直近5日平均出来高
            row["Volume_Avg5d_Price"] = volume.tail(5).mean()
        else:
            row["Volume_W01_Avg"] = row["Volume_Change_W1vsW4"] = row["Volume_Avg5d_Price"] = float("nan")

        results.append(row)

        if i % 500 == 0 or i == total:
            print(f"  指標計算: {i}/{total}")

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Step 4: スクリーニングマスターと結合
# ---------------------------------------------------------------------------

def build_stock_table(
    metrics_df: pd.DataFrame,
    master_df: pd.DataFrame,
    investor_df: pd.DataFrame,
    today: date,
    etl_run_id: str,
    etl_started_jst: str,
    price_data_asof: str,
) -> pd.DataFrame:
    metrics_df["Code"] = metrics_df["Code"].astype("string")
    master_df = master_df.copy()
    master_df["Code"] = master_df["Code"].astype("string").str.strip()

    drop_meta = ["ETLRunId", "ETLStartedAtUTC", "ETLStartedAtJST"]
    master_df = master_df.drop(columns=[c for c in drop_meta if c in master_df.columns])

    merged = metrics_df.merge(master_df, on="Code", how="left")

    # 時価総額ウェイト（セクター内）
    merged["MarketCap"] = pd.to_numeric(merged.get("MarketCap", pd.Series(dtype=float)), errors="coerce")
    sector_mcap = merged.groupby("Sector17CodeName")["MarketCap"].transform("sum")
    merged["MarketCap_Weight"] = merged["MarketCap"] / sector_mcap

    # 信用残（最新・発行株数比は下流で算出。買残÷売残の比率は不採用・PM 2026-06-14）
    long_latest = pd.to_numeric(merged.get("LongMargin_WkSeq01", pd.Series(dtype=float)), errors="coerce")
    short_latest = pd.to_numeric(merged.get("ShortMargin_WkSeq01", pd.Series(dtype=float)), errors="coerce")
    merged["ShortMargin_Latest"] = short_latest
    merged["LongMargin_Latest"] = long_latest

    # セクター内リターン順位
    merged["Return_Rank_InSector"] = (
        merged.groupby("Sector17CodeName")["Return_W01"]
        .rank(ascending=False, method="min", na_option="bottom")
        .astype("Int64")
    )

    # 投資主体別売買（東証全体・最新週）をメタとして付与
    if not investor_df.empty and "Week" in investor_df.columns:
        latest_inv = investor_df.sort_values("Week").iloc[-1]
        for col in investor_df.columns:
            if col != "Week":
                merged[f"TSE_{col}"] = latest_inv[col]
        merged["TSE_Week"] = latest_inv["Week"]

    merged["AsOf"] = today.isoformat()
    merged["PriceDataAsOf"] = price_data_asof
    merged["ETLRunId"] = etl_run_id
    merged["ETLStartedAtJST"] = etl_started_jst

    return merged.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Step 5: セクター集計
# ---------------------------------------------------------------------------

def build_sector_table(
    stock_df: pd.DataFrame,
    today: date,
    etl_run_id: str,
    etl_started_jst: str,
    price_data_asof: str,
) -> pd.DataFrame:
    sectors = stock_df["Sector17CodeName"].dropna().unique()
    rows: list[dict] = []

    for sector in sorted(sectors):
        sg = stock_df[stock_df["Sector17CodeName"] == sector].copy()
        row: dict = {"Sector17CodeName": sector}

        row["StockCount"] = len(sg)
        row["MarketCap_Total"] = sg["MarketCap"].sum(skipna=True)

        w = sg["MarketCap_Weight"].fillna(0)

        def wavg(col: str) -> float:
            if col not in sg.columns:
                return float("nan")
            sg[col] = pd.to_numeric(sg[col], errors="coerce")
            valid = sg[col].notna() & (w > 0)
            if not valid.any():
                return float("nan")
            return float((sg.loc[valid, col] * w[valid]).sum() / w[valid].sum())

        # 週次・スナップショットリターン（加重平均）
        for wk in range(1, WEEKLY_SLOTS + 1):
            row[f"Return_W{wk:02d}"] = wavg(f"Return_W{wk:02d}")
        for label in SNAPSHOT_LABELS:
            row[f"Return_{label}"] = wavg(f"Return_{label}")

        # バリュエーション（加重平均）
        row["PER_WAvg"] = wavg("PER_Trailing")
        row["PBR_WAvg"] = wavg("PBR_Trailing")
        row["ROE_WAvg"] = wavg("ROE_LatestYear")

        # テクニカル（加重平均）
        row["Volatility_20d_WAvg"] = wavg("Volatility_20d")
        row["MA25_Deviation_WAvg"] = wavg("MA25_Deviation")
        row["MA75_Deviation_WAvg"] = wavg("MA75_Deviation")
        row["Volume_Change_WAvg"] = wavg("Volume_Change_W1vsW4")

        # 1ヶ月リターン（4週累積）
        sg["_Return_1M"] = (
            (1 + sg.get("Return_W01", pd.Series(0, index=sg.index)).fillna(0))
            * (1 + sg.get("Return_W02", pd.Series(0, index=sg.index)).fillna(0))
            * (1 + sg.get("Return_W03", pd.Series(0, index=sg.index)).fillna(0))
            * (1 + sg.get("Return_W04", pd.Series(0, index=sg.index)).fillna(0))
            - 1
        )

        def _fmt(r: pd.DataFrame) -> str:
            parts = []
            for _, s in r.iterrows():
                pct = f"{s['_Return_1M']*100:.1f}%" if pd.notna(s.get("_Return_1M")) else "N/A"
                name = str(s.get("CompanyName", s["Code"]))[:10]
                parts.append(f"{s['Code']} {name}({pct})")
            return " / ".join(parts)

        sorted_asc = sg.dropna(subset=["_Return_1M"]).sort_values("_Return_1M")
        row["Top3_Return_1M"] = _fmt(sorted_asc[::-1].head(3))
        row["Bottom3_Return_1M"] = _fmt(sorted_asc.head(3))

        top3_mcap = sg.dropna(subset=["MarketCap"]).nlargest(3, "MarketCap")
        row["Top3_MarketCap"] = " / ".join(
            f"{r['Code']} {str(r.get('CompanyName', ''))[:10]}"
            for _, r in top3_mcap.iterrows()
        )

        top5_sum = sg.dropna(subset=["MarketCap"]).nlargest(5, "MarketCap")["MarketCap"].sum()
        row["MarketCap_Ratio_Top5"] = top5_sum / row["MarketCap_Total"] if row["MarketCap_Total"] > 0 else float("nan")

        row["AsOf"] = today.isoformat()
        row["PriceDataAsOf"] = price_data_asof
        row["ETLRunId"] = etl_run_id
        row["ETLStartedAtJST"] = etl_started_jst
        rows.append(row)

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--skip-price-fetch", action="store_true")
    parser.add_argument("--skip-investor-fetch", action="store_true")
    parser.add_argument(
        "--anchor",
        choices=["friday", "today"],
        default="friday",
        help="W01の起算日: 'friday'（直近金曜終値・週末実行向け・デフォルト） / 'today'（実行日終値・平日実行で当日まで含めたい場合）",
    )
    parser.add_argument(
        "--expect-price-date",
        default=None,
        help="対象日ゲート: PriceDataAsOf がこの日付 (YYYY-MM-DD) と不一致なら exit 3（週次動意 GHA 専用・opt-in）",
    )
    parser.add_argument(
        "--cold-lookback-days",
        type=int,
        default=0,
        help="price_history 不在時のコールドフェッチ日数を絞る（0=従来どおり3年分・週次動意 GHA は 90 を指定）",
    )
    args = parser.parse_args()

    etl_run_id = str(uuid.uuid4())
    jst = timezone(timedelta(hours=9))
    etl_started_jst = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S JST")
    today = date.today()

    print(f"=== セクター週次レポート ETL ===")
    print(f"実行日: {today}  RunId: {etl_run_id}")

    # ユニバース
    master_df = pd.read_parquet(SCREENING_MASTER_PATH)
    master_df["Code"] = master_df["Code"].astype("string").str.strip().str[:4]
    codes = master_df["Code"].dropna().unique().tolist()
    print(f"ユニバース: {len(codes)} 銘柄（全市場）")

    # 価格データ: daily 更新済の price_history を入力源に使用（PM 2026-06-14 確定・旧 JQuants コールドフェッチ廃止）
    if args.skip_price_fetch:
        print("価格取得スキップ（既存 sector_prices.parquet を使用）")
        prices = _load_price_cache()
    else:
        prices = load_price_from_history()
        if prices.empty:
            print("price_history が見つからないため JQuants コールドフェッチにフォールバック（低速）")
            prices = fetch_price_history(codes, limit_codes=args.limit_codes,
                                         lookback_days=args.cold_lookback_days)
        else:
            print(f"price_history から読込: {len(prices)} 行、{prices['Code'].nunique()} 銘柄（直近{LOOKBACK_YEARS}年）")

    if prices.empty:
        raise RuntimeError(
            "価格データがありません。--skip-price-fetch なしで実行してください。\n"
            f"キャッシュ保存先: {PRICE_CACHE_PATH}"
        )

    prices["Code"] = prices["Code"].astype("string").str.strip().str[:4]
    print(f"価格データ: {len(prices)} 行、銘柄 {prices['Code'].nunique()} 件")

    # 価格マスター鮮度ガード（PM 2026-06-27）: AsOf(実行日)とは別に「実際の価格データ最新日」を
    # 算出し、営業日ベース（jpholiday で祝日除外・カレンダー日数では判定しない）で陳腐化を見る。
    # 古ければ「警告して古い値のまま」にはせず、JQuants で直近営業日を取得して補完する（＝何とかして
    # 最新値を取りに行く）。それでも届かない場合のみ警告を残すが、送付は止めない。
    # PM 2026-08-29 追加（対象日ゲート強化）: 補完の発火条件を is_stale_close 単独から拡張する。
    # is_stale_close は「実行日の直前営業日」を期待最新日とするため、金曜当日 22 時台に走る
    # 週次動意では期待最新日＝木曜となり、木曜までしか無い parquet を陳腐化と判定せず補完が
    # 発火しなかった（対象日である金曜 EOD が欠けたまま前週窓で生成される事故源）。
    # --expect-price-date が指定されている場合は「prices の最新日 < 対象日」でも補完を発火させる。
    _prices_latest = prices["Date"].max().date()
    _expect_dt = None
    if args.expect_price_date:
        try:
            _expect_dt = datetime.strptime(args.expect_price_date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[WARN] --expect-price-date の形式が不正です（無視して従来判定）: {args.expect_price_date}")
    _need_topup = is_stale_close(_prices_latest, today) or (
        _expect_dt is not None and _prices_latest < _expect_dt
    )
    if _need_topup and not args.skip_price_fetch:
        print(
            f"[INFO] 価格マスターに対象日のデータが不足（最新 {_prices_latest}"
            f"{f' / 対象日 {_expect_dt}' if _expect_dt else ''}）"
            " → JQuants で直近営業日を取得して補完します（週末・祝日は即空で返るため無駄打ちなし）"
        )
        # JQuants が空で返す事象（EOD 反映待ち）への短いリトライ。--expect-price-date 指定時のみ
        # 対象日が揃うまで最大 3 回・60 秒間隔で再取得する（他レポートは従来どおり 1 回のみ）。
        _topup_attempts = 3 if _expect_dt is not None else 1
        for _attempt in range(1, _topup_attempts + 1):
            try:
                topup = fetch_price_history(codes, limit_codes=args.limit_codes,
                                            lookback_days=args.cold_lookback_days)
                if topup is not None and not topup.empty:
                    topup = topup.copy()
                    topup["Code"] = topup["Code"].astype("string").str.strip().str[:4]
                    topup["Date"] = pd.to_datetime(topup["Date"])
                    prices = (
                        pd.concat([prices, topup], ignore_index=True)
                        .drop_duplicates(subset=["Date", "Code"], keep="last")
                        .sort_values(["Code", "Date"])
                        .reset_index(drop=True)
                    )
                else:
                    print(f"[INFO] JQuants 補完が空で返りました（{_attempt}/{_topup_attempts} 回目）")
            except Exception as e:
                print(f"[WARN] JQuants 補完に失敗（{_attempt}/{_topup_attempts} 回目）: {type(e).__name__}: {e}")
            _prices_latest = prices["Date"].max().date()
            if _expect_dt is None or _prices_latest >= _expect_dt:
                break
            if _attempt < _topup_attempts:
                print(f"[WAIT] 対象日 {_expect_dt} が未着のため 60 秒待機して再取得します")
                time.sleep(60)

    price_data_asof_ts = prices["Date"].max()
    price_data_asof = price_data_asof_ts.date().isoformat()
    still_stale = is_stale_close(price_data_asof_ts.date(), today)
    print(
        f"価格データ最新日: {price_data_asof}（実行日 {today.isoformat()}・"
        f"営業日陳腐化={'あり' if still_stale else 'なし'}）"
    )
    if still_stale:
        print(
            f"[WARN] 価格マスターが営業日基準で陳腐化: 最新 {price_data_asof} / 実行日 {today.isoformat()}。"
            "price_history.yml の更新状況を確認してください（古い終値で週次リターンを算出する恐れ）。"
        )

    # PM 2026-07-12 確定（絶対配信原則・同日改定）: 対象日ゲート（--expect-price-date 指定時のみ）。
    # 価格データが対象日（当週金曜）まで届いていないと Return_W01 等の「今週」の窓が丸ごと前週へ
    # ずれる（2026-07-10 週次動意で 6/29〜7/3 の値を当週として全行配信・2986 -66.3%／4596 +55.4%）。
    # PM 2026-08-29 改定: 本スクリプトは従来どおり parquet を生成・保存した上で exit 3 を返すが、
    # 週次動意 workflow 側の扱いが「品質注記つき配信」から「生成中止」へ変わった。exit 3 は
    # mover_weekly.yml のハードゲート（generate-market へ進ませない）を発火させる検知シグナルであり、
    # 品質注記を付けて配信する経路は廃止済み。土日の再実行は新規営業日が無く
    # PriceDataAsOf=金曜のまま一致する。--expect-price-date は週次動意 GHA（mover_weekly.yml）のみが
    # 指定する。セクター週次 GHA・ローカル実行は従来動作のまま（フラグなし＝ゲート不適用）。
    date_gate_mismatch = bool(args.expect_price_date and price_data_asof != args.expect_price_date)
    if date_gate_mismatch:
        print(f"[BLOCKING] 価格データ最新日 {price_data_asof} が対象日 {args.expect_price_date} と不一致。"
              f"parquet は生成・保存した上で exit 3 を返す（週次動意 workflow は生成中止へ分岐）")

    # 投資主体別売買
    investor_df = fetch_investor_trading(skip=args.skip_investor_fetch)

    # 銘柄別指標計算
    print(f"指標計算中... (anchor={args.anchor})")
    metrics_df = compute_stock_metrics(prices, today, anchor=args.anchor)
    print(f"指標計算完了: {len(metrics_df)} 銘柄")

    # 結合
    print("銘柄テーブル構築中...")
    stock_df = build_stock_table(metrics_df, master_df, investor_df, today, etl_run_id, etl_started_jst, price_data_asof)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    stock_df.to_parquet(OUT_STOCK_PATH, index=False)
    print(f"保存: {OUT_STOCK_PATH} ({len(stock_df)} 行, {len(stock_df.columns)} カラム)")

    # セクター集計
    print("セクター集計中...")
    sector_df = build_sector_table(stock_df, today, etl_run_id, etl_started_jst, price_data_asof)
    sector_df.to_parquet(OUT_SECTOR_PATH, index=False)
    print(f"保存: {OUT_SECTOR_PATH} ({len(sector_df)} 行, {len(sector_df.columns)} カラム)")

    # サマリー
    print("\n=== セクター集計サマリー ===")
    cols = ["Sector17CodeName", "StockCount", "Return_W01", "Return_1Y", "PER_WAvg", "PBR_WAvg", "ROE_WAvg"]
    cols = [c for c in cols if c in sector_df.columns]
    print(sector_df[cols].to_string(index=False))
    print("\n完了！")

    # 対象日ゲート不一致の検知シグナル（生成・保存は完了済み・PM 2026-08-29 改定で workflow は生成中止）
    if date_gate_mismatch:
        print(f"[BLOCKING] PriceDataAsOf={price_data_asof} != 対象日 {args.expect_price_date} のため exit 3")
        sys.exit(3)


if __name__ == "__main__":
    main()
