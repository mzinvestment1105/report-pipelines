"""
動意銘柄レポート 生データ生成
==============================
市場区分（プライム/スタンダード/グロース）ごとに動意銘柄を抽出し、
TDNet・みんかぶニュースと組み合わせた生データMarkdownを出力する。

レポート構成:
  Layer 1: セクター別フロー（17セクター）
  Layer 2: 全動意銘柄リスト（上位100銘柄・基本情報のみ）
  Layer 3: 注目銘柄詳細（TDNet+みんかぶニュース付き）
    プライム    値上がり5社 + 値下がり5社
    スタンダード  値上がり5社 + 値下がり5社
    グロース    値上がり5社 + 値下がり5社
  Layer 4: 売買代金ランキング
    プライム    上位5社
    スタンダード  上位5社
    グロース    上位10社

各銘柄には市場区分・時価総額・セクター・事業内容を付与。
過去のDeep Dive（research/stocks/）・セクター調査（research/sectors/）・
マクロレポート（research/markets/）を自動参照。

出力: market/daily/YYYY-MM-DD_movers_raw.md

実行:
  cd bi/pipelines
  python make_mover_report.py
  python make_mover_report.py --date 2026-04-10
  python make_mover_report.py --no-pdf   # PDF取得スキップ（高速）

環境変数:
  JQUANTS_API_KEY  必須
"""

from __future__ import annotations

import argparse
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
import requests
from dotenv import load_dotenv

from jq_client_utils import fetch_paginated_v2, normalize_code_4
from edinetdb_client import EdinetDBClient

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / ".." / "outputs"
MARKET_DAILY_DIR = BASE_DIR / ".." / ".." / "market" / "daily"
RESEARCH_DIR = BASE_DIR / ".." / ".." / "research"
SCREENING_MASTER_PATH = OUTPUTS_DIR / "screening_master.parquet"
SECTOR_AGG_PATH = OUTPUTS_DIR / "sector_weekly.parquet"

# ---------------------------------------------------------------------------
# 市場区分定義
# ---------------------------------------------------------------------------
MARKET_PRIME    = "プライム"
MARKET_STANDARD = "スタンダード"
MARKET_GROWTH   = "グロース"

# 注目銘柄（TDNet+Yahoo取得対象）
DETAIL_CONFIG = {
    MARKET_PRIME:    {"top": 5, "bottom": 5},
    MARKET_STANDARD: {"top": 5, "bottom": 5},
    MARKET_GROWTH:   {"top": 10, "bottom": 5},
}

# 売買代金ランキング件数
TURNOVER_CONFIG = {
    MARKET_PRIME:    5,
    MARKET_STANDARD: 5,
    MARKET_GROWTH:   10,
}

# Layer 2: 全動意銘柄リスト上位件数
ALL_MOVERS_TOP_N = 100

# TDNet設定
DEFAULT_TDNET_DAYS   = 30
TDNET_PDF_MAX_CHARS  = 2000
REQUEST_SLEEP        = 0.5

_TDNET_ATOM_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.atom"
_NS = {"a": "http://purl.org/atom/ns#"}

_MINKABU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

_BBS_NOISE = re.compile(
    r"(JavaScript|ポートフォリオ|ランキング|ログイン|VIP倶楽部|掲示板マイページ"
    r"|前のページ|変更点|リニューアル|^返信|投資の参考|^はい\d|^いいえ\d"
    r"|^No\.\d|^\d{4}/\d+/\d+|NISA|カードローン|証券会社|不動産投資"
    r"|投資信託|FX・為替|米国株|日本株トップ|マイアカウント|検索やさしい"
    r"|興味ある方|LINEで|lineで|ライン.*登録|登録.*ライン|お問い合わせはline"
    r"|無料相談|公式line|LINE公式|友達追加|@[a-zA-Z0-9_]+$"
    # 感情スコアテキスト
    r"|強く買いたい.*%|買いたい.*売りたい.*%|直近1週間でユーザーが掲示板"
    # 投稿メタデータ（ユーザー名＋番号＋日時＋報告 が混入するパターン）
    r"|No\.\d{5,7}|報告$|\d{4}/\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}報告"
    # ナビ・フッター系
    r"|JASRAC|プライバシーポリシー|利用規約|免責事項|ヘルプ・お問い合わせ"
    r"|情報提供会社|東京証券取引所.*大阪取引所|最近見た銘柄.*ランキング)"
)
_BBS_POST_LIKE = re.compile(r"[。！？ねよわだます]")
# 引用付き投稿（>>番号）は引用なし版が直後に来るため除外
_BBS_QUOTE = re.compile(r"^>>\d+")


# ---------------------------------------------------------------------------
# Step 1: 日次価格を全銘柄一括取得
# ---------------------------------------------------------------------------

