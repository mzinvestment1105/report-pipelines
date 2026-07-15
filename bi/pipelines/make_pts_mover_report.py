"""
夜間PTS動意レポート 生データ生成
================================
引け後〜夜間の PTS（私設取引システム）ナイトタイムセッションで急騰した銘柄を
株探から抽出し、当日の適時開示（TDNet）・みんかぶ・Yahoo掲示板と組み合わせた
生データ Markdown を出力する。日次動意レポート（make_mover_report.py）の亜種で、
材料フェッチャは同スクリプトから import 再利用する（新規実装は PTS 価格取得のみ）。

レポート構成:
  セクションA: 夜間PTS急騰銘柄（新規材料の発見）
    株探「PTSナイトタイムセッション 株価上昇率ランキング」から
    PTS乖離率 >= PCT_MIN かつ PTS売買代金 >= VALUE_MIN を抽出（ETF/REIT除外）。
    各銘柄に「本日引け後の適時開示」を最優先材料として付与。
  セクションB: 当日急騰・S高銘柄の夜間持続チェック（翌朝の寄り判断）
    JQuants EOD で当日 +SECTION_B_PCT_MIN% 以上の銘柄を抽出し、
    その銘柄の夜間PTS価格を突合して「続伸／失速」を判定する。

データソース:
  - 夜間PTS価格・乖離率: 株探 kabutan.jp（ジャパンネクスト証券 J-Market・ナイト17:00-翌6:00）
  - 当日EOD: JQuants v2（セクションB用）
  - 材料: TDNet日次リスト（やのしん WebAPI）・みんかぶ・Yahoo掲示板・EDINET事業内容

出力: market/daily/YYYY-MM-DD_pts_movers_raw.md

実行:
  cd bi/pipelines
  python make_pts_mover_report.py
  python make_pts_mover_report.py --date 2026-07-15
  python make_pts_mover_report.py --fast          # PDF本文・掲示板をスキップ（高速）
  python make_pts_mover_report.py --date-gate      # 絶対配信モード（GHA用・未着でも注記付き続行）

環境変数:
  JQUANTS_API_KEY  必須（セクションB用）
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# 親スクリプト（日次動意）から材料フェッチャ・共通ユーティリティを再利用
from make_mover_report import (
    build_full_table,
    fetch_company_description,
    fetch_daily_all,
    fetch_minkabu_news,
    fetch_pdf_text,
    fetch_yahoo_bbs,
    estimate_tokens,
    load_research_context,
    log_token_usage,
    resolve_trading_days,
    MARKET_DAILY_DIR,
    SCREENING_MASTER_PATH,
    TDNET_PDF_MAX_CHARS,
)
from jq_client_utils import normalize_code_4
from edinetdb_client import EdinetDBClient

BASE_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# 抽出パラメータ（運用1〜2週間後に実測で再調整する。プランの実測分布に基づく初期値）
# ---------------------------------------------------------------------------
PCT_MIN = 5.0            # セクションA: PTS乖離率 下限（%）。実測で +5%以上=約21件/晩・代金併用で約13件
VALUE_MIN = 10_000_000   # セクションA: PTS売買代金 下限（円）。%単独はノイズ行混入のため代金必須
MAX_SECTION_A = 15       # セクションA 掲載上限（乖離率降順・JPEG1枚の可読性）
SECTION_B_PCT_MIN = 8.0  # セクションB: 当日EOD前日比 下限（%）＝当日急騰銘柄の定義
MAX_SECTION_B = 12       # セクションB 掲載上限（前日比降順）

# 株探 夜間PTSランキング
_KABUTAN_UP_URL    = "https://kabutan.jp/warning/pts_night_price_increase"
_KABUTAN_DOWN_URL  = "https://kabutan.jp/warning/pts_night_price_decrease"
_KABUTAN_VALUE_URL = "https://kabutan.jp/warning/pts_night_trading_value_ranking"
_KABUTAN_STOCK_URL = "https://kabutan.jp/stock/?code={code}"
_KABUTAN_MAX_PAGES = 6   # 1ページ15行。値上がりは閾値割れで早期打ち切り

_KABUTAN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}
_KABUTAN_SLEEP = 1.0

# 市場区分マップ（株探表記 → 正式名称）。東Ｅ＝ETF/ETN は除外対象
_MARKET_MAP = {
    "東Ｐ": "プライム", "東Ｓ": "スタンダード", "東Ｇ": "グロース",
    "東Ｅ": "ETF/ETN", "名Ｍ": "名証メイン", "名Ｎ": "名証ネクスト",
    "札": "札証", "福": "福証",
}
_ETF_MARKET_TOKENS = {"東Ｅ"}
_ETF_REIT_NAME = re.compile(
    r"ETF|ETN|上場投信|上場投資信託|投信|NEXT FUNDS|iShares|MAXIS|ダイワ上場|"
    r"日経レバ|日経ダブル|指数連動|連動型上場|レバレッジ|インバース|"
    r"ブル\d|ベア\d|J-REIT|REIT|リート|不動産投資法人|投資法人|インフラファンド"
)
# ファンド固有の開示タイトル語（個別株は絶対に出さない）。screening_master 未登録の
# A接尾ETF（例 600A・略称にETF語を含まない）を開示タイトルから機械判定するために使う。
_FUND_DISCLOSURE = re.compile(
    r"基準価額|投資信託|上場投信|ＥＴＦ|ETF|ＥＴＮ|ETN|分配金|投資法人|リート|REIT|投資口"
)

# TDNet 日次リスト（やのしん WebAPI・1リクエストで全開示）
_TDNET_LIST_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{ymd}.json?limit=3000"
_POST_CLOSE_TIME = dtime(15, 0)  # 引け後開示の判定閾値（15:00以降）


def _code_key(code) -> str:
    """株探コード / screening_master コード / TDNet company_code を4桁キーに正規化。"""
    return str(code).strip().upper()[:4]


# ---------------------------------------------------------------------------
# 株探 夜間PTSランキング スクレイピング
# ---------------------------------------------------------------------------

def _fetch_kabutan(url: str) -> str:
    r = requests.get(url, headers=_KABUTAN_HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return r.text


def _pick_ranking_table(html: str) -> pd.DataFrame | None:
    """株探ランキングHTMLから本体テーブルを1つ返す（コード列を持つ最大表）。"""
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return None
    for t in tables:
        if t.shape[1] >= 13 and t.shape[0] >= 1:
            first_col = "".join(str(c) for c in (t.columns[0] if isinstance(t.columns[0], tuple) else [t.columns[0]]))
            if "コード" in first_col:
                return t
    return None


def _parse_pct(v) -> float | None:
    s = str(v).replace(",", "").replace("％", "%").strip()
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def _parse_num(v) -> float | None:
    s = str(v).replace(",", "").strip()
    if s in ("", "－", "-", "nan", "NaN", "None"):
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def scrape_pts_timestamp() -> str:
    """ランキングページ上部の時点表記（例「7月15日 20:15現在」）を返す。"""
    try:
        html = _fetch_kabutan(_KABUTAN_UP_URL)
    except Exception:
        return ""
    md = re.search(r"(\d{1,2})月(\d{1,2})日", html)
    mt = re.search(r"(\d{1,2}:\d{2})現在", html)
    date_str = f"{int(md.group(1))}月{int(md.group(2))}日" if md else ""
    time_str = f"{mt.group(1)}現在" if mt else ""
    return f"{date_str} {time_str}".strip()


def scrape_pts_ranking(url: str, value_col_is_turnover: bool = False,
                       max_pages: int = _KABUTAN_MAX_PAGES,
                       stop_below_pct: float | None = None) -> pd.DataFrame:
    """夜間PTSランキングをページ送りで取得して DataFrame を返す。

    列: Code, Name, MarketRaw, BaseClose, PtsPrice, DiffPct, RawVolume(株), RawValueMil(百万円)
    value_col_is_turnover=True の場合、9列目は売買代金（百万円単位）。
    stop_below_pct 指定時、ページ内最大 DiffPct がそれを下回ったら打ち切る（値上がり用）。
    """
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        page_url = url if page == 1 else f"{url}?page={page}"
        try:
            html = _fetch_kabutan(page_url)
        except Exception as e:
            print(f"  [WARN] 株探取得失敗 {page_url}: {e}")
            break
        table = _pick_ranking_table(html)
        if table is None or table.empty:
            break
        page_rows: list[dict] = []
        for _, r in table.iterrows():
            code = _code_key(r.iloc[0])
            if not re.match(r"^[0-9A-Z]{4}$", code):
                continue
            diff_pct = _parse_pct(r.iloc[8])
            if diff_pct is None:
                continue
            vol_or_val = _parse_num(r.iloc[9])
            page_rows.append({
                "Code": code,
                "Name": str(r.iloc[1]).strip(),
                "MarketRaw": str(r.iloc[2]).strip(),
                "BaseClose": _parse_num(r.iloc[5]),
                "PtsPrice": _parse_num(r.iloc[6]),
                "DiffPct": diff_pct,
                "RawVolume": None if value_col_is_turnover else vol_or_val,
                "RawValueMil": vol_or_val if value_col_is_turnover else None,
            })
        rows.extend(page_rows)
        time.sleep(_KABUTAN_SLEEP)
        if stop_below_pct is not None and page_rows:
            if max(pr["DiffPct"] for pr in page_rows) < stop_below_pct:
                break
        if not page_rows:
            break
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates("Code").reset_index(drop=True)
    return df


def scrape_individual_pts(code: str) -> float | None:
    """個別ページから現在の夜間PTS価格を取得（セクションB圏外銘柄用）。"""
    from bs4 import BeautifulSoup
    try:
        html = _fetch_kabutan(_KABUTAN_STOCK_URL.format(code=code))
        soup = BeautifulSoup(html, "html.parser")
        for lbl in soup.find_all("div", class_="kabuka1"):
            if lbl.get_text(strip=True) == "PTS":
                val = lbl.find_next_sibling("div", class_="kabuka2")
                if val:
                    return _parse_num(val.get_text(strip=True))
    except Exception:
        return None
    return None


def compute_pts_value_yen(row: pd.Series) -> float | None:
    """PTS売買代金（円）。値上がり表は出来高×PTS株価、売買代金表は百万円列×1e6。"""
    if pd.notna(row.get("RawValueMil")):
        return float(row["RawValueMil"]) * 1e6
    vol, price = row.get("RawVolume"), row.get("PtsPrice")
    if pd.notna(vol) and pd.notna(price):
        return float(vol) * float(price)
    return None


def is_etf_reit(market_raw: str, name: str) -> bool:
    return (market_raw in _ETF_MARKET_TOKENS) or bool(_ETF_REIT_NAME.search(str(name)))


# ---------------------------------------------------------------------------
# TDNet 日次リスト（本日引け後開示の検出・1リクエスト）
# ---------------------------------------------------------------------------

def fetch_tdnet_daily(target: date) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """対象日の全適時開示を ({4桁コード: [開示...]}, {4桁コード: 社名}) で返す。

    各開示: {title, pubdate(datetime), hhmm, is_post_close(bool), pdf_url}
    社名マップは screening_master 未登録銘柄（A接尾IPO・ETF等）の名寄せ・ETF判定に使う。
    """
    url = _TDNET_LIST_URL.format(ymd=target.strftime("%Y%m%d"))
    result: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        print(f"  [WARN] TDNet日次リスト取得失敗 {url}: {e}")
        return result, names
    for it in items:
        td = it.get("Tdnet", it)
        code = _code_key(td.get("company_code", ""))
        if not re.match(r"^[0-9A-Z]{4}$", code):
            continue
        cname = str(td.get("company_name", "")).strip()
        if cname and code not in names:
            names[code] = cname
        pub_raw = td.get("pubdate", "")
        try:
            pub = datetime.strptime(pub_raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            pub = None
        doc = td.get("document_url", "") or ""
        # やのしん rd.php ラッパを剥がして生PDF URLへ
        pdf_url = doc.split("rd.php?", 1)[1] if "rd.php?" in doc else doc
        result.setdefault(code, []).append({
            "title": str(td.get("title", "")).strip(),
            "pubdate": pub,
            "hhmm": pub.strftime("%H:%M") if pub else "",
            "is_post_close": bool(pub and pub.time() >= _POST_CLOSE_TIME),
            "pdf_url": pdf_url,
        })
    # 各コード内は時刻降順（最新の引け後開示を先頭に）
    for code in result:
        result[code].sort(key=lambda e: e["pubdate"] or datetime.min, reverse=True)
    return result, names


def _tdnet_lines(code: str, tdnet_daily: dict, target: date,
                 fetch_pdf: bool, max_items: int = 4) -> list[str]:
    """1銘柄の「本日の適時開示」ブロック行を返す（引け後を最優先明示）。"""
    entries = tdnet_daily.get(code, [])
    if not entries:
        return [f"**本日（{target}）の適時開示:** なし", ""]
    lines = [f"**本日（{target}）の適時開示（{len(entries)}件）:**", ""]
    pdf_budget = 2
    for e in entries[:max_items]:
        flag = "【引け後】" if e["is_post_close"] else ""
        lines.append(f"- {e['hhmm']}　{flag}{e['title']}")
        if fetch_pdf and e["is_post_close"] and pdf_budget > 0 and e["pdf_url"]:
            body = fetch_pdf_text(e["pdf_url"])
            if body:
                lines.append(f"  > {body[:600].replace(chr(10), ' ').strip()}")
                pdf_budget -= 1
            time.sleep(0.4)
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# screening_master 属性付与
# ---------------------------------------------------------------------------

_SHARES_COL = "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"


def load_master() -> pd.DataFrame:
    m = pd.read_parquet(SCREENING_MASTER_PATH)
    m["Code"] = m["Code"].astype(str).str[:4]
    return m


def attach_master(df: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    """PTSランキング df に時価総額（当日終値ベース）・セクター・正式社名を付与。"""
    cols = [c for c in ["Code", "CompanyName", "Sector17CodeName", "MarketCodeName",
                        _SHARES_COL] if c in master.columns]
    meta = master[cols].copy()
    meta["Code"] = meta["Code"].astype(str).str[:4]
    out = df.merge(meta, left_on="Code", right_on="Code", how="left")
    # 時価総額 = 通常取引終値(株探BaseClose) × 発行済株数（§C 対象日EOD基準）
    if _SHARES_COL in out.columns:
        shares = pd.to_numeric(out[_SHARES_COL], errors="coerce")
        out["MarketCapOku"] = pd.to_numeric(out["BaseClose"], errors="coerce") * shares / 1e8
    else:
        out["MarketCapOku"] = pd.NA
    return out


# ---------------------------------------------------------------------------
# レポート組み立て
# ---------------------------------------------------------------------------

def _fmt_cap(cap_oku) -> str:
    return f"{cap_oku:,.0f}億円" if pd.notna(cap_oku) else "─"


def _fmt_yen(v) -> str:
    if v is None or pd.isna(v):
        return "─"
    if v >= 1e8:
        return f"{v/1e8:.1f}億円"
    if v >= 1e6:
        return f"約{v/1e6:.0f}百万円"
    return f"約{v/1e4:.0f}万円"


def build_section_a(df: pd.DataFrame, master: pd.DataFrame, tdnet_daily: dict,
                    edinet: EdinetDBClient | None, target: date,
                    fast: bool) -> list[str]:
    lines = [
        "## セクションA: 夜間PTS急騰銘柄（新規材料の発見）",
        "",
        f"> 抽出条件: PTS乖離率 +{PCT_MIN:.0f}% 以上 かつ PTS売買代金 {_fmt_yen(VALUE_MIN)} 以上"
        f"（ETF/REIT除外・乖離率降順・上限{MAX_SECTION_A}件）。"
        "「なぜ動いた」の最有力材料は本日引け後の適時開示。",
        "",
    ]
    if df.empty:
        lines += ["（抽出条件を満たす銘柄なし）", ""]
        return lines
    for _, row in df.iterrows():
        code = row["Code"]
        name = row.get("CompanyName")
        name = str(name) if pd.notna(name) else row["Name"]
        market = row.get("MarketCodeName")
        market = str(market) if pd.notna(market) else _MARKET_MAP.get(row["MarketRaw"], row["MarketRaw"])
        sector = row.get("Sector17CodeName", "")
        sector = str(sector) if pd.notna(sector) else ""
        pts_val = compute_pts_value_yen(row)
        lines += [
            f"### {code} {name}　PTS {row['DiffPct']:+.2f}%　[{market}]",
            "",
            f"- PTS株価: {row['PtsPrice']:,.1f}円　（通常取引終値 {row['BaseClose']:,.0f}円）",
            f"- PTS売買代金: {_fmt_yen(pts_val)}　時価総額: {_fmt_cap(row.get('MarketCapOku'))}　セクター: {sector}",
        ]
        desc = fetch_company_description(edinet, code) if edinet else ""
        if desc:
            lines.append(f"- 事業: {desc}")
        lines.append("")
        # 本日の適時開示（最優先材料）
        lines += _tdnet_lines(code, tdnet_daily, target, fetch_pdf=not fast)
        # みんかぶニュース
        news = fetch_minkabu_news(code)
        if news:
            lines.append(f"**みんかぶニュース（{len(news)}件）:**")
            lines.append("")
            for n in news:
                lines.append(f"- {n['title']}")
            lines.append("")
        else:
            lines += ["**みんかぶニュース:** なし", ""]
        # Yahoo掲示板（--fast時はスキップ）
        if not fast:
            bbs = fetch_yahoo_bbs(code)
            sentiment, posts = bbs.get("sentiment", ""), bbs.get("posts", [])
            if sentiment or posts:
                lines.append("**Yahoo掲示板:**")
                if sentiment:
                    lines.append(f"- みんなの評価: {sentiment}")
                lines.append("")
                for p in posts[:8]:
                    body = p.get("body", "") if isinstance(p, dict) else str(p)
                    if body:
                        lines.append(f"> {body}")
                lines.append("")
        # 過去リサーチ
        research = load_research_context(code, sector)
        if research:
            lines += ["**過去リサーチ:**", "", research, ""]
        time.sleep(0.3)
    return lines


def build_section_b(movers: pd.DataFrame, pts_lookup: dict, tdnet_daily: dict,
                    edinet: EdinetDBClient | None, target: date) -> list[str]:
    lines = [
        "## セクションB: 当日急騰・S高銘柄の夜間持続チェック（翌朝の寄り判断）",
        "",
        f"> 当日EOD前日比 +{SECTION_B_PCT_MIN:.0f}% 以上の急騰銘柄が、夜間PTSで続伸しているか失速しているか。"
        "続伸＝夜間も買い継続（強い）／失速＝夜間に剥落（翌朝の寄り弱さに注意）。",
        "",
    ]
    if movers.empty:
        lines += ["（当日急騰銘柄なし）", ""]
        return lines
    for _, row in movers.iterrows():
        code = _code_key(row["Code"])
        name = row.get("CompanyName")
        name = str(name) if pd.notna(name) else code
        market = row.get("MarketCodeName")
        market = str(market) if pd.notna(market) else ""
        sector = row.get("Sector17CodeName")
        sector = str(sector) if pd.notna(sector) else ""
        close_t = row.get("Close_T")
        day_ret = row.get("DailyReturn")
        cap_oku = row.get("MarketCapOku")
        # 夜間PTS価格: ランキング由来 → なければ個別ページ
        pts = pts_lookup.get(code)
        pts_price = pts["PtsPrice"] if pts else scrape_individual_pts(code)
        if pts_price and pd.notna(close_t) and close_t:
            hold_pct = (pts_price / close_t - 1) * 100
            if hold_pct >= 1.0:
                verdict = f"続伸（PTS {hold_pct:+.2f}%）"
            elif hold_pct <= -1.0:
                verdict = f"失速（PTS {hold_pct:+.2f}%）"
            else:
                verdict = f"横ばい（PTS {hold_pct:+.2f}%）"
            pts_str = f"PTS株価 {pts_price:,.1f}円 → 当日終値比 {verdict}"
        else:
            pts_str = "夜間PTS: 気配なし（夜間の商いが乏しい）"
        lines += [
            f"### {code} {name}　当日 {day_ret:+.1f}%　[{market}]",
            "",
            f"- 当日終値: {close_t:,.0f}円　時価総額: {_fmt_cap(cap_oku)}　セクター: {sector}",
            f"- {pts_str}",
        ]
        desc = fetch_company_description(edinet, code) if edinet else ""
        if desc:
            lines.append(f"- 事業: {desc}")
        lines.append("")
        lines += _tdnet_lines(code, tdnet_daily, target, fetch_pdf=False, max_items=3)
        time.sleep(0.3)
    return lines


def build_report(section_a_df, section_b_df, master, tdnet_daily, edinet,
                 pts_lookup, target, pts_timestamp, quality_note, fast) -> str:
    lines = [
        f"# 夜間PTS動意レポート 生データ ({target.strftime('%Y-%m-%d')})",
        "",
        "> 株探（ジャパンネクスト証券 J-Market ナイトタイムセッション）+ TDNet + みんかぶ + Yahoo から自動取得。"
        "Claude が「何の会社」「なぜ動いた」を推論してレポートを生成する。",
        f"- **生成日時**: {datetime.now().strftime('%Y-%m-%d %H:%M')} JST",
        f"- **PTSデータ時点**: {pts_timestamp or '株探ページ表記参照'}",
        f"- **抽出条件A**: PTS乖離率 +{PCT_MIN:.0f}% 以上 かつ 売買代金 {_fmt_yen(VALUE_MIN)} 以上",
        f"- **抽出条件B**: 当日EOD前日比 +{SECTION_B_PCT_MIN:.0f}% 以上",
        "",
    ]
    if quality_note:
        lines += [f"> ⚠️ **品質注記**: {quality_note}", ""]
    lines += build_section_a(section_a_df, master, tdnet_daily, edinet, target, fast)
    lines += build_section_b(section_b_df, pts_lookup, tdnet_daily, edinet, target)

    body = "\n".join(lines)
    tokens, chars = estimate_tokens(body), len(body)
    lines += [
        "---", "## トークン使用量", "",
        "| 項目 | 値 |", "|------|-----|",
        f"| 推定トークン数 | {tokens:,} |", f"| 文字数 | {chars:,} |", "",
        "---", "## ⚠️ Claude への必須出力ルール", "",
        "**本レポートでは Deep Research 候補セクションを一切出力しない。**", "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description="夜間PTS動意レポート 生データ生成")
    parser.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（省略時は本日）")
    parser.add_argument("--fast", action="store_true", help="TDNet PDF本文・Yahoo掲示板をスキップ")
    parser.add_argument("--pct-min", type=float, default=None, help="セクションA 乖離率下限を上書き")
    parser.add_argument("--value-min", type=float, default=None, help="セクションA 売買代金下限(円)を上書き")
    parser.add_argument("--date-gate", action="store_true",
                        help="絶対配信モード（GHA用）: 当日EOD未着でも中止せず品質注記付きで続行")
    args = parser.parse_args()

    global PCT_MIN, VALUE_MIN
    if args.pct_min is not None:
        PCT_MIN = args.pct_min
    if args.value_min is not None:
        VALUE_MIN = args.value_min

    target = date.fromisoformat(args.date) if args.date else date.today()
    print(f"対象日: {target}")

    master = load_master()
    print(f"screening_master: {len(master)} 銘柄")

    try:
        edinet = EdinetDBClient()
    except Exception as e:
        print(f"  [WARN] EDINET DB クライアント初期化失敗: {e}")
        edinet = None

    # --- 本日の適時開示（1リクエスト） ---
    print("TDNet日次リスト取得中...")
    tdnet_daily, tdnet_names = fetch_tdnet_daily(target)
    print(f"  {sum(len(v) for v in tdnet_daily.values())} 件 / {len(tdnet_daily)} 銘柄")

    # --- セクションA: 夜間PTS値上がりランキング ---
    print("株探 夜間PTS値上がりランキング取得中...")
    up_df = scrape_pts_ranking(_KABUTAN_UP_URL, stop_below_pct=PCT_MIN)
    print(f"  取得 {len(up_df)} 件")
    section_a_df = pd.DataFrame()
    if not up_df.empty:
        up_df["ValueYen"] = up_df.apply(compute_pts_value_yen, axis=1)
        up_df["_is_etf"] = up_df.apply(lambda r: is_etf_reit(r["MarketRaw"], r["Name"]), axis=1)
        section_a_df = up_df[
            (up_df["DiffPct"] >= PCT_MIN)
            & (up_df["ValueYen"].fillna(0) >= VALUE_MIN)
            & (~up_df["_is_etf"])
        ].sort_values("DiffPct", ascending=False).head(MAX_SECTION_A).copy()
        section_a_df = attach_master(section_a_df, master)
    print(f"  セクションA 抽出 {len(section_a_df)} 件")

    # --- 夜間PTS価格ルックアップ辞書（セクションB用・値上がり+値下がり+代金） ---
    print("株探 夜間PTS ルックアップ辞書構築中...")
    pts_lookup: dict[str, dict] = {}
    lookup_frames = [up_df] if not up_df.empty else []
    for url, is_val in [(_KABUTAN_DOWN_URL, False), (_KABUTAN_VALUE_URL, True)]:
        d = scrape_pts_ranking(url, value_col_is_turnover=is_val, max_pages=4)
        if not d.empty:
            lookup_frames.append(d)
    for frame in lookup_frames:
        for code, price in zip(frame["Code"], frame["PtsPrice"]):
            if code not in pts_lookup and pd.notna(price):
                pts_lookup[code] = {"PtsPrice": float(price)}
    print(f"  ルックアップ {len(pts_lookup)} 銘柄")

    # --- セクションB: 当日EOD急騰銘柄（JQuants） ---
    quality_note = ""
    section_b_df = pd.DataFrame()
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        quality_note = "JQUANTS_API_KEY 未設定のためセクションB（当日急騰の夜間持続チェック）を省略。"
        print(f"  [QUALITY FLAG] {quality_note}")
    else:
        try:
            import jquantsapi
            client = jquantsapi.ClientV2(api_key=api_key)
            today_dt, prev_dt = resolve_trading_days(client, target)
            print(f"  EOD 本日: {today_dt}　前日: {prev_dt}")
            if today_dt != target and args.date_gate:
                quality_note = (
                    f"当日EOD（{target}）未着のため直近営業日 {today_dt} のEODでセクションBを生成。"
                )
                print(f"  [QUALITY FLAG] {quality_note}")
            today_df = fetch_daily_all(client, today_dt)
            prev_df = fetch_daily_all(client, prev_dt)
            full = build_full_table(today_df, prev_df, master)
            movers = full.dropna(subset=["DailyReturn"])
            if "HasCorporateAction" in movers.columns:
                movers = movers[~movers["HasCorporateAction"].fillna(False)]
            section_b_df = movers[movers["DailyReturn"] >= SECTION_B_PCT_MIN] \
                .sort_values("DailyReturn", ascending=False).copy()
            # screening_master 未登録の A接尾コードは build_full_table のETFフィルタをすり抜ける
            # （例: 600A・略称「ＮＡＭラセル２０００」でETF語を含まず名前判定不可）。
            # 名前判定に加え、未登録銘柄は本日開示タイトルのファンド固有語（基準価額・分配金等）で除外する。
            if not section_b_df.empty:
                def _sb_is_fund(r):
                    code = _code_key(r["Code"])
                    name = r.get("CompanyName")
                    if pd.notna(name) and is_etf_reit("", str(name)):
                        return True
                    if pd.isna(name):  # 未登録銘柄は開示から判定
                        titles = " ".join(e["title"] for e in tdnet_daily.get(code, []))
                        tn = tdnet_names.get(code, "")
                        if _FUND_DISCLOSURE.search(titles) or is_etf_reit("", tn) \
                                or _FUND_DISCLOSURE.search(tn):
                            return True
                    return False
                section_b_df = section_b_df[~section_b_df.apply(_sb_is_fund, axis=1)].copy()
                # 表示用に社名を名寄せ（NaN → TDNet社名 → コード）
                section_b_df["CompanyName"] = [
                    (r.get("CompanyName") if pd.notna(r.get("CompanyName"))
                     else (tdnet_names.get(_code_key(r["Code"])) or _code_key(r["Code"])))
                    for _, r in section_b_df.iterrows()
                ]
            section_b_df = section_b_df.head(MAX_SECTION_B)
        except Exception as e:
            quality_note = f"セクションB（当日EOD）取得失敗のため省略: {e}"
            print(f"  [QUALITY FLAG] {quality_note}")
    print(f"  セクションB 抽出 {len(section_b_df)} 件")

    if args.date_gate:
        MARKET_DAILY_DIR.mkdir(parents=True, exist_ok=True)
        (MARKET_DAILY_DIR / f"{target}_pts_quality_flags.txt").write_text(
            quality_note + ("\n" if quality_note else ""), encoding="utf-8")

    pts_timestamp = scrape_pts_timestamp()

    report_md = build_report(
        section_a_df=section_a_df, section_b_df=section_b_df, master=master,
        tdnet_daily=tdnet_daily, edinet=edinet, pts_lookup=pts_lookup,
        target=target, pts_timestamp=pts_timestamp, quality_note=quality_note,
        fast=args.fast,
    )

    MARKET_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MARKET_DAILY_DIR / f"{target}_pts_movers_raw.md"
    out_path.write_text(report_md, encoding="utf-8")
    tokens = estimate_tokens(report_md)
    log_token_usage(target, "make_pts_mover_report", tokens, len(report_md))
    print(f"\n出力: {out_path}")
    print(f"推定トークン数: {tokens:,} ({len(report_md):,} 文字)")


if __name__ == "__main__":
    main()
