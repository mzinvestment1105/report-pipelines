"""
セクター週次 生データ生成
=========================
sector_weekly.parquet + 過去 research（markets / sectors）を束ねた
生データ Markdown を出力する。

Claude Code がこのファイルを読んで _sector_analysis.md を生成する。
Deep Research の結果を --deep-research-file で渡すと、
定量データ・過去文脈と一緒に同梱され、Claude が統合分析できる。

出力:
  market/daily/YYYY-MM-DD_sector_raw.md

使い方:
  cd bi/pipelines
  python make_sector_raw.py
  python make_sector_raw.py --date 2026-04-11
  python make_sector_raw.py --deep-research-file dr.txt
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
BASE_DIR        = Path(__file__).resolve().parent
OUTPUTS_DIR     = BASE_DIR / ".." / "outputs"
MARKET_DIR      = BASE_DIR / ".." / ".." / "market" / "daily"
RESEARCH_DIR    = BASE_DIR / ".." / ".." / "research"
SECTOR_PATH             = OUTPUTS_DIR / "sector_weekly.parquet"
SCREENING_MASTER_PATH   = OUTPUTS_DIR / "screening_master.parquet"

# 営業日ベースの陳腐化判定は全レポート共通ロジックを共有（カレンダー日数では判定しない）
import sys  # noqa: E402
sys.path.insert(0, str(BASE_DIR))
from lib.snapshot_utils import business_days_after, is_stale_close  # noqa: E402

JST = timezone(timedelta(hours=9))

RESEARCH_MARKETS_KEEP = 3   # 直近何件のマクロレポートを参照するか
RESEARCH_SECTORS_KEEP = 3   # 直近何件のセクターレポートを参照するか


# ---------------------------------------------------------------------------
# 入力鮮度チェック
# ---------------------------------------------------------------------------

def _is_stale_sector_parquet(path: Path, target_date: date) -> bool:
    """sector_weekly.parquet の鮮度判定（ファイル未存在 or mtime日付不一致なら stale）"""
    if not path.exists():
        return True
    mtime_jst = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone(JST)
    return mtime_jst.date() != target_date


def _refresh_sector_weekly(anchor: str = "friday") -> None:
    """
    セクター集計 parquet を再生成する。

    anchor: "friday"（直近金曜終値ベース・デフォルト） / "today"（実行日終値ベース）
    """
    script_dir = Path(__file__).resolve().parent
    print(f"[INFO] セクター集計データを更新します（make_sector_report.py, anchor={anchor}）")
    subprocess.run(
        [sys.executable, "make_sector_report.py", "--anchor", anchor],
        cwd=script_dir,
        check=True,
    )


# ---------------------------------------------------------------------------
# 定量サマリー整形
# ---------------------------------------------------------------------------

def _pct(val: float, digits: int = 1) -> str:
    if pd.isna(val):
        return "N/A"
    return f"{val * 100:+.{digits}f}%"


def _fmt_val(val: float, digits: int = 1) -> str:
    if pd.isna(val):
        return "N/A"
    return f"{val:.{digits}f}x"


def build_sector_table_md(df: pd.DataFrame) -> str:
    """セクター一覧テーブル（週次リターン + バリュエーション）"""
    df = df.copy().sort_values("Return_W01", ascending=False)

    lines = [
        "## セクター週次パフォーマンス一覧",
        "",
        "| セクター | 銘柄数 | W01 | W02 | W03 | W04 | 3M | 1Y | PER | PBR | ROE | 出来高変化 | MA25乖離 |",
        "|----------|--------|-----|-----|-----|-----|----|----|-----|-----|-----|------------|---------|",
    ]
    for _, r in df.iterrows():
        lines.append(
            f"| {r['Sector17CodeName']} "
            f"| {int(r['StockCount']) if pd.notna(r['StockCount']) else '-'} "
            f"| {_pct(r.get('Return_W01'))} "
            f"| {_pct(r.get('Return_W02'))} "
            f"| {_pct(r.get('Return_W03'))} "
            f"| {_pct(r.get('Return_W04'))} "
            f"| {_pct(r.get('Return_3M'))} "
            f"| {_pct(r.get('Return_1Y'))} "
            f"| {_fmt_val(r.get('PER_WAvg'))} "
            f"| {_fmt_val(r.get('PBR_WAvg'))} "
            f"| {_pct(r.get('ROE_WAvg'))} "
            f"| {_pct(r.get('Volume_Change_WAvg'))} "
            f"| {_pct(r.get('MA25_Deviation_WAvg'))} |"
        )
    return "\n".join(lines)


def build_top_bottom_md(df: pd.DataFrame) -> str:
    """週次上位/下位セクター + 代表銘柄"""
    df = df.copy().sort_values("Return_W01", ascending=False)

    lines = ["## 週次上位・下位セクター"]
    lines.append("")
    lines.append("### 上位 5 セクター（週次リターン）")
    for _, r in df.head(5).iterrows():
        top3 = r.get("Top3_Return_1M", "")
        lines.append(f"- **{r['Sector17CodeName']}** {_pct(r.get('Return_W01'))}　代表銘柄: {top3}")

    lines.append("")
    lines.append("### 下位 5 セクター（週次リターン）")
    for _, r in df.tail(5).iterrows():
        bot3 = r.get("Bottom3_Return_1M", "")
        lines.append(f"- **{r['Sector17CodeName']}** {_pct(r.get('Return_W01'))}　代表銘柄: {bot3}")

    return "\n".join(lines)


def build_mcap_md(df: pd.DataFrame) -> str:
    """時価総額上位銘柄"""
    lines = ["## セクター別 時価総額上位銘柄"]
    df_sorted = df.sort_values("MarketCap_Total", ascending=False)
    for _, r in df_sorted.head(10).iterrows():
        mcap_t = r.get("MarketCap_Total", 0)
        mcap_str = f"{mcap_t / 1e12:.1f}兆円" if mcap_t >= 1e12 else f"{mcap_t / 1e8:.0f}億円"
        top3 = r.get("Top3_MarketCap", "")
        lines.append(f"- **{r['Sector17CodeName']}** 時価総額合計 {mcap_str}　上位: {top3}")
    return "\n".join(lines)


def build_top_sector_profiles(
    df: pd.DataFrame,
    screening_master_path: Path,
    top_n_sectors: int = 5,
    top_n_stocks: int = 5,
    profile_dir: Path | None = None,
) -> str:
    """
    週次上位 top_n_sectors セクターの時価総額上位 top_n_stocks 銘柄について
    EDINET DB からプロフィールを取得してMarkdownを返す。
    profile_dir が指定されていれば research/stocks/{コード}/profile.md にも保存する。
    """
    try:
        from edinetdb_client import EdinetDBClient
        client = EdinetDBClient()
    except Exception as e:
        return f"## 主要銘柄プロフィール\n\n（EDINET DB 初期化失敗: {e}）"

    # screening_master から銘柄ごとの時価総額・市場・セクターを取得
    try:
        master = pd.read_parquet(screening_master_path)
        master["Code"] = master["Code"].astype(str).str[:4]
    except Exception as e:
        return f"## 主要銘柄プロフィール\n\n（screening_master 読み込み失敗: {e}）"

    top_sectors = (
        df.sort_values("Return_W01", ascending=False)
        .head(top_n_sectors)["Sector17CodeName"]
        .tolist()
    )

    lines = [
        "## 主要銘柄プロフィール（週次上位セクター）",
        "",
        f"> 週次上位{top_n_sectors}セクターの時価総額上位{top_n_stocks}社。EDINET DBから自動取得。",
        "> Claudeはこのセクションを参照し、セクター深掘りに各銘柄の事業・財務概要を追記すること。",
        "",
    ]

    for sector_name in top_sectors:
        sector_stocks = master[master.get("Sector17CodeName", master.columns[0]) == sector_name] if "Sector17CodeName" in master.columns else pd.DataFrame()
        if sector_stocks.empty:
            # カラム名の揺れに対応
            for col in master.columns:
                if "sector" in col.lower() or "セクター" in col:
                    sector_stocks = master[master[col] == sector_name]
                    if not sector_stocks.empty:
                        break

        if sector_stocks.empty:
            lines += [f"### {sector_name}", "", "（銘柄データなし）", ""]
            continue

        # 時価総額上位 top_n_stocks
        if "MarketCap" in sector_stocks.columns:
            sector_stocks = sector_stocks.copy()
            sector_stocks["MarketCap"] = pd.to_numeric(sector_stocks["MarketCap"], errors="coerce")
            top_stocks = sector_stocks.nlargest(top_n_stocks, "MarketCap")
        else:
            top_stocks = sector_stocks.head(top_n_stocks)

        lines += [f"### {sector_name}", ""]

        for _, row in top_stocks.iterrows():
            code4 = str(row["Code"])[:4]
            name  = row.get("CompanyName", code4)
            mcap  = row.get("MarketCap", None)
            mcap_str = f"{float(mcap)/1e8:.0f}億円" if mcap and pd.notna(mcap) else "─"

            profile_text = _fetch_stock_profile(client, code4, name, mcap_str)
            lines.append(profile_text)
            lines.append("")

            # profile.md に保存
            if profile_dir:
                _save_profile(profile_dir, code4, name, sector_name, profile_text)

    return "\n".join(lines)


def _fetch_stock_profile(client, code4: str, name: str, mcap_str: str) -> str:
    """1銘柄のプロフィールブロックを返す。"""
    try:
        edinet_code = client.code_to_edinet(code4)
        if not edinet_code:
            return f"#### {code4} {name}（時価総額 {mcap_str}）\n- EDINETコード取得不可"

        company  = client.get_company(edinet_code)
        fins     = client.get_financials(edinet_code, years=3)
        analysis = client.get_analysis(edinet_code)

        # 事業概要
        description = ""
        for key in ["businessDescription", "businessSummary", "businessOverview", "description"]:
            v = company.get(key, "")
            if v:
                description = str(v)[:200]
                break
        if not description:
            description = company.get("industryName", "")

        # 最新財務
        latest = sorted(fins, key=lambda r: r.get("fiscalYear", 0))[-1] if fins else {}
        revenue     = latest.get("revenue")
        op_income   = latest.get("operatingIncome")
        eq_ratio    = latest.get("equityRatioOfficial")
        bps         = latest.get("bps") or latest.get("adjustedBps")
        roe         = latest.get("roeOfficial")

        rev_str = f"{revenue/1e8:.0f}億円" if revenue else "─"
        op_str  = f"{op_income/1e8:.0f}億円" if op_income else "─"
        eq_str  = f"{eq_ratio*100:.1f}%" if eq_ratio else "─"
        roe_str = f"{roe*100:.1f}%" if roe else "─"
        bps_str = f"{bps:.0f}円" if bps else "─"

        # 健全性スコア
        score = analysis.get("credit", {}).get("score", "─")

        block = (
            f"#### {code4} {name}（時価総額 {mcap_str}）\n"
            f"- **事業**: {description}\n"
            f"- **規模**: 売上 {rev_str} / 営業利益 {op_str}\n"
            f"- **財務**: 自己資本比率 {eq_str} / ROE {roe_str} / BPS {bps_str}\n"
            f"- **健全性スコア**: {score}/100"
        )
        return block

    except Exception as e:
        return f"#### {code4} {name}（時価総額 {mcap_str}）\n- 取得失敗: {e}"


def _save_profile(profile_dir: Path, code4: str, name: str, sector: str, profile_text: str) -> None:
    """research/stocks/{コード}/profile.md を作成・更新する。"""
    try:
        stock_dir = profile_dir / code4
        stock_dir.mkdir(parents=True, exist_ok=True)
        path = stock_dir / "profile.md"
        now  = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        content = (
            f"# {code4} {name}\n\n"
            f"- **セクター**: {sector}\n"
            f"- **最終更新**: {now}\n\n"
            f"{profile_text}\n"
        )
        path.write_text(content, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 過去 research 読み込み
# ---------------------------------------------------------------------------

def load_past_research(research_dir: Path) -> str:
    sections: list[str] = []

    # マクロレポート（直近 N 件）
    markets_dir = research_dir / "markets"
    if markets_dir.exists():
        files = sorted(markets_dir.glob("*.md"), key=lambda f: f.stem, reverse=True)
        for f in files[:RESEARCH_MARKETS_KEEP]:
            text = f.read_text(encoding="utf-8")
            sections.append(f"### マクロレポート: {f.stem}\n\n{text}")

    # セクターレポート（直近 N 件）
    sectors_dir = research_dir / "sectors"
    if sectors_dir.exists():
        files = sorted(sectors_dir.glob("*.md"), key=lambda f: f.stem, reverse=True)
        for f in files[:RESEARCH_SECTORS_KEEP]:
            text = f.read_text(encoding="utf-8")
            sections.append(f"### セクターレポート: {f.stem}\n\n{text}")

    if not sections:
        return "（参照できる過去レポートなし）"

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Deep Research プロンプト生成
# ---------------------------------------------------------------------------

DEEP_RESEARCH_PROMPT_TEMPLATE = """\
# 日本株セクター週次 定性分析依頼（{as_of}）