def fetch_daily_all(client, target_date: date) -> pd.DataFrame:
    rows = fetch_paginated_v2(
        client,
        "/equities/bars/daily",
        params={"date": target_date.strftime("%Y-%m-%d")},
        sleep_seconds=1.0,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    col_map = {}
    for col in df.columns:
        cl = col.lower()
        if cl in ("open", "o"):           col_map[col] = "Open"
        elif cl in ("high", "h"):         col_map[col] = "High"
        elif cl in ("low", "l"):          col_map[col] = "Low"
        elif cl in ("close", "c"):        col_map[col] = "Close"
        elif cl in ("volume", "v", "vo"): col_map[col] = "Volume"
        elif cl in ("va",):               col_map[col] = "TurnoverJQ"  # JQuants実績売買代金
        elif cl == "adjfactor":           col_map[col] = "AdjFactor"
        elif cl == "adjc":                col_map[col] = "AdjClose"
        elif cl == "code":                col_map[col] = "Code"
        elif cl == "date":                col_map[col] = "Date"
    df = df.rename(columns=col_map)
    df["Code"] = df["Code"].astype(str).str[:4]
    for col in ("Close", "Volume", "Open", "High", "Low", "TurnoverJQ", "AdjFactor", "AdjClose"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_ohlc_history(
    client,
    target_codes: set[str],
    today_date: date,
    n_days: int = 60,
    cache_dir: Path | None = None,
) -> pd.DataFrame:
    """直近 n_days 営業日の OHLC を target_codes 銘柄について取得。

    JQuants `/equities/bars/daily?date=YYYY-MM-DD` を日付ごとに呼び出して
    全銘柄一括取得 → target_codes でフィルタする方式。
    キャッシュ: cache_dir/ohlc_history_{today}.parquet
    """
    if cache_dir is None:
        cache_dir = BASE_DIR / ".." / "data" / "raw"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"ohlc_history_{today_date.isoformat()}.parquet"

    if cache_path.exists():
        try:
            cached = pd.read_parquet(cache_path)
            cached["Code"] = cached["Code"].astype(str).str[:4]
            return cached[cached["Code"].isin(target_codes)].copy()
        except Exception:
            pass

    rows_all: list[dict] = []
    days_found = 0
    cursor = today_date
    max_calendar_days = n_days * 2 + 20  # 営業日 60 確保のため余裕を持ったループ
    for _ in range(max_calendar_days):
        if days_found >= n_days:
            break
        rows = fetch_paginated_v2(
            client,
            "/equities/bars/daily",
            params={"date": cursor.strftime("%Y-%m-%d")},
            sleep_seconds=0.6,
        )
        if rows:
            for r in rows:
                rows_all.append({
                    "Code": str(r.get("code", ""))[:4],
                    "Date": cursor.isoformat(),
                    "Open":   r.get("o", r.get("Open")),
                    "High":   r.get("h", r.get("High")),
                    "Low":    r.get("l", r.get("Low")),
                    "Close":  r.get("c", r.get("Close")),
                    "Volume": r.get("v", r.get("Volume")),
                })
            days_found += 1
        cursor -= timedelta(days=1)

    if not rows_all:
        return pd.DataFrame()

    full = pd.DataFrame(rows_all)
    for col in ("Open", "High", "Low", "Close", "Volume"):
        full[col] = pd.to_numeric(full[col], errors="coerce")

    try:
        full.to_parquet(cache_path, index=False)
    except Exception:
        pass

    return full[full["Code"].isin(target_codes)].copy()


def compute_price_levels(hist_df: pd.DataFrame, code4: str, close_today: float) -> dict:
    """52週・1ヶ月の高安、現在水準を計算して返す。

    hist_df: fetch_ohlc_history の戻り値（複数銘柄分・60 営業日分）
    """
    df = hist_df[hist_df["Code"] == code4].copy()
    if df.empty or pd.isna(close_today):
        return {}
    df = df.sort_values("Date")
    n = len(df)
    df60 = df  # 取得済み 60 営業日
    df20 = df.tail(20)
    out = {}
    if df60["High"].notna().any():
        h60 = df60["High"].max()
        l60 = df60["Low"].min()
        out["High_60d"] = h60
        out["Low_60d"]  = l60
        if h60 and l60 and (h60 != l60):
            out["Pos60_pct"] = (close_today - l60) / (h60 - l60) * 100
        out["FromHigh60_pct"] = (close_today / h60 - 1) * 100 if h60 else None
        out["FromLow60_pct"]  = (close_today / l60 - 1) * 100 if l60 else None
    if df20["High"].notna().any():
        h20 = df20["High"].max()
        l20 = df20["Low"].min()
        out["High_20d"] = h20
        out["Low_20d"]  = l20
        if h20 and l20 and (h20 != l20):
            out["Pos20_pct"] = (close_today - l20) / (h20 - l20) * 100
        out["FromHigh20_pct"] = (close_today / h20 - 1) * 100 if h20 else None
        out["FromLow20_pct"]  = (close_today / l20 - 1) * 100 if l20 else None
    out["DaysCovered"] = n
    return out


def build_supply_block(row: pd.Series, hist_df: pd.DataFrame | None) -> list[str]:
    """個別銘柄の「需給（信用・株価水準）」ブロックを生成する。

    PM 2026-05-22 確定の必須セクション。信用残・時価総額比・週次推移・機関空売り・
    52 週/1 ヶ月の高安・現在水準を 1 銘柄ぶん出力する。
    """
    code4 = normalize_code_4(row["Code"])
    close_today = row.get("Close_T")
    mcap_yen = row.get("MarketCap")

    long_m  = row.get("LongMarginTradeVolume")
    short_m = row.get("ShortMarginTradeVolume")
    # PM 2026-05-26 確定: spot 信用残（LongMarginTradeVolume / ShortMarginTradeVolume）が NaN なのに
    # 週次 Seq01（=最新値・同データ別ソース）には値がある事象が発生する。Seq01 を最新値として
    # フォールバックすることで「信用残 ─ なのに週次推移 1,583 万」型の構造的矛盾を解消する。
    if pd.isna(long_m):
        long_m = row.get("LongMargin_WkSeq01")
    if pd.isna(short_m):
        short_m = row.get("ShortMargin_WkSeq01")
    lm_per_shares = row.get("Scr_LongMargin_to_SharesOutstanding")
    lm_per_vol5d  = row.get("Scr_LongMargin_to_AvgVol5d")
    inst_short    = row.get("ShortPositionsToSharesOutstandingRatio")
    avg_vol5d     = row.get("AvgDailyVolume5d")
    shares_out    = row.get("NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock")
    # ShortSale_WkSeq01=最新 → ShortSale_WkSeq08=最古（直近4週を使用）
    inst_short_wk = []
    for i in range(1, 5):
        v = row.get(f"ShortSale_WkSeq0{i}")
        if pd.notna(v) and pd.notna(shares_out) and shares_out > 0:
            inst_short_wk.append(v / shares_out * 100)
        else:
            inst_short_wk.append(None)

    lines: list[str] = ["**需給（信用・株価水準）:**"]

    # --- 信用残（最新） ---
    if pd.notna(long_m):
        long_str = f"{long_m/1e4:.1f}万株"
    else:
        long_str = "─"
    if pd.notna(short_m):
        short_str = f"{short_m/1e4:.1f}万株"
    else:
        short_str = "─"
    ratio_str = "─"
    if pd.notna(long_m) and pd.notna(short_m):
        if short_m and short_m > 0:
            ratio_str = f"{long_m/short_m:.2f} 倍"
        elif long_m and long_m > 0:
            ratio_str = "∞（売り残ゼロ）"
        else:
            ratio_str = "0.00 倍"
    lines.append(f"- 信用残: 買 {long_str} / 売 {short_str}（信用倍率 {ratio_str}）")

    # --- 発行済株数比・5日平均出来高比 ---
    # PM 2026-05-30: 信用買残/時価総額 は信用残報告日（過去）の株数に当日終値を掛けるため、
    # 急騰銘柄では分子が過大評価され実態と乖離するbugがあるため削除。
    # 発行済株数比（株数ベース・株価非依存）のみ採用。
    vol_days_str = "─"
    if pd.notna(lm_per_vol5d) and lm_per_vol5d > 0:
        vol_days_str = f"{lm_per_vol5d:.1f} 日分"
    elif pd.notna(long_m) and pd.notna(avg_vol5d) and avg_vol5d > 0:
        vol_days_str = f"{long_m / avg_vol5d:.1f} 日分"
    lm_per_shares_str = "─"
    if pd.notna(lm_per_shares):
        lm_per_shares_str = f"{lm_per_shares*100:.2f}%"
    lines.append(
        f"- 信用買残 / 発行済株数: {lm_per_shares_str} "
        f"／ 解消日数（信用買残 ÷ 5日平均出来高）: {vol_days_str}"
    )

    # --- 信用買残 週次推移（直近 6 週・Seq08=最古→Seq01=最新） ---
    # WkSeq01=最新・WkSeq08=最古のため、逆順(8→1)で取得して古→新の数列を作る
    wk_long_raw = [row.get(f"LongMargin_WkSeq0{i}") for i in range(8, 0, -1)]  # Seq08..Seq01
    wk_long_clean = [v for v in wk_long_raw if pd.notna(v)]
    if len(wk_long_clean) >= 2:
        wk_str = " → ".join(f"{v/1e4:.0f}万" for v in wk_long_clean)
        oldest, newest = wk_long_clean[0], wk_long_clean[-1]
        if oldest:
            chg_pct = (newest - oldest) / oldest * 100
            chg_label = "増加" if chg_pct > 3 else "減少" if chg_pct < -3 else "横ばい"
            chg_str = f"{chg_label}（{chg_pct:+.1f}%）"
        else:
            chg_str = "判定不可"
        lines.append(f"- 信用買残 週次推移（古→新・直近{len(wk_long_clean)}週）: {wk_str}　判定: {chg_str}")
    else:
        lines.append("- 信用買残 週次推移: 過去データなし")

    # --- 信用売残 週次推移（参考・直近3週・Seq03→Seq01=古→新） ---
    wk_short = [row.get(f"ShortMargin_WkSeq0{i}") for i in (3, 2, 1)]  # 古→新
    wk_short_clean = [v for v in wk_short if pd.notna(v)]
    if len(wk_short_clean) >= 2:
        wk_short_str = " → ".join(f"{v/1e4:.1f}万" for v in wk_short_clean)
        lines.append(f"- 信用売残 週次推移（直近3週）: {wk_short_str}")

    # --- 機関空売り（5% 超報告対象のみ） ---
    # inst_short_wk[0] = Seq1 = 最新、inst_short_wk[-1] = 最古
    latest_inst = inst_short_wk[0] if inst_short_wk and inst_short_wk[0] is not None else (
        inst_short * 100 if pd.notna(inst_short) and inst_short > 0 else None
    )
    if latest_inst is not None and latest_inst > 0:
        wk_vals = [v for v in inst_short_wk if v is not None]
        if len(wk_vals) >= 2:
            wk_str = " → ".join(f"{v:.2f}%" for v in reversed(wk_vals))  # 古→新の順で表示
            delta = wk_vals[0] - wk_vals[-1]  # 最新 - 最古（直近4週での変化）
            trend = f"▲+{delta:.2f}%" if delta > 0.1 else (f"▼{delta:.2f}%" if delta < -0.1 else "横ばい")
            lines.append(f"- 機関空売り比率（発行株比・5%超報告）: {latest_inst:.2f}% | 直近4週推移: {wk_str}（{trend}）")
        else:
            lines.append(f"- 機関空売り比率（発行株比・5%超報告）: {latest_inst:.2f}%")
    else:
        lines.append("- 機関空売り比率: 5%超報告対象外（または報告なし）")

    # --- 株価水準（52週/1ヶ月レンジ・現在位置） ---
    levels = compute_price_levels(hist_df, code4, close_today) if hist_df is not None and not hist_df.empty else {}
    if levels.get("High_60d") is not None:
        h60 = levels.get("High_60d"); l60 = levels.get("Low_60d")
        pos60 = levels.get("Pos60_pct")
        from_h60 = levels.get("FromHigh60_pct")
        from_l60 = levels.get("FromLow60_pct")
        pos60_str = f"レンジ下から {pos60:.0f}%" if pos60 is not None else "─"
        h60_diff = f"高値からマイナス {abs(from_h60):.1f}%" if from_h60 is not None else "─"
        l60_diff = f"安値からプラス {from_l60:.1f}%" if from_l60 is not None else "─"
        lines.append(
            f"- 直近60営業日（≒3ヶ月）レンジ: 高値 {h60:,.0f}円 / 安値 {l60:,.0f}円 "
            f"／ 現在位置: {pos60_str}（{h60_diff} / {l60_diff}）"
        )
    if levels.get("High_20d") is not None:
        h20 = levels.get("High_20d"); l20 = levels.get("Low_20d")
        pos20 = levels.get("Pos20_pct")
        pos20_str = f"レンジ下から {pos20:.0f}%" if pos20 is not None else "─"
        lines.append(
            f"- 直近20営業日（≒1ヶ月）レンジ: 高値 {h20:,.0f}円 / 安値 {l20:,.0f}円 "
            f"／ 現在位置: {pos20_str}"
        )
    if not levels:
        lines.append("- 株価水準（60d/20d レンジ）: 取得不可")

    lines.append("")
    return lines


def resolve_trading_days(client, target_date: date, lookback: int = 14) -> tuple[date, date]:
    found: list[date] = []
    for i in range(lookback):
        d = target_date - timedelta(days=i)
        rows = fetch_paginated_v2(
            client, "/equities/bars/daily",
            params={"date": d.strftime("%Y-%m-%d")},
            sleep_seconds=0.5,
        )
        if rows:
            found.append(d)
            if len(found) == 2:
                break
    if len(found) < 2:
        raise RuntimeError("直近2営業日が見つかりません")
    return found[0], found[1]


# ---------------------------------------------------------------------------
# Step 2: リターン計算・市場区分付き全銘柄テーブル作成
# ---------------------------------------------------------------------------

def build_full_table(
    today_df: pd.DataFrame,
    prev_df: pd.DataFrame,
    master_df: pd.DataFrame,
) -> pd.DataFrame:
    """前日比リターン・出来高・市場区分・セクターを全銘柄分結合したテーブルを返す。"""
    keep_cols = ["Code", "Close", "Volume"]
    if "TurnoverJQ" in today_df.columns:
        keep_cols.append("TurnoverJQ")
    if "AdjFactor" in today_df.columns:
        keep_cols.append("AdjFactor")
    t = today_df[keep_cols].rename(columns={"Close": "Close_T", "Volume": "Volume_T"})
    p = prev_df[["Code", "Close"]].rename(columns={"Close": "Close_P"})
    # left join: IPO初日など前日価格なし銘柄も売買代金ランキングに含める
    df = t.merge(p, on="Code", how="left")

    # PM 2026-05-28 確定: 株式分割等の権利落ち日は AdjFactor を反映して真のリターンを算出する。
    # AdjFactor は当日の調整係数（1.0=コーポレートアクションなし・0.333=1:3 分割等）。
    # 過去終値に AdjFactor を掛けることで分割後スケールに揃え、見かけ上の -80% 大幅安を防ぐ。
    # 1:5 分割（5/28 485A 等）でランキング Bottom が分割組で埋まる事故の再発防止。
    if "AdjFactor" in df.columns:
        adj = df["AdjFactor"].fillna(1.0)
    else:
        adj = pd.Series(1.0, index=df.index)
    df["AdjFactor"] = adj
    df["HasCorporateAction"] = (adj != 1.0) & adj.notna()
    df["DailyReturn"] = (df["Close_T"] / (df["Close_P"] * adj) - 1) * 100
    df = df.dropna(subset=["Close_T"])

    meta_cols = [c for c in ["Code", "CompanyName", "Sector17CodeName",
                              "MarketCodeName", "MarketCap"] if c in master_df.columns]
    meta = master_df[meta_cols].copy()
    meta["Code"] = meta["Code"].astype(str).str[:4]
    df = df.merge(meta, on="Code", how="left")

    # screening_master未登録銘柄（IPO直後等）の MarketCodeName を
    # JQuantsのコード体系（末尾Aは新興市場IPO）からグロースと推定して補完
    # ただし ETF/REIT/上場投信 は個別株として扱わないため除外
    # （2026-05-20 200A NEXT FUNDS 日経半導体株指数連動型上場投信を誤ってグロース個別株として
    #  動意銘柄レポートに含めた事案の再発防止・[memory feedback_etf_reit_not_individual.md] 参照）
    if "MarketCodeName" in df.columns:
        mask = df["MarketCodeName"].isna()
        if mask.any():
            etf_reit_keywords = (
                "ETF|上場投信|上場投資信託|NEXT FUNDS|iShares|MAXIS|"
                "ダイワ上場|J-REIT|不動産投資法人|投資法人|リート|"
                "連動型上場|レバレッジ|インバース"
            )
            company_name_str = df.get("CompanyName", pd.Series([None] * len(df))).astype(str)
            cn_col = df.get("CompanyName", pd.Series([None] * len(df)))
            is_new_ipo_a_mask = df["Code"].astype(str).str.endswith("A")
            is_etf_reit = (
                # 末尾Aでない銘柄かつ CompanyName 未取得（旧来コードで screening_master 未登録の場合）
                (cn_col.isna() & ~is_new_ipo_a_mask)
                # 銘柄名がコードと同じ（ETF/REIT の典型・200A 200A 等）
                | (company_name_str == df["Code"].astype(str))
                # 銘柄名に ETF/REIT 系キーワード
                | company_name_str.str.contains(etf_reit_keywords, na=False, regex=True)
            )
            # ETF/REIT でない末尾 A 銘柄のみ「グロース」と推定（個別株 IPO 直後）
            df.loc[
                mask & df["Code"].str.endswith("A") & ~is_etf_reit,
                "MarketCodeName"
            ] = MARKET_GROWTH

    # PM 2026-05-22 確定: 需給ブロック（信用残・株価水準）を全銘柄に付与するため、
    # screening_master.parquet から信用・出来高関連列をマージする
    # [prompts/_common_rules.md] [memory feedback_mover_supply_required.md]
    supply_cols = [
        c for c in (
            "LongMarginTradeVolume",
            "ShortMarginTradeVolume",
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
            "Scr_LongMargin_to_SharesOutstanding",
            "Scr_LongMargin_to_AvgVol5d",
            "ShortPositionsToSharesOutstandingRatio",
            "ShortSale_WkSeq01",
            "ShortSale_WkSeq02",
            "ShortSale_WkSeq03",
            "ShortSale_WkSeq04",
            "AvgDailyVolume5d",
            "AvgDailyValue5d",
            "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock",
        ) if c in master_df.columns
    ]
    if supply_cols:
        supply_meta = master_df[["Code"] + supply_cols].copy()
        supply_meta["Code"] = supply_meta["Code"].astype(str).str[:4]
        df = df.merge(supply_meta, on="Code", how="left")

    if "MarketCap" in df.columns:
        df["MarketCapOku"] = pd.to_numeric(df["MarketCap"], errors="coerce") / 1e8

    # PM 2026-05-22 確定: ETF/REIT/上場投信を全レポート全セクションから完全除外
    # raw データ生成時点で除外することで claude-code-action が ETF を Top10 に入れる事故を構造的に防止
    # [prompts/_common_rules.md §1] [memory feedback_etf_reit_not_individual.md]
    if "CompanyName" in df.columns:
        etf_reit_keywords_full = (
            r"ETF|ETN|上場投信|上場投資信託|投信|NEXT FUNDS|iShares|MAXIS|"
            r"ダイワ上場|日経連動|指数連動|連動型上場|"
            r"レバレッジ|インバース|ブル\d|ベア\d|ダブル\s?(ブル|ベア)|"
            r"J-REIT|REIT|リート|不動産投資法人|投資法人|インフラファンド"
        )
        company_name_str = df["CompanyName"].astype(str)
        # 末尾Aコードは新規IPO個別株（グロース市場）のため CompanyName=NaN でも除外しない
        is_new_ipo_a = df["Code"].astype(str).str.endswith("A")
        is_etf_reit = (
            # CompanyName が NaN かつ末尾Aでない（旧来の数字4桁コードで未登録=ETF/REIT の可能性）
            (df["CompanyName"].isna() & ~is_new_ipo_a)
            # 銘柄名がコードと同じ（ETF/REIT の典型: 200A 200A 等）
            | (company_name_str == df["Code"].astype(str))
            # 銘柄名に ETF/REIT 系キーワード
            | company_name_str.str.contains(etf_reit_keywords_full, na=False, regex=True)
        )
        excluded_count = int(is_etf_reit.sum())
        if excluded_count:
            print(
                f"[ETF/REIT filter] {excluded_count} 銘柄を完全除外: "
                + ", ".join(
                    f"{c} {n}"
                    for c, n in zip(
                        df.loc[is_etf_reit, "Code"].head(20).tolist(),
                        df.loc[is_etf_reit, "CompanyName"].head(20).fillna("N/A").tolist(),
                    )
                )
            )
        df = df[~is_etf_reit].copy()

    return df.drop_duplicates("Code").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Step 3: 各レイヤー用のサブセット抽出
# ---------------------------------------------------------------------------

def extract_detail_stocks(full_df: pd.DataFrame) -> pd.DataFrame:
    """市場区分ごとに注目銘柄（TDNet+Yahoo対象）を抽出して返す。

    PM 2026-05-28 確定: 値上がり/値下がり ランキングから権利落ち（株式分割等・AdjFactor != 1.0）
    銘柄を完全除外する。生 Close 比較の -80% 等の見かけ上の急落で実際の動意銘柄が
    弾き飛ばされる事故の再発防止。売買代金ランキングには分割組も残す（出来高は本物の動意のため）。
    """
    frames = []
    # IPO初日など前日価格なし銘柄はリターン計算不可のため値動きランキングから除外
    df = full_df.dropna(subset=["DailyReturn"])
    if "HasCorporateAction" in df.columns:
        df = df[~df["HasCorporateAction"].fillna(False)]
    for market, cfg in DETAIL_CONFIG.items():
        mdf = df[df["MarketCodeName"] == market]
        top    = mdf.nlargest(cfg["top"],    "DailyReturn")
        bottom = mdf.nsmallest(cfg["bottom"], "DailyReturn")
        for df_part, direction in [(top, "up"), (bottom, "down")]:
            df_part = df_part.copy()
            df_part["_market"]    = market
            df_part["_direction"] = direction
            frames.append(df_part)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).drop_duplicates("Code").reset_index(drop=True)


def extract_all_movers(full_df: pd.DataFrame, top_n: int = ALL_MOVERS_TOP_N) -> pd.DataFrame:
    """全市場から値動き上位N銘柄（絶対値順）を返す。権利落ち銘柄は除外。"""
    df = full_df.dropna(subset=["DailyReturn"]).copy()
    if "HasCorporateAction" in df.columns:
        df = df[~df["HasCorporateAction"].fillna(False)]
    df["AbsReturn"] = df["DailyReturn"].abs()
    return df.nlargest(top_n, "AbsReturn").reset_index(drop=True)


def extract_turnover_ranking(full_df: pd.DataFrame) -> pd.DataFrame:
    """市場区分ごとに売買代金上位を返す。JQuants実績値(TurnoverJQ)を優先し、なければ終値×出来高で計算。"""
    df = full_df.copy()
    if "TurnoverJQ" in df.columns:
        df["Turnover"] = df["TurnoverJQ"].fillna(df["Close_T"] * df["Volume_T"])
    else:
        df["Turnover"] = df["Close_T"] * df["Volume_T"]
    frames = []
    for market, n in TURNOVER_CONFIG.items():
        mdf = df[df["MarketCodeName"] == market]
        top = mdf.nlargest(n, "Turnover").copy()
        top["_market"] = market
        frames.append(top)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).drop_duplicates("Code").reset_index(drop=True)


