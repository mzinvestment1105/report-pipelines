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


def fetch_price_history(codes: list[str], *, limit_codes: int = 0) -> pd.DataFrame:
    """全銘柄の価格履歴を get_eq_bars_daily_range で一括取得する。"""
    import jquantsapi

    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY が未設定です")
    client = jquantsapi.ClientV2(api_key=api_key)

    cache = _load_price_cache()
    fetch_from = date.today() - timedelta(days=LOOKBACK_YEARS * 365 + 30)
    if not cache.empty and "Date" in cache.columns:
        cached_max = cache["Date"].max().date()
        if cached_max >= fetch_from:
            fetch_from = cached_max + timedelta(days=1)

    today = date.today()
    if fetch_from > today:
        print(f"価格キャッシュは最新（{fetch_from} > {today}）、スキップ")
        return cache

    print(f"価格一括取得: {fetch_from} 〜 {today}（全銘柄）")
    df = client.get_eq_bars_daily_range(
        start_dt=fetch_from.strftime("%Y%m%d"),
        end_dt=today.strftime("%Y%m%d"),
    )
    print(f"取得完了: {len(df)} 行")

    if df.empty:
        print("新規価格データなし、キャッシュをそのまま使用")
        return cache

    # カラム正規化
    col_map = {}
    for col in df.columns:
        cl = col.lower()
        if cl in ("open", "o"):           col_map[col] = "O"
        elif cl in ("high", "h"):         col_map[col] = "H"
        elif cl in ("low", "l"):          col_map[col] = "L"
        elif cl in ("close", "c"):        col_map[col] = "C"
        elif cl in ("volume", "vo", "v"): col_map[col] = "V"
        elif cl == "date":                col_map[col] = "Date"
        elif cl == "code":                col_map[col] = "Code"
    df = df.rename(columns=col_map)

    for col in ["O", "H", "L", "C", "V"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = float("nan")

    df["Code"] = df["Code"].astype("string").str.strip().str[:4]
    df["Date"] = pd.to_datetime(df["Date"])

    keep = [c for c in ["Date", "Code", "O", "H", "L", "C", "V"] if c in df.columns]
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
        for w in range(1, WEEKLY_SLOTS + 1):
            cur_target = _week_target(w - 1)
            prev_target = _week_target(w)
            cur_series = close[close.index <= pd.Timestamp(cur_target)]
            prev_series = close[close.index <= pd.Timestamp(prev_target)]
            c_cur = cur_series.iloc[-1] if not cur_series.empty else float("nan")
            c_prev = prev_series.iloc[-1] if not prev_series.empty else float("nan")
            if pd.notna(c_cur) and pd.notna(c_prev) and c_prev != 0:
                row[f"Return_W{w:02d}"] = c_cur / c_prev - 1
            else:
                row[f"Return_W{w:02d}"] = float("nan")

        # スナップショットリターン（3M/6M/1Y/2Y/3Y）
        for label, bdays in zip(SNAPSHOT_LABELS, SNAPSHOT_DAYS):
            target = today - timedelta(days=int(bdays * 365 / 252))
            sub_before = close[close.index <= pd.Timestamp(target)]
            if sub_before.empty:
                row[f"Close_{label}"] = float("nan")
                row[f"Return_{label}"] = float("nan")
            else:
                snap_close = sub_before.iloc[-1]
                row[f"Close_{label}"] = snap_close
                row[f"Return_{label}"] = (latest_close / snap_close - 1) if snap_close != 0 and pd.notna(snap_close) else float("nan")

        # --- ボラティリティ（過去20営業日の日次リターンσ、年率換算） ---
        daily_ret = close.pct_change().dropna()
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

    # 信用倍率
    long_latest = pd.to_numeric(merged.get("LongMargin_WkSeq01", pd.Series(dtype=float)), errors="coerce")
    short_latest = pd.to_numeric(merged.get("ShortMargin_WkSeq01", pd.Series(dtype=float)), errors="coerce")
    merged["ShortMargin_Latest"] = short_latest
    merged["LongMargin_Latest"] = long_latest
    merged["MarginRatio"] = long_latest / short_latest.replace(0, float("nan"))

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

    # 価格キャッシュ
    if args.skip_price_fetch:
        print("価格取得スキップ")
        prices = _load_price_cache()
    else:
        prices = fetch_price_history(codes, limit_codes=args.limit_codes)

    if prices.empty:
        raise RuntimeError(
            "価格データがありません。--skip-price-fetch なしで実行してください。\n"
            f"キャッシュ保存先: {PRICE_CACHE_PATH}"
        )

    prices["Code"] = prices["Code"].astype("string").str.strip().str[:4]
    print(f"価格データ: {len(prices)} 行、銘柄 {prices['Code'].nunique()} 件")

    # 投資主体別売買
    investor_df = fetch_investor_trading(skip=args.skip_investor_fetch)

    # 銘柄別指標計算
    print(f"指標計算中... (anchor={args.anchor})")
    metrics_df = compute_stock_metrics(prices, today, anchor=args.anchor)
    print(f"指標計算完了: {len(metrics_df)} 銘柄")

    # 結合
    print("銘柄テーブル構築中...")
    stock_df = build_stock_table(metrics_df, master_df, investor_df, today, etl_run_id, etl_started_jst)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    stock_df.to_parquet(OUT_STOCK_PATH, index=False)
    print(f"保存: {OUT_STOCK_PATH} ({len(stock_df)} 行, {len(stock_df.columns)} カラム)")

    # セクター集計
    print("セクター集計中...")
    sector_df = build_sector_table(stock_df, today, etl_run_id, etl_started_jst)
    sector_df.to_parquet(OUT_SECTOR_PATH, index=False)
    print(f"保存: {OUT_SECTOR_PATH} ({len(sector_df)} 行, {len(sector_df.columns)} カラム)")

    # サマリー
    print("\n=== セクター集計サマリー ===")
    cols = ["Sector17CodeName", "StockCount", "Return_W01", "Return_1Y", "PER_WAvg", "PBR_WAvg", "ROE_WAvg"]
    cols = [c for c in cols if c in sector_df.columns]
    print(sector_df[cols].to_string(index=False))
    print("\n完了！")


if __name__ == "__main__":
    main()
