"""price_history parquet に対象日の価格データが入っているかを判定するハードゲート。

PM 2026-08-29 確定（「parquet の数値が出ていなかったら回らないように設計しろ」）:
週次動意レポートは、対象週金曜の EOD が price_history/{YYYY}.parquet へ入っていない状態では
生成も配信もしない。旧「品質注記を付けて配信続行」（PM 2026-07-12 絶対配信原則）は
「データが揃っている前提での配信絶対」であり、対象日データ未着時は本ゲートが優先する。

処理:
  1. bi/outputs/price_history/{YYYY}.parquet の Date 最大値を読み、対象日以上なら即 exit 0。
  2. 未着なら JQuants から対象日分を取得して parquet へ反映（--topup 指定時）。
  3. それでも未着なら --retries / --interval-seconds に従って 1〜2 を繰り返す。
  4. 最終的に未着なら exit 4（呼び出し側の workflow が generate 以降を止める）。

exit code:
  0 = 対象日のデータあり（続行可）
  4 = リトライ上限まで待っても対象日のデータが未着（生成中止）
  1 = 判定自体が失敗（parquet 不在・読込不可等のインフラ失敗）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICE_HISTORY_DIR = REPO_ROOT / "bi" / "outputs" / "price_history"
PIPELINES_DIR = Path(__file__).resolve().parents[1]

EXIT_OK = 0
EXIT_INFRA = 1
EXIT_MISSING = 4


def latest_price_date(target_year: int) -> date | None:
    """price_history の対象年 parquet から Date の最大値を返す（Date 列のみ読むので軽量）。"""
    p = PRICE_HISTORY_DIR / f"{target_year}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=["Date"])
    except Exception as e:
        print(f"[ERROR] {p} の読込に失敗: {type(e).__name__}: {e}")
        return None
    if df.empty:
        return None
    return pd.to_datetime(df["Date"]).max().date()


def run_topup() -> bool:
    """make_price_history_master.py daily を実行して対象日分を parquet へ反映する。"""
    if not os.environ.get("JQUANTS_API_KEY", "").strip():
        print("[WARN] JQUANTS_API_KEY が未設定のため JQuants 補完をスキップします")
        return False
    cmd = [sys.executable, "make_price_history_master.py", "daily"]
    print(f"[INFO] JQuants 補完を実行: {' '.join(cmd)} (cwd={PIPELINES_DIR})")
    try:
        proc = subprocess.run(cmd, cwd=str(PIPELINES_DIR), timeout=1800)
    except subprocess.TimeoutExpired:
        print("[WARN] JQuants 補完が 30 分で打ち切られました")
        return False
    except Exception as e:
        print(f"[WARN] JQuants 補完の起動に失敗: {type(e).__name__}: {e}")
        return False
    if proc.returncode != 0:
        print(f"[WARN] JQuants 補完が異常終了しました (exit={proc.returncode})")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-date", required=True,
                        help="対象日 (YYYY-MM-DD)。price_history の最新日がこの日付以上なら合格")
    parser.add_argument("--retries", type=int, default=6,
                        help="未着時の再判定回数（初回判定を除く・既定 6）")
    parser.add_argument("--interval-seconds", type=int, default=600,
                        help="再判定の待機間隔（秒・既定 600 = 10 分）")
    parser.add_argument("--topup", action="store_true",
                        help="未着時に make_price_history_master.py daily で JQuants 補完を試みる")
    args = parser.parse_args()

    try:
        expect = datetime.strptime(args.expect_date, "%Y-%m-%d").date()
    except ValueError:
        print(f"[ERROR] --expect-date の形式が不正です: {args.expect_date}")
        return EXIT_INFRA

    if not PRICE_HISTORY_DIR.exists():
        print(f"[ERROR] price_history ディレクトリが存在しません: {PRICE_HISTORY_DIR}")
        return EXIT_INFRA

    attempts = args.retries + 1
    for i in range(1, attempts + 1):
        latest = latest_price_date(expect.year)
        shown = latest.isoformat() if latest else "（データなし）"
        print(f"[{i}/{attempts}] price_history 最新日 = {shown} / 対象日 = {expect.isoformat()}")

        if latest is not None and latest >= expect:
            print(f"[OK] 対象日 {expect.isoformat()} の価格データが price_history に存在します。続行します。")
            return EXIT_OK

        if args.topup:
            if run_topup():
                latest = latest_price_date(expect.year)
                shown = latest.isoformat() if latest else "（データなし）"
                print(f"[INFO] 補完後の price_history 最新日 = {shown}")
                if latest is not None and latest >= expect:
                    print(f"[OK] JQuants 補完で対象日 {expect.isoformat()} が揃いました。続行します。")
                    return EXIT_OK

        if i < attempts:
            print(f"[WAIT] 対象日データが未着のため {args.interval_seconds} 秒待機して再判定します"
                  f"（残り {attempts - i} 回）")
            time.sleep(args.interval_seconds)

    final = latest_price_date(expect.year)
    final_shown = final.isoformat() if final else "（データなし）"
    print(f"[BLOCKED] 対象日 {expect.isoformat()} の価格データが最後まで price_history に届きませんでした"
          f"（最新日 {final_shown}）。レポート生成・配信を中止します"
          f"（PM 2026-08-29: parquet に対象日の数値が無ければ回さない）。")
    return EXIT_MISSING


if __name__ == "__main__":
    sys.exit(main())