def extract_sector_flow(full_df: pd.DataFrame) -> pd.DataFrame:
    """セクター別の平均リターン・銘柄数集計を返す。権利落ち銘柄は集計から除外。"""
    if "Sector17CodeName" not in full_df.columns:
        return pd.DataFrame()
    df = full_df
    if "HasCorporateAction" in df.columns:
        df = df[~df["HasCorporateAction"].fillna(False)]
    g = df.groupby("Sector17CodeName").agg(
        AvgReturn=("DailyReturn", "mean"),
        CountUp=("DailyReturn", lambda x: (x > 0).sum()),
        CountDown=("DailyReturn", lambda x: (x < 0).sum()),
        CountTotal=("DailyReturn", "count"),
    ).reset_index()
    return g.sort_values("AvgReturn", ascending=False)


# ---------------------------------------------------------------------------
# Step 4: TDNet取得
# ---------------------------------------------------------------------------

def fetch_tdnet_atom(code4: str) -> tuple[list[dict], str]:
    url = _TDNET_ATOM_URL.format(code=code4)
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        return [], ""

    entries, company_name = [], ""
    for entry in root.findall("a:entry", _NS):
        def _text(tag):
            el = entry.find(tag, _NS)
            return el.text.strip() if el is not None and el.text else ""
        published = _text("a:issued") or _text("a:created") or _text("a:modified")
        link_el = entry.find("a:link[@rel='alternate']", _NS)
        link_href = link_el.get("href", "") if link_el is not None else ""
        pdf_url = ""
        if "rd.php?" in link_href:
            pdf_url = link_href.split("rd.php?", 1)[1]
        elif link_href.endswith(".pdf"):
            pdf_url = link_href
        raw_title = _text("a:title")
        if ":" in raw_title and not company_name:
            company_name = raw_title.split(":", 1)[0].strip()
        title = raw_title.split(":", 1)[1].strip() if ":" in raw_title else raw_title
        entries.append({"title": title, "published": published, "pdf_url": pdf_url, "pdf_text": ""})
    return entries, company_name


