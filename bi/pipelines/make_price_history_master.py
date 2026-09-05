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
from universe_utils import universe_codes, load_screening_master_codes, RE_LETTER
from data_guards import check_adjustment_factor_consistency

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

#: 取得対象の母集団モード（universe_utils の唯一の正本に委譲）。
#: "equity" は 4桁数字コードに加えて英字コード（^[0-9]{3}[A-Z]$。2024年以降採番の
#: 285A 等）も取り込む。以前はここが 4桁数字だけを通していたため、英字コードが
#: price_history に一度も入らなかった。
UNIVERSE_MODE = "equity"


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
    """screening_master から取得対象コードを、出現順・重複排除済みで返す。

    形状の判定は universe_utils（母集団判定の唯一の正本）へ委譲する。UNIVERSE_MODE
    が "equity" なので 4桁数字コードに加えて英字コードも通る。以前はここに
    ``_is_4digit_code`` のインライン判定を持っていて英字コードを落としていた。

    universe_codes() は集合を返すため順序を持たない。screening_master の並びが
    そのまま取得順（=リトライ・進捗ログの並び）になるよう、元のリスト順で絞り直す。
    これにより 4桁コードの取得順・重複排除は従来と完全に一致する。
    """
    if not SCREENING_MASTER.exists():
        raise FileNotFoundError(
            f"{SCREENING_MASTER} が存在しません。screening_master を先に実行してください。"
        )
    df = pd.read_parquet(SCREENING_MASTER, columns=["Code"])
    codes = df["Code"].astype(str).map(normalize_code_4).drop_duplicates().tolist()
    allowed = universe_codes(codes, mode=UNIVERSE_MODE)
    return [c for c in codes if c in allowed]


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
    """年別 partition へ保存する。書き込みは一時ファイル + os.replace で原子的に行う。

    途中でプロセスが落ちても partition が半端な状態で残らないようにするため、直接
    to_parquet せず同一ディレクトリの .tmp へ書いてから置換する（別ドライブを跨がない
    ので os.replace は原子的に働く）。
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for year, year_df in df.groupby("Year"):
        path = OUT_DIR / f"{int(year)}.parquet"
        if merge_existing and path.exists():
            existing = pd.read_parquet(path)
            # 既存の列順を正とし、新規行を同じ並びへ揃えてから連結する。
            year_df = year_df.reindex(columns=list(existing.columns))
            combined = pd.concat([existing, year_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["Code", "Date"], keep="last")
            combined = combined.sort_values(["Code", "Date"]).reset_index(drop=True)
        else:
            combined = year_df.sort_values(["Code", "Date"]).reset_index(drop=True)
        tmp = path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp, index=False)
        os.replace(tmp, path)
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


#: 派生指標として enrich_price_data() が生成する列（= API 由来ではない列）。
#: 日次更新で「API から埋めるべき素の列」を partition のスキーマから逆算するのに使う。
DERIVED_COLS: frozenset[str] = frozenset({
    "PrevDayClose", "GapPct", "IntradayRangePct",
    "Return_1d", "Return_5d", "Return_20d", "Return_60d", "Return_120d", "Return_252d",
    "Year", "YearHigh", "YearLow", "QuarterHigh", "QuarterLow", "MonthHigh", "MonthLow",
    "YTDHigh", "YTDLow", "High52w", "Low52w", "DistFromHigh52w", "DistFromLow52w",
    "AllTimeHigh", "AllTimeLow", "Volume_vs_SMA20Ratio",
    "SMA20", "SMA50", "SMA200", "BB_Upper2sigma", "BB_Lower2sigma",
})

#: partition の列名 -> J-Quants 日足 API の列名候補（先に見つかったものを採る）。
#: partition は年によって調整済株価が短縮形（AdjO/AdjC）と長形（AdjustmentOpen/Close）に
#: 割れているため、どちらの並びにも同じ実データを流し込めるよう両方を張ってある。
API_COL_CANDIDATES: dict[str, list[str]] = {
    "Date": ["Date"], "Code": ["Code"],
    "Open": ["O", "Open"], "High": ["H", "High"], "Low": ["L", "Low"], "Close": ["C", "Close"],
    "Volume": ["Vo", "Volume"], "Value": ["Va", "TurnoverValue", "Value"],
    "AdjustmentFactor": ["AdjFactor", "AdjustmentFactor"],
    "AdjO": ["AdjO"], "AdjH": ["AdjH"], "AdjL": ["AdjL"], "AdjC": ["AdjC"], "AdjVo": ["AdjVo"],
    "AdjustmentOpen": ["AdjO", "AdjustmentOpen"], "AdjustmentHigh": ["AdjH", "AdjustmentHigh"],
    "AdjustmentLow": ["AdjL", "AdjustmentLow"], "AdjustmentClose": ["AdjC", "AdjustmentClose"],
    "UL": ["UL"], "LL": ["LL"],
}


def partition_latest_date(year: int) -> date | None:
    """`{year}.parquet` に入っている最新の Date を返す（無ければ None）。"""
    path = OUT_DIR / f"{year}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["Date"])
    if df.empty:
        return None
    return pd.to_datetime(df["Date"]).max().date()


def missing_trading_days(client, upto: date, *, max_scan_days: int = 30) -> list[date]:
    """partition の最新日より後・`upto` 以下で、API にデータがある営業日を古い順に返す。

    「1日でも走り損ねるとその日が永久に欠損する」という日次更新の構造的欠陥を塞ぐための
    探索。partition の最新日を起点に、`upto` までの各日を API へ問い合わせ、実際にデータが
    返る日（＝営業日）だけを拾う。土日祝はデータが返らないので自然に除外される。
    値の推定・補完は一切しない（API が返さない日は候補にしない）。
    """
    last = partition_latest_date(upto.year)
    if last is None:
        prev_last = partition_latest_date(upto.year - 1)
        if prev_last is None:
            raise FileNotFoundError(
                f"{OUT_DIR / f'{upto.year}.parquet'} がありません。initial を先に実行してください。")
        last = prev_last
    if last >= upto:
        return []

    days: list[date] = []
    d = last + timedelta(days=1)
    scanned = 0
    while d <= upto and scanned < max_scan_days:
        rows = fetch_paginated_v2(
            client, "/equities/bars/daily",
            params={"date": d.strftime("%Y-%m-%d")},
        )
        if rows:
            days.append(d)
        d += timedelta(days=1)
        scanned += 1
    return days


def cmd_daily(args) -> int:
    """欠損している営業日を古い順に年別 partition へ **追記** する（既存行は書き換えない）。

    既定では partition の最新日の翌日から最新営業日までの欠損営業日をすべて埋める
    （`--no-backfill` を付けると従来どおり最新営業日 1 日だけを処理する）。GHA の cron
    着火が遅延・失敗した日があっても、次に走った回が自動的に穴を埋める。
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY 未設定")

    client = jquantsapi.ClientV2(api_key=api_key)
    latest = latest_trading_day_date_v2(client)

    if getattr(args, "no_backfill", False):
        targets = [latest]
    else:
        targets = missing_trading_days(client, latest)
        if not targets:
            print(f"=== 欠損なし: partition は {latest.isoformat()} まで最新です ===")
            return 0
        if len(targets) > 1:
            print(f"=== バックフィル: 欠損 {len(targets)} 営業日を古い順に追記します "
                  f"({targets[0].isoformat()} 〜 {targets[-1].isoformat()}) ===")

    failed: list[date] = []
    for i, day in enumerate(targets, 1):
        print(f"\n---------- [{i}/{len(targets)}] {day.isoformat()} ----------")
        rc = append_one_day(client, day)
        if rc != 0:
            failed.append(day)

    if failed:
        print(f"\n=== 失敗 {len(failed)} 日: {[d.isoformat() for d in failed]} ===")
        return 1
    print(f"\n=== 完了: {len(targets)} 営業日を追記しました ===")
    return 0


