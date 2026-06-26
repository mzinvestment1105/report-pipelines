import argparse
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import jquantsapi
import pandas as pd

from jq_client_utils import (
    fetch_paginated_v2 as _fetch_paginated_v2,
    latest_trading_day_date_v2 as _latest_trading_day_date_v2,
    previous_trading_day_date_v2 as _previous_trading_day_date_v2,
    normalize_code_4 as _normalize_code_4,
)
from short_sale_utils import (
    QUERY_DISC_DATE_COL,
    aggregate_short_sale_monthly_pool,
    aggregate_short_sale_weekly_snapshots,
)
from update_statements import (
    CRITICAL_COLS,
    STATEMENT_NUMERIC_COLS,
    aggregate_fins_summary_df,
    fins_summary_code_variants,
)
from tdnet_forecast_parser import fetch_tdnet_range_forecasts
from yfinance_statement_fallback import (
    build_statement_dict_from_yfinance,
    is_jquants_fins_history_thin,
    merge_jquants_with_yfinance_thin,
)
from yfinance_utils import fetch_yfinance_market_snapshot, fetch_yfinance_shares_fast


OUTPUT_PATH = Path("..") / "outputs" / "screening_master.parquet"
TEST_OUTPUT_PATH = Path("..") / "outputs" / "screening_master_test.parquet"
TEST_EXCEL_PATH = Path("..") / "outputs" / "screening_master_test.xlsx"
YFINANCE_AUDIT_PATH = Path("..") / "outputs" / "yfinance_audit.parquet"

UNIVERSE_MARKET_NAMES = {"プライム", "スタンダード", "グロース"}