def filter_by_days(entries: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    result = []
    for e in entries:
        try:
            dt = datetime.fromisoformat(e["published"].replace("Z", "+00:00"))
            if dt >= cutoff:
                result.append(e)
        except Exception:
            result.append(e)
    return result


def fetch_pdf_text(pdf_url: str) -> str:
    if not pdf_url:
        return ""
    try:
        from io import BytesIO, StringIO
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        resp = requests.get(pdf_url, timeout=20)
        resp.raise_for_status()
        out = StringIO()
        extract_text_to_fp(BytesIO(resp.content), out, laparams=LAParams(), output_type="text", codec=None)
        text = re.sub(r"\n{3,}", "\n\n", out.getvalue()).strip()
        return text[:TDNET_PDF_MAX_CHARS]
    except Exception:
        return ""


def fetch_tdnet_batch(codes: list[str], no_pdf: bool = False) -> dict[str, dict]:
    result = {}
    total = len(codes)
    for i, code in enumerate(codes, 1):
        code4 = normalize_code_4(code)
        print(f"  TDNet [{i}/{total}] {code4}")
        entries, company_name = fetch_tdnet_atom(code4)
        entries = filter_by_days(entries, DEFAULT_TDNET_DAYS)
        if not no_pdf:
            for e in entries[:3]:
                if e["pdf_url"]:
                    e["pdf_text"] = fetch_pdf_text(e["pdf_url"])
                    time.sleep(REQUEST_SLEEP)
        result[code4] = {"entries": entries, "company_name": company_name}
        time.sleep(REQUEST_SLEEP)
    return result


# ---------------------------------------------------------------------------
# Step 5: みんかぶ ニューススクレイピング（Yahoo Finance Japan 代替）
# ---------------------------------------------------------------------------

def fetch_minkabu_news(code4: str, max_items: int = 8) -> list[dict]:
    from bs4 import BeautifulSoup
    url = f"https://minkabu.jp/stock/{code4}/news"
    try:
        r = requests.get(url, headers=_MINKABU_HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        items = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if not re.match(rf"/stock/{code4}/news/\d+", href):
                continue
            title = a.get_text(strip=True)
            if not title or len(title) < 10:
                continue
            if title in seen:
                continue
            seen.add(title)
            items.append({"title": title, "date": "", "source": "みんかぶ"})
            if len(items) >= max_items:
                break
        return items
    except Exception as e:
        print(f"  [WARN] {code4} minkabu news fetch failed: {e}")
        return []


_YAHOO_BBS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://finance.yahoo.co.jp/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_BBS_UI_NOISE = re.compile(
    r"^(JavaScript|ポートフォリオ|ログイン|VIP倶楽部|前のページ|"
    r"利用規約|免責事項|プライバシーポリシー|ヘルプ|お問い合わせ|"
    r"東京証券取引所|情報提供会社|JASRAC|最近見た銘柄)"
)

# Yahoo セッション cookie 共有用（最初の 1 回だけトップページ訪問してセッション構築）
_YAHOO_SESSION: requests.Session | None = None


def _get_yahoo_session() -> requests.Session:
    """Yahoo!ファイナンス用 requests.Session を構築・キャッシュ。

    GHA Ubuntu runner からの bot 判定回避のため、最初にトップページにアクセスして
    セッション cookie を取得し、それを以降のリクエストで使い回す。
    """
    global _YAHOO_SESSION
    if _YAHOO_SESSION is not None:
        return _YAHOO_SESSION
    s = requests.Session()
    s.headers.update(_YAHOO_BBS_HEADERS)
    try:
        s.get("https://finance.yahoo.co.jp/", timeout=15)
    except Exception as e:
        print(f"  [WARN] Yahoo top page session init failed: {e}")
    _YAHOO_SESSION = s
    return s


def _parse_yahoo_bbs_html(html: str, max_posts: int) -> dict:
    """Yahoo!ファイナンス掲示板 HTML を投稿リスト + sentiment にパース。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    sentiment = ""
    for el in soup.find_all(string=re.compile(r"強く買いたい.*%")):
        m = re.search(r"強く買いたい\s*([\d.]+)%.*?強く売りたい\s*([\d.]+)%", str(el))
        if m:
            sentiment = f"強く買いたい{m.group(1)}% / 強く売りたい{m.group(2)}%"
        break
    posts: list[dict] = []
    seen_keys: set[str] = set()
    for article in soup.find_all("article"):
        text = article.get_text(" ", strip=True)
        if not text or len(text) < 30:
            continue
        no_m = re.search(r"No\.\s*(\d+)", text)
        date_m = re.search(r"(\d{4}/\d+/\d+\s+\d+:\d+)", text)
        body_m = re.search(r"報告\s+(.+?)\s+(?:返信|投資の参考)", text)
        yes_m = re.search(r"はい\s+(\d+)", text)
        no_v_m = re.search(r"いいえ\s+(\d+)", text)
        body = body_m.group(1).strip() if body_m else text[:200]
        if len(body) < 10:
            continue
        key = (no_m.group(1) if no_m else body[:50])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        posts.append({
            "no": no_m.group(1) if no_m else "",
            "date": date_m.group(1) if date_m else "",
            "body": body,
            "yes": int(yes_m.group(1)) if yes_m else 0,
            "no_count": int(no_v_m.group(1)) if no_v_m else 0,
        })
        if len(posts) >= max_posts:
            break
    return {"sentiment": sentiment, "posts": posts}


def _fetch_yahoo_bbs_playwright(code4: str, max_posts: int) -> dict:
    """Playwright（headless Chromium）経由で Yahoo!ファイナンス掲示板を取得。

    PM 2026-05-26 確定: requests + Cookie セッション方式が GHA でも失敗する場合の
    最終手段。本物のブラウザフィンガープリントで bot 判定を回避。
    フォールバックは「Yahoo 内での取得手段切替」のみ・他サイトへの切替は禁止。
    """
    url = f"https://finance.yahoo.co.jp/quote/{code4}.T/forum"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"  [WARN] {code4} playwright not installed, skip")
        return {"sentiment": "", "posts": []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_YAHOO_BBS_HEADERS["User-Agent"],
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_selector("article", timeout=8000)
            except Exception:
                pass
            html = page.content()
            browser.close()
        return _parse_yahoo_bbs_html(html, max_posts)
    except Exception as e:
        print(f"  [WARN] {code4} playwright yahoo bbs fetch failed: {e}")
        return {"sentiment": "", "posts": []}


def fetch_yahoo_bbs(code4: str, max_posts: int = 30) -> dict:
    """Yahoo!ファイナンス掲示板から投稿を取得する。

    手順（PM 2026-05-26 確定・フォールバックは Yahoo 内手段切替のみ）：
    1. requests + Cookie セッション + Referer ヘッダで取得試行
    2. 投稿 0 件なら Playwright（headless Chromium）で再試行
    3. それでも 0 件なら空配列を返す（他サイトへのフォールバック禁止）

    HTML 構造（2026-05 時点）:
      <article> ユーザー名 No.XXXXX 日付 報告 本文 返信 投資の参考になりましたか？ はいN いいえN </article>
    """
    url = f"https://finance.yahoo.co.jp/quote/{code4}.T/forum"
    # Step 1: requests + Cookie セッション
    try:
        s = _get_yahoo_session()
        r = s.get(url, timeout=15)
        r.raise_for_status()
        result = _parse_yahoo_bbs_html(r.text, max_posts)
        if result["posts"]:
            return result
        print(f"  [INFO] {code4} requests yielded 0 posts (likely GHA blocked), retry with Playwright")
    except Exception as e:
        print(f"  [WARN] {code4} requests yahoo bbs fetch failed: {e}, retry with Playwright")
    # Step 2: Playwright フォールバック
    return _fetch_yahoo_bbs_playwright(code4, max_posts)


def fetch_yahoo_batch(codes: list[str]) -> dict[str, dict]:
    # EDINET DB クライアント（事業概要取得用）
    try:
        edinet_client = EdinetDBClient()
    except Exception as e:
        print(f"  [WARN] EDINET DB クライアント初期化失敗: {e}")
        edinet_client = None

    result = {}
    total = len(codes)
    for i, code in enumerate(codes, 1):
        code4 = normalize_code_4(code)
        print(f"  minkabu [{i}/{total}] {code4}")
        description = fetch_company_description(edinet_client, code4) if edinet_client else ""
        result[code4] = {
            "news":        fetch_minkabu_news(code4),
            "bbs":         fetch_yahoo_bbs(code4),
            "description": description,
        }
        time.sleep(REQUEST_SLEEP)
    return result


def fetch_company_description(client: EdinetDBClient, code4: str) -> str:
    """EDINET DB APIから事業内容を取得する。"""
    try:
        edinet_code = client.code_to_edinet(code4)
        if not edinet_code:
            return ""
        data = client.get_company(edinet_code)
        # get_company のレスポンスから事業概要を抽出（フィールド名候補を順に試す）
        for key in ["businessDescription", "businessSummary", "businessOverview", "description"]:
            v = data.get(key, "")
            if v:
                return str(v)[:300]
        # フォールバック: 業種名のみ返す
        industry = data.get("industryName") or data.get("industry", "")
        return str(industry) if industry else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Step 5b: research/ からDeep Dive・マクロコンテキストを読み込む
# ---------------------------------------------------------------------------

def load_research_context(code4: str, sector: str) -> str:
    """個別銘柄・セクター調査の過去Deep Diveを読み込んで返す。"""
    snippets = []

    # 個別銘柄 Deep Dive（最新2件）
    stock_dir = RESEARCH_DIR / "stocks"
    if stock_dir.exists():
        files = sorted(stock_dir.glob(f"{code4}_*.md"), reverse=True)[:2]
        for f in files:
            text = f.read_text(encoding="utf-8")[:1200]
            snippets.append(f"[過去Deep Dive: {f.stem}]\n{text}")

    # セクター調査（セクター名前方一致、最新1件）
    sector_dir = RESEARCH_DIR / "sectors"
    if sector_dir.exists() and sector and isinstance(sector, str):
        key = sector[:3]
        matches = sorted(
            [f for f in sector_dir.glob("*.md") if key in f.stem],
            reverse=True,
        )[:1]
        for f in matches:
            text = f.read_text(encoding="utf-8")[:800]
            snippets.append(f"[セクター調査: {f.stem}]\n{text}")

    return "\n\n".join(snippets)


# ---------------------------------------------------------------------------
# Step 6: Markdown生成ヘルパー
# ---------------------------------------------------------------------------

def _row_basic(row: pd.Series) -> str:
    code4   = normalize_code_4(row["Code"])
    _name   = row.get("CompanyName", code4)
    name    = code4 if pd.isna(_name) else _name
    ret     = row["DailyReturn"]
    close   = row["Close_T"]
    vol     = row.get("Volume_T", None)
    cap     = row.get("MarketCapOku", None)
    sector  = row.get("Sector17CodeName", "")
    market  = row.get("MarketCodeName", "")
    vol_str = f"{vol/1e4:.0f}万株" if pd.notna(vol) else "─"
    cap_str = f"{cap:.0f}億"       if pd.notna(cap) else "─"
    ret_str = f"{ret:+.1f}%" if pd.notna(ret) else "IPO初日"
    return (f"| {code4} | {name} | {market} | {ret_str} | {close:,.0f}円 "
            f"| {vol_str} | {cap_str} | {sector} |")


def _append_detail(
    lines: list[str],
    row: pd.Series,
    tdnet_data: dict,
    yahoo_data: dict,
    hist_df: pd.DataFrame | None = None,
) -> None:
    code4   = normalize_code_4(row["Code"])
    _name   = row.get("CompanyName", code4)
    name    = code4 if pd.isna(_name) else _name
    ret     = row["DailyReturn"]
    close   = row["Close_T"]
    sector  = row.get("Sector17CodeName", "")
    market  = row.get("MarketCodeName", "")
    cap     = row.get("MarketCapOku", None)
    vol     = row.get("Volume_T", None)
    cap_str = f"{cap:.0f}億" if pd.notna(cap) else "─"
    vol_str = f"{vol/1e4:.0f}万株" if pd.notna(vol) else "─"

    turnover_label = row.get("_turnover_label", "")
    turnover_str   = f"　売買代金: {turnover_label}" if turnover_label else ""

    # Yahoo 事業内容・Deep Dive
    yahoo       = yahoo_data.get(code4, {})
    description = yahoo.get("description", "")
    research    = load_research_context(code4, sector)

    ret_str = f"{ret:+.1f}%" if pd.notna(ret) else "IPO初日"
    lines += [
        f"### {code4} {name}　{ret_str}　[{market}]",
        f"",
        f"- 市場: {market}　セクター: {sector}　時価総額: {cap_str}",
        f"- 終値: {close:,.0f}円　出来高: {vol_str}{turnover_str}",
    ]
    if description:
        lines.append(f"- 事業: {description}")
    lines.append("")

    # PM 2026-05-22 確定: 需給（信用・株価水準）ブロックを必須セクションとして挿入
    # [prompts/_common_rules.md] [memory feedback_mover_supply_required.md]
    lines += build_supply_block(row, hist_df)

    # 過去Deep Dive
    if research:
        lines += ["**過去リサーチ:**", ""]
        lines.append(research)
        lines.append("")

    # TDNet
    tdnet   = tdnet_data.get(code4, {})
    entries = tdnet.get("entries", [])
    if entries:
        lines.append(f"**TDNet（直近{DEFAULT_TDNET_DAYS}日: {len(entries)}件）:**")
        lines.append("")
        for e in entries:
            lines.append(f"- {e['published'][:10]}　{e['title']}")
            if e.get("pdf_text"):
                lines.append(f"  > {e['pdf_text'][:600].replace(chr(10), ' ').strip()}")
        lines.append("")
    else:
        lines += [f"**TDNet（直近{DEFAULT_TDNET_DAYS}日）:** なし", ""]

    # みんかぶ ニュース
    news = yahoo.get("news", [])
    if news:
        lines.append(f"**みんかぶニュース（{len(news)}件）:**")
        lines.append("")
        for n in news:
            lines.append(f"- {n['title']}")
        lines.append("")
    else:
        lines += ["**みんかぶニュース:** なし", ""]

    # Yahoo 掲示板
    bbs       = yahoo.get("bbs", {})
    sentiment = bbs.get("sentiment", "")
    posts     = bbs.get("posts", [])
    if sentiment or posts:
        lines.append("**Yahoo掲示板:**")
        if sentiment:
            lines.append(f"- みんなの評価: {sentiment}")
        lines.append("")
        for p in posts:
            lines.append(f"> {p}")
        lines.append("")
    else:
        lines += ["**Yahoo掲示板:** なし", ""]

    # Deep Dive候補フラグ（TDNet・Yahooニュース両方なし）
    if not entries and not news:
        lines += ["**>> Deep Dive候補: 動意理由が不明のため優先調査を推奨**", ""]


# ---------------------------------------------------------------------------
# Step 6b: グロース スイング候補バリュエーション
# ---------------------------------------------------------------------------

def load_recent_mover_codes(today: date, n: int = 2) -> set[str]:
    """直近n回の完成レポートに登場した銘柄コード（4桁）を返す。"""
    movers_dir = MARKET_DAILY_DIR / "movers"
    if not movers_dir.exists():
        return set()
    files = sorted(
        [f for f in movers_dir.glob("????-??-??.md") if f.stem < today.isoformat()],
        reverse=True,
    )[:n]
    codes: set[str] = set()
    pattern = re.compile(r"^###\s+(\d{4})\s+")
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            m = pattern.match(line)
            if m:
                codes.add(m.group(1))
    return codes


def pick_valuation_candidates(
    full_df: pd.DataFrame,
    volume_df: pd.DataFrame,
    recent_codes: set[str],
    n: int = 3,
) -> list[pd.Series]:
    """
    グロース 値上がりn社・値下がりn社・売買代金n社を選ぶ。
    直近2回に登場済みの銘柄は除外し、1つ下の銘柄をとる。
    重複は除去して返す（同一銘柄が複数カテゴリに入る場合は最初の出現のみ）。
    """
    growth = full_df[full_df["MarketCodeName"] == MARKET_GROWTH].copy()
    # PM 2026-05-28: バリュエーション候補からも権利落ち銘柄を除外（値上がり/値下がり選定対象）
    if "HasCorporateAction" in growth.columns:
        growth = growth[~growth["HasCorporateAction"].fillna(False)]

    def pick(df: pd.DataFrame, ascending: bool, key: str) -> list[str]:
        sorted_df = df.sort_values(key, ascending=ascending)
        picked: list[str] = []
        for code in sorted_df["Code"].astype(str).str[:4]:
            if code not in recent_codes:
                picked.append(code)
            if len(picked) >= n:
                break
        return picked

    growth["Turnover"] = growth["Close_T"] * growth["Volume_T"]
    top_codes     = pick(growth, False, "DailyReturn")
    bottom_codes  = pick(growth, True,  "DailyReturn")
    turnover_codes = pick(growth, False, "Turnover")

    seen: set[str] = set()
    result: list[pd.Series] = []
    for code in top_codes + bottom_codes + turnover_codes:
        if code in seen:
            continue
        seen.add(code)
        rows = growth[growth["Code"].astype(str).str[:4] == code]
        if not rows.empty:
            result.append(rows.iloc[0])
    return result


def fetch_valuation_block(code4: str, close: float) -> str:
    """
    EDINET DB から financials + earnings を取得し、バリュエーションブロック文字列を返す。
    取得失敗時は空文字を返す（レポート生成を止めない）。
    """
    try:
        client = EdinetDBClient()
        edinet_code = client.code_to_edinet(code4)
        if not edinet_code:
            return ""

        financials = client.get_financials(edinet_code, years=5)
        earnings   = client.get_earnings(edinet_code, limit=3)

        # --- PER遍歴（直近3期） ---
        per_lines: list[str] = []
        for rec in sorted(financials, key=lambda r: r.get("fiscalYear", 0))[-3:]:
            fy  = rec.get("fiscalYear", "?")
            eps = rec.get("eps") or rec.get("adjustedEps")
            if eps and eps > 0 and close:
                per_val = close / eps
                per_lines.append(f"FY{fy} {per_val:.1f}倍")
            else:
                per_lines.append(f"FY{fy} N/A（赤字）")

        # 最新期の予想EPS（earnings から取得）
        forecast_per = ""
        for e in earnings:
            feps = e.get("forecastEps")
            if feps and feps > 0 and close:
                forecast_per = f"予想PER {close / feps:.1f}倍（会社予想EPS {feps:.2f}円）"
                break

        # --- 株価水準（BPS・52週高安は financials から） ---
        latest = sorted(financials, key=lambda r: r.get("fiscalYear", 0))[-1] if financials else {}
        bps = latest.get("bps") or latest.get("adjustedBps")
        pbr_str = f"PBR {close / bps:.2f}倍（BPS {bps:.0f}円）" if bps and bps > 0 else "PBR 算出不可"

        high = latest.get("highestSharePrice")
        low  = latest.get("lowestSharePrice")
        range_str = ""
        if high and low:
            pct = (close - low) / (high - low) * 100 if high != low else 0
            range_str = f"年間レンジ {low:.0f}〜{high:.0f}円（現在 {pct:.0f}%水準）"

        # --- 自己資本比率 ---
        eq_ratio = latest.get("equityRatioOfficial")
        eq_str = f"自己資本比率 {eq_ratio*100:.1f}%" if eq_ratio else ""

        # --- 組み立て ---
        lines = ["**バリュエーション**:"]
        lines.append(f"- 株価水準: {pbr_str}" + (f"　{range_str}" if range_str else ""))
        if per_lines:
            lines.append(f"- PER推移: {' → '.join(per_lines)}")
        if forecast_per:
            lines.append(f"- {forecast_per}")
        if eq_str:
            lines.append(f"- {eq_str}")
        return "\n".join(lines)

    except Exception as e:
        return f"**バリュエーション**: 取得失敗（{e}）"


# ---------------------------------------------------------------------------
# Step 7: レポート全体組み立て
# ---------------------------------------------------------------------------

def build_report(
    full_df: pd.DataFrame,
    detail_df: pd.DataFrame,
    all_movers_df: pd.DataFrame,
    volume_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    tdnet_data: dict,
    yahoo_data: dict,
    macro_snippet: str,
    today: date,
    prev: date,
    hist_df: pd.DataFrame | None = None,
) -> str:
    lines = [
        f"# 動意銘柄レポート 生データ ({today.strftime('%Y-%m-%d')})",
        f"",
        f"> JQuants + TDNet + Yahoo Finance から自動取得。Claude が「なぜ動いたか」を推論してレポートを生成する。",
        f"- **生成日時**: {datetime.now().strftime('%Y-%m-%d %H:%M')} JST",
        f"- **価格比較**: {prev} → {today}（前営業日比）",
        f"- **TDNet対象期間**: 直近{DEFAULT_TDNET_DAYS}日",
        f"- **推定トークン数**: （レポート末尾参照）",
        f"",
    ]

    # ---- Layer 1: セクター別フロー ----
    lines += ["## Layer 1: セクター別フロー", ""]
    if not sector_df.empty:
        lines += [
            "| セクター | 平均リターン | 上昇 | 下落 | 合計 |",
            "|---------|-------------|------|------|------|",
        ]
        for _, r in sector_df.iterrows():
            lines.append(
                f"| {r['Sector17CodeName']} | {r['AvgReturn']:+.2f}% "
                f"| {int(r['CountUp'])} | {int(r['CountDown'])} | {int(r['CountTotal'])} |"
            )
        lines.append("")
    else:
        lines += ["（セクターデータなし）", ""]

    # ---- Layer 2: 全動意銘柄リスト（アーカイブ・通常は出力しない） ----
    # 必要な時は以下のコメントを外す。トークン節約のため通常はスキップ。
    # lines += [f"## Layer 2: 全動意銘柄リスト（上位{len(all_movers_df)}銘柄・絶対値順）", ""]
    # lines += [
    #     "| コード | 銘柄名 | 市場 | リターン | 終値 | 出来高 | 時価総額 | セクター |",
    #     "|--------|--------|------|---------|------|--------|---------|---------|",
    # ]
    # for _, row in all_movers_df.iterrows():
    #     lines.append(_row_basic(row))
    # lines.append("")

    # ---- 市場別まとめ（値動き + 売買代金を市場ごとにセット） ----
    for market in [MARKET_PRIME, MARKET_STANDARD, MARKET_GROWTH]:
        lines += [f"## {market}", ""]

        # 値動き
        cfg = DETAIL_CONFIG[market]
        mdf = detail_df[detail_df["_market"] == market]
        top_df    = mdf[mdf["_direction"] == "up"].sort_values("DailyReturn", ascending=False)
        bottom_df = mdf[mdf["_direction"] == "down"].sort_values("DailyReturn")

        lines += [f"### 値上がり Top {cfg['top']}", ""]
        for _, row in top_df.iterrows():
            _append_detail(lines, row, tdnet_data, yahoo_data, hist_df=hist_df)

        lines += [f"### 値下がり Bottom {cfg['bottom']}", ""]
        for _, row in bottom_df.iterrows():
            _append_detail(lines, row, tdnet_data, yahoo_data, hist_df=hist_df)

        # 売買代金
        n = TURNOVER_CONFIG[market]
        vdf = volume_df[volume_df["_market"] == market].sort_values("Turnover", ascending=False)
        lines += [f"### 売買代金 Top {n}", ""]
        for _, row in vdf.iterrows():
            turnover_oku = row.get("Turnover", 0) / 1e8
            row = row.copy()
            row["_turnover_label"] = f"{turnover_oku:.0f}億円"
            _append_detail(lines, row, tdnet_data, yahoo_data, hist_df=hist_df)

    # ---- Layer 5: グロース スイング候補バリュエーション ----
    recent_codes = load_recent_mover_codes(today)
    candidates   = pick_valuation_candidates(full_df, volume_df, recent_codes, n=3)
    lines += ["## Layer 5: グロース スイング候補バリュエーション", ""]
    lines += [
        "> 値上がり3社・値下がり3社・売買代金3社（直近2回登場済み除外）にEDINETバリュエーション付与。",
        "> Claude はこのセクションを参照し、各銘柄の **バリュエーション** フィールドをレポートに追記すること。",
        "",
    ]
    if candidates:
        for row in candidates:
            code4 = normalize_code_4(row["Code"])
            _name = row.get("CompanyName", code4)
            name  = code4 if pd.isna(_name) else _name
            ret   = row["DailyReturn"]
            close = row["Close_T"]
            lines.append(f"### {code4} {name}　{ret:+.1f}%　（終値 {close:,.0f}円）")
            lines.append("")
            val_block = fetch_valuation_block(code4, close)
            lines.append(val_block if val_block else "**バリュエーション**: データなし")
            lines.append("")
    else:
        lines += ["（候補銘柄なし）", ""]

    # ---- マクロコンテキスト ----
    if macro_snippet:
        lines += [
            "## マクロコンテキスト（直近レポート冒頭）",
            "",
            "```",
            macro_snippet,
            "```",
            "",
        ]

    # ---- トークン数（末尾に追記） ----
    body = "\n".join(lines)
    tokens = estimate_tokens(body)
    chars  = len(body)
    lines += [
        "---",
        "## トークン使用量",
        "",
        f"| 項目 | 値 |",
        f"|------|-----|",
        f"| 推定トークン数 | {tokens:,} |",
        f"| 文字数 | {chars:,} |",
        f"| 計測方式 | {'tiktoken (cl100k_base)' if _has_tiktoken() else '文字数÷3（推定）'} |",
        "",
    ]
    # ---- Deep Research 必須出力指示 ----
    lines += [
        "---",
        "## ⚠️ Claude への必須出力ルール（Deep Research 候補）",
        "",
        "このファイルを読んで動意銘柄レポートを分析・生成する場合、**レポートの最後に必ず以下のセクションを出力すること。省略禁止。**",
        "",
        "出力すべき条件：",
        "- TDNet・Yahoo情報だけでは動意理由の説明が不十分な銘柄",
        "- 情報はあるが、投資判断に使えるレベルの解像度に達していない論点",
        "- 急騰・急落の背景にもっと掘り下げるべき業界トレンド・マクロ要因がある",
        "- 売買代金が突出しているが理由が不明確な銘柄",
        "- 投資チャンスとして面白そうだが根拠を深掘りしたいテーマ",
        "",
        "出力フォーマット（チェックボックス形式で3〜7件）：",
        "",
        "```",
        "## 📌 Deep Research 候補",
        "",
        "- [ ] 〇〇（証券コード）について（理由: △△が不明確なため）",
        "- [ ] 〇〇セクターの動向について（理由: △△銘柄複数が動いており背景を確認したい）",
        "- [ ] 〇〇について（理由: 投資チャンスとして△△の観点で深掘りしたい）",
        "```",
        "",
        "候補が全くない場合も「Deep Research 候補なし（本日は全銘柄の動意理由が明確）」と必ず明記すること。",
        "",
    ]

    # ヘッダーの placeholder を実数値に置換
    result = "\n".join(lines).replace(
        "- **推定トークン数**: （レポート末尾参照）",
        f"- **推定トークン数**: {tokens:,} tokens（{chars:,} 文字）",
    )
    return result


def _has_tiktoken() -> bool:
    try:
        import tiktoken  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def load_latest_macro_report() -> str:
    """research/markets/ から最新マクロレポートを読み込む。"""
    markets_dir = RESEARCH_DIR / "markets"
    if not markets_dir.exists():
        return ""
    files = sorted(markets_dir.glob("*.md"), reverse=True)
    if not files:
        return ""
    return files[0].read_text(encoding="utf-8")[:2000]


def estimate_tokens(text: str) -> int:
    """テキストの推定トークン数を返す。tiktokenがあれば正確に、なければ文字数÷3で推定。"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return len(text) // 3


TOKEN_LOG_PATH = OUTPUTS_DIR / "token_usage_log.csv"


def log_token_usage(today: date, script: str, tokens: int, chars: int) -> None:
    """bi/outputs/token_usage_log.csv にトークン使用量を追記する。"""
    import csv
    TOKEN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not TOKEN_LOG_PATH.exists()
    with TOKEN_LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["date", "script", "estimated_tokens", "chars"])
        w.writerow([today.isoformat(), script, tokens, chars])


