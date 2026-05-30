"""全銘柄 × 過去 10 年の業績本体（四半期 + 年次）+ 会社予想 + 配当の履歴を単一 parquet で保存。

データソース:
    JQuants `/fins/summary` エンドポイント（スタンダードプランで利用可）。
    本来仕様書では `/fins/statements`（決算短信サマリ詳細）を想定していたが、
    JQuants v2 では `/fins/details` 同等の詳細エンドポイントは上位プラン専用で利用不可。
    `/fins/summary` は同じ業績本体・予想・配当・CF を含むため、本 ETL ではこちらを採用する。

使用方法:
    # テスト（指定銘柄のみ）
    python make_financial_history_master.py test --codes 4180,3905,3778

    # 初回フル取得（全銘柄 × 過去 10 年・数時間規模）
    python make_financial_history_master.py initial

    # 初回フル取得（先頭 N 銘柄のみ・動作確認用）
    python make_financial_history_master.py initial --limit 50

    # 日次差分更新（本日付近の新規開示分のみ追記）
    python make_financial_history_master.py daily

出力:
    本番:  bi/outputs/financial_history_master.parquet（四半期粒度・単一ファイル）
    テスト: bi/outputs/financial_history/_test.parquet

スキーマ（業績本体メイン）:
    キー        : Code, FiscalYear (CurFYEn), FiscalQuarter (1Q/2Q/3Q/FY),
                  AnnouncementDate (DiscDate), AnnouncementTime, DocType
    期間情報    : PeriodStart, PeriodEnd, FiscalYearStart, FiscalYearEnd
    業績本体    : NetSales, OperatingProfit, OrdinaryProfit, Profit, EPS, DilutedEPS
    財務        : TotalAssets, Equity, CashAndEquivalents, EquityRatio, BPS
    CF          : CFOperating, CFInvesting, CFFinancing
    会社予想    : ForecastNetSales, ForecastOperatingProfit, ForecastOrdinaryProfit,
                  ForecastProfit, ForecastEPS
    配当        : DividendPerShare (DivAnn), ForecastDPS (FDivAnn), PayoutRatio
    株式        : SharesOutstanding (ShOutFY), TreasuryShares (TrShFY), AverageShares (AvgSh)
    期間比較    : YoY_Sales, YoY_OP, YoY_NI  (前年同期比 %・四半期 + 年次でそれぞれ算出)

注釈（中学生レベル）:
    - 四半期 (Quarter): 1 年を 4 つに区切った 3 ヶ月単位。1Q/2Q/3Q/FY (= 通期)
    - CF (キャッシュフロー): 期中に現金が入った/出た差し引き。本業 (営業)・設備投資 (投資)・借入返済等 (財務) の 3 種
    - 会社予想: 会社自身が公表する次期の見通し売上・利益
    - EPS (Earnings Per Share): 1 株あたり利益
    - 配当: 株主に分配される現金
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
)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PARQUET = REPO_ROOT / "bi" / "outputs" / "financial_history_master.parquet"
TEST_OUT = REPO_ROOT / "bi" / "outputs" / "financial_history" / "_test.parquet"
SCREENING_MASTER = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"

REQUEST_SLEEP = 1.2  # /fins/summary は 429 になりやすいため長め
MAX_RETRIES = 6

# /fins/summary -> 論理カラム名 のマッピング
COL_MAP: dict[str, str] = {
    # キー・期間
    "Code": "Code",
    "DiscDate": "AnnouncementDate",
    "DiscTime": "AnnouncementTime",
    "DocType": "DocType",
    "CurPerType": "FiscalQuarter",
    "CurPerSt": "PeriodStart",
    "CurPerEn": "PeriodEnd",
    "CurFYSt": "FiscalYearStart",
    "CurFYEn": "FiscalYear",
    # 業績本体（連結）
    "Sales": "NetSales",
    "OP": "OperatingProfit",
    "OdP": "OrdinaryProfit",
    "NP": "Profit",
    "EPS": "EPS",
    "DEPS": "DilutedEPS",
    # 財務
    "TA": "TotalAssets",
    "Eq": "Equity",
    "EqAR": "EquityRatio",
    "BPS": "BPS",
    "CashEq": "CashAndEquivalents",
    # CF
    "CFO": "CFOperating",
    "CFI": "CFInvesting",
    "CFF": "CFFinancing",
    # 配当（実績）
    "DivAnn": "DividendPerShare",
    "PayoutRatioAnn": "PayoutRatio",
    # 配当（予想・今期）
    "FDivAnn": "ForecastDPS",
    "FPayoutRatioAnn": "ForecastPayoutRatio",
    # 会社予想（通期）
    "FSales": "ForecastNetSales",
    "FOP": "ForecastOperatingProfit",
    "FOdP": "ForecastOrdinaryProfit",
    "FNP": "ForecastProfit",
    "FEPS": "ForecastEPS",
    # 株式
    "ShOutFY": "SharesOutstanding",
    "TrShFY": "TreasuryShares",
    "AvgSh": "AverageShares",
    # 単体 (非連結) — 連結値が空のときの補完用に残す
    "NCSales": "NetSales_NonCons",
    "NCOP": "OperatingProfit_NonCons",
    "NCNP": "Profit_NonCons",
}

NUMERIC_COLS = [
    "NetSales", "OperatingProfit", "OrdinaryProfit", "Profit", "EPS", "DilutedEPS",
    "TotalAssets", "Equity", "EquityRatio", "BPS", "CashAndEquivalents",
    "CFOperating", "CFInvesting", "CFFinancing",
    "DividendPerShare", "PayoutRatio", "ForecastDPS", "ForecastPayoutRatio",
    "ForecastNetSales", "ForecastOperatingProfit", "ForecastOrdinaryProfit",
    "ForecastProfit", "ForecastEPS",
    "SharesOutstanding", "TreasuryShares", "AverageShares",
    "NetSales_NonCons", "OperatingProfit_NonCons", "Profit_NonCons",
]


def _is_4digit_code(code4: str) -> bool:
    s = str(code4).strip()
    return len(s) == 4 and s.isdigit()


def _to_num(x: object) -> float | None:
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _fetch_summary_with_backoff(client: jquantsapi.ClientV2, code4: str) -> list[dict]:
    """`/fins/summary` を 1 銘柄分・全期間取得。429 / 一過性失敗は backoff リトライ。"""
    backoff = 5.0
    last_err: BaseException | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            rows = fetch_paginated_v2(
                client,
                "/fins/summary",
                params={"code": code4},
                sleep_seconds=REQUEST_SLEEP,
            )
            return rows
        except Exception as e:
            last_err = e
            msg = str(e)
            wait = min(120.0, backoff)
            print(f"  retry {attempt}/{MAX_RETRIES} for {code4}: {type(e).__name__} {msg[:80]} wait {wait:.1f}s")
            time.sleep(wait)
            backoff *= 1.8
    if last_err is not None:
        raise last_err
    return []


def load_universe() -> list[str]:
    if not SCREENING_MASTER.exists():
        raise FileNotFoundError(
            f"{SCREENING_MASTER} が存在しません。screening_master を先に実行してください。"
        )
    df = pd.read_parquet(SCREENING_MASTER, columns=["Code"])
    codes = df["Code"].astype(str).map(normalize_code_4).drop_duplicates().tolist()
    return [c for c in codes if _is_4digit_code(c)]


def normalize_summary_df(rows: list[dict]) -> pd.DataFrame:
    """`/fins/summary` の生 list[dict] を論理スキーマに整形した DataFrame に変換する。"""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    # 必要カラムのみリネーム抽出。存在しないカラムは None で埋める
    out_cols: dict[str, pd.Series] = {}
    for src, dst in COL_MAP.items():
        if src in df.columns:
            out_cols[dst] = df[src]
        else:
            out_cols[dst] = pd.Series([None] * len(df), index=df.index)
    out = pd.DataFrame(out_cols)

    # Code を 4 桁に正規化（/fins/summary は 5 桁 "41800" を返すことがある）
    out["Code"] = out["Code"].astype(str).map(normalize_code_4)

    # 数値変換（空文字 → NaN）— pd.to_numeric で必ず float dtype に固定する。
    # 全 None の列は object dtype になると後段の .abs() で TypeError になるため、
    # _to_num で None/空文字を None 化 → pd.to_numeric で float64 に正規化する。
    for c in NUMERIC_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c].map(_to_num), errors="coerce")

    # 日付変換
    out["AnnouncementDate"] = pd.to_datetime(out["AnnouncementDate"], errors="coerce")

    # 連結値が空なら単体で埋める（NetSales / OperatingProfit / Profit のみ）
    for cons, nc in [
        ("NetSales", "NetSales_NonCons"),
        ("OperatingProfit", "OperatingProfit_NonCons"),
        ("Profit", "Profit_NonCons"),
    ]:
        if cons in out.columns and nc in out.columns:
            out[cons] = pd.to_numeric(
                out[cons].where(out[cons].notna(), out[nc]),
                errors="coerce",
            )

    return out


def _pick_latest_per_period(df: pd.DataFrame) -> pd.DataFrame:
    """同一 (Code, FiscalYear, FiscalQuarter) で複数開示がある場合、最新 DiscDate を採用。

    決算短信→訂正→数値訂正等で複数回開示されるため、最新を「確定値」として採用する。
    """
    if df.empty:
        return df
    df = df.sort_values(["Code", "FiscalYear", "FiscalQuarter", "AnnouncementDate"])
    df = df.drop_duplicates(subset=["Code", "FiscalYear", "FiscalQuarter"], keep="last")
    return df.reset_index(drop=True)


def _enrich_yoy(df: pd.DataFrame) -> pd.DataFrame:
    """同一 (Code, FiscalQuarter) 内で 1 期前比 (YoY) を計算する。

    四半期 1Q を前年 1Q と比較・FY を前年 FY と比較。
    """
    if df.empty:
        return df
    df = df.copy()
    df = df.sort_values(["Code", "FiscalQuarter", "FiscalYear"]).reset_index(drop=True)

    def _yoy(group: pd.DataFrame, col: str) -> pd.Series:
        # 念のため float に強制変換（object dtype/None 混入で .abs() が失敗する事故を防ぐ）
        cur = pd.to_numeric(group[col], errors="coerce")
        prev = cur.shift(1)
        return ((cur - prev) / prev.abs() * 100).round(2)

    out_frames: list[pd.DataFrame] = []
    for (code, fq), g in df.groupby(["Code", "FiscalQuarter"], sort=False):
        g = g.copy()
        g["YoY_Sales"] = _yoy(g, "NetSales")
        g["YoY_OP"] = _yoy(g, "OperatingProfit")
        g["YoY_NI"] = _yoy(g, "Profit")
        out_frames.append(g)

    out = pd.concat(out_frames, ignore_index=True)
    return out.sort_values(["Code", "AnnouncementDate"]).reset_index(drop=True)


def build_history_frame(rows_by_code: dict[str, list[dict]]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for code, rows in rows_by_code.items():
        if not rows:
            continue
        df = normalize_summary_df(rows)
        if df.empty:
            continue
        df = _pick_latest_per_period(df)
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    all_df = _enrich_yoy(all_df)
    return all_df


def save_parquet(df: pd.DataFrame, path: Path, merge_existing: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if merge_existing and path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, df], ignore_index=True)
        combined = combined.sort_values(["Code", "FiscalYear", "FiscalQuarter", "AnnouncementDate"])
        combined = combined.drop_duplicates(
            subset=["Code", "FiscalYear", "FiscalQuarter"], keep="last"
        )
    else:
        combined = df.sort_values(["Code", "AnnouncementDate"]).reset_index(drop=True)
    combined.to_parquet(path, index=False)
    size_kb = path.stat().st_size / 1024
    print(f"  saved: {path.name} ({len(combined):,} rows, {size_kb:.0f} KB)")


def cmd_initial(args) -> int:
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY 未設定")

    client = jquantsapi.ClientV2(api_key=api_key)
    codes = load_universe()
    if args.limit and args.limit > 0:
        codes = codes[: args.limit]

    print(f"=== 初回フル取得モード: {len(codes)} 銘柄 ===")
    rows_by_code: dict[str, list[dict]] = {}
    failures: list[tuple[str, str]] = []

    for i, code4 in enumerate(codes, start=1):
        try:
            rows = _fetch_summary_with_backoff(client, code4)
            if rows:
                rows_by_code[code4] = rows
        except Exception as e:
            failures.append((code4, f"{type(e).__name__}: {e}"))

        if i == 1 or i % 50 == 0 or i == len(codes):
            ok = len(rows_by_code)
            fail = len(failures)
            print(f"  fetch progress: {i}/{len(codes)} (ok={ok} fail={fail})")

    if not rows_by_code:
        print("ERROR: 1 銘柄も取得できませんでした")
        return 1

    print("\n=== 正規化 + YoY 計算 ===")
    enriched = build_history_frame(rows_by_code)
    print(f"  total rows: {len(enriched):,}")

    print("\n=== parquet 保存 ===")
    save_parquet(enriched, OUT_PARQUET, merge_existing=False)

    if failures:
        print(f"\n失敗: {len(failures)} 件")
        for c, m in failures[:20]:
            print(f"  - {c}: {m}")

    return 0


def cmd_daily(args) -> int:
    """日次差分: 当日 ± 7 日付近の開示があった銘柄のみ取得して既存 parquet にマージする。

    実装方針: 銘柄リストは screening_master 全件。各銘柄について /fins/summary を取得し、
    既存 parquet と (Code, FiscalYear, FiscalQuarter) で merge_existing=True で結合する。
    既存にない期は新規追加・既存にある期は最新 DiscDate で上書きされる。
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY 未設定")

    if not OUT_PARQUET.exists():
        print(f"{OUT_PARQUET} が存在しません。initial を先に実行してください。")
        return 1

    client = jquantsapi.ClientV2(api_key=api_key)
    codes = load_universe()
    print(f"=== 日次差分更新: {len(codes)} 銘柄 ===")

    rows_by_code: dict[str, list[dict]] = {}
    failures: list[tuple[str, str]] = []
    cutoff = (date.today() - timedelta(days=14)).isoformat()

    for i, code4 in enumerate(codes, start=1):
        try:
            rows = _fetch_summary_with_backoff(client, code4)
            # 直近 14 日以内に新規開示があったものだけ採用（差分効率化）
            recent = [r for r in rows if str(r.get("DiscDate", "")) >= cutoff]
            if recent:
                rows_by_code[code4] = rows  # 全期間入れる（マージ時に diff 解決）
        except Exception as e:
            failures.append((code4, f"{type(e).__name__}: {e}"))

        if i == 1 or i % 100 == 0 or i == len(codes):
            print(f"  progress: {i}/{len(codes)} (with_recent={len(rows_by_code)} fail={len(failures)})")

    if not rows_by_code:
        print("直近 14 日以内に新規開示なし。何もせず終了。")
        return 0

    new_df = build_history_frame(rows_by_code)
    print(f"\n  new/updated rows: {len(new_df):,}")
    print("\n=== parquet マージ保存 ===")
    save_parquet(new_df, OUT_PARQUET, merge_existing=True)
    return 0


