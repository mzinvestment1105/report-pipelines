"""
JQuants get_fin_summary で全カバレッジ銘柄の対象月の決算発表日を取得する。

各銘柄の指定月内開示の DiscDate（=決算発表日）を真の発表日として記録。
失敗時は内部で最大4回リトライする。

使い方:
  python fetch_jq_statements.py                  # 今月
  python fetch_jq_statements.py --month 2026-05  # 月指定

出力:
  research/earnings/jq_statements.csv（最新月で上書き）
  research/earnings/jq_statements_{month}.csv（月別アーカイブ）
"""

from __future__ import annotations

import argparse
import calendar
import os
import sys
import time
from datetime import date
from pathlib import Path

import jquantsapi
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
COVERAGE_CSV = ROOT / "research/earnings/coverage_stocks.csv"
ENV_PATH = Path(__file__).resolve().parent / ".env"

# 完走ゲート: 取得銘柄数がカバレッジの MIN_COVERAGE_RATIO 未満なら
# 既存 CSV を上書きせず非ゼロ終了（部分失敗で発表日ファイルを壊さない）。
# 注: 対象月に開示が無い銘柄は正常に records 外なので、閾値は保守的に低めに設定。
MIN_COVERAGE_RATIO = 0.50


def month_range(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def fetch_with_retry(client, code: str, max_retries: int = 4) -> pd.DataFrame:
    for attempt in range(max_retries):
        try:
            return client.get_fin_summary(code=code)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = 5 * (attempt + 1)
            print(f"    retry {attempt+1}/{max_retries} for {code} ({type(e).__name__}): wait {wait}s")
            time.sleep(wait)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--month", help="対象月 YYYY-MM（既定: 今月）")
    p.add_argument("--sleep", type=float, default=0.5, help="リクエスト間隔秒（既定 0.5）")
    args = p.parse_args()

    month = args.month or date.today().strftime("%Y-%m")
    start_date, end_date = month_range(month)
    print(f"対象月: {month}（{start_date} 〜 {end_date}）")

    load_dotenv(ENV_PATH)
    key = os.environ.get("JQUANTS_API_KEY", "")
    if not key:
        raise RuntimeError("JQUANTS_API_KEY not found")
    client = jquantsapi.ClientV2(api_key=key)

    cov = pd.read_csv(COVERAGE_CSV)
    cov["code"] = cov["code"].astype(str)

    records: list[dict] = []
    for i, row in cov.iterrows():
        code = str(row["code"])[:4]
        try:
            df = fetch_with_retry(client, code)
        except Exception as e:
            print(f"  [{i+1}/{len(cov)}] {code} GIVE UP: {type(e).__name__} {str(e)[:60]}")
            time.sleep(1.0)
            continue

        if df is None or df.empty or "DiscDate" not in df.columns:
            print(f"  [{i+1}/{len(cov)}] {code} no data")
            time.sleep(args.sleep)
            continue

        # DiscDate を datetime に正規化して範囲判定・並べ替えする（文字列の辞書順比較を避ける）。
        # 元の表記は出力にそのまま使うため別列 _DiscDateTs に保持。パース不能（NaT）は範囲外。
        df = df.copy()
        df["_DiscDateTs"] = pd.to_datetime(df["DiscDate"], errors="coerce")
        df_in = df[
            (df["_DiscDateTs"] >= pd.Timestamp(start_date))
            & (df["_DiscDateTs"] <= pd.Timestamp(end_date))
        ].copy()
        if df_in.empty:
            time.sleep(args.sleep)
            continue

        latest = df_in.sort_values("_DiscDateTs").iloc[-1]
        records.append({
            "code": code,
            "name": row.get("name"),
            "sectors": row.get("sectors"),
            "DiscDate": str(latest.get("DiscDate")),
            "DiscTime": str(latest.get("DiscTime")),
            "DocType": str(latest.get("DocType")),
            "CurPerType": str(latest.get("CurPerType")),
            "CurFYEn": str(latest.get("CurFYEn")),
        })
        print(f"  [{i+1}/{len(cov)}] {code} {latest['DiscDate']} {latest['DiscTime']}")
        time.sleep(args.sleep)

    out = pd.DataFrame(records)
    out_latest = ROOT / "research/earnings/jq_statements.csv"
    out_month = ROOT / f"research/earnings/jq_statements_{month}.csv"
    out_latest.parent.mkdir(parents=True, exist_ok=True)

    # 完走ゲート: 取得銘柄数が下限未満なら既存 CSV を上書きせず非ゼロ終了。
    floor = int(len(cov) * MIN_COVERAGE_RATIO)
    if out.empty or len(out) < floor:
        print()
        print(
            f"ERROR: 取得 {len(out)}/{len(cov)} 件が下限 {floor}"
            f"（{MIN_COVERAGE_RATIO:.0%}）未満のため上書き中止。"
            f"既存 {out_latest} / {out_month} は保持。"
        )
        sys.exit(1)

    # アトミック書き込み: 同一ディレクトリの一時ファイルに書いてから os.replace で差し替え。
    for dest in (out_latest, out_month):
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        out.to_csv(tmp, encoding="utf-8", index=False)
        os.replace(tmp, dest)
    print()
    print(f"取得銘柄数: {len(out)} / {len(cov)}")
    print(f"保存: {out_latest}")
    print(f"保存: {out_month}")


if __name__ == "__main__":
    main()