def _to_numeric_df(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _gap_reason_series(api_err: pd.Series, field_note: pd.Series) -> pd.Series:
    """財務欠損行の分類（API失敗 / 部分欠損メモあり / ログなし）。"""
    ae = api_err.astype(object).where(api_err.notna(), "")
    fn = field_note.astype(object).where(field_note.notna(), "")
    ae_s = ae.astype(str).str.strip()
    fn_s = fn.astype(str).str.strip()
    out = pd.Series("fins_critical_all_na_no_api_log", index=api_err.index, dtype=object)
    out = out.mask(ae_s.ne(""), "fins_summary_api_fail")
    out = out.mask(ae_s.eq("") & fn_s.ne(""), "fins_summary_field_issue_or_empty_aggregate")
    return out


def _build_fins_data_gaps_df(
    master: pd.DataFrame,
    stmt_failures: list[tuple[str, str]],
    stmt_field_issues: list[tuple[str, str]],
) -> pd.DataFrame:
    """
    CRITICAL_COLS（売上・OP・純利益・自己資本比率・期末発行済株数）がすべて欠損の行だけ抽出。
    /fins/summary 失敗・部分欠損のメッセージを付与（財務だけ空の銘柄の一覧用）。
    """
    crit = list(CRITICAL_COLS)
    m = master.copy()
    for c in crit:
        if c not in m.columns:
            m[c] = pd.NA
    mask = m[crit].isna().all(axis=1)
    out = m.loc[mask].copy()
    if out.empty:
        return out

    fmap = {str(code): msg for code, msg in stmt_failures}
    imap = {str(code): msg for code, msg in stmt_field_issues}
    cstr = out["Code"].astype(str)
    out["fins_summary_api_error"] = cstr.map(fmap)
    out["fins_summary_field_note"] = cstr.map(imap)
    out["gap_reason"] = _gap_reason_series(out["fins_summary_api_error"], out["fins_summary_field_note"])

    front = [
        "Code",
        "CompanyName",
        "MarketCodeName",
        "Sector17CodeName",
        "Sector33CodeName",
        "gap_reason",
        "fins_summary_api_error",
        "fins_summary_field_note",
        "Close",
    ]
    front = [c for c in front if c in out.columns]
    rest = [c for c in out.columns if c not in front]
    return out[front + rest].sort_values("Code", kind="mergesort").reset_index(drop=True)


def _short_sale_institution_names_concat(g: pd.DataFrame, inst_col: str) -> str:
    """機関名を重複なく、出現順で「、」連結（空・「-」は除外）。"""
    parts: list[str] = []
    seen: set[str] = set()
    for raw in g[inst_col].tolist():
        s = str(raw).strip() if raw is not None else ""
        if not s or s in ("-", "nan", "None"):
            continue
        if s not in seen:
            seen.add(s)
            parts.append(s)
    return "、".join(parts)


def _last_friday(d: date) -> date:
    days_since_friday = (d.weekday() - 4) % 7
    return d - timedelta(days=days_since_friday)


def _fetch_fins_summary_rows_for_code(
    client: Any,
    code4: str,
    *,
    sleep_seconds: float = 1.2,
) -> list[Any]:
    """
    /fins/summary を **code パラメータのみ**で取得（4桁 / 5桁末尾0 の順）。

    日付さかのぼり（code+date・全市場 date）は **行わない**（遅い・429 のため削除）。
    code で 0 件の銘柄は空リスト → 集約は欠損のまま。
    """
    variants = fins_summary_code_variants(code4)
    for code_try in variants:
        rows = _fetch_paginated_v2(
            client,
            "/fins/summary",
            params={"code": code_try},
            sleep_seconds=sleep_seconds,
        )
        if rows:
            return rows
    return []


def _required_final_col_names(*, yfinance: bool, ss_weeks: int) -> list[str]:
    """screening_master 出力列の固定順（フルETL・market-refresh-only 共通）。"""
    cols = [
        "Code",
        "CompanyName",
        "MarketCodeName",
        "Sector17CodeName",
        "Sector33CodeName",
        "Close",
        "MarketCap",
    ]
    if yfinance:
        cols.extend(["YFinanceMarketCap", "YFinanceSharesOutstanding"])
    cols.extend(
        [
            "NetSales_PriorYear_Actual",
            "NetSales_LatestYear_Actual",
            "NetSales_NextYear_Forecast",
            "OperatingProfit_PriorYear_Actual",
            "OperatingProfit_LatestYear_Actual",
            "OperatingProfit_NextYear_Forecast",
            "Profit_PriorYear_Actual",
            "Profit_LatestYear_Actual",
            "Profit_NextYear_Forecast",
            "NetSales_TwoYearsPrior_Actual",
            "OperatingProfit_TwoYearsPrior_Actual",
            "Profit_TwoYearsPrior_Actual",
            "CashAndEquivalents_LatestFY",
            "Equity_LatestFY",
            "PER_Trailing",
            "PBR_Trailing",
            "PSR_Trailing",
            "ROE_LatestYear",
            "EquityToAssetRatio",
            "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock",
            "ShortMarginTradeVolume",
            "LongMarginTradeVolume",
            "LongMargin_WkSeq01",
            "LongMargin_WkSeq02",
            "LongMargin_WkSeq03",
            "LongMargin_WkSeq04",
            "LongMargin_WkSeq05",
            "LongMargin_WkSeq06",
            "LongMargin_WkSeq07",
            "LongMargin_WkSeq08",
            "ShortMargin_WkSeq01",
            "ShortMargin_WkSeq02",
            "ShortMargin_WkSeq03",
            "ShortMargin_WkSeq04",
            "ShortMargin_WkSeq05",
            "ShortMargin_WkSeq06",
            "ShortMargin_WkSeq07",
            "ShortMargin_WkSeq08",
            "DiscretionaryInvestmentContractorName",
            "ShortPositionsToSharesOutstandingRatio",
            "ShortPositionsInSharesNumber",
        ]
    )
    cols.extend([f"ShortSale_WkSeq{i:02d}" for i in range(1, ss_weeks + 1)])
    cols.extend(
        [
            "AvgDailyVolume5d",
            "VolAvg5d_BlkSeq01",
            "VolAvg5d_BlkSeq02",
            "VolAvg5d_BlkSeq03",
            "VolAvg5d_BlkSeq04",
            "VolAvg5d_BlkSeq05",
            "VolAvg5d_BlkSeq06",
            "VolAvg5d_BlkSeq07",
            "VolAvg5d_BlkSeq08",
            "AvgDailyValue5d",
            "ValAvg5d_BlkSeq01",
            "ValAvg5d_BlkSeq02",
            "ValAvg5d_BlkSeq03",
            "ValAvg5d_BlkSeq04",
            "ValAvg5d_BlkSeq05",
            "ValAvg5d_BlkSeq06",
            "ValAvg5d_BlkSeq07",
            "ValAvg5d_BlkSeq08",
            "AnnouncementDate",
            "FiscalQuarter",
            "FiscalYear",
            "YFinance_Supplemented",
            "ETLRunId",
            "ETLStartedAtUTC",
            "ETLStartedAtJST",
            # スクリーニング派生列（成長率・比率・偏差値）
            "Scr_LongMargin_to_SharesOutstanding",
            "Scr_LongMargin_to_AvgVol5d",
            "Scr_InstShort_to_Mcap",
            "Scr_Cash_to_Mcap",
            "Scr_Sales_CAGR2y",
            "Scr_Sales_Y1",
            "Scr_Sales_Y2",
            "Scr_OP_CAGR2y",
            "Scr_OP_Y1",
            "Scr_OP_Y2",
            "Scr_NI_CAGR2y",
            "Scr_NI_Y1",
            "Scr_NI_Y2",
            "Scr_Sales_FcstGrowth",
            "Scr_OP_FcstGrowth",
            "Scr_NI_FcstGrowth",
            "Scr_Hensachi_PER",
            "Scr_Hensachi_PBR",
            "Scr_Hensachi_ROE",
            "Scr_Hensachi_Sales_Y2",
            "Scr_Hensachi_OP_Y2",
            "Scr_Hensachi_NI_Y2",
            "Scr_Hensachi_Total",
        ]
    )
    return cols


def main() -> None:
    parser = argparse.ArgumentParser(
        description="スクリーニング用 master parquet 生成（J-Quants v2 API）"
    )
    parser.add_argument(
        "--code",
        type=str,
        default="",
        help="4桁銘柄だけ処理（検証用）。出力は screening_master_test.parquet（例: --code 1414）。"
        " 未指定かつ --limit も無いときだけ環境変数 SCREENING_TEST_CODE を参照。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="universe を銘柄コード昇順の先頭 N 件だけ処理（時間短縮）。出力は screening_master_limit{N}.parquet。"
        " --code と併用不可。例: --limit 100",
    )
    parser.add_argument(
        "--no-excel",
        action="store_true",
        help="Excel（.xlsx）を出さない。省略時は parquet と同名の .xlsx も出力する（要 openpyxl）。",
    )
    parser.add_argument(
        "--excel",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-data-gaps",
        action="store_true",
        help="財務欠損銘柄の別ファイル（*_data_gaps.parquet）を出さない。",
    )
    parser.add_argument(
        "--yfinance",
        action="store_true",
        help="yfinance で marketCap / sharesOutstanding を取得し列を追加。"
        " 値がある銘柄は MarketCap・期末発行済株数を Yahoo 値で上書き（J-Quants 計算は欠損時のみ）。"
        " 要: pip install yfinance。間隔は環境変数 YFINANCE_SLEEP（秒、既定0.35）。",
    )
    parser.add_argument(
        "--no-yfinance-statements",
        action="store_true",
        help="J-Quants /fins/summary の行数が少ない銘柄でも、Yahoo 年次損益へフォールバックしない。"
        " 省略時は yfinance が入っていれば自動（YFINANCE_STATEMENT_FALLBACK=0 で無効化）。",
    )
    parser.add_argument(
        "--market-refresh-only",
        action="store_true",
        help="既存 parquet を読み、信用・空売り・終値/出来高/売買代金ブロックだけ再取得して上書き（財務・上場情報は据え置き）。"
        " 要: あらかじめ screening_master.parquet が存在すること。",
    )
    parser.add_argument(
        "--check-splits",
        action="store_true",
        help="株式分割検出モード（旧版・一律補正）: JQuants v2 /equities/bars/daily の AdjFactor (1.0以外=分割発生日) を"
        " SPLIT_LOOKBACK_DAYS（既定 250営業日 ≒ 1年）遡ってスキャンし、"
        " 累積分割比率が SPLIT_RATIO_THRESHOLD（既定 1.05）以上の銘柄について"
        " ShOutFY を比率倍に補正し MarketCap を再計算する。"
        " 注意: JQuants /fins/summary が決算開示時点で分割反映済みの ShOutFY を返すため、"
        " 分割後に決算開示があった銘柄では二重補正になる。--check-splits-conditional の使用を推奨。",
    )
    parser.add_argument(
        "--check-splits-conditional",
        action="store_true",
        help="株式分割検出モード（条件付き補正版・推奨）: --check-splits と同じく AdjFactor をスキャンするが、"
        " 各分割イベントについて『分割発生日 > 最新の決算開示日（StatementDisclosedDate）』の銘柄のみ"
        " ShOutFY を補正する。JQuants /fins/summary が分割反映済みで返す仕様に対応し、"
        " 旧 --check-splits の二重補正バグを回避する。design.md: dev/review/2026-05-09_split_double_correction/",
    )
    args = parser.parse_args()
    etl_started_at_utc = datetime.now(timezone.utc).replace(microsecond=0)
    jst = timezone(timedelta(hours=9))
    etl_started_at_jst = etl_started_at_utc.astimezone(jst)
    etl_started_at_utc_str = etl_started_at_utc.isoformat()
    etl_started_at_jst_str = etl_started_at_jst.isoformat()
    etl_run_id = etl_started_at_jst.strftime("%Y%m%d-%H%M%S")
    want_excel = not args.no_excel
    _yf_stmt_env = os.environ.get("YFINANCE_STATEMENT_FALLBACK", "1").strip().lower()
    use_yfinance_statement_fallback = (not args.no_yfinance_statements) and _yf_stmt_env not in (
        "0",
        "false",
        "no",
        "off",
    )

    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY が未設定です。")

    limit_n = max(0, int(args.limit))
    # --limit のときは環境変数 SCREENING_TEST_CODE を無視（残っていると単一銘柄と衝突する）
    test_code_raw = (args.code or "").strip()
    if limit_n > 0 and test_code_raw:
        raise ValueError("--code と --limit は同時に指定できません。")
    if not test_code_raw and limit_n == 0:
        test_code_raw = os.environ.get("SCREENING_TEST_CODE", "").strip()
    test_code_norm = _normalize_code_4(test_code_raw) if test_code_raw else ""

    if test_code_norm:
        out_path = TEST_OUTPUT_PATH
    elif limit_n > 0:
        out_path = OUTPUT_PATH.with_name(f"screening_master_limit{limit_n}.parquet")
    else:
        out_path = OUTPUT_PATH

    client = jquantsapi.ClientV2(api_key=api_key)

    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    if args.market_refresh_only:
        from market_overlays import (
            fetch_px_vol_val_overlay,
            fetch_short_sale_overlay,
            fetch_weekly_margin_overlay,
            merge_market_into_master,
        )

        if not out_path.exists():
            raise FileNotFoundError(
                f"--market-refresh-only には既存の parquet が必要です: {out_path}"
            )
        master_refresh = pd.read_parquet(out_path)
        codes_set_refresh = set(master_refresh["Code"].dropna().astype(str))
        _ss_w_refresh = max(1, int(os.environ.get("SHORT_SALE_LOOKBACK_WEEKS", "8")))
        print(f"market-refresh-only: codes={len(codes_set_refresh)} -> {out_path}")
        ss_ov = fetch_short_sale_overlay(client, today, codes_set_refresh)
        wm_ov = fetch_weekly_margin_overlay(client, today, codes_set_refresh)
        px_ov = fetch_px_vol_val_overlay(client, today)
        _codes_list_refresh = master_refresh["Code"].dropna().astype(str).tolist()
        master_refresh = merge_market_into_master(
            master_refresh,
            px_ov,
            wm_ov,
            ss_ov,
            refresh_yfinance=args.yfinance,
            yf_codes=_codes_list_refresh if args.yfinance else None,
        )
        master_refresh["ETLRunId"] = etl_run_id
        master_refresh["ETLStartedAtUTC"] = etl_started_at_utc_str
        master_refresh["ETLStartedAtJST"] = etl_started_at_jst_str
        _req = _required_final_col_names(yfinance=args.yfinance, ss_weeks=_ss_w_refresh)
        for c in _req:
            if c not in master_refresh.columns:
                master_refresh[c] = pd.NA
        master_refresh = master_refresh[_req].copy().sort_values("Code", kind="mergesort").reset_index(drop=True)
        tmp_r = out_path.with_suffix(out_path.suffix + ".tmp")
        master_refresh.to_parquet(tmp_r, index=False)
        tmp_r.replace(out_path)
        print(f"market-refresh-only saved: {out_path} rows={len(master_refresh)} cols={len(master_refresh.columns)}")
        if want_excel:
            from convert_to_excel import parquet_to_excel

            xlsx_path = out_path.with_suffix(".xlsx")
            try:
                xout, _xf = parquet_to_excel(out_path, xlsx_path)
                print(f"excel: {xout}")
            except PermissionError:
                print(
                    f"Excel 出力スキップ（ロック）: {xlsx_path} を閉じて "
                    f"`python convert_to_excel.py --input {out_path} --output ...` を実行してください。"
                )
        return

    # 1) listed/info (v2: equities/master)
    print(f"listed: date={today_str}")
    eq_master_rows = _fetch_paginated_v2(
        client, "/equities/master", params={"date": today_str}, sleep_seconds=1.2
    )
    eq_master_df = pd.DataFrame.from_records(eq_master_rows)

    if eq_master_df.empty:
        universe_df = pd.DataFrame(
            columns=["Code", "CompanyName", "MarketCodeName", "Sector17CodeName", "Sector33CodeName"]
        )
    else:
        eq_master_df = eq_master_df.copy()
        eq_master_df["Code"] = eq_master_df["Code"].map(_normalize_code_4).astype(str)
        eq_master_df = eq_master_df.rename(
            columns={
                "CoName": "CompanyName",
                "MktNm": "MarketCodeName",
                "S17Nm": "Sector17CodeName",
                "S33Nm": "Sector33CodeName",
            }
        )
        universe_df = eq_master_df[
            ["Code", "CompanyName", "MarketCodeName", "Sector17CodeName", "Sector33CodeName"]
        ].copy()
        universe_df = universe_df[universe_df["MarketCodeName"].isin(UNIVERSE_MARKET_NAMES)]
        universe_df = universe_df.drop_duplicates("Code").reset_index(drop=True)

    codes = universe_df["Code"].dropna().astype(str).unique().tolist()
    if test_code_norm:
        u_before = len(universe_df)
        universe_df = universe_df.loc[universe_df["Code"].astype(str) == test_code_norm].copy()
        if universe_df.empty:
            raise ValueError(
                f"--code / SCREENING_TEST_CODE={test_code_raw!r} は universe に存在しません "
                f"(正規化後={test_code_norm!r}, master件数={u_before})。"
            )
        codes = [test_code_norm]
        print(f"単一銘柄モード: {test_code_norm} のみ（出力: {out_path}）")
    elif limit_n > 0:
        universe_df = (
            universe_df.sort_values("Code", kind="mergesort").head(limit_n).reset_index(drop=True)
        )
        codes = universe_df["Code"].dropna().astype(str).tolist()
        print(f"--limit {limit_n}: 先頭 {len(codes)} 銘柄のみ処理（出力: {out_path}）")

    print(f"universe codes: {len(codes)}")

    # 1b) short-sale-report（財務・信用枠より先）
    # --limit などで先に /fins/summary を大量に叩いた直後に空売りを取ると 429 等でページが欠け、
    # プール行数が減って株数が小さく出ることがある。universe 確定後すぐ全市場を取得し、
    # 集計後に codes_set だけ残す（銘柄別の合算は独立のため数値は一致する）。
    ss_weeks = max(1, int(os.environ.get("SHORT_SALE_LOOKBACK_WEEKS", "8")))
    short_sale_back_days = max(0, int(os.environ.get("SHORT_SALE_LOOKBACK_DAYS", str(ss_weeks * 7 + 14))))
    ss_sleep = float(os.environ.get("SHORT_SALE_SLEEP", "1.2"))
    codes_set = set(universe_df["Code"].astype(str)) if not universe_df.empty else set()
    ss_frames: list[pd.DataFrame] = []
    for i in range(0, short_sale_back_days + 1):
        d_scan = today - timedelta(days=i)
        disc_date = d_scan.strftime("%Y-%m-%d")
        ss_rows = _fetch_paginated_v2(
            client,
            "/markets/short-sale-report",
            params={"disc_date": disc_date},
            sleep_seconds=ss_sleep,
        )
        if not ss_rows:
            continue
        tmp = pd.DataFrame.from_records(ss_rows)
        if tmp.empty:
            continue
        tmp = tmp.copy()
        tmp["Code"] = tmp["Code"].map(_normalize_code_4).astype(str)
        tmp[QUERY_DISC_DATE_COL] = disc_date
        ss_frames.append(tmp)
        n_u = int(tmp["Code"].isin(codes_set).sum()) if codes_set else len(tmp)
        print(f"short_sale-report: disc_date={disc_date} rows_all={len(tmp)} rows_in_universe={n_u}")

    if not ss_frames:
        ss_df = pd.DataFrame(
            columns=[
                "Code",
                "DiscretionaryInvestmentContractorName",
                "ShortPositionsToSharesOutstandingRatio",
                "ShortPositionsInSharesNumber",
            ]
        )
    else:
        ss_df = pd.concat(ss_frames, ignore_index=True)
        ss_df = ss_df.copy()
        ss_df["Code"] = ss_df["Code"].map(_normalize_code_4).astype(str)
        ss_df = ss_df.rename(
            columns={
                "DICName": "DiscretionaryInvestmentContractorName",
                "ShrtPosToSO": "ShortPositionsToSharesOutstandingRatio",
                "ShrtPosShares": "ShortPositionsInSharesNumber",
            }
        )
        ss_df = _to_numeric_df(
            ss_df,
            ["ShortPositionsToSharesOutstandingRatio", "ShortPositionsInSharesNumber"],
        )
        ss_pool_df = ss_df.copy()  # 週次スナップショット用に生プールを保持
        print(
            f"short_sale: pool rows={len(ss_df)} lookback_days={short_sale_back_days} "
            f"(全市場→各 inst_key 最新1行→銘柄合算→universe のみ出力)"
        )

        inst_col = "DiscretionaryInvestmentContractorName"
        shares_col = "ShortPositionsInSharesNumber"
        ratio_col = "ShortPositionsToSharesOutstandingRatio"

        inst_dedup, total_shares, ratio_max = aggregate_short_sale_monthly_pool(ss_df)
        if codes_set:
            _cs = codes_set
            inst_dedup = inst_dedup[inst_dedup["Code"].astype(str).isin(_cs)]
            total_shares = total_shares[total_shares["Code"].astype(str).isin(_cs)]
            ratio_max = ratio_max[ratio_max["Code"].astype(str).isin(_cs)]

        names_rows: list[dict[str, Any]] = []
        for code, g in inst_dedup.groupby("Code", sort=False):
            names_rows.append(
                {
                    "Code": code,
                    "DiscretionaryInvestmentContractorName": _short_sale_institution_names_concat(g, inst_col),
                }
            )
        names_df = (
            pd.DataFrame(names_rows)
            if names_rows
            else pd.DataFrame(columns=["Code", "DiscretionaryInvestmentContractorName"])
        )

        ss_df = total_shares.merge(names_df, on="Code", how="left").merge(ratio_max, on="Code", how="left")

        ss_df = ss_df[
            [
                "Code",
                "DiscretionaryInvestmentContractorName",
                "ShortPositionsToSharesOutstandingRatio",
                "ShortPositionsInSharesNumber",
            ]
        ].copy()

        # 週次スナップショット: ShortSale_WkSeq01（最古）〜WkSeqNN（直近）
        ss_fridays: list[date] = []
        _d_fr = _last_friday(today)
        for _ in range(ss_weeks):
            ss_fridays.append(_d_fr)
            _d_fr = _d_fr - timedelta(days=7)
        ss_weekly_df = aggregate_short_sale_weekly_snapshots(
            ss_pool_df, ss_fridays, n_weeks=ss_weeks
        )
        if codes_set:
            ss_weekly_df = ss_weekly_df[ss_weekly_df["Code"].astype(str).isin(codes_set)]
        ss_df = ss_df.merge(ss_weekly_df, on="Code", how="left")
        print(
            f"short_sale_weekly: weeks={ss_weeks} "
            f"cols={[c for c in ss_df.columns if c.startswith('ShortSale_WkSeq')]}"
        )

    # 2) fins/statements (v2: fins/summary) latest per Code
    statement_latest_rows: list[pd.DataFrame] = []
    stmt_failures: list[tuple[str, str]] = []
    stmt_field_issues: list[tuple[str, str]] = []
    yf_audit_rows: list[dict] = []  # Yahoo Finance 補完監査ログ
    _fins_sleep = float(os.environ.get("FINS_SUMMARY_FALLBACK_SLEEP", "1.2"))
    print(
        "fins/summary: code のみ取得（日付さかのぼりなし）"
        + (
            " | 薄い銘柄→Yahoo年次損益で補完（要 yfinance / YFINANCE_STATEMENT_FALLBACK=0 で無効）"
            if use_yfinance_statement_fallback
            else ""
        )
    )

    for i, code4 in enumerate(codes, start=1):
        try:
            # 銘柄ごとに /fins/summary を叩くため 429 になりやすい → 間隔を長めに
            rows = _fetch_fins_summary_rows_for_code(
                client, code4, sleep_seconds=_fins_sleep
            )
            fin_df = pd.DataFrame.from_records(rows)
            ser, agg_err = aggregate_fins_summary_df(fin_df)
            if agg_err is not None or ser is None:
                raise RuntimeError(agg_err or "empty /fins/summary")

            _is_thin = use_yfinance_statement_fallback and is_jquants_fins_history_thin(fin_df)
            _yf_fetched = False
            _yf_used = False
            _ser_before_yf: dict | None = None
            _ydict: dict | None = None
            if _is_thin:
                try:
                    _ydict = build_statement_dict_from_yfinance(code4)
                    _yf_fetched = True
                    if _ydict is not None:
                        _ser_before_yf = dict(ser)
                        ser = merge_jquants_with_yfinance_thin(
                            ser, _ydict,
                            jq_fye_latest=ser.get("_jq_fye_latest"),
                        )
                        _yf_used = True
                except ImportError:
                    pass
                except Exception:
                    pass
                if _ydict is not None:
                    _ys = float(os.environ.get("YFINANCE_SLEEP", "0.35"))
                    if _ys > 0:
                        time.sleep(_ys)
                # 監査レコード: thin 銘柄は全件記録
                _audit: dict = {
                    "Code": code4,
                    "JQ_Thin": True,
                    "YFinance_Fetched": _yf_fetched,
                    "YFinance_Used": _yf_used,
                    "JQ_TotalRows": len(fin_df) if fin_df is not None else 0,
                }
                for _k in STATEMENT_NUMERIC_COLS:
                    _jv = (_ser_before_yf or ser).get(_k)
                    _yv = _ydict.get(_k) if _ydict else None
                    _fv = ser.get(_k)
                    _audit[f"{_k}_JQ"] = _jv
                    _audit[f"{_k}_YF"] = _yv
                    _audit[f"{_k}_Final"] = _fv
                    if pd.isna(_fv):
                        _src = "NONE"
                    elif _yf_used and _ydict and pd.notna(_yv) and (pd.isna(_jv) or _fv == _yv):
                        _src = "YF"
                    else:
                        _src = "JQ"
                    _audit[f"{_k}_Source"] = _src
                yf_audit_rows.append(_audit)

            # --- TDNet 幅付き予想フォールバック ---
            # JQuants が幅付き予想（レンジ形式）のため NULL を返す銘柄向け。
            # TDNet Atom から最新の決算短信 PDF をパースして中央値を補完。
            _FORECAST_COLS = [
                "NetSales_NextYear_Forecast",
                "OperatingProfit_NextYear_Forecast",
                "Profit_NextYear_Forecast",
            ]
            _tdnet_used = False
            if any(pd.isna(ser.get(c)) for c in _FORECAST_COLS):
                try:
                    _tdnet = fetch_tdnet_range_forecasts(code4, sleep_seconds=1.0)
                    for _fc, _fv in _tdnet.items():
                        if _fc in _FORECAST_COLS and pd.isna(ser.get(_fc)) and pd.notna(_fv):
                            ser[_fc] = _fv
                            _tdnet_used = True
                except Exception as e:
                    print(f"[WARN] TDNet幅付き予想取得失敗 {code4}: {e}")

            # 開示日はマージ用に raw の最大値（列集約後も「直近の開示」に近い）
            if "DiscDate" in fin_df.columns:
                dd = pd.to_datetime(fin_df["DiscDate"], errors="coerce")
                disc_out = dd.max() if dd.notna().any() else pd.NaT
            else:
                disc_out = pd.NaT

            # 内部キー（_jq_fye_*, _yf_* 等）を除去してから出力行を作成
            for _internal_key in list(ser.keys()):
                if _internal_key.startswith("_jq_fye_") or _internal_key.startswith("_yf_"):
                    del ser[_internal_key]

            one_row = pd.DataFrame(
                [{"Code": code4, **{c: ser[c] for c in STATEMENT_NUMERIC_COLS}, "DiscDate": disc_out, "YFinance_Supplemented": _yf_used, "TDNet_Supplemented": _tdnet_used}]
            )
            statement_latest_rows.append(one_row)

            # Log missing required numeric fields for easier debugging.
            nan_fields = [
                c
                for c in STATEMENT_NUMERIC_COLS
                if pd.isna(ser[c])
            ]
            critical_cols = set(CRITICAL_COLS)
            if nan_fields and any(c in critical_cols for c in nan_fields):
                shown = nan_fields[:10]
                suffix = "..." if len(nan_fields) > len(shown) else ""
                stmt_field_issues.append(
                    (code4, f"missing {len(nan_fields)} fields: {','.join(shown)}{suffix}")
                )
        except KeyboardInterrupt:
            print("\n[中断]")
            raise
        except Exception as e:
            stmt_failures.append((code4, f"{type(e).__name__}: {e}"))
        finally:
            if test_code_norm or i == 1 or i % 50 == 0 or i == len(codes):
                print(
                    f"statements progress: {i}/{len(codes)} "
                    f"(ok={len(statement_latest_rows)} fail={len(stmt_failures)})"
                )

    statements_df = (
        pd.concat(statement_latest_rows, ignore_index=True)
        if statement_latest_rows
        else pd.DataFrame(columns=["Code"] + STATEMENT_NUMERIC_COLS + ["DiscDate"])
    )
    if not statements_df.empty:
        statements_df = statements_df.drop_duplicates("Code")
        if "DiscDate" in statements_df.columns:
            statements_df["DiscDate"] = pd.to_datetime(statements_df["DiscDate"], errors="coerce")
            statements_df = statements_df.rename(columns={"DiscDate": "StatementDisclosedDate"})
        else:
            statements_df["StatementDisclosedDate"] = pd.NaT
    else:
        statements_df["StatementDisclosedDate"] = pd.NaT

    # 3) /fins/announcement (v2: equities/earnings-calendar)
    print("announcement: (next business day)")
    ann_rows = _fetch_paginated_v2(client, "/equities/earnings-calendar", params={})
    ann_df = pd.DataFrame.from_records(ann_rows)
    if ann_df.empty:
        ann_df = pd.DataFrame(columns=["Code", "AnnouncementDate", "FiscalQuarter", "FiscalYear"])
    else:
        ann_df = ann_df.copy()
        ann_df["Code"] = ann_df["Code"].map(_normalize_code_4).astype(str)
        ann_df["Date"] = pd.to_datetime(ann_df["Date"], errors="coerce")
        ann_df = ann_df.rename(columns={"Date": "AnnouncementDate", "FQ": "FiscalQuarter", "FY": "FiscalYear"})
        ann_df = ann_df[["Code", "AnnouncementDate", "FiscalQuarter", "FiscalYear"]].copy()
        ann_df = ann_df.drop_duplicates("Code", keep="last")

    # 4) weekly_margin_interest (v2: markets/margin-interest)
    # 直近金曜を週次アンカーに、欠損は当日から最大 N 日さかのぼり。
    # デフォルト 8 週 ≒2 か月分の買残履歴（playbook: トレンド確認用）。MARGIN_INTEREST_LOOKBACK_WEEKS で変更可。
    margin_weeks = max(1, int(os.environ.get("MARGIN_INTEREST_LOOKBACK_WEEKS", "8")))
    margin_day_fallback = max(0, int(os.environ.get("MARGIN_INTEREST_DAY_FALLBACK", "2")))
    margin_verbose = os.environ.get("SCREENING_VERBOSE_MARGIN", "").strip().lower() in ("1", "true", "yes")
    print(
        f"weekly_margin_interest: weeks={margin_weeks} day_fallback={margin_day_fallback} "
        f"(増やす: MARGIN_INTEREST_* / 週ごと詳細: SCREENING_VERBOSE_MARGIN=1)"
    )
    _m_sleep = float(os.environ.get("MARGIN_INTEREST_SLEEP", "1.5"))
    fridays: list[date] = []
    d_fr = _last_friday(today)
    for _ in range(margin_weeks):
        fridays.append(d_fr)
        d_fr = d_fr - timedelta(days=7)

    # 週ごとのデータを収集し、後でピボット（W1=最新〜WN=N週前）
    wm_all_weeks: list[pd.DataFrame] = []
    for i, frd in enumerate(fridays):
        # 週次の基準日は「通常金曜」だが祝日等で空の週がある → 同一週内で最大N日さかのぼって取得
        wm_rows: list[dict[str, Any]] = []
        chosen_str = ""
        for delta in range(margin_day_fallback + 1):
            d_try = frd - timedelta(days=delta)
            ds = d_try.strftime("%Y-%m-%d")
            cand = _fetch_paginated_v2(
                client,
                "/markets/margin-interest",
                params={"date": ds},
                sleep_seconds=_m_sleep,
            )
            if cand:
                wm_rows = cand
                chosen_str = ds
                break
        if not wm_rows:
            print(
                f"weekly_margin_interest: week {i + 1}/{margin_weeks} anchor={frd}: "
                f"empty ({margin_day_fallback}d fallback exhausted)"
            )
            continue
        anchor_s = frd.strftime("%Y-%m-%d")
        if margin_verbose:
            if chosen_str != anchor_s:
                _cd = date.fromisoformat(chosen_str)
                print(
                    f"weekly_margin_interest: week {i + 1}/{margin_weeks} date={chosen_str} "
                    f"(anchor {anchor_s}, -{(frd - _cd).days}d)"
                )
            else:
                print(f"weekly_margin_interest: week {i + 1}/{margin_weeks} date={chosen_str}")

        chunk = pd.DataFrame.from_records(wm_rows)
        if chunk.empty:
            continue
        chunk = chunk.copy()
        # 5桁のまま保持してから重複解消する（先に4桁化すると 94340/94345/94346 のような
        # 別銘柄が同一視され、0/0 行が優先されることがある）
        chunk["CodeRaw"] = chunk["Code"].astype(str)
        # 同一 CodeRaw に IssType 別の複数行: 1→2→3 の順で、かつ ShrtVol/LongVol が埋まっている行を優先
        did_iss_dedup = False
        if "IssType" in chunk.columns:
            it = pd.to_numeric(chunk["IssType"], errors="coerce")
            rank = pd.Series(99, index=chunk.index, dtype="int64")
            rank = rank.where(~it.eq(1), 0)
            rank = rank.where(~it.eq(2), 1)
            rank = rank.where(~it.eq(3), 2)
            sv0 = pd.to_numeric(chunk["ShrtVol"], errors="coerce")
            lv0 = pd.to_numeric(chunk["LongVol"], errors="coerce")
            vol_score = sv0.notna().astype("int8") + lv0.notna().astype("int8")
            chunk = (
                chunk.assign(_iss_rank=rank, _vol_score=vol_score)
                .sort_values(["CodeRaw", "_iss_rank", "_vol_score"], ascending=[True, True, False])
                .drop(columns=["_iss_rank", "_vol_score"])
            )
            chunk = chunk.drop_duplicates("CodeRaw", keep="first")
            did_iss_dedup = True
        chunk = chunk.rename(
            columns={"ShrtVol": "ShortMarginTradeVolume", "LongVol": "LongMarginTradeVolume"}
        )
        chunk = _to_numeric_df(chunk, ["ShortMarginTradeVolume", "LongMarginTradeVolume"])
        chunk["Code"] = chunk["CodeRaw"].map(_normalize_code_4).astype(str)
        chunk = chunk[["Code", "ShortMarginTradeVolume", "LongMarginTradeVolume"]].copy()
        if not did_iss_dedup:
            pass
        s_num = pd.to_numeric(chunk["ShortMarginTradeVolume"], errors="coerce")
        l_num = pd.to_numeric(chunk["LongMarginTradeVolume"], errors="coerce")
        chunk = (
            chunk.assign(
                _vol_score=(
                    s_num.notna().astype("int8")
                    + l_num.notna().astype("int8")
                    + s_num.fillna(0).ne(0).astype("int8")
                    + l_num.fillna(0).ne(0).astype("int8")
                ),
                _vol_abs=(s_num.abs().fillna(0) + l_num.abs().fillna(0)),
            )
            .sort_values(["Code", "_vol_score", "_vol_abs"], ascending=[True, False, False])
            .drop_duplicates("Code", keep="first")
            .drop(columns=["_vol_score", "_vol_abs"])
        )
        chunk["_week_idx"] = i  # 0=最新週（直近金曜系）, 1=1週前, ...
        wm_all_weeks.append(chunk)

    # 週次を結合: Short は各 Code の最新週（最小 _week_idx）行。Long は 8 週分を列展開
    # LongMargin_WkSeq01=最古 … WkSeq08=直近（week_idx 0）。欠損週は NA。
    wm_df = pd.DataFrame(columns=["Code", "ShortMarginTradeVolume", "LongMarginTradeVolume"])
    if wm_all_weeks:
        all_long = pd.concat(wm_all_weeks, ignore_index=True)

        idx_latest = all_long.groupby("Code", sort=False)["_week_idx"].idxmin()
        latest = (
            all_long.loc[idx_latest, ["Code", "ShortMarginTradeVolume", "LongMarginTradeVolume"]]
            .drop_duplicates("Code")
            .reset_index(drop=True)
        )

        def _pivot_margin_col(all_df: pd.DataFrame, value_col: str, prefix: str, n: int) -> tuple[pd.DataFrame, list[str]]:
            """週次データを WkSeq01（最古）〜WkSeqNN（直近）列にピボット展開して返す。"""
            src = all_df[["Code", "_week_idx", value_col]].drop_duplicates(["Code", "_week_idx"])
            pv = src.pivot(index="Code", columns="_week_idx", values=value_col).reset_index()
            result = pv[["Code"]].copy()
            cols: list[str] = []
            for seq in range(1, n + 1):
                wi = n - seq  # week_idx: 0=直近, n-1=最古
                col = f"{prefix}_WkSeq{seq:02d}"
                cols.append(col)
                if wi in pv.columns:
                    result = result.merge(pv[["Code", wi]].rename(columns={wi: col}), on="Code", how="outer")
                else:
                    result[col] = pd.NA
            return result, cols

        long_pivot, long_seq_cols = _pivot_margin_col(all_long, "LongMarginTradeVolume", "LongMargin", margin_weeks)
        short_pivot, short_seq_cols = _pivot_margin_col(all_long, "ShortMarginTradeVolume", "ShortMargin", margin_weeks)

        wm_df = latest[["Code"]].copy()
        wm_df = wm_df.merge(long_pivot, on="Code", how="outer")
        wm_df = wm_df.merge(short_pivot, on="Code", how="outer")

        for val_col, seq_col in [
            ("LongMarginTradeVolume",  f"LongMargin_WkSeq{margin_weeks:02d}"),
            ("ShortMarginTradeVolume", f"ShortMargin_WkSeq{margin_weeks:02d}"),
        ]:
            if seq_col in wm_df.columns:
                wm_df[val_col] = pd.to_numeric(wm_df[seq_col], errors="coerce")
            else:
                wm_df[val_col] = pd.NA

        _s = wm_df["ShortMarginTradeVolume"].notna().sum()
        _l = wm_df["LongMarginTradeVolume"].notna().sum()
        n_weeks_got = len(wm_all_weeks)
        print(
            f"margin_interest 集計後: rows={len(wm_df)} weeks_fetched={n_weeks_got} "
            f"Short非欠損={int(_s)} Long非欠損={int(_l)} "
            f"(買残8週列: {long_seq_cols} / 売残8週列: {short_seq_cols})"
        )
        if not universe_df.empty:
            uc = set(universe_df["Code"].astype(str))
            sub = wm_df.loc[wm_df["Code"].astype(str).isin(uc)]
            nu = len(uc)
            su = int(sub["ShortMarginTradeVolume"].notna().sum())
            lu = int(sub["LongMarginTradeVolume"].notna().sum())
            if nu > 0:
                print(
                    f"  universe({nu}銘柄)に対し Short埋まり={su} ({100.0 * su / nu:.1f}%) "
                    f"Long埋まり={lu} ({100.0 * lu / nu:.1f}%)"
                )

    # 6) daily_quotes (v2: equities/bars/daily)
    trading_day_latest = _latest_trading_day_date_v2(client)
    # 出来高ブロック数（5日×N）: デフォルト8ブロック=40営業日
    vol_blocks = max(1, int(os.environ.get("VOLUME_BLOCK_WEEKS", "8")))
    vol_days_total = vol_blocks * 5  # 5営業日×ブロック数
    trading_days: list[date] = [trading_day_latest]
    d_prev = trading_day_latest
    for _ in range(vol_days_total - 1):
        d_prev = _previous_trading_day_date_v2(client, before=d_prev, max_back_days=14)
        trading_days.append(d_prev)

    # latest day: Close 用（従来どおり 1日分のみ）
    trading_str_latest = trading_day_latest.strftime("%Y-%m-%d")
    print(f"daily_quotes: latest date={trading_str_latest} vol_blocks={vol_blocks}({vol_days_total}日)")
    latest_rows = _fetch_paginated_v2(
        client,
        "/equities/bars/daily",
        params={"date": trading_str_latest},
        sleep_seconds=1.2,
    )
    px_latest_df = pd.DataFrame.from_records(latest_rows)
    # 普通株（5桁コード末尾"0"）のみ残す。優先株・新株予約権等を除外することで
    # normalize_code_4 後の重複（例: 94340/94345/94346 → 全部"9434"）を防ぐ
    if not px_latest_df.empty and "Code" in px_latest_df.columns:
        px_latest_df = px_latest_df[px_latest_df["Code"].astype(str).str.endswith("0")].copy()
    # Vo / Close ともに Code は 4桁正規化した値で揃える
    if not px_latest_df.empty and "Code" in px_latest_df.columns:
        px_latest_df = px_latest_df.copy()
        px_latest_df["Code"] = px_latest_df["Code"].map(_normalize_code_4).astype(str)
    if px_latest_df.empty or "C" not in px_latest_df.columns:
        px_close_df = pd.DataFrame(columns=["Code", "Close"])
    else:
        px_latest_df = px_latest_df.copy()
        px_latest_df = px_latest_df.rename(columns={"C": "Close"})
        px_latest_df = _to_numeric_df(px_latest_df, ["Close"])
        px_close_df = px_latest_df[["Code", "Close"]].copy()
        px_close_df = px_close_df.drop_duplicates("Code", keep="last")

    # 全期間の出来高（Vo）・売買代金（Va）を収集: (Code, day_idx) で管理
    # day_idx=0が直近、day_idx=vol_days_total-1が最古
    vo_frames: list[pd.DataFrame] = []
    # latest day は Close 用に取得済みレスポンスから流用
    if not px_latest_df.empty and "Vo" in px_latest_df.columns:
        df_latest_vo = px_latest_df.copy()
        _vo_va_cols = [c for c in ["Vo", "Va"] if c in df_latest_vo.columns]
        df_latest_vo = _to_numeric_df(df_latest_vo, _vo_va_cols)
        df_latest_vo = df_latest_vo[["Code"] + _vo_va_cols].drop_duplicates("Code", keep="last")
        df_latest_vo["_day_idx"] = 0
        vo_frames.append(df_latest_vo)

    for day_idx, d_scan in enumerate(trading_days[1:], start=1):
        ds = d_scan.strftime("%Y-%m-%d")
        rows = _fetch_paginated_v2(
            client,
            "/equities/bars/daily",
            params={"date": ds},
            sleep_seconds=1.2,
        )
        df_day = pd.DataFrame.from_records(rows)
        if df_day.empty:
            continue
        if "Vo" not in df_day.columns:
            raise ValueError("equities/bars/daily missing Vo (volume) column")
        # 普通株（末尾"0"）のみ残す
        if "Code" in df_day.columns:
            df_day = df_day[df_day["Code"].astype(str).str.endswith("0")].copy()
        df_day = df_day.copy()
        df_day["Code"] = df_day["Code"].map(_normalize_code_4).astype(str)
        _vo_va_cols = [c for c in ["Vo", "Va"] if c in df_day.columns]
        df_day = _to_numeric_df(df_day, _vo_va_cols)
        df_day = df_day[["Code"] + _vo_va_cols].drop_duplicates("Code", keep="last")
        df_day["_day_idx"] = day_idx
        vo_frames.append(df_day)

    if vo_frames:
        vo_all = pd.concat(vo_frames, ignore_index=True)
        # ブロックインデックス: day_idx 0〜4→block0(直近), 5〜9→block1, ...
        vo_all["_block"] = vo_all["_day_idx"] // 5

        def _build_block_pivot(all_df: pd.DataFrame, raw_col: str, out_prefix: str, n: int) -> tuple[pd.DataFrame, str]:
            """raw_col の5日平均をブロック展開。後方互換列名も返す。"""
            if raw_col not in all_df.columns:
                return pd.DataFrame(columns=["Code"]), ""
            compat_col = f"AvgDaily{'Volume' if raw_col == 'Vo' else 'Value'}5d"
            compat_df = (
                all_df[all_df["_block"] == 0]
                .groupby("Code", as_index=False)[raw_col]
                .mean()
                .rename(columns={raw_col: compat_col})
            )
            pivot = pd.DataFrame()
            for seq in range(1, n + 1):
                blk = n - seq
                col = f"{out_prefix}_BlkSeq{seq:02d}"
                blk_avg = (
                    all_df[all_df["_block"] == blk]
                    .groupby("Code", as_index=False)[raw_col]
                    .mean()
                    .rename(columns={raw_col: col})
                )
                pivot = blk_avg if pivot.empty else pivot.merge(blk_avg, on="Code", how="outer")
            result = compat_df.merge(pivot, on="Code", how="outer") if not pivot.empty else compat_df
            return result, compat_col

        vol_pivot, _ = _build_block_pivot(vo_all, "Vo", "VolAvg5d", vol_blocks)
        val_pivot, _ = _build_block_pivot(vo_all, "Va", "ValAvg5d", vol_blocks)
        print(
            f"volume_blocks: {vol_blocks}ブロック×5日 "
            f"Vo cols={[c for c in vol_pivot.columns if 'BlkSeq' in c]} "
            f"Va cols={[c for c in val_pivot.columns if 'BlkSeq' in c]}"
        )
    else:
        vol_pivot = pd.DataFrame(columns=["Code", "AvgDailyVolume5d"])
        val_pivot = pd.DataFrame(columns=["Code", "AvgDailyValue5d"])

    # AvgDailyVolume5d / AvgDailyValue5d を単独列として保持（後方互換）
    avg_df = vol_pivot[["Code", "AvgDailyVolume5d"]].copy() if "AvgDailyVolume5d" in vol_pivot.columns else pd.DataFrame(columns=["Code", "AvgDailyVolume5d"])

    # Merge: Close + AvgDailyVolume5d + VolAvg5d_BlkSeq + ValAvg5d_BlkSeq
    px_df = px_close_df.merge(avg_df, on="Code", how="left")
    if not vol_pivot.empty:
        px_df = px_df.merge(vol_pivot, on="Code", how="left", suffixes=("", "_dup"))
        px_df = px_df[[c for c in px_df.columns if not c.endswith("_dup")]]
    if not val_pivot.empty:
        px_df = px_df.merge(val_pivot, on="Code", how="left")

    # Left join: start from universe
    master = universe_df.merge(statements_df, on="Code", how="left")
    master = master.merge(px_df, on="Code", how="left")
    master["MarketCap"] = (
        master["Close"]
        * master["NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"]
    )

    sh_out = "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"
    if args.yfinance:
        yf_sleep = float(os.environ.get("YFINANCE_SLEEP", "0.35"))
        print(f"yfinance: {len(codes)} 銘柄 (sleep={yf_sleep}s between tickers)")
        yf_df = fetch_yfinance_market_snapshot(codes, sleep_seconds=yf_sleep)
        master = master.merge(yf_df, on="Code", how="left")
        _mc_jq = pd.to_numeric(master["MarketCap"], errors="coerce")
        _mc_yf = pd.to_numeric(master["YFinanceMarketCap"], errors="coerce")
        master["MarketCap"] = _mc_yf.where(_mc_yf.notna(), _mc_jq)
        _sh_jq = pd.to_numeric(master[sh_out], errors="coerce")
        _sh_yf = pd.to_numeric(master["YFinanceSharesOutstanding"], errors="coerce")
        master[sh_out] = _sh_yf.where(_sh_yf.notna(), _sh_jq)
        _n_m = int(_mc_yf.notna().sum())
        _n_s = int(_sh_yf.notna().sum())
        print(f"yfinance: MarketCap 取得 {_n_m}/{len(codes)} 株数 {_n_s}/{len(codes)}")
    else:
        # --yfinance なしでも MarketCap NaN 銘柄だけ yfinance で自動補完
        _mc_tmp = pd.to_numeric(master["MarketCap"], errors="coerce")
        _nan_codes = master.loc[_mc_tmp.isna(), "Code"].tolist()
        if _nan_codes:
            yf_sleep = float(os.environ.get("YFINANCE_SLEEP", "0.35"))
            print(f"yfinance (NaN補完): MarketCap NaN {len(_nan_codes)} 銘柄を取得中...")
            try:
                yf_patch = fetch_yfinance_market_snapshot(_nan_codes, sleep_seconds=yf_sleep)
                yf_patch = yf_patch.rename(columns={
                    "YFinanceMarketCap": "_yf_mc_patch",
                    "YFinanceSharesOutstanding": "_yf_sh_patch",
                })
                master = master.merge(yf_patch, on="Code", how="left")
                _mc_patch = pd.to_numeric(master["_yf_mc_patch"], errors="coerce")
                _sh_patch = pd.to_numeric(master["_yf_sh_patch"], errors="coerce")
                master["MarketCap"] = _mc_tmp.where(_mc_tmp.notna(), _mc_patch)
                _sh_col = pd.to_numeric(master[sh_out], errors="coerce")
                master[sh_out] = _sh_col.where(_sh_col.notna(), _sh_patch)
                _n_fixed = int(_mc_patch.notna().sum())
                print(f"yfinance (NaN補完): {_n_fixed} / {len(_nan_codes)} 銘柄のMarketCapを補完")
                master = master.drop(columns=["_yf_mc_patch", "_yf_sh_patch"], errors="ignore")
            except Exception as e:
                print(f"yfinance (NaN補完) 失敗（スキップ）: {e}")

    # ── 株式分割検出・自動補正（JQuants v2 AdjFactor ベース）───────────────
    # --check-splits が指定された場合のみ実行。
    # /equities/bars/daily の AdjFactor (1.0 以外なら分割発生日) を直近 N 日分スキャンして
    # 累積分割比率を算出し、ShOutFY と MarketCap を補正する。
    # 公式ソース（JQuants v2）のため yfinance より精度・信頼性が高い。
    if getattr(args, "check_splits", False):
        # 旧 --check-splits（一律補正）は JQuants /fins/summary が分割反映済みの ShOutFY を
        # 返す仕様に対し二重補正を起こすため封印。条件付き補正版を使うこと。
        # design.md: dev/review/2026-05-09_split_double_correction/
        raise RuntimeError(
            "--check-splits（一律補正）は二重補正バグのため使用禁止です。"
            " --check-splits-conditional を使用してください。"
        )
        split_lookback = int(os.environ.get("SPLIT_LOOKBACK_DAYS", "250"))
        split_threshold = float(os.environ.get("SPLIT_RATIO_THRESHOLD", "1.05"))
        print(
            f"株式分割チェック (JQuants AdjFactor): lookback={split_lookback}営業日 "
            f"threshold={split_threshold}x"
        )
        try:
            # 直近 N 営業日を遡って /equities/bars/daily を取得
            scan_dates: list[date] = [trading_day_latest]
            d_prev = trading_day_latest
            for _ in range(split_lookback - 1):
                d_prev = _previous_trading_day_date_v2(client, before=d_prev, max_back_days=14)
                scan_dates.append(d_prev)
            print(f"  スキャン期間: {scan_dates[-1]} 〜 {scan_dates[0]} ({len(scan_dates)}営業日)")
            split_events: list[dict] = []
            codes_set = set(master["Code"].astype(str))
            for ds_date in scan_dates:
                ds = ds_date.strftime("%Y-%m-%d")
                rows = _fetch_paginated_v2(
                    client,
                    "/equities/bars/daily",
                    params={"date": ds},
                    sleep_seconds=1.2,
                )
                for r in rows:
                    adj = r.get("AdjFactor")
                    if adj is None:
                        continue
                    try:
                        af = float(adj)
                    except (TypeError, ValueError):
                        continue
                    if af == 1.0 or af <= 0:
                        continue
                    code_raw = str(r.get("Code", ""))
                    if not code_raw.endswith("0"):
                        continue  # 普通株のみ
                    code4 = _normalize_code_4(code_raw)
                    if code4 in codes_set:
                        split_events.append({"Code": code4, "Date": ds, "AdjFactor": af})
            if split_events:
                ev_df = pd.DataFrame(split_events)
                # 累積比率: AdjFactor が分割で 1/N の場合、株数は N 倍 → 1/AdjFactor の積
                grouped = (
                    ev_df.groupby("Code")
                    .agg(
                        cum_split_ratio=("AdjFactor", lambda s: float((1.0 / s).prod())),
                        split_dates=("Date", lambda s: ", ".join(sorted(s))),
                    )
                    .reset_index()
                )
                master = master.merge(grouped, on="Code", how="left")
                _ratio_s = pd.to_numeric(master["cum_split_ratio"], errors="coerce")
                _split_mask = _ratio_s.notna() & (_ratio_s >= split_threshold)
                n_split = int(_split_mask.sum())
                if n_split > 0:
                    _sh_pre = pd.to_numeric(master[sh_out], errors="coerce")
                    _sh_new = (_sh_pre * _ratio_s).round().astype("Int64")
                    master.loc[_split_mask, sh_out] = _sh_new[_split_mask]
                    _close_s = pd.to_numeric(master["Close"], errors="coerce")
                    _sh_fixed = pd.to_numeric(master[sh_out], errors="coerce")
                    master.loc[_split_mask, "MarketCap"] = (
                        _close_s * _sh_fixed
                    )[_split_mask]
                    _split_log = master.loc[_split_mask, ["Code"]].copy()
                    if "CompanyName" in master.columns:
                        _split_log["CompanyName"] = master.loc[_split_mask, "CompanyName"].values
                    _split_log["JQ_shares_orig"] = _sh_pre[_split_mask].astype("Int64").values
                    _split_log["adjusted_shares"] = _sh_new[_split_mask].values
                    _split_log["cum_ratio"] = _ratio_s[_split_mask].round(3).values
                    _split_log["split_dates"] = master.loc[_split_mask, "split_dates"].values
                    print(f"株式分割補正: {n_split} 銘柄を JQuants AdjFactor で補正")
                    print(_split_log.to_string(index=False))
                else:
                    print(f"株式分割補正: AdjFactor イベント検出 {len(ev_df)} 件あったが閾値超えなし")
                master = master.drop(columns=["cum_split_ratio", "split_dates"], errors="ignore")
            else:
                print("株式分割補正: スキャン期間内に AdjFactor != 1.0 のイベントなし")
        except Exception as e:
            print(f"株式分割チェック失敗（スキップ）: {e}")

    elif getattr(args, "check_splits_conditional", False):
        split_lookback = int(os.environ.get("SPLIT_LOOKBACK_DAYS", "250"))
        split_threshold = float(os.environ.get("SPLIT_RATIO_THRESHOLD", "1.05"))
        print(
            f"株式分割チェック（条件付き補正版）: lookback={split_lookback}営業日 "
            f"threshold={split_threshold}x  分割日>最新決算開示日 の銘柄のみ補正"
        )
        try:
            scan_dates_c: list[date] = [trading_day_latest]
            d_prev_c = trading_day_latest
            for _ in range(split_lookback - 1):
                d_prev_c = _previous_trading_day_date_v2(client, before=d_prev_c, max_back_days=14)
                scan_dates_c.append(d_prev_c)
            print(f"  スキャン期間: {scan_dates_c[-1]} 〜 {scan_dates_c[0]} ({len(scan_dates_c)}営業日)")
            # Code -> 最新の決算開示日（pd.Timestamp）
            disc_lookup: dict[str, pd.Timestamp] = {}
            if "StatementDisclosedDate" in master.columns:
                _disc_s = pd.to_datetime(master["StatementDisclosedDate"], errors="coerce")
                for code_v, ts_v in zip(master["Code"].astype(str), _disc_s):
                    if pd.notna(ts_v):
                        disc_lookup[code_v] = ts_v
            split_events_all: list[dict] = []
            split_events_skipped: list[dict] = []
            codes_set_c = set(master["Code"].astype(str))
            for ds_date in scan_dates_c:
                ds = ds_date.strftime("%Y-%m-%d")
                rows = _fetch_paginated_v2(
                    client,
                    "/equities/bars/daily",
                    params={"date": ds},
                    sleep_seconds=1.2,
                )
                for r in rows:
                    adj = r.get("AdjFactor")
                    if adj is None:
                        continue
                    try:
                        af = float(adj)
                    except (TypeError, ValueError):
                        continue
                    if af == 1.0 or af <= 0:
                        continue
                    code_raw = str(r.get("Code", ""))
                    if not code_raw.endswith("0"):
                        continue
                    code4 = _normalize_code_4(code_raw)
                    if code4 not in codes_set_c:
                        continue
                    # 条件判定: 分割日 > 最新決算開示日 のときだけ補正対象
                    disc_ts = disc_lookup.get(code4)
                    split_ts = pd.Timestamp(ds_date)
                    ev = {"Code": code4, "Date": ds, "AdjFactor": af, "DiscDate": str(disc_ts.date()) if disc_ts is not None else "NaT"}
                    if disc_ts is None or split_ts > disc_ts:
                        split_events_all.append(ev)
                    else:
                        split_events_skipped.append(ev)
            print(
                f"  分割イベント検出: 補正対象={len(split_events_all)} スキップ（既反映）={len(split_events_skipped)}"
            )
            if split_events_skipped:
                _skip_df = pd.DataFrame(split_events_skipped)
                if "CompanyName" in master.columns:
                    _skip_df = _skip_df.merge(
                        master[["Code", "CompanyName"]].drop_duplicates("Code"),
                        on="Code", how="left"
                    )
                print("  スキップしたイベント（JQuants反映済み）:")
                print(_skip_df.head(20).to_string(index=False))
            if split_events_all:
                ev_df = pd.DataFrame(split_events_all)
                grouped = (
                    ev_df.groupby("Code")
                    .agg(
                        cum_split_ratio=("AdjFactor", lambda s: float((1.0 / s).prod())),
                        split_dates=("Date", lambda s: ", ".join(sorted(s))),
                    )
                    .reset_index()
                )
                master = master.merge(grouped, on="Code", how="left")
                _ratio_s = pd.to_numeric(master["cum_split_ratio"], errors="coerce")
                _split_mask = _ratio_s.notna() & (_ratio_s >= split_threshold)
                n_split = int(_split_mask.sum())
                if n_split > 0:
                    _sh_pre = pd.to_numeric(master[sh_out], errors="coerce")
                    _sh_new = (_sh_pre * _ratio_s).round().astype("Int64")
                    master.loc[_split_mask, sh_out] = _sh_new[_split_mask]
                    _close_s = pd.to_numeric(master["Close"], errors="coerce")
                    _sh_fixed = pd.to_numeric(master[sh_out], errors="coerce")
                    master.loc[_split_mask, "MarketCap"] = (
                        _close_s * _sh_fixed
                    )[_split_mask]
                    _split_log = master.loc[_split_mask, ["Code"]].copy()
                    if "CompanyName" in master.columns:
                        _split_log["CompanyName"] = master.loc[_split_mask, "CompanyName"].values
                    _split_log["JQ_shares_orig"] = _sh_pre[_split_mask].astype("Int64").values
                    _split_log["adjusted_shares"] = _sh_new[_split_mask].values
                    _split_log["cum_ratio"] = _ratio_s[_split_mask].round(3).values
                    _split_log["split_dates"] = master.loc[_split_mask, "split_dates"].values
                    print(f"株式分割補正（条件付き）: {n_split} 銘柄を補正")
                    print(_split_log.to_string(index=False))
                else:
                    print(f"株式分割補正（条件付き）: 補正対象イベント {len(ev_df)} 件あったが閾値超えなし")
                master = master.drop(columns=["cum_split_ratio", "split_dates"], errors="ignore")
            else:
                print("株式分割補正（条件付き）: 補正対象なし（全銘柄でJQuantsが既に分割反映済み or 分割なし）")
        except Exception as e:
            print(f"株式分割チェック（条件付き）失敗（スキップ）: {e}")

    master = master.merge(wm_df, on="Code", how="left")

    master = master.merge(ss_df, on="Code", how="left")
    # 合算空売り株数 ÷ 期末発行済株式数 で比率を一貫させる（機関別比率の max は合算と整合しないため）
    if sh_out in master.columns and "ShortPositionsInSharesNumber" in master.columns:
        sh = pd.to_numeric(master[sh_out], errors="coerce")
        shortn = pd.to_numeric(master["ShortPositionsInSharesNumber"], errors="coerce")
        ok = sh.notna() & (sh > 0) & shortn.notna()
        master.loc[ok, "ShortPositionsToSharesOutstandingRatio"] = shortn[ok] / sh[ok]
    master = master.merge(ann_df, on="Code", how="left")
    if "AnnouncementDate" in master.columns:
        master["AnnouncementDate"] = pd.to_datetime(master["AnnouncementDate"], errors="coerce")
    # helper column is not part of final output; it will be ignored by required_final_cols
    master["MarketCap"] = pd.to_numeric(master["MarketCap"], errors="coerce")
    _mc_val = master["MarketCap"]
    _np_lt = pd.to_numeric(master["Profit_LatestYear_Actual"], errors="coerce")
    _eq_lt = pd.to_numeric(master["Equity_LatestFY"], errors="coerce")
    _ok_per = _np_lt.notna() & (_np_lt > 0) & _mc_val.notna()
    _ok_pbr = _eq_lt.notna() & (_eq_lt > 0) & _mc_val.notna()
    _ok_roe = _eq_lt.notna() & (_eq_lt != 0) & _np_lt.notna()
    master["PER_Trailing"] = (_mc_val / _np_lt).where(_ok_per)
    master["PBR_Trailing"] = (_mc_val / _eq_lt).where(_ok_pbr)
    master["ROE_LatestYear"] = (_np_lt / _eq_lt).where(_ok_roe)
    _ns_lt = pd.to_numeric(master["NetSales_LatestYear_Actual"], errors="coerce")
    _ok_psr = _ns_lt.notna() & (_ns_lt > 0) & _mc_val.notna()
    master["PSR_Trailing"] = (_mc_val / _ns_lt).where(_ok_psr)
    # 出力監査用: このデータがいつの ETL 実行で作られたかを全行に明示
    master["ETLRunId"] = etl_run_id
    master["ETLStartedAtUTC"] = etl_started_at_utc_str
    master["ETLStartedAtJST"] = etl_started_at_jst_str

    required_final_cols = _required_final_col_names(yfinance=args.yfinance, ss_weeks=ss_weeks)

    for c in required_final_cols:
        if c not in master.columns:
            master[c] = pd.NA

    master = master[required_final_cols].copy()
    master = master.sort_values("Code").reset_index(drop=True)

    _nr = len(master)
    if _nr:
        _fs = pd.to_numeric(master["ShortMarginTradeVolume"], errors="coerce").notna().sum()
        _fl = pd.to_numeric(master["LongMarginTradeVolume"], errors="coerce").notna().sum()
        print(
            f"master 信用枠（結合後）: Short埋まり={int(_fs)}/{_nr} ({100.0 * _fs / _nr:.1f}%) "
            f"Long埋まり={int(_fl)}/{_nr} ({100.0 * _fl / _nr:.1f}%)"
        )

    # ── スクリーニング派生列（成長率・比率）を先に計算 ───────────────
    from convert_to_excel import _add_screening_derived_columns
    master = _add_screening_derived_columns(master)

    _h_ok = pd.to_numeric(master["Scr_Hensachi_Total"], errors="coerce").notna().sum()
    print(f"偏差値計算完了: 総合スコア非欠損={int(_h_ok)}/{len(master)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Streamlit が同時に読み込む可能性があるため、一時ファイルへ書き出してから rename（原子置換）
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    master.to_parquet(tmp_path, index=False)
    tmp_path.replace(out_path)

    print(f"saved: {out_path} rows={len(master)} cols={len(master.columns)}")
    print(
        f"etl metadata: run_id={etl_run_id} "
        f"started_utc={etl_started_at_utc_str} started_jst={etl_started_at_jst_str}"
    )
    print(master.head(5))

    # Yahoo Finance 補完監査ログ出力
    if yf_audit_rows:
        audit_path = out_path.parent / "yfinance_audit.parquet"
        audit_df = pd.DataFrame(yf_audit_rows)
        # CompanyName をメイン出力からマージ
        if "CompanyName" in master.columns:
            audit_df = audit_df.merge(master[["Code", "CompanyName"]], on="Code", how="left")
            cols = ["Code", "CompanyName", "JQ_Thin", "YFinance_Fetched", "YFinance_Used", "JQ_TotalRows"]
            for _k in STATEMENT_NUMERIC_COLS:
                for _s in ("Final", "JQ", "YF", "Source"):
                    _c = f"{_k}_{_s}"
                    if _c in audit_df.columns and _c not in cols:
                        cols.append(_c)
            cols += [c for c in audit_df.columns if c not in cols]
            audit_df = audit_df[cols]
        audit_df.to_parquet(audit_path, index=False)
        _n_thin = len(audit_df)
        _n_yf_used = int(audit_df["YFinance_Used"].sum())
        _n_yf_fail = int(audit_df["YFinance_Fetched"].sum()) - _n_yf_used
        print(
            f"yfinance_audit: thin={_n_thin} yf_used={_n_yf_used} yf_fetch_failed={_n_yf_fail}"
            f" -> {audit_path}"
        )
        if want_excel:
            audit_xlsx = audit_path.with_suffix(".xlsx")
            try:
                audit_df.to_excel(audit_xlsx, index=False)
                print(f"yfinance_audit excel: {audit_xlsx}")
            except (ImportError, PermissionError) as _e:
                print(f"yfinance_audit excel skip: {_e}")
    else:
        print("yfinance_audit: thin 銘柄なし（全銘柄 J-Quants データ十分）")

    if not args.no_data_gaps:
        gaps_path = out_path.with_name(f"{out_path.stem}_data_gaps.parquet")
        gaps_df = _build_fins_data_gaps_df(master, stmt_failures, stmt_field_issues)
        gaps_df.to_parquet(gaps_path, index=False)
        print(f"data_gaps: {len(gaps_df)} rows -> {gaps_path}")
        if want_excel:
            gx = gaps_path.with_suffix(".xlsx")
            try:
                gaps_df.to_excel(gx, index=False)
                print(f"data_gaps excel: {gx}")
            except ImportError:
                print("data_gaps: Excel 出力は openpyxl が必要です（pip install openpyxl）")
            except PermissionError:
                print(
                    f"data_gaps excel: 書き込み不可（{gx} を Excel で開いていませんか？閉じて再実行）"
                )

    if want_excel:
        from convert_to_excel import parquet_to_excel

        xlsx_path = out_path.with_suffix(".xlsx")
        try:
            xout, _xf = parquet_to_excel(out_path, xlsx_path)
            print(f"excel: {xout}")
        except PermissionError:
            print(
                f"Excel 出力をスキップしました: {xlsx_path} がロックされています。\n"
                "  → 当該 .xlsx を閉じてから再実行するか、別名で保存: "
                f"python convert_to_excel.py --input {out_path} --output data/processed/tmp_master.xlsx"
            )

    if stmt_failures:
        head_n = min(20, len(stmt_failures))
        print(f"statement failures: {len(stmt_failures)} showing {head_n}")
        for code4, msg in stmt_failures[:head_n]:
            print(f"- {code4}: {msg}")
    if stmt_field_issues:
        head_n = min(20, len(stmt_field_issues))
        print(f"statement field issues: {len(stmt_field_issues)} showing {head_n}")
        for code4, msg in stmt_field_issues[:head_n]:
            print(f"- {code4}: {msg}")


if __name__ == "__main__":
    main()

