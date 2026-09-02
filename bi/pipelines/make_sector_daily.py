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
from datetime import date
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


def _resolve_target_date(arg_date: str | None) -> date:
    """--date 未指定時は price_history の最新営業日を採用する。"""
    if arg_date:
        return date.fromisoformat(arg_date)
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
    raise FileNotFoundError(
        f"price_history に有効なデータがありません（探索先: {PRICE_HISTORY_DIR.resolve()}）"
    )


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

def build_sector_daily(target: date) -> tuple[pd.DataFrame, int]:
    """
    対象営業日の 17 セクター別平均騰落率を返す。

    Returns: (sectors_df, universe_count)
      sectors_df の列: Sector17CodeName / mean_pct / count / up_count / down_count / rank
      universe_count: 当日の結合後（セクター・騰落率が揃った）銘柄数

    該当日のデータが price_history に無い場合は例外を送出する
    （欠損を推測で埋めることは agents/bi.md の方針により禁止）。
    """
    px = _load_price_history(target.year)
    day = px[px["Date"] == pd.Timestamp(target)]
    if day.empty:
        available = pd.to_datetime(px["Date"]).max()
        raise ValueError(
            f"price_history に {target.isoformat()} のデータがありません"
            f"（{target.year}.parquet の最新日: {available.date() if pd.notna(available) else 'なし'}）。\n"
            "休場日か、価格マスターが当日分まで更新されていない可能性があります。"
            "推測での補完は行わないため中止します。"
        )

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
    return out, universe_count


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
    args = parser.parse_args()

    print(
        "\n[make_sector_daily] これから行うこと:\n"
        "  1) bi/outputs/price_history の該当年 parquet から、対象営業日の全銘柄の前日比を読み込みます。\n"
        "  2) bi/outputs/screening_master.parquet の 17 業種区分を突き合わせ、業種ごとの平均騰落率・\n"
        "     構成銘柄数・上昇/下落の内訳を集計します。\n"
        "  3) bi/data/processed に CSV と JSON を保存します（上位3・下位3は JSON に同梱）。\n"
        "（入力の parquet は読み取り専用で、上書きは一切行いません。）\n"
    )

    target = _resolve_target_date(args.date)
    sectors, universe_count = build_sector_daily(target)
    top3, bottom3 = build_top_bottom(sectors)

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"sector_daily_{target.isoformat()}"
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"

    sectors.to_csv(csv_path, index=False, encoding="utf-8-sig")
    payload = {
        "as_of": target.isoformat(),
        "universe_count": universe_count,
        "min_count_for_ranking": MIN_COUNT_FOR_RANKING,
        "sectors": _records(sectors),
        "top3": top3,
        "bottom3": bottom3,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"as_of={target.isoformat()} sectors={len(sectors)} universe={universe_count}")
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