def append_one_day(client, latest: date) -> int:
    """指定 1 営業日分を年別 partition へ **追記** する（既存行は一切書き換えない）。

    【2026-08 に修理した3点】
    旧実装は (1) 4桁数字コードだけを通す (2) partition を素の OHLCV だけから作り直す
    (3) merge_existing=False で partition 全体を置換する、という作りだった。このため
    日次更新が走るたびに

        - 英字コード（285A 等）が 1 行も入らない
        - 売買代金 Value が API の実値 Va ではなく round(Close x Volume) の近似で
          上書きされる（実測: 2026.parquet の4桁行 508,025 行すべてが近似値）
        - AdjustmentFactor が 1.0 に、AdjustmentClose が Close に潰される
          （API が返す AdjO/AdjC を _detect_columns が拾えず捏造側の枝に落ちるため）

    が起きていた。本実装は素の列を **partition の実スキーマから逆算** して API の実値を
    そのまま流し込み、派生指標を計算したあと **当日分の行だけ** を切り出して追記する。
    既存行に触れないので、過去に投入済みの実データ（英字コードの調整済株価など）が
    日次更新で潰れることはない。値の推定・補完は一切しない（欠損は欠損のまま）。

    【母集団】
    4桁コードは従来どおり形状のみで通す（screening_master と積を取ると既存 partition に
    居る銘柄を日次更新のたびに追い出してしまうため）。英字コードは screening_master との
    積集合に限る（ETF・投資信託など個別株でないものの混入を防ぐ）。
    """
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
    n_all = len(new_df)

    sm_codes = load_screening_master_codes(SCREENING_MASTER)
    keep = new_df["Code"].map(
        lambda c: _is_4digit_code(c) or (bool(RE_LETTER.match(c)) and c in sm_codes))
    new_df = new_df[keep].copy()
    n_letter = int(new_df["Code"].map(lambda c: bool(RE_LETTER.match(c))).sum())
    print(f"  取得: API {n_all:,} 行 -> 採用 {len(new_df):,} 銘柄 × 1 日"
          f"（うち英字コード {n_letter}）")

    year = latest.year
    target_path = OUT_DIR / f"{year}.parquet"
    if not target_path.exists():
        raise FileNotFoundError(f"{target_path} がありません。initial を先に実行してください。")

    target = pd.read_parquet(target_path)
    part_cols = list(target.columns)
    src_cols = [c for c in part_cols if c not in DERIVED_COLS]
    print(f"  partition スキーマ: {len(part_cols)} 列（うち素の列 {len(src_cols)}）")

    # --- API の1日分を partition の素の列へ写像する（実値のみ・捏造しない） ---------
    new_raw = pd.DataFrame()
    unmapped = []
    for col in src_cols:
        api_col = next((a for a in API_COL_CANDIDATES.get(col, [col]) if a in new_df.columns), None)
        if api_col is None:
            unmapped.append(col)
            continue
        new_raw[col] = new_df[api_col].to_numpy()
    if unmapped:
        # 対応する API 列が無い素の列は欠損のまま残す（推定値で埋めない）。
        print(f"  警告: API に対応列が無く欠損のまま残す素の列: {unmapped}")
        for col in unmapped:
            new_raw[col] = pd.NA
    new_raw["Date"] = pd.to_datetime(new_raw["Date"])

    # --- 派生指標のローリング窓を埋めるための助走データ（当年 + 前年） ---------------
    warm = [target[src_cols]]
    prev_path = OUT_DIR / f"{year - 1}.parquet"
    if prev_path.exists():
        prev = pd.read_parquet(prev_path)
        base_cols = ["Date", "Code", "Open", "High", "Low", "Close", "Volume"]
        if all(c in prev.columns for c in base_cols):
            warm.append(prev[base_cols])

    combined_raw = pd.concat([*warm, new_raw], ignore_index=True)
    combined_raw["Date"] = pd.to_datetime(combined_raw["Date"])
    combined_raw = combined_raw.drop_duplicates(subset=["Code", "Date"], keep="last")

    print(f"\n=== 派生指標計算（助走 {len(combined_raw):,} 行） ===")
    enriched = enrich_price_data(combined_raw)

    # 当日分の行だけを取り出して追記する（既存行は触らない）。
    day_df = enriched[enriched["Date"] == pd.Timestamp(latest)].copy()
    if day_df.empty:
        print(f"  {latest.isoformat()} の行が派生指標計算後に消えました（想定外）")
        return 1
    for col in part_cols:
        if col not in day_df.columns:
            day_df[col] = pd.NA
    # 既存ファイルの dtype に合わせる（UL/LL は既存が文字列 '0'/'1'）。
    for col in part_cols:
        want = str(target[col].dtype)
        if want == "object" and str(day_df[col].dtype) != "object":
            day_df[col] = day_df[col].astype("Int64").astype(str)
    day_df = day_df[part_cols]
    print(f"  追記対象: {len(day_df):,} 行 ({latest.isoformat()})")

    print(f"\n=== 年別 partition 保存（追記） ===")
    save_year_partition(day_df, merge_existing=True)

    # --- ガード: 分割・併合係数と生の終値比の整合 --------------------------------
    print(f"\n=== ガード: check_adjustment_factor_consistency ({year}.parquet) ===")
    check = pd.read_parquet(target_path, columns=["Date", "Code", "Close", "AdjustmentFactor"])
    print(check_adjustment_factor_consistency(check))
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

    p_daily = sub.add_parser(
        "daily", help="日次差分更新（既定で欠損営業日をすべてバックフィル）")
    p_daily.add_argument("--no-backfill", action="store_true",
                         help="最新営業日 1 日だけを処理する（従来動作）")

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