以下の定量データをもとに、今週の日本株セクター動向を分析してください。

## 分析観点

1. **今週の強弱要因**：各セクターの騰落を決定づけたマクロ・産業ニュースは何か
2. **上位セクターの持続性**：上昇は継続するか、それとも短期的な材料反応か
3. **下位セクターの逆張り余地**：下落セクターに買い場はあるか
4. **来週以降の注目点**：決算・政策発表・イベントで影響が大きいものは何か

## 定量データ（週次リターン順）

| セクター | W01 | 1Y | PER | PBR |
|----------|-----|----|-----|-----|
{quantitative_summary}

## 出力フォーマット（必ず守ること）

- Markdownで出力する
- 各観点を `##` 見出しで区切る
- セクター名を **太字** で明示する
- 根拠となるニュース・データがあれば出典を添える
- 日本語で出力する
"""


def build_deep_research_prompt(df: pd.DataFrame, as_of: str) -> str:
    summary_lines = []
    df_sorted = df.sort_values("Return_W01", ascending=False)
    for _, r in df_sorted.iterrows():
        summary_lines.append(
            f"| {r['Sector17CodeName']} "
            f"| {_pct(r.get('Return_W01'))} "
            f"| {_pct(r.get('Return_1Y'))} "
            f"| {_fmt_val(r.get('PER_WAvg'))} "
            f"| {_fmt_val(r.get('PBR_WAvg'))} |"
        )
    quantitative_summary = "\n".join(summary_lines)
    return DEEP_RESEARCH_PROMPT_TEMPLATE.format(
        as_of=as_of,
        quantitative_summary=quantitative_summary,
    )


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--no-ensure-fresh", action="store_true", help="sector_weekly.parquet の鮮度チェックと自動更新を行わない")
    parser.add_argument(
        "--deep-research-file",
        default=None,
        help="Deep Research の結果テキストファイルパス（省略時はプロンプトのみ出力）",
    )
    parser.add_argument(
        "--allow-no-deep-research",
        action="store_true",
        help="緊急時のみ Deep Research なしで raw を出力する（通常運用では使用しない）",
    )
    parser.add_argument(
        "--anchor",
        choices=["friday", "today"],
        default="friday",
        help="W01 の起算日: 'friday'（直近金曜・週末実行向け・デフォルト） / 'today'（実行日・平日実行で当日まで含めたい場合）",
    )
    args = parser.parse_args()
    target_date = args.date
    target_date_obj = date.fromisoformat(target_date)

    if not args.no_ensure_fresh:
        if target_date_obj == datetime.now(JST).date():
            if _is_stale_sector_parquet(SECTOR_PATH, target_date_obj):
                try:
                    _refresh_sector_weekly(anchor=args.anchor)
                except subprocess.CalledProcessError as e:
                    raise RuntimeError(f"sector_weekly.parquet の更新に失敗しました: {e}") from e
        else:
            if not SECTOR_PATH.exists():
                raise FileNotFoundError(
                    f"sector_weekly.parquet が見つかりません: {SECTOR_PATH}（過去日付は事前にデータを用意してください）"
                )

    if not SECTOR_PATH.exists():
        raise FileNotFoundError(f"sector_weekly.parquet が見つかりません: {SECTOR_PATH}")

    df = pd.read_parquet(SECTOR_PATH)
    as_of = df["AsOf"].iloc[0] if "AsOf" in df.columns else target_date
    print(f"セクターデータ読み込み: {len(df)} セクター (AsOf: {as_of})")

    # 価格マスター鮮度（PM 2026-06-27）: 実行日(as_of)とは別に「実際の価格データ最新日」を
    # ヘッダーに明記し、古い場合は ⚠️ 警告を出してレポートに stale を可視化する。
    price_data_asof = df["PriceDataAsOf"].iloc[0] if "PriceDataAsOf" in df.columns else None
    price_freshness_line = f"- **価格データ最新日**: {price_data_asof}" if price_data_asof else ""
    price_stale_note = ""
    if price_data_asof:
        try:
            _pdate = date.fromisoformat(str(price_data_asof)[:10])
            _rdate = date.fromisoformat(str(as_of)[:10])
            if is_stale_close(_pdate, _rdate):
                _gap = business_days_after(_pdate, _rdate)
                price_stale_note = (
                    f"- ⚠️ **価格データ鮮度警告**: 価格マスターが営業日基準で陳腐化（最新 {price_data_asof}・"
                    f"約 {_gap} 営業日前）。週次リターン等は古い終値ベースの可能性があるため要確認。"
                )
        except ValueError:
            pass

    # 定量セクション
    sector_table = build_sector_table_md(df)
    top_bottom    = build_top_bottom_md(df)
    mcap_section  = build_mcap_md(df)

    # 主要銘柄プロフィール（上位5セクター × 時価総額上位5社）
    profile_dir = RESEARCH_DIR / "stocks"
    print("主要銘柄プロフィール取得中（EDINET DB）...")
    profiles_section = build_top_sector_profiles(
        df,
        screening_master_path=SCREENING_MASTER_PATH,
        top_n_sectors=5,
        top_n_stocks=5,
        profile_dir=profile_dir,
    )
    print("  完了")

    # Deep Research
    dr_prompt = build_deep_research_prompt(df, as_of)
    print("\n" + "=" * 60)
    print("【Deep Research 用プロンプト】以下を外部ツールに貼ってください")
    print("=" * 60)
    print(dr_prompt)
    print("=" * 60 + "\n")

    dr_content = ""
    if args.deep_research_file:
        dr_path = Path(args.deep_research_file)
        if dr_path.exists():
            dr_content = dr_path.read_text(encoding="utf-8")
            print(f"Deep Research 結果を読み込みました: {dr_path}")
        else:
            print(f"警告: Deep Research ファイルが見つかりません: {dr_path}")

    if not dr_content and not args.allow_no_deep_research:
        raise RuntimeError(
            "Deep Research が未入力です。スキップ不可運用のため中止します。\n"
            "手順:\n"
            f"  1) 上記プロンプトで Deep Research を実行\n"
            f"  2) 結果を market/daily/sector/{target_date}_deep_research.md に保存\n"
            f"  3) python make_sector_raw.py --date {target_date} --deep-research-file ../../market/daily/sector/{target_date}_deep_research.md\n"
        )

    # 過去 research
    past_research = load_past_research(RESEARCH_DIR)

    # 生成日時
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    # raw ファイル組み立て
    sections = [
        f"# セクター週次レポート 生データ ({target_date})",
        "",
        f"> sector_weekly.parquet + 過去 research から自動生成。Claude が統合分析を行う。",
        f"- **生成日時**: {now_jst}",
        f"- **データ基準日（実行日）**: {as_of}",
        *([price_freshness_line] if price_freshness_line else []),
        *([price_stale_note] if price_stale_note else []),
        f"- **セクター数**: {len(df)}",
        "",
        sector_table,
        "",
        top_bottom,
        "",
        mcap_section,
        "",
        "---",
        "",
        profiles_section,
    ]

    sections += [
        "",
        "---",
        "",
        "## Deep Research 定性分析結果（外部入力）",
        "",
        dr_content.strip(),
    ]

    sections += [
        "",
        "---",
        "",
        "## 過去レポート参照（マクロ・セクター）",
        "",
        past_research,
    ]

    raw_text = "\n".join(sections)

    # 出力
    MARKET_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MARKET_DIR / f"{target_date}_sector_raw.md"
    out_path.write_text(raw_text, encoding="utf-8")

    char_count = len(raw_text)
    token_est  = char_count // 3
    print(f"出力: {out_path}")
    print(f"文字数: {char_count:,}  推定トークン: {token_est:,}")
    if args.allow_no_deep_research:
        print("\n※ allow-no-deep-research が指定されたため、例外的に Deep Research なしで出力しました。")


if __name__ == "__main__":
    main()