def load_sector_weekly_context() -> dict[str, float]:
    if not SECTOR_AGG_PATH.exists():
        return {}
    try:
        df = pd.read_parquet(SECTOR_AGG_PATH)
        if "Sector17CodeName" in df.columns and "Return_W01" in df.columns:
            return dict(zip(df["Sector17CodeName"], df["Return_W01"]))
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    parser = argparse.ArgumentParser(description="動意銘柄レポート 生データ生成")
    parser.add_argument("--date",   default=None, help="対象日 YYYY-MM-DD（省略時は本日）")
    parser.add_argument("--no-pdf", action="store_true", help="TDNet PDF本文取得スキップ")
    args = parser.parse_args()

    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JQUANTS_API_KEY が未設定です")

    import jquantsapi
    client = jquantsapi.ClientV2(api_key=api_key)

    target = date.fromisoformat(args.date) if args.date else date.today()
    print(f"対象日: {target}")

    # --- 価格取得 ---
    print("直近2営業日を確認中...")
    today_dt, prev_dt = resolve_trading_days(client, target)
    print(f"  本日: {today_dt}　前日: {prev_dt}")

    print(f"価格取得: {today_dt} ...")
    today_df = fetch_daily_all(client, today_dt)
    print(f"  {len(today_df)} 銘柄")

    print(f"価格取得: {prev_dt} ...")
    prev_df = fetch_daily_all(client, prev_dt)
    print(f"  {len(prev_df)} 銘柄")

    # --- screening_master ---
    master_df = pd.read_parquet(SCREENING_MASTER_PATH)
    master_df["Code"] = master_df["Code"].astype(str).str[:4]
    print(f"screening_master: {len(master_df)} 銘柄")

    # --- 全銘柄テーブル ---
    print("リターン計算中...")
    full_df = build_full_table(today_df, prev_df, master_df)
    print(f"  有効銘柄: {len(full_df)}")

    # 市場区分別概況
    for market in [MARKET_PRIME, MARKET_STANDARD, MARKET_GROWTH]:
        mdf = full_df[full_df["MarketCodeName"] == market]
        if not mdf.empty:
            top = mdf["DailyReturn"].max()
            bot = mdf["DailyReturn"].min()
            print(f"  {market}: {len(mdf)}銘柄 最大{top:+.1f}% 最小{bot:+.1f}%")

    # --- 各レイヤー抽出 ---
    detail_df     = extract_detail_stocks(full_df)
    all_movers_df = extract_all_movers(full_df)
    volume_df     = extract_turnover_ranking(full_df)
    sector_df     = extract_sector_flow(full_df)

    total_detail = len(detail_df["Code"].unique())
    print(f"注目銘柄（TDNet+Yahoo対象）: {total_detail} 銘柄")

    # --- TDNet/Yahoo取得対象: 注目銘柄 + 出来高ランキング（重複除去） ---
    detail_codes = detail_df["Code"].astype(str).str[:4].unique().tolist()
    volume_codes = volume_df["Code"].astype(str).str[:4].unique().tolist()
    fetch_codes  = list(dict.fromkeys(detail_codes + volume_codes))  # 順序保持・重複除去
    print(f"TDNet+Yahoo対象（出来高含む）: {len(fetch_codes)} 銘柄")

    # --- TDNet取得 ---
    print("TDNet取得中...")
    tdnet_data = fetch_tdnet_batch(fetch_codes, no_pdf=args.no_pdf)

    # --- Yahoo取得 ---
    print("Yahoo Finance 取得中...")
    yahoo_data = fetch_yahoo_batch(fetch_codes)

    # --- 60営業日 OHLC 履歴（株価水準ブロック用・PM 2026-05-22 確定） ---
    print(f"60営業日 OHLC 履歴取得中（対象 {len(fetch_codes)} 銘柄）...")
    hist_df = fetch_ohlc_history(client, set(fetch_codes), today_dt, n_days=60)
    print(f"  履歴: {len(hist_df)} 行（{hist_df['Code'].nunique() if not hist_df.empty else 0} 銘柄カバー）")

    # --- マクロレポート ---
    macro_snippet = load_latest_macro_report()

    # --- レポート生成 ---
    report_md = build_report(
        full_df=full_df,
        detail_df=detail_df,
        all_movers_df=all_movers_df,
        volume_df=volume_df,
        sector_df=sector_df,
        tdnet_data=tdnet_data,
        yahoo_data=yahoo_data,
        macro_snippet=macro_snippet,
        today=today_dt,
        prev=prev_dt,
        hist_df=hist_df,
    )

    MARKET_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MARKET_DAILY_DIR / f"{today_dt}_movers_raw.md"
    out_path.write_text(report_md, encoding="utf-8")

    tokens = estimate_tokens(report_md)
    log_token_usage(today_dt, "make_mover_report", tokens, len(report_md))
    print(f"\n出力: {out_path}")
    print(f"推定トークン数: {tokens:,} ({len(report_md):,} 文字)")


if __name__ == "__main__":
    main()