def cmd_test(args) -> int:
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY 未設定")

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    print(f"=== テストモード: {codes} ===")

    client = jquantsapi.ClientV2(api_key=api_key)
    rows_by_code: dict[str, list[dict]] = {}
    for code4 in codes:
        rows = _fetch_summary_with_backoff(client, code4)
        rows_by_code[code4] = rows
        print(f"  {code4}: {len(rows)} raw rows")

    enriched = build_history_frame(rows_by_code)
    print(f"\n統合後 (重複排除 + YoY): {len(enriched):,} 行")

    save_parquet(enriched, TEST_OUT, merge_existing=False)

    print(f"\nカラム数: {len(enriched.columns)}")
    print(f"カラム: {list(enriched.columns)}")
    print(f"\n直近 5 行サンプル（最初の銘柄）:")
    first_code = enriched["Code"].iloc[0]
    sample = enriched[enriched["Code"] == first_code].sort_values("AnnouncementDate").tail(5)
    show_cols = [
        "Code", "FiscalYear", "FiscalQuarter", "AnnouncementDate",
        "NetSales", "OperatingProfit", "Profit", "EPS",
        "CFOperating", "DividendPerShare", "YoY_Sales", "YoY_OP",
    ]
    show_cols = [c for c in show_cols if c in sample.columns]
    print(sample[show_cols].to_string(index=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="全銘柄 業績履歴 (四半期 + 通期) ETL")
    sub = parser.add_subparsers(dest="mode")

    p_init = sub.add_parser("initial", help="初回フル取得（全銘柄 × 過去 10 年）")
    p_init.add_argument("--limit", type=int, default=0,
                        help="先頭 N 銘柄のみ取得（動作確認用・0 で全銘柄）")

    sub.add_parser("daily", help="日次差分更新（直近 14 日以内の新規開示のみ）")

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
