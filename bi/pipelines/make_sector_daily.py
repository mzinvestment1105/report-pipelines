"""
セクター日次騰落率ランキング生成
================================
price_history/{year}.parquet の Return_1d と screening_master.parquet の
Sector17CodeName を突き合わせ、対象営業日の 17 セクター全件の平均騰落率を
CSV / JSON で出力する。

マクロ市況ツイート（/macro-tweet）の【セクター】欄が上位3・下位3を必要とする
のに対し、動意レポートは個別銘柄の解説文中でセクター平均に付随的に触れるだけ
のため全件が揃わない。本スクリプトが日次セクターランキングの第一ソースとなる。
動意レポートの誌面は変更しない（prompts/mover-report.md の除外指示は維持）。

入力（いずれも読み取り専用・上書きしない）:
  bi/outputs/price_history/{year}.parquet   Date / Code / Return_1d（既に % 単位）
  bi/outputs/screening_master.parquet       Code / Sector17CodeName

データソースの二段構え（PM 2026-09-03 承認）:
  price_history の日次 ETL は GHA cron の遅延で夕刊（JST 17:45）に間に合わない日があり、
  2026-09-02・2026-09-03 と 2 日連続でマクロ市況ツイートが未発行になった。動意レポートは
  同じセクター騰落率を JQuants からライブ取得しており両日とも正常に発行されていたため、
  price_history の着地を待つ必然性は元々なかった。
    source=parquet（第一優先・既定）… 従来どおり price_history の Return_1d を使う。
    source=jquants_live（フォールバック）… parquet に対象日が無い場合のみ、JQuants
      /equities/bars/daily から対象日と直前営業日の終値をライブ取得し、
      make_price_history_master.py と同一の手順（normalize_code_4 → 4桁コード重複は
      keep="last" → 生 Close の pct_change × 100 を小数第 3 位で丸め）で Return_1d を
      再現する。2026-09-02 の全 4,167 銘柄で parquet と完全一致することを実測確認済み。
  どちらの経路でも集計ロジック・出力スキーマは同一。ライブ取得も失敗した場合は
  推測での代替を一切行わず異常終了する（agents/bi.md の欠損方針）。

出力:
  bi/data/processed/sector_daily_{date}.csv
  bi/data/processed/sector_daily_{date}.json

使い方:
  cd bi/pipelines
  python make_sector_daily.py
  python make_sector_daily.py --date 2026-08-24
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = BASE_DIR / ".." / "outputs"
PRICE_HISTORY_DIR = OUTPUTS_DIR / "price_history"
SCREENING_MASTER_PATH = OUTPUTS_DIR / "screening_master.parquet"
PROCESSED_DIR = BASE_DIR / ".." / "data" / "processed"

# ランキング（top3 / bottom3）に採用するセクターの構成銘柄数の下限。
# 構成銘柄が極端に少ない区分（「その他」等）が平均値の外れ値として
# 上位・下位に紛れ込むのを防ぐ。全件出力（sectors）からは除外しない。
MIN_COUNT_FOR_RANKING = 3

# JQuants ライブ取得のフォールバックで、直前営業日を探してさかのぼる暦日の上限。
JQ_LOOKBACK_DAYS = 14


# ---------------------------------------------------------------------------
# フォールバック: JQuants ライブ取得
# ---------------------------------------------------------------------------

def _jq_client():
    """既存パイプラインと同一の作法で JQuants v2 クライアントを作る。

    認証・リトライ・429 バックオフは bi/pipelines/jq_client_utils.py を再利用し、
    このスクリプト独自の実装は持たない（make_mover_report.py と同じ構成）。
    """
    sys.path.insert(0, str(BASE_DIR))
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env")

    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "JQUANTS_API_KEY が未設定のため、price_history のフォールバック"
            "（JQuants ライブ取得）を実行できません。"
        )
    import jquantsapi

    return jquantsapi.ClientV2(api_key=api_key)


def _jq_fetch_close(client, target: date) -> pd.DataFrame:
    """対象日の全市場終値を Code（4桁）/ Close で返す。データ無しなら空 DataFrame。

    make_price_history_master.append_one_day と同じ前処理を踏む:
      normalize_code_4 でコードを 4 桁化し、同一 4 桁コードの重複行（5 桁コードの
      別クラス銘柄）は keep="last" で 1 行に畳む。この 2 手順まで一致させないと
      2593 / 9201 / 9434 等で parquet と別の銘柄を拾って値がずれる（実測確認済み）。
    """
    from jq_client_utils import fetch_paginated_v2, normalize_code_4

    rows = fetch_paginated_v2(
        client,
        "/equities/bars/daily",
        params={"date": target.strftime("%Y-%m-%d")},
        sleep_seconds=1.0,
    )
    if not rows:
        return pd.DataFrame(columns=["Code", "Close"])

    df = pd.DataFrame(rows)
    close_col = next(
        (c for c in df.columns if c.lower() in ("close", "c")), None
    )
    code_col = next((c for c in df.columns if c.lower() == "code"), None)
    if close_col is None or code_col is None:
        raise RuntimeError(
            f"JQuants /equities/bars/daily の応答に終値またはコード列がありません: "
            f"{list(df.columns)}"
        )
    df["Code"] = df[code_col].map(normalize_code_4)
    df["Close"] = pd.to_numeric(df[close_col], errors="coerce")
    df = df.dropna(subset=["Close"])
    df = df.drop_duplicates(subset=["Code"], keep="last")
    return df[["Code", "Close"]].reset_index(drop=True)


def _load_returns_jquants(target: date) -> pd.DataFrame:
    """JQuants から対象日の Return_1d を算出し Date / Code / Return_1d を返す。

    price_history の Return_1d は「調整前の生 Close の前営業日比 × 100 を小数第 3 位で
    丸めた値」（make_price_history_master.enrich_price_data）。ここでも同じ式を使う。
    """
    client = _jq_client()

    today_df = _jq_fetch_close(client, target)
    if today_df.empty:
        raise ValueError(
            f"JQuants に {target.isoformat()} の日足がありません"
            "（休場日か、EOD が未着です）。推測での補完は行わないため中止します。"
        )

    prev_date: date | None = None
    prev_df = pd.DataFrame()
    for i in range(1, JQ_LOOKBACK_DAYS + 1):
        d = target - timedelta(days=i)
        cand = _jq_fetch_close(client, d)
        if not cand.empty:
            prev_date, prev_df = d, cand
            break
    if prev_date is None:
        raise ValueError(
            f"JQuants で {target.isoformat()} の直前営業日が "
            f"{JQ_LOOKBACK_DAYS} 日さかのぼっても見つかりません。"
        )

    print(f"  JQuants ライブ取得: 当日 {target.isoformat()} ({len(today_df)} 銘柄) / "
          f"前営業日 {prev_date.isoformat()} ({len(prev_df)} 銘柄)")

    merged = today_df.merge(prev_df, on="Code", suffixes=("", "_prev"))
    merged["Return_1d"] = (
        (merged["Close"] / merged["Close_prev"] - 1) * 100
    ).round(3)
    merged["Date"] = pd.Timestamp(target)
    return merged[["Date", "Code", "Return_1d"]].dropna(subset=["Return_1d"])




# ---------------------------------------------------------------------------
# 入力読み込み
# ---------------------------------------------------------------------------

def _load_price_history(year: int) -> pd.DataFrame:
    """指定年の price_history parquet から Date / Code / Return_1d を読む。"""
    path = (PRICE_HISTORY_DIR / f"{year}.parquet").resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"price_history が見つかりません: {path}\n"
            "（対象日の年の parquet が未生成です。make_price_history_master.py の実行状況を確認してください）"
        )
    df = pd.read_parquet(path, columns=["Date", "Code", "Return_1d"])
    df["Date"] = pd.to_datetime(df["Date"])
    df["Code"] = df["Code"].astype(str)
    return df


def _load_sector_master() -> pd.DataFrame:
    """screening_master から Code / Sector17CodeName を読む。"""
    path = SCREENING_MASTER_PATH.resolve()
    if not path.exists():
        raise FileNotFoundError(f"screening_master.parquet が見つかりません: {path}")
    df = pd.read_parquet(path, columns=["Code", "Sector17CodeName"])
    df["Code"] = df["Code"].astype(str)
    return df.drop_duplicates("Code").reset_index(drop=True)


def _resolve_target_date(arg_date: str | None, source: str = "auto") -> date:
    """--date 未指定時の対象日を決める。

    source="jquants" のときだけ price_history を見ず、JQuants の最新営業日を採用する
    （parquet が未更新の日に古い日付を拾わないため）。
    """
    if arg_date:
        return date.fromisoformat(arg_date)

    if source == "jquants":
        from jq_client_utils import latest_trading_day_date_v2

        return latest_trading_day_date_v2(_jq_client())

    # 年をまたぐケースを避けるため、当年 → 前年の順に最新日を探す
    today_year = date.today().year
    for year in (today_year, today_year - 1):
        path = (PRICE_HISTORY_DIR / f"{year}.parquet").resolve()
        if not path.exists():
            continue
        dates = pd.read_parquet(path, columns=["Date"])["Date"]
        if len(dates) == 0:
            continue
        return pd.to_datetime(dates).max().date()

    if source == "auto":
        # parquet が一つも無い場合も JQuants へ落とす（ETL 未実行日の救済）。
        from jq_client_utils import latest_trading_day_date_v2

        print("[fallback] price_history が見つからないため、JQuants の最新営業日を採用します。")
        return latest_trading_day_date_v2(_jq_client())

    raise FileNotFoundError(
        f"price_history に有効なデータがありません（探索先: {PRICE_HISTORY_DIR.resolve()}）"
    )


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

def _resolve_day_returns(target: date, source: str) -> tuple[pd.DataFrame, str]:
    """対象日の Date / Code / Return_1d と、実際に使ったデータソース名を返す。

    source:
      "auto"    … parquet を第一優先で試し、対象日が無い場合のみ JQuants へ落とす。
      "parquet" … parquet のみ（従来の振る舞いそのまま。無ければ異常終了）。
      "jquants" … JQuants ライブ取得のみ（検証用に経路を強制する）。

    どの経路でも取れなければ推測での代替は一切せず例外を送出する。
    """
    if source not in ("auto", "parquet", "jquants"):
        raise ValueError(f"--source の値が不正です: {source}")

    if source == "jquants":
        return _load_returns_jquants(target), "jquants_live"

    # parquet 経路（auto の第一優先を含む）
    parquet_error: str | None = None
    try:
        px = _load_price_history(target.year)
        day = px[px["Date"] == pd.Timestamp(target)]
        if not day.empty:
            return day[["Date", "Code", "Return_1d"]], "parquet"
        available = pd.to_datetime(px["Date"]).max()
        parquet_error = (
            f"price_history に {target.isoformat()} のデータがありません"
            f"（{target.year}.parquet の最新日: "
            f"{available.date() if pd.notna(available) else 'なし'}）。"
        )
    except FileNotFoundError as e:
        parquet_error = str(e)

    if source == "parquet":
        raise ValueError(
            parquet_error
            + "\n休場日か、価格マスターが当日分まで更新されていない可能性があります。"
            "推測での補完は行わないため中止します。"
        )

    # auto: price_history の ETL 遅延に引きずられないよう JQuants ライブ取得へ落とす。
    print(f"[fallback] {parquet_error}")
    print("[fallback] price_history の着地を待たず JQuants ライブ取得へ切り替えます。")
    return _load_returns_jquants(target), "jquants_live"


def build_sector_daily(target: date, source: str = "auto") -> tuple[pd.DataFrame, int, str]:
    """
    対象営業日の 17 セクター別平均騰落率を返す。

    Returns: (sectors_df, universe_count, data_source)
      sectors_df の列: Sector17CodeName / mean_pct / count / up_count / down_count / rank
      universe_count: 当日の結合後（セクター・騰落率が揃った）銘柄数
      data_source: "parquet" または "jquants_live"

    該当日のデータがどちらの経路でも取れない場合は例外を送出する
    （欠損を推測で埋めることは agents/bi.md の方針により禁止）。
    """
    day, data_source = _resolve_day_returns(target, source)

    master = _load_sector_master()
    merged = day.merge(master, on="Code", how="inner").dropna(
        subset=["Return_1d", "Sector17CodeName"]
    )
    if merged.empty:
        raise ValueError(
            f"{target.isoformat()} は price_history と screening_master の結合結果が 0 件です。"
            "Code の桁揃え・screening_master の鮮度を確認してください。"
        )

    universe_count = int(len(merged))

    grouped = merged.groupby("Sector17CodeName")["Return_1d"].agg(
        mean_pct="mean",
        count="count",
    )
    ups = merged[merged["Return_1d"] > 0].groupby("Sector17CodeName").size()
    downs = merged[merged["Return_1d"] < 0].groupby("Sector17CodeName").size()

    out = grouped.reset_index()
    out["up_count"] = out["Sector17CodeName"].map(ups).fillna(0).astype(int)
    out["down_count"] = out["Sector17CodeName"].map(downs).fillna(0).astype(int)
    out["mean_pct"] = out["mean_pct"].round(2)
    out["count"] = out["count"].astype(int)
    out = out.sort_values(["mean_pct", "Sector17CodeName"], ascending=[False, True])
    out["rank"] = range(1, len(out) + 1)
    out = out[
        ["Sector17CodeName", "mean_pct", "count", "up_count", "down_count", "rank"]
    ].reset_index(drop=True)
    return out, universe_count, data_source


def _records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", force_ascii=False))


def build_top_bottom(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """構成銘柄 MIN_COUNT_FOR_RANKING 社以上のセクターだけから上位3・下位3を選ぶ。"""
    eligible = df[df["count"] >= MIN_COUNT_FOR_RANKING].sort_values(
        "mean_pct", ascending=False
    )
    top3 = _records(eligible.head(3))
    bottom3 = _records(eligible.tail(3).sort_values("mean_pct", ascending=True))
    return top3, bottom3


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="日次セクター（17区分）平均騰落率ランキングを CSV / JSON に出力"
    )
    parser.add_argument(
        "--date",
        default=None,
        help="対象営業日 YYYY-MM-DD（省略時は price_history の最新営業日）",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=PROCESSED_DIR,
        help="CSV/JSON の出力先（既定 bi/data/processed）",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "parquet", "jquants"],
        default="auto",
        help=(
            "騰落率の取得経路。"
            "auto（既定）= price_history を第一優先とし、対象日が無ければ JQuants ライブ取得へ落とす / "
            "parquet = price_history のみ / jquants = JQuants ライブ取得のみ（検証用）"
        ),
    )
    args = parser.parse_args()

    print(
        "\n[make_sector_daily] これから行うこと:\n"
        "  1) bi/outputs/price_history の該当年 parquet から、対象営業日の全銘柄の前日比を読み込みます。\n"
        "     （parquet に対象日が無い場合は JQuants から当日・前営業日の終値をライブ取得し、\n"
        "     同じ式で前日比を再現します。--source で経路を強制できます。）\n"
        "  2) bi/outputs/screening_master.parquet の 17 業種区分を突き合わせ、業種ごとの平均騰落率・\n"
        "     構成銘柄数・上昇/下落の内訳を集計します。\n"
        "  3) bi/data/processed に CSV と JSON を保存します（上位3・下位3は JSON に同梱）。\n"
        "（入力の parquet は読み取り専用で、上書きは一切行いません。）\n"
    )

    target = _resolve_target_date(args.date, args.source)
    sectors, universe_count, data_source = build_sector_daily(target, args.source)
    top3, bottom3 = build_top_bottom(sectors)

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"sector_daily_{target.isoformat()}"
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"

    sectors.to_csv(csv_path, index=False, encoding="utf-8-sig")
    payload = {
        "as_of": target.isoformat(),
        "data_source": data_source,
        "universe_count": universe_count,
        "min_count_for_ranking": MIN_COUNT_FOR_RANKING,
        "sectors": _records(sectors),
        "top3": top3,
        "bottom3": bottom3,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"as_of={target.isoformat()} source={data_source} "
        f"sectors={len(sectors)} universe={universe_count}"
    )
    print(
        "top3   : "
        + " / ".join(f"{r['Sector17CodeName']} {r['mean_pct']:+.2f}% (n={r['count']})" for r in top3)
    )
    print(
        "bottom3: "
        + " / ".join(f"{r['Sector17CodeName']} {r['mean_pct']:+.2f}% (n={r['count']})" for r in bottom3)
    )
    print(f"saved: {csv_path}")
    print(f"saved: {json_path}")


if __name__ == "__main__":
    main()
