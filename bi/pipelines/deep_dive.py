"""
銘柄 Deep Dive データ収集。

EDINET DB REST API から企業情報・財務データ・定性テキストを取得し、
以下のデータを統合した生データ Markdown を出力する。レポート本文は Claude が生成する。

収集データ:
  1. EDINET DB 企業基本情報・最新財務（get_company）
  2. EDINET DB 財務時系列（get_financials）
  3. EDINET DB 定性テキスト（事業概要・リスク・MD&A）（get_text_blocks）
  3-2. EDINET DB セグメント別売上・利益（get_segments）
  3-3. EDINET DB 大株主の状況（get_major_shareholders・有報／半期報告書の上位10名・直近2期）
       と、株主の「会社との関係」判定材料（役員一覧・親会社/関係会社・主要販売先・
       大量保有報告書の保有目的）
  4. Yahoo 掲示板（個人投資家センチメント）。投稿日時つきで取得し、反応スコア
     対象日の前後1日の投稿を別掲する（過去日の値動きの説明に別の日の投稿を
     使わせないため。日付を解釈できない投稿は「日付不明」と明記する）
  5. TDNet 適時開示（直近30日）
  6. Yahoo Finance ニュース（直近8件）
  7. 需給データ（screening_master: 信用残・空売り残）
  8. セクター週次コンテキスト（sector_weekly.parquet）
  9. 直近マクロレポート（market/daily/macro の最新1件。過去日は market/archive/macro）
  10. 過去 Deep Dive・Perplexity レポート（各直近2件）
  11. 株価時系列（yfinance 日足 OHLCV・2年）と水準の実績集計
      （直近高値安値・MA5/25/75・BB±2σ±3σ への到達回数と到達後5日/10日の値動き）
  12. 反応スコア対象日（日中値幅 × 出来高5日平均比の上位3日）の外部環境。
      自社の開示（TDNet）・国内指数（日経平均は yfinance、TOPIX と東証グロース
      市場250 は J-Quants 指数 API）・米国指数の前営業日（SOX・ナスダック総合・
      S&P500、yfinance）・ドル円・同業の当日騰落率と開示（peers.yml）・
      当日のマクロ／動意レポートでの当該銘柄への言及。
      「特定できる材料が確認できなかった」で終わる誌面を無くすための材料であり、
      執筆側は data.md に無い事実を書けないため機械で先に揃える。

使い方:
  python deep_dive.py --code 7256
  python deep_dive.py --code 7256 --years 3

出力: research/stocks/{code}_{date}_data.md
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from dotenv import load_dotenv

# Windows の既定 cp932 では日本語・記号の print で UnicodeEncodeError になり
# 収集が途中で落ちる。PYTHONIOENCODING の指定がなくても落ちないようにする。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from edinetdb_client import EdinetDBClient
from jq_client_utils import normalize_code_4
import reaction_supply_demand as rsd

import edinet_client
import edinet_pdf_extractor


BASE_DIR       = Path(__file__).resolve().parent
OUTPUT_DIR     = BASE_DIR / ".." / ".." / "research" / "stocks"
RESEARCH_DIR   = BASE_DIR / ".." / ".." / "research"
OUTPUTS_DIR    = BASE_DIR / ".." / "outputs"
_ENV_PATH      = BASE_DIR / ".env"

SCREENING_MASTER_PATH = OUTPUTS_DIR / "screening_master.parquet"
SECTOR_WEEKLY_PATH    = OUTPUTS_DIR / "sector_weekly.parquet"
DEEP_DIVE_CACHE_DIR   = OUTPUTS_DIR / "deep_dive_cache"
REPO_ROOT             = BASE_DIR / ".." / ".."
MARKET_DIR            = REPO_ROOT / "market"
MARKET_DAILY_MACRO    = MARKET_DIR / "daily" / "macro"
MARKET_ARCHIVE_MACRO  = MARKET_DIR / "archive" / "macro"
MARKET_ARCHIVE_MOVERS = MARKET_DIR / "archive" / "movers"
MARKET_DAILY_MOVERS   = MARKET_DIR / "daily" / "movers"
PEERS_YAML_PATH       = RESEARCH_DIR / "stocks" / "peers.yml"

# --- 反応スコア対象日の外部環境（PM 2026-09-06 指示） ---
# 「特定できる材料が確認できなかった」で終わらせないため、値動きの原因になりうる
# 事実（自社開示・国内指数・米国指数・為替・同業）を機械的に揃えて data.md へ入れる。
# 執筆側は data.md に無い事実を書けないため、先に機械で揃えるのが唯一の解決になる。
REACTION_TOP_N        = 3      # 対象日は上位3日のみ（全営業日へ広げるとトークンが肥大する）
REACTION_VOL_WINDOW   = 5      # 出来高の平均を取る営業日数
REACTION_LOOKBACK_DAYS = 180   # 反応スコアを探す期間（直近およそ半年）
# 「直近で大きく動いたものは必ず3件に含める」（agents/stock_analyst.md）を機械で担保するため、
# 直近この日数の中の最高スコア日が上位3件から漏れていたら必ず差し込む。
REACTION_RECENT_DAYS  = 45

# 米国指数・為替は yfinance（HTTP のみ・GHA でも動く。実測確認済み 2026-09-06）。
REACTION_US_TICKERS = {
    "SOX指数": "^SOX",
    "ナスダック総合": "^IXIC",
    "S&P500": "^GSPC",
}
REACTION_FX_TICKERS = {"ドル円": "USDJPY=X"}
# 日経平均のみ yfinance に指数が存在する。TOPIX・グロース250 は yfinance に指数が無く、
# ETF（1306 等）は _cr §1 で内部参照も含めて使わない方針のため J-Quants 指数 API を使う。
REACTION_JP_YF_TICKERS = {"日経平均": "^N225"}
# J-Quants v2 /indices/bars/daily の指数コード
#   0000 = TOPIX / 0070 = 東証グロース市場250指数
#   （公式 spec jpx-jquants.com/ja/spec/idx-bars-daily/indexcodes。ETF ではなく指数そのもの）
REACTION_JQ_INDEX_CODES = {
    "TOPIX": "0000",
    "東証グロース市場250": "0070",
}

# 機関空売り残（空売り残高報告制度・発行済の0.5%以上で報告義務）の走査幅。
# 公表日（DiscDate）は計算日（CalcDate）の約2営業日後になるため、対象日の前後に余裕を取る。
# PM 2026-09-06 指示: 反応スコアの需給要因として機関空売りの増減を必ず確認する。
SHORT_SALE_SCAN_BACK_DAYS = 21   # 対象日より前（直前の残高＝増減の比較対象を取るため）
SHORT_SALE_SCAN_FWD_DAYS  = 6    # 対象日より後（対象日の CalcDate 分が公表されるまで）

# 取得経路の記録（provenance）。値は "EDINET DB" / "EDINET公式API(有報)" / "取得不可"
PROV_DB      = "EDINET DB"
PROV_EDINET  = "EDINET公式API(有報)"
PROV_NONE    = "取得不可"

# 決算説明資料などの重要ページ判定に使うキーワード
IR_PAGE_KEYWORDS = re.compile(r"市場規模|TAM|SAM|中期|中計|受注残|生産能力|KPI|目標")
# 決算説明資料・中期経営計画・成長可能性資料の判定に使うタイトルキーワード。
# TDNet のタイトル・会社 IR ページのリンク文字列の双方に同じ語彙を当てる。
IR_TITLE_KEYWORDS = (
    "決算説明資料", "決算説明会資料", "決算補足説明資料", "決算説明",
    "説明資料", "補足説明",
    "中期経営計画", "中期計画", "中計",
    "成長可能性に関する説明資料", "成長可能性",
    "事業計画及び成長可能性に関する事項", "事業計画",
    "経営計画", "長期ビジョン", "長期経営", "統合報告",
)
IR_MAX_DOCS       = 3
IR_MAX_PAGES      = 20

# --- フォワードガイダンス（会社自身が公表した将来目標）の抽出設定 ---
# 取得した資料の全文から、将来の売上・利益・KPI 目標に関する行を拾う。
# 「決算短信には載らないが説明資料・中期経営計画には載っている」情報を
# 誌面へ確実に届けるための必須インプット。
GUIDANCE_MAX_DOCS       = 4      # ガイダンス抽出の対象とする資料の最大件数
# data.md へ書き出すガイダンスセクション全体の文字数上限。
# 決算説明資料は40〜60ページあり全文を載せると執筆側の入力トークンが跳ね上がるため、
# PDF 全文の抽出は Python 内のメモリ上だけで行い（ここはトークンを消費しない）、
# data.md には機械的に絞り込んだ該当箇所だけを書く。
GUIDANCE_SECTION_MAX_CHARS = 8000
GUIDANCE_MAX_QUOTES        = 12    # 表に落とせなかった原文引用の最大件数
GUIDANCE_QUOTE_CHARS       = 200   # 原文引用1件あたりの最大文字数
GUIDANCE_MAX_TARGET_ROWS   = 24    # 数値目標テーブルの最大行数
IR_DECK_MAX_IMAGE_PATHS    = 5     # data.md に列挙する PNG パスの上限（パス羅列もトークンを食う）

# --- 将来目標の抽出パターン（すべて Python 側で機械的に判定する） ---
# 目標年度の表現
GUIDANCE_FY_PATTERN = re.compile(
    r"(?:FY|fy)\s?(20[2-4][0-9])(?:\s?[-〜~ー]\s?(?:FY|fy)?(20[2-4][0-9]|[0-9]{2}))?"
    r"|(20[2-4][0-9])\s?年\s?([0-9]{1,2})\s?月期"
    r"|(20[2-4][0-9])\s?年度?"
    r"|((?:20)?[2-4][0-9])\s?/\s?([0-9]{1,2})\s?期"
)
# 指標名（会社が使う財務・KPI 指標）
GUIDANCE_METRIC_PATTERN = re.compile(
    r"売上高|売上収益|営業利益率|営業利益|経常利益|当期純利益|純利益|"
    r"ARR|MRR|EBITDA|ROE|ROIC|CAGR|GMV|取扱高|受注残|契約社数|契約企業数|"
    r"解約率|チャーン|配当性向|自己資本比率|利益率|伸長率|成長率|シェア|"
    r"管理楽曲数|取扱原盤数|ユーザ数|会員数"
)
# 数値＋単位（実額・率・倍率）
GUIDANCE_VALUE_PATTERN = re.compile(
    r"[0-9][0-9,\.]*\s*(?:兆円|億円|百万円|千円|万円|億|兆|%|％|ポイント|pt|倍|万人|万曲|万原盤|万社|社|人)"
)
# 目標を示す語
GUIDANCE_GOAL_PATTERN = re.compile(
    r"目標|計画|ビジョン|中期経営計画|中期業績計画|中期計画|中計|達成|見通し|想定|目指"
)
GUIDANCE_CONTEXT_LINES = 2   # マッチ行の前後何行を文脈として残すか

# 会社 IR ページ探索に使う URL/リンク文字列のヒント
IR_SITE_URL_HINTS  = ("/ir", "ir/", "investor", "library", "presentation",
                      "financial", "kessan", "shiryo", "material", "irnews",
                      "plan", "management", "strategy")
IR_SITE_MAX_PAGES  = 14   # 巡回するページ数の上限
IR_SITE_MAX_DEPTH  = 2    # トップから辿る階層の上限
IR_SITE_TIMEOUT    = 25


def normalize_code(code: object) -> str:
    """証券コードを正規化する。

    normalize_code_4 は先頭4文字へ切り詰めるだけで、485A のような英数字混在の
    4文字コードはそのまま通る。J-Quants 由来の 5 桁（末尾 0 付き）だけを
    4 文字へ丸め、それ以外は原文を保つ。
    """
    s = str(code).strip().upper()
    if len(s) == 5 and s.endswith("0"):
        return s[:4]
    return s


# TDNet 開示の遡及日数。決算説明資料は四半期ごとの開示のため、30日窓では
# 前四半期の資料を構造的に取り逃す（実測: 33日前の開示が窓外で落ちた）。
# 400日にして直近1年分の説明資料・中期経営計画を必ず候補に載せる。
# ただし TDNet は PDF 実体を約30日で削除する（実測: 30日超は 404）ため、
# 期間拡張だけでは本文は取れない。会社 IR ページ経路と併用する。
TDNET_DAYS      = 400
TDNET_PDF_MAX   = 30000
# 表示件数の上限。400日窓では開示が数百件になるため、一覧は直近30件へ絞る。
TDNET_MAX_ITEMS = 30
# 誌面の「TDNet 適時開示」セクションに載せる直近日数（従来と同じ体裁を保つ）。
TDNET_RECENT_DISPLAY_DAYS = 30
BBS_MAX_POSTS   = 30
REQUEST_SLEEP   = 0.5

# --- Yahoo 掲示板の遡り取得（PM 2026-09-06 指示） ---
# 掲示板の一覧ページは最新約70件しか返さず、ページング用のクエリも存在しない
# （?page= 等は無視される。実測確認済み 2026-09-06）。一方で投稿単票の URL
# /quote/{code}.T/forum/{投稿No} は任意の過去投稿を返し、投稿 No は時系列に単調増加する。
# そのため「投稿 No の二分探索で対象日の境界を特定し、そこから連番で少数件だけ読む」
# 方式で遡る。無限に遡らないよう日数・リクエスト数の両方に上限を置く。
BBS_LOOKBACK_DAYS      = 60    # これより古い日付は遡らない
BBS_MAX_PAGES          = 10    # 一覧ページ相当の取得上限（現状の一覧は1ページのみ）
BBS_DAY_WINDOW_POSTS   = 8     # 対象日1日あたり拾う投稿数の上限
BBS_MAX_DETAIL_FETCHES = 260   # 単票取得の総リクエスト上限（二分探索＋連番の合計）
                               # 内訳の目安: 対象3日 ×（二分探索 約21回 ＋ 前進走査 約60回）
BBS_SECTION_MAX_CHARS  = 5000  # 掲示板セクション全体の文字数上限
BBS_DETAIL_SLEEP       = 0.25  # 単票取得の間隔（相手サイトへの配慮）
BBS_LIST_RETRIES       = 3     # 一覧ページの再試行回数（500 を返すことがある）
BBS_LIST_RETRY_SLEEP   = 4.0   # 再試行の待ち秒（回数に比例して伸ばす）

# 株価時系列（日足）。250営業日を確実に満たすため 2 年分取得する。
PRICE_HISTORY_PERIOD  = "2y"
PRICE_MIN_BARS        = 250
PRICE_MA_WINDOWS      = (5, 25, 75)
PRICE_RANGE_WINDOWS   = (75, 250)
PRICE_BB_WINDOW       = 25
PRICE_TOUCH_GAP_DAYS  = 5

_TDNET_ATOM_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.atom"
_NS = {"a": "http://purl.org/atom/ns#"}

# ---------------------------------------------------------------------------
# Yahoo 掲示板取得
# ---------------------------------------------------------------------------

_YAHOO_HEADERS = {
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
    r"|強く買いたい.*%|買いたい.*売りたい.*%|直近1週間でユーザーが掲示板"
    r"|No\.\d{5,7}|報告$|\d{4}/\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}報告"
    r"|JASRAC|プライバシーポリシー|利用規約|免責事項|ヘルプ・お問い合わせ"
    r"|情報提供会社|東京証券取引所.*大阪取引所|最近見た銘柄.*ランキング)"
)
_BBS_POST_LIKE = re.compile(r"[。！？ねよわだます]")
_BBS_QUOTE    = re.compile(r"^>>\s*\d+")
# 本文中どこにでも現れる引用参照（>>123）。1行に潰れた投稿から引用だけを除くために使う。
_BBS_QUOTE_REF = re.compile(r">>\s*\d+")


# ---------------------------------------------------------------------------
# TDNet 適時開示
# ---------------------------------------------------------------------------

def _parse_pub_datetime(pub: str):
    """Atom の published 文字列を aware datetime へ変換。解釈できなければ None。"""
    s = (pub or "").strip()
    if not s:
        return None
    candidates = [s, s.replace("Z", "+00:00"), s.replace("/", "-")]
    for cand in candidates:
        try:
            dt = datetime.fromisoformat(cand)
            return dt if dt.tzinfo else dt.astimezone()
        except (ValueError, TypeError):
            pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
        "%a, %d %b %Y %H:%M:%S %z",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.astimezone()
        except (ValueError, TypeError):
            pass
    return None


def _fetch_pdf_text(pdf_url: str) -> str:
    """TDNet PDF の本文テキストを抽出する。失敗時は stderr に警告して空文字を返す。"""
    try:
        from io import BytesIO, StringIO
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        resp = requests.get(pdf_url, timeout=30)
        resp.raise_for_status()
        out = StringIO()
        extract_text_to_fp(BytesIO(resp.content), out, laparams=LAParams(),
                           output_type="text", codec=None)
        text = re.sub(r"\n{3,}", "\n\n", out.getvalue()).strip()
        return text[:TDNET_PDF_MAX]
    except Exception as e:
        print(f"  → 取得失敗: _fetch_pdf_text({pdf_url}): {e}", file=sys.stderr)
        return ""


def fetch_tdnet(code4: str) -> list[dict]:
    """TDNet 適時開示（直近 TDNET_DAYS 日・最大 TDNET_MAX_ITEMS 件）を取得する。

    PDF 本文は「タイトルに『決算短信』を含む最新2件」＋「それ以外の直近1件」について取得する。
    """
    url = _TDNET_ATOM_URL.format(code=code4)
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        cutoff = datetime.now().astimezone() - timedelta(days=TDNET_DAYS)
        entries: list[dict] = []
        for entry in root.findall("a:entry", _NS):
            title   = (entry.findtext("a:title", "", _NS) or "").strip()
            link_el = entry.find("a:link", _NS)
            link    = link_el.attrib.get("href", "") if link_el is not None else ""
            # yanoshin の TDNet フィードは Atom 0.3 で、日付は published ではなく
            # issued / created / modified に入る（published は存在しない）。
            pub = ""
            for tag in ("a:published", "a:issued", "a:created", "a:modified", "a:updated"):
                pub = (entry.findtext(tag, "", _NS) or "").strip()
                if pub:
                    break
            pdf_url = ""
            for enc in entry.findall("a:enclosure", _NS):
                if enc.attrib.get("type", "") == "application/pdf":
                    pdf_url = enc.attrib.get("href", "")
                    break
            if not pdf_url:
                # enclosure がない場合、link href に PDF が入る
                # （https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/....pdf）
                for cand in (link, ""):
                    if ".pdf" in cand.lower():
                        m_pdf = re.search(r"(https?://[^?\s]+\.pdf)", cand.split("rd.php?")[-1])
                        pdf_url = m_pdf.group(1) if m_pdf else cand
                        break
            dt = _parse_pub_datetime(pub)
            if dt is None:
                # 日付を解釈できない開示は「直近30日」の保証ができないため除外する
                print(f"  → 取得失敗: fetch_tdnet: published を解釈できません ({pub!r} / {title})",
                      file=sys.stderr)
                continue
            if dt < cutoff:
                continue
            entries.append({"title": title, "link": link, "published": pub,
                            "pdf_url": pdf_url, "pdf_text": ""})
            if len(entries) >= TDNET_MAX_ITEMS:
                break

        # PDF 本文取得対象: 決算短信の最新2件 ＋ それ以外の直近1件
        targets: list[dict] = [e for e in entries if "決算短信" in e["title"] and e["pdf_url"]][:2]
        for e in entries:
            if e["pdf_url"] and e not in targets:
                targets.append(e)
                break
        for e in targets:
            e["pdf_text"] = _fetch_pdf_text(e["pdf_url"])
            time.sleep(REQUEST_SLEEP)
        return entries
    except Exception as e:
        print(f"  → 取得失敗: fetch_tdnet({code4}): {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Yahoo Finance ニュース
# ---------------------------------------------------------------------------

def fetch_yahoo_news(code4: str, max_items: int = 0) -> list[dict]:
    """Yahoo Finance JPのニュースを取得する。max_items=0で全件取得。"""
    from bs4 import BeautifulSoup
    url = f"https://finance.yahoo.co.jp/quote/{code4}.T/news"
    try:
        r = requests.get(url, headers=_YAHOO_HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        items = []
        for a in soup.find_all("a", href=True):
            if "/news/detail/" not in a.get("href", ""):
                continue
            text = a.get_text(strip=True)
            if not text or len(text) < 10:
                continue
            m = re.search(r"(\d+/\d+)([^\d].{1,20})$", text)
            if m:
                title  = text[: m.start()].strip()
                date_s = m.group(1)
                source = m.group(2).strip()
            else:
                title, date_s, source = text, "", ""
            if not title:
                continue
            items.append({"title": title, "date": date_s, "source": source})
            if max_items and len(items) >= max_items:
                break
        return items
    except Exception as e:
        print(f"  ⚠️ Yahooニュース取得失敗: {e}")
        return []


# ---------------------------------------------------------------------------
# 需給・財務データ（screening_master）
# ---------------------------------------------------------------------------

def _mv(r, col, fmt="{:,.0f}"):
    import pandas as pd
    v = r.get(col)
    return fmt.format(v) if pd.notna(v) else "N/A"


def _mv_yen_to_mn(r, col):
    """screening_master は円単位で格納されているため百万円へ換算して表示する。"""
    import pandas as pd
    v = r.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "N/A"
    try:
        return f"{float(v) / 1e6:,.0f}百万円"
    except (TypeError, ValueError):
        return "N/A"


def load_supply_demand(code4: str) -> dict:
    """
    screening_master.parquet から財務・バリュエーション・需給データを返す。
    'sector' / 'market' / 'company_name' キーも含む。
    """
    if not SCREENING_MASTER_PATH.exists():
        return {}
    try:
        import pandas as pd
        df = pd.read_parquet(SCREENING_MASTER_PATH)
        df["Code"] = df["Code"].astype("string").str.strip().str[:4]
        row = df[df["Code"] == code4]
        if row.empty:
            return {}
        r = row.iloc[0]

        result: dict = {
            "sector":       str(r.get("Sector17CodeName", "")),
            "market":       str(r.get("MarketCodeName", "")),
            "company_name": str(r.get("CompanyName", "")),
        }

        # --- バリュエーション ---
        result["_section_valuation"] = True
        result["PER"]            = _mv(r, "PER_Trailing", "{:.1f}x")
        result["PBR"]            = _mv(r, "PBR_Trailing", "{:.1f}x")
        result["ROE"]            = _mv(r, "ROE_LatestYear", "{:.1%}")
        result["自己資本比率"]    = _mv(r, "EquityToAssetRatio", "{:.1%}")
        result["時価総額"]        = (f"{r['MarketCap']/1e8:.0f}億円"
                                    if pd.notna(r.get("MarketCap")) else "N/A")

        # --- 損益（3期実績＋来期予想）百万円単位 ---
        result["_section_pl"] = True
        for label, col in [
            ("売上高_2期前",   "NetSales_TwoYearsPrior_Actual"),
            ("売上高_前期",    "NetSales_PriorYear_Actual"),
            ("売上高_今期",    "NetSales_LatestYear_Actual"),
            ("売上高_来期予想","NetSales_NextYear_Forecast"),
            ("営業利益_2期前", "OperatingProfit_TwoYearsPrior_Actual"),
            ("営業利益_前期",  "OperatingProfit_PriorYear_Actual"),
            ("営業利益_今期",  "OperatingProfit_LatestYear_Actual"),
            ("営業利益_来期予想","OperatingProfit_NextYear_Forecast"),
            ("純利益_2期前",   "Profit_TwoYearsPrior_Actual"),
            ("純利益_前期",    "Profit_PriorYear_Actual"),
            ("純利益_今期",    "Profit_LatestYear_Actual"),
            ("純利益_来期予想","Profit_NextYear_Forecast"),
        ]:
            result[label] = _mv_yen_to_mn(r, col)

        # --- BS ---
        result["_section_bs"] = True
        result["現金同等物"] = _mv_yen_to_mn(r, "CashAndEquivalents_LatestFY")
        result["純資産"]     = _mv_yen_to_mn(r, "Equity_LatestFY")

        # --- 流動性 ---
        result["_section_liquidity"] = True
        result["5日平均出来高"] = _mv(r, "AvgDailyVolume5d", "{:,.0f}株")
        result["5日平均売買代金"] = (f"{r['AvgDailyValue5d']/1e8:.1f}億円"
                                     if pd.notna(r.get("AvgDailyValue5d")) else "N/A")

        # --- 需給 ---
        result["_section_supply"] = True
        if pd.notna(r.get("ShortPositionsToSharesOutstandingRatio")):
            result["機関空売り残比率"] = f"{r['ShortPositionsToSharesOutstandingRatio']:.2%}"
        if pd.notna(r.get("ShortPositionsInSharesNumber")):
            result["機関空売り残株数"] = f"{r['ShortPositionsInSharesNumber']:,.0f}株"

        # WkSeq01=最古 … WkSeqNN=直近（最新）。最大シーケンス番号を動的に特定して最新週を先頭に出力
        for prefix, label in [
            ("LongMargin",  "信用買い残"),
            ("ShortMargin", "信用売り残"),
            ("ShortSale",   "空売り残"),
        ]:
            # 存在するカラムを番号降順（直近→最古）で列挙
            seqs = sorted(
                [c.replace(f"{prefix}_WkSeq", "") for c in r.index
                 if c.startswith(f"{prefix}_WkSeq") and pd.notna(r[c])],
                key=lambda x: int(x),
                reverse=True,  # 大きい番号=直近が先頭
            )
            for i, seq_num in enumerate(seqs):
                col = f"{prefix}_WkSeq{seq_num}"
                week_label = "直近" if i == 0 else f"{i}週前"
                result[f"{label}_{week_label}"] = f"{r[col]:,.0f}株"

        return result
    except Exception as e:
        print(f"  → 取得失敗: load_supply_demand({code4}): {e}", file=sys.stderr)
        return {}


def _sm_row(code4: str):
    """screening_master.parquet から該当銘柄の行を返す（見つからなければ None）。

    Code は 4 桁とは限らない（485A のような英数字混在コードがある）ため、
    切り詰めではなく「完全一致 → 末尾 0 を落とした5桁一致」の順で照合する。
    """
    if not SCREENING_MASTER_PATH.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(SCREENING_MASTER_PATH)
        codes = df["Code"].astype("string").str.strip()
        hit = df[codes == str(code4).strip()]
        if hit.empty:
            # J-Quants は 5 桁（末尾 0 付き）で格納することがある
            hit = df[codes == f"{str(code4).strip()}0"]
        if hit.empty:
            # 従来挙動（先頭4文字一致）を最後の手段として残す
            hit = df[codes.str[:4] == str(code4).strip()[:4]]
        if hit.empty:
            return None
        return hit.iloc[0]
    except Exception as e:
        print(f"  → 取得失敗: _sm_row({code4}): {e}", file=sys.stderr)
        return None


def build_supply_demand_axes(code4: str) -> dict:
    """需給3軸（発行済比率・解消日数・トレンド）と誌面参照列を実数で返す。

    列が欠損している場合は推定せず None のまま残す。
    """
    import pandas as pd

    out: dict = {"available": False, "missing": [], "axes": {}, "raw": {}}
    r = _sm_row(code4)
    if r is None:
        out["note"] = "screening_master.parquet に該当行が見つかりませんでした（未収録）。"
        return out
    out["available"] = True

    def num(col):
        v = r.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            out["missing"].append(col)
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            out["missing"].append(col)
            return None

    shares_out = num(
        "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock")
    long_margin  = num("LongMarginTradeVolume")
    short_margin = num("ShortMarginTradeVolume")
    short_pos    = num("ShortPositionsInSharesNumber")
    short_ratio  = num("ShortPositionsToSharesOutstandingRatio")
    vol5d        = num("AvgDailyVolume5d")
    val5d        = num("AvgDailyValue5d")

    out["raw"] = {
        "発行済株式総数": shares_out,
        "信用買残":       long_margin,
        "信用売残":       short_margin,
        "機関空売り残株数": short_pos,
        "機関空売り残比率": short_ratio,
        "5日平均出来高":  vol5d,
        "5日平均売買代金": val5d,
    }

    # ① 発行済比率
    ratio_col = num("Scr_LongMargin_to_SharesOutstanding")
    if ratio_col is not None:
        pct = ratio_col * 100.0
    elif long_margin is not None and shares_out:
        pct = long_margin / shares_out * 100.0
    else:
        pct = None
    if pct is None:
        judge1 = None
    elif pct > 20:
        judge1 = "要警戒（20%超）"
    elif pct > 10:
        judge1 = "重い（10%超）"
    else:
        judge1 = "通常（10%以下）"
    out["axes"]["issued_ratio_pct"] = pct
    out["axes"]["issued_ratio_judge"] = judge1

    # ② 解消日数
    days = num("Scr_LongMargin_to_AvgVol5d")
    if days is None and long_margin is not None and vol5d:
        days = long_margin / vol5d
    if days is None:
        judge2 = None
    elif days >= 10:
        judge2 = "非常に重い（10日以上）"
    elif days >= 5:
        judge2 = "重い（5日以上10日未満）"
    elif days >= 1:
        judge2 = "普通（1日以上5日未満）"
    else:
        judge2 = "軽い（1日未満）"
    out["axes"]["unwind_days"] = days
    out["axes"]["unwind_days_judge"] = judge2

    # ③ トレンド（LongMargin_WkSeq06 → LongMarginTradeVolume）
    start = r.get("LongMargin_WkSeq06")
    start = None if (start is None or (isinstance(start, float) and pd.isna(start))) else float(start)
    end   = long_margin
    if start is None:
        out["missing"].append("LongMargin_WkSeq06")
    if start is not None and end is not None and start != 0:
        chg = (end - start) / start * 100.0
        direction = "減少（良い兆候）" if chg < 0 else ("横ばい" if abs(chg) < 1 else "増加（悪化）")
    else:
        chg = None
        direction = None
    out["axes"]["trend_start"] = start
    out["axes"]["trend_end"]   = end
    out["axes"]["trend_pct"]   = chg
    out["axes"]["trend_judge"] = direction

    return out


def _fmt_supply_demand_axes(sd: dict | None) -> list[str]:
    """需給3軸セクションを組み立てる。取得できない項目は N/A のまま残す。"""
    if not sd:
        return []
    lines = ["## 需給3軸評価（screening_master・自動計算）", ""]
    if not sd.get("available"):
        lines += [f"*{sd.get('note', '取得できませんでした')}*", ""]
        return lines

    ax  = sd.get("axes", {})
    raw = sd.get("raw", {})
    shares_out = raw.get("発行済株式総数")

    def pct_of_shares(v):
        if v is None or not shares_out:
            return "N/A"
        return f"{v / shares_out * 100:.2f}%"

    def f_num(v, unit="", fmt="{:,.0f}"):
        return f"{fmt.format(v)}{unit}" if v is not None else "N/A"

    lines += [
        "| 軸 | 実数 | 判定 | 判定閾値（正本） |",
        "|----|------|------|------------------|",
        (f"| ① 発行済比率（信用買残÷発行済） "
         f"| {f_num(ax.get('issued_ratio_pct'), '%', '{:.2f}')} "
         f"| {ax.get('issued_ratio_judge') or 'N/A'} "
         f"| 10%超=重い / 20%超=要警戒 |"),
        (f"| ② 解消日数（信用買残÷5日平均出来高） "
         f"| {f_num(ax.get('unwind_days'), '日', '{:.2f}')} "
         f"| {ax.get('unwind_days_judge') or 'N/A'} "
         f"| 1日未満=軽い / 1〜5日=普通 / 5〜10日=重い / 10日以上=非常に重い |"),
        (f"| ③ トレンド（6週前→直近の信用買残） "
         f"| {f_num(ax.get('trend_start'), '株')} → {f_num(ax.get('trend_end'), '株')}"
         f"（{f_num(ax.get('trend_pct'), '%', '{:+.1f}')}） "
         f"| {ax.get('trend_judge') or 'N/A'} "
         f"| 減少=良い兆候 |"),
        "",
        "### 需給・流動性の実数（対発行済%併記）",
        "",
        "| 項目 | 実数 | 対発行済株式総数 |",
        "|------|------|------------------|",
        f"| 発行済株式総数（自己株式含む） | {f_num(shares_out, '株')} | - |",
        (f"| 信用買残（LongMarginTradeVolume） | {f_num(raw.get('信用買残'), '株')} "
         f"| {pct_of_shares(raw.get('信用買残'))} |"),
        (f"| 信用売残（ShortMarginTradeVolume） | {f_num(raw.get('信用売残'), '株')} "
         f"| {pct_of_shares(raw.get('信用売残'))} |"),
        (f"| 機関空売り残株数（ShortPositionsInSharesNumber） "
         f"| {f_num(raw.get('機関空売り残株数'), '株')} "
         f"| {pct_of_shares(raw.get('機関空売り残株数'))} |"),
        (f"| 機関空売り残比率（ShortPositionsToSharesOutstandingRatio） "
         f"| {f_num(raw.get('機関空売り残比率') * 100 if raw.get('機関空売り残比率') is not None else None, '%', '{:.2f}')} "
         f"| 同左 |"),
        (f"| 5日平均出来高（AvgDailyVolume5d） | {f_num(raw.get('5日平均出来高'), '株')} "
         f"| {pct_of_shares(raw.get('5日平均出来高'))} |"),
        (f"| 5日平均売買代金（AvgDailyValue5d） "
         f"| {f_num(raw.get('5日平均売買代金') / 1e8 if raw.get('5日平均売買代金') is not None else None, '億円', '{:,.1f}')} "
         f"| - |"),
        "",
    ]

    missing = sorted(set(sd.get("missing") or []))
    if missing:
        lines += [
            f"> 欠損列（推定せず欠損のまま残した）: {', '.join(missing)}",
            "",
        ]
    return lines


# ---------------------------------------------------------------------------
# セクター週次コンテキスト
# ---------------------------------------------------------------------------

def load_sector_context(sector_name: str) -> str:
    """sector_weekly.parquet から該当セクターの行を整形して返す。"""
    if not SECTOR_WEEKLY_PATH.exists() or not sector_name:
        return ""
    try:
        import pandas as pd
        df = pd.read_parquet(SECTOR_WEEKLY_PATH)
        row = df[df["Sector17CodeName"] == sector_name]
        if row.empty:
            return ""
        r = row.iloc[0]

        def pct(v):
            return f"{v*100:+.1f}%" if pd.notna(v) else "N/A"

        def num(col, fmt="{:.1f}x"):
            """個別項目の欠損・型不一致でセクション全体を落とさない。"""
            v = r.get(col)
            if v is None or not pd.notna(v):
                return "N/A"
            try:
                return fmt.format(float(v))
            except (TypeError, ValueError):
                return "N/A"

        lines = [
            f"**セクター**: {sector_name}",
            f"| 期間 | リターン |",
            f"|------|---------|",
            f"| 今週（W01） | {pct(r.get('Return_W01'))} |",
            f"| 先週（W02） | {pct(r.get('Return_W02'))} |",
            f"| 3ヶ月 | {pct(r.get('Return_3M'))} |",
            f"| 1年 | {pct(r.get('Return_1Y'))} |",
            f"",
            f"PER（加重平均）: {num('PER_WAvg')}  "
            f"PBR: {num('PBR_WAvg')}  "
            f"ROE: {pct(r.get('ROE_WAvg'))}",
        ]
        return "\n".join(lines)
    except Exception as e:
        print(f"  → 取得失敗: load_sector_context({sector_name}): {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# 直近マクロレポート
# ---------------------------------------------------------------------------

def _macro_report_files() -> list:
    """マクロレポートの実ファイルを新しい順に返す。

    正本は market/daily/macro/（保持5件程度）で、それより古い日付は
    market/archive/macro/ へ退避される（dev/architecture.md §3）。両方を見る。
    """
    files: list = []
    for d in (MARKET_DAILY_MACRO, MARKET_ARCHIVE_MACRO):
        if d.exists():
            files += list(d.glob("*.md"))
    # 同一日が daily と archive の両方にある場合は daily 側を優先する。
    by_stem: dict = {}
    for f in files:
        prev = by_stem.get(f.stem)
        if prev is None or str(MARKET_DAILY_MACRO) in str(f.parent):
            by_stem[f.stem] = f
    return sorted(by_stem.values(), key=lambda f: f.stem, reverse=True)


def load_macro_context() -> str:
    """最新のマクロレポートを1件読み込む。

    従来は research/markets/ を見ていたが、そこにはマクロレポートではなく
    バックテスト等の別文書が入っており、分析対象と無関係な文書を data.md へ
    投入していた（PM 2026-09-06 指摘）。正しい参照先である
    market/daily/macro/（最新）と market/archive/macro/（過去日）へ変更する。
    """
    files = _macro_report_files()
    if not files:
        return ""
    text = files[0].read_text(encoding="utf-8")
    return f"[マクロレポート: {files[0].stem}]\n\n{text[:2000]}"


def _market_report_excerpt(day, code4: str, sector_name: str = "",
                           max_paras: int = 2, max_chars: int = 500) -> list:
    """指定日のマクロ・動意レポートから、当該銘柄またはセクターに触れた段落だけを抜き出す。

    全文を入れると data.md が肥大するため、銘柄コード・セクター名を含む段落に絞る。
    daily（新しい日）と archive（古い日）の両方を探す。見つからなければ空リストを返す。
    """
    stem = day.isoformat()
    cands: list = []
    for d in (MARKET_DAILY_MACRO, MARKET_ARCHIVE_MACRO,
              MARKET_DAILY_MOVERS, MARKET_ARCHIVE_MOVERS):
        if d.exists():
            cands += sorted(d.glob(stem + "*.md"))
    keys = [k for k in (code4, sector_name) if k]
    if not keys:
        return []
    out: list = []
    seen: set = set()
    used = 0
    for f in cands:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para or para.startswith("#"):
                continue
            if not any(k in para for k in keys):
                continue
            para = re.sub(r"\s+", " ", para)
            # 銘柄コードを含む文だけに絞る。動意レポートの段落は複数銘柄の解説が
            # 連なっており、全文を入れると当該銘柄と無関係な記述まで混ざる。
            sents = [t for t in re.split(r"(?<=。)", para) if any(k in t for k in keys)]
            if sents:
                para = "".join(sents)
            para = para[:220]
            # 動意レポートは prime / weekly / weekly_prime に同じ段落が重複して載る。
            # 先頭60字で重複判定し、同じ内容を二重に入れない。
            sig = para[:60]
            if sig in seen:
                continue
            seen.add(sig)
            if used + len(para) > max_chars:
                break
            out.append("- （" + f.stem + "）" + para)
            used += len(para)
            if len(out) >= max_paras:
                return out
    return out


# ---------------------------------------------------------------------------
# 過去 Deep Dive・Perplexity レポート
# ---------------------------------------------------------------------------

def load_past_research(code4: str) -> str:
    """research/stocks/ から過去の deepdive / perplexity / 分析レポートを各直近2件読み込む。
    フラット形式（{code4}_*.md）とコード別フォルダ（{code4}/YYYY-MM-DD.md）の両方を参照する。
    """
    if not OUTPUT_DIR.exists():
        return ""
    snippets = []

    # フラット形式の過去 Deep Dive データ（_data.md 以外）
    deepdive_files = sorted(
        [f for f in OUTPUT_DIR.glob(f"{code4}_*.md")
         if any(k in f.stem for k in ("deepdive", "report")) and "_data" not in f.stem],
        key=lambda f: f.stem, reverse=True
    )[:2]
    for f in deepdive_files:
        text = f.read_text(encoding="utf-8")[:2000]
        snippets.append(f"[過去Deep Dive: {f.stem}]\n\n{text}")

    # コード別フォルダ形式の分析レポート（research/stocks/{code4}/*.md）
    code_dir = OUTPUT_DIR / code4
    if code_dir.exists():
        report_files = sorted(
            [f for f in code_dir.glob("*.md") if "archive" not in str(f)],
            key=lambda f: f.stem, reverse=True
        )[:2]
        for f in report_files:
            text = f.read_text(encoding="utf-8")[:2000]
            snippets.append(f"[過去分析レポート: {f.stem}]\n\n{text}")

    # Perplexity 調査ファイル（フラット・コード別フォルダ両方）
    perplexity_files = sorted(
        [f for f in OUTPUT_DIR.glob(f"{code4}_*.md") if "perplexity" in f.stem],
        key=lambda f: f.stem, reverse=True
    )[:2]
    if code_dir.exists():
        perplexity_files += sorted(
            [f for f in code_dir.glob("*perplexity*.md")],
            key=lambda f: f.stem, reverse=True
        )[:2]
    for f in perplexity_files[:2]:
        text = f.read_text(encoding="utf-8")[:2000]
        snippets.append(f"[過去Perplexity調査: {f.stem}]\n\n{text}")

    return "\n\n---\n\n".join(snippets)


# ---------------------------------------------------------------------------
# Yahoo 掲示板取得
# ---------------------------------------------------------------------------

def _is_valid_bbs_post(body: str) -> bool:
    """掲示板の1投稿がセンチメント分析に使えるかを判定する。

    - _BBS_NOISE: サイト UI・宣伝・定型文などのノイズを除外
    - _BBS_QUOTE: 引用（>>123）だけで本文のない投稿を除外
    - _BBS_POST_LIKE: 日本語の投稿らしさ（句読点・語尾）がない断片を除外
    """
    if not body:
        return False
    # 引用（>>123）を除いた実本文で判定する。
    # 掲示板の本文は空白で1行に潰れて取得されるため、引用行ごと落とすと本文まで
    # 消えてしまう。引用参照だけをその場で除去し、残りを実本文として扱う。
    # 引用行（>>123）だけで本文のない投稿を除外する。引用参照を取り除いた残りが
    # 空になる行を「引用のみの行」とみなす（本文は空白で1行に潰れて取得されるため、
    # 行頭一致だけで判定すると引用付きの通常投稿まで落ちる）。
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if lines and all(
        _BBS_QUOTE.match(ln) and not _BBS_QUOTE_REF.sub(" ", ln).strip()
        for ln in lines
    ):
        return False
    body_wo_quote = _BBS_QUOTE_REF.sub(" ", body).strip()
    if len(body_wo_quote) < 10:
        return False
    if _BBS_NOISE.search(body_wo_quote):
        return False
    if not _BBS_POST_LIKE.search(body_wo_quote):
        return False
    return True


def _parse_bbs_datetime(text: str, no: str = ""):
    """掲示板の投稿テキストから投稿日時を取り出す。

    Yahoo 掲示板の投稿は「ユーザー名 No.XXXXXXX 2026/9/6 16:48 報告 本文 …」の形で
    1行に潰れて取得される。No. の直後に来る日時を投稿日時として拾う。
    年が省略される表記（「9/6 16:48」）にも備え、その場合は「今日を超えない直近の
    その月日」として年を補う。

    Returns:
        (datetime|None, bool)  bool は年を推定で補ったかどうか。
        解釈できなければ (None, False) を返す（推定で埋めない）。
    """
    s = text or ""
    if no:
        m = re.search(r"No\.\s*" + re.escape(str(no)) + r"\s+([^報]{0,24}?)報告", s)
        head = m.group(1) if m else s[:200]
    else:
        head = s[:200]

    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", head) or \
        re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", s[:200])
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5))), False
        except ValueError:
            return None, False

    m = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", head) or \
        re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", s[:200])
    if m:
        today = date.today()
        for yr in (today.year, today.year - 1):
            try:
                dt = datetime(yr, int(m.group(1)), int(m.group(2)),
                              int(m.group(3)), int(m.group(4)))
            except ValueError:
                continue
            if dt.date() <= today:
                return dt, True
    return None, False


def _extract_bbs_posts_from_soup(soup, seen: set) -> list:
    """BeautifulSoup 済みのページから投稿（本文＋投稿日時）を取り出す。

    HTML構造（2026-09時点）:
      <article> ユーザー名 No.XXXXX 2026/9/6 16:48 報告 本文 返信 投資の参考…</article>
    """
    posts: list = []
    for article in soup.find_all("article"):
        text = article.get_text(" ", strip=True)
        if not text or len(text) < 30:
            continue
        no_m = re.search(r"No\.\s*(\d+)", text)
        no = no_m.group(1) if no_m else ""
        body_m = re.search(r"報告\s+(.+?)\s+(?:返信|投資の参考)", text)
        body = body_m.group(1).strip() if body_m else text[:200]
        if not _is_valid_bbs_post(body):
            continue
        key = no or body[:50]
        if key in seen:
            continue
        seen.add(key)
        dt, estimated = _parse_bbs_datetime(text, no)
        posts.append({"no": no, "body": body, "dt": dt, "date_estimated": estimated})
    return posts


def _fetch_bbs_post_by_no(session, code4: str, no: int):
    """投稿単票 /quote/{code}.T/forum/{No} を1件取得する。

    一覧ページは最新約70件しか返さずページング用のクエリも存在しない（実測 2026-09-06）
    ため、過去へ遡るにはこの単票 URL を使う。投稿 No は時系列に単調増加する。

    Returns:
        {"no","body","dt","date_estimated"} または None。
    """
    from bs4 import BeautifulSoup
    url = f"https://finance.yahoo.co.jp/quote/{code4}.T/forum/{no}"
    soup = None
    for attempt in range(2):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                break
            if r.status_code in (404, 410):
                return None      # 欠番・削除済み。再試行しても意味がない。
        except Exception:
            pass
        time.sleep(BBS_DETAIL_SLEEP * 4)
    if soup is None:
        return None
    for article in soup.find_all("article"):
        text = article.get_text(" ", strip=True)
        if f"No. {no}" not in text and f"No.{no}" not in text:
            continue
        body_m = re.search(r"報告\s+(.+?)\s+(?:返信|投資の参考)", text)
        body = body_m.group(1).strip() if body_m else ""
        dt, estimated = _parse_bbs_datetime(text, str(no))
        return {"no": str(no), "body": body, "dt": dt, "date_estimated": estimated}
    return None


def _bbs_find_first_no_on_or_after(session, code4: str, target_day, lo: int, hi: int,
                                   budget: dict):
    """投稿 No の二分探索で「target_day 以降に投稿された最小の No」を返す。

    投稿 No が時系列に単調増加することを利用する。1銘柄あたりのリクエストは
    log2(投稿総数) 回程度（実測 285A で 21 回）。budget["left"] を消費し、
    予算切れなら None を返して run を止めない。
    """
    found = None
    while lo < hi and budget.get("left", 0) > 0:
        mid = (lo + hi) // 2
        budget["left"] -= 1
        rec = _fetch_bbs_post_by_no(session, code4, mid)
        time.sleep(BBS_DETAIL_SLEEP)
        if rec is None or rec.get("dt") is None:
            # 欠番・削除済みの投稿。1つ上へずらして探索を続ける。
            lo = mid + 1
            continue
        if rec["dt"].date() < target_day:
            lo = mid + 1
        else:
            hi = mid
            found = mid
    return found


def fetch_bbs_for_deep_dive(code4: str, target_days: list | None = None) -> dict:
    """Yahoo掲示板から投稿を投稿日時つきで取得する。

    従来は一覧1ページ・最新30件・日付なしだった。それでは過去日（例: 8/7）の投稿を
    参照できず、9月の投稿を8月の値動きの説明に使う時点の取り違えが起きる
    （PM 2026-09-06 指摘）。そこで:
      1. 一覧ページの投稿を投稿日時つきで取得する
      2. target_days（反応スコア対象日）が指定されたら、投稿 No の二分探索で
         その日の境界を特定し、前後1日の投稿を少数だけ拾う
    無限に遡らないよう BBS_LOOKBACK_DAYS・BBS_MAX_DETAIL_FETCHES で上限を置く。

    Args:
        code4: 証券コード
        target_days: 反応スコア対象日（date のリスト）。None なら遡り取得はしない。

    Returns:
        {"sentiment": str, "posts": [{"no","body","dt","date_estimated"}],
         "day_posts": {isoformat日付: [投稿…]}, "oldest": date|None,
         "lookback_note": str, "error": str}
    """
    from bs4 import BeautifulSoup
    url = f"https://finance.yahoo.co.jp/quote/{code4}.T/forum"
    session = requests.Session()
    session.headers.update(_YAHOO_HEADERS)

    # 一覧ページは短時間に叩くと 500 を返すことがある（実測 2026-09-06）。
    # 1回の失敗で掲示板を丸ごと失わないよう、間隔を空けて数回だけ再試行する。
    soup = None
    last_err: object = None
    for attempt in range(BBS_LIST_RETRIES + 1):
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            break
        except Exception as e:
            last_err = e
            if attempt < BBS_LIST_RETRIES:
                time.sleep(BBS_LIST_RETRY_SLEEP * (attempt + 1))
    if soup is None:
        print(f"  → 取得失敗: fetch_bbs_for_deep_dive({code4}): {last_err}", file=sys.stderr)
        return {"sentiment": "", "posts": [], "day_posts": {}, "oldest": None,
                "lookback_note": "", "error": str(last_err)}

    sentiment = ""
    for el in soup.find_all(string=re.compile(r"強く買いたい.*%")):
        m = re.search(r"強く買いたい\s*([\d.]+)%.*?強く売りたい\s*([\d.]+)%", str(el))
        if m:
            sentiment = f"強く買いたい{m.group(1)}% / 強く売りたい{m.group(2)}%"
        break

    seen: set = set()
    recent = _extract_bbs_posts_from_soup(soup, seen)
    recent.sort(key=lambda p: (p["dt"] is not None, p["dt"] or datetime.min), reverse=True)
    posts = recent[:BBS_MAX_POSTS]

    # 一覧ページは1ページのみ（?page= 等のページングは無効。実測 2026-09-06）。
    # BBS_MAX_PAGES は将来ページングが復活した場合の上限として保持する。
    notes: list = []
    day_posts: dict = {}

    # 対象日の周辺投稿を拾う（反応スコア対象日ごとに前後1日）
    budget = {"left": BBS_MAX_DETAIL_FETCHES}
    max_no = 0
    for p in posts:
        try:
            max_no = max(max_no, int(p["no"]))
        except (TypeError, ValueError):
            continue

    lookback_floor = date.today() - timedelta(days=BBS_LOOKBACK_DAYS)
    if target_days and max_no:
        # 対象日ごとに「前日 → 当日 → 翌日」の3日を1回の前進走査でまとめて拾う。
        # 二分探索は1対象日につき1回だけ行う（対象日×3回行うとリクエストが3倍になる）。
        for tday in sorted(set(target_days)):
            span = [tday - timedelta(days=1), tday, tday + timedelta(days=1)]
            span = [d for d in span if lookback_floor <= d <= date.today()]
            if not span:
                notes.append(
                    f"{tday.isoformat()} は遡り上限（{BBS_LOOKBACK_DAYS}日）より古いため取得していません")
                continue
            if budget["left"] <= 0:
                notes.append("リクエスト上限に達したため一部の対象日は取得していません")
                break
            start_no = _bbs_find_first_no_on_or_after(
                session, code4, span[0], 1, max_no, budget
            )
            if start_no is None:
                notes.append(f"{tday.isoformat()} 前後の投稿境界を特定できませんでした")
                continue

            last_day = span[-1]
            per_day: dict = {d: 0 for d in span}
            no = start_no
            while budget["left"] > 0 and no <= max_no:
                # その3日分がすべて上限に達したら打ち切る。
                if all(per_day[d] >= BBS_DAY_WINDOW_POSTS for d in span):
                    break
                budget["left"] -= 1
                rec = _fetch_bbs_post_by_no(session, code4, no)
                time.sleep(BBS_DETAIL_SLEEP)
                no += 1
                if rec is None or rec.get("dt") is None:
                    continue
                d = rec["dt"].date()
                if d > last_day:
                    break
                if d not in per_day or per_day[d] >= BBS_DAY_WINDOW_POSTS:
                    continue
                if not _is_valid_bbs_post(rec.get("body", "")):
                    continue
                if rec["no"] in seen:
                    continue
                seen.add(rec["no"])
                day_posts.setdefault(d.isoformat(), []).append(rec)
                per_day[d] += 1

            got = sum(per_day.values())
            if not got:
                notes.append(f"{tday.isoformat()} 前後の有効な投稿を取得できませんでした")

    dated = [p["dt"].date() for p in posts if p.get("dt")] + \
            [p["dt"].date() for v in day_posts.values() for p in v if p.get("dt")]
    oldest = min(dated) if dated else None

    return {"sentiment": sentiment, "posts": posts, "day_posts": day_posts,
            "oldest": oldest, "lookback_note": "／".join(notes[:5]), "error": ""}

# ---------------------------------------------------------------------------
# EDINET DB データ整形ヘルパー
# ---------------------------------------------------------------------------

def _n(val, fmt="{:,.0f}", unit="百万円", fallback="N/A") -> str:
    """数値を整形。get_company は円建てなので百万円に変換して表示。"""
    if val is None:
        return fallback
    try:
        v = float(val)
        # 10億以上なら円建てと判断して百万円に変換
        if unit == "百万円" and abs(v) >= 1_000_000:
            v = v / 1_000_000
        return fmt.format(v) + (f" {unit}" if unit else "")
    except (TypeError, ValueError):
        return str(val)


def _pct(val, fallback="N/A") -> str:
    """パーセント整形。小数（0.085）と整数（8.5）両方に対応。"""
    if val is None:
        return fallback
    try:
        v = float(val)
        # 小数形式（-1〜1）なら×100して%表示
        if -2.0 < v < 2.0 and v != 0:
            v = v * 100
        return f"{v:.1f}%"
    except (TypeError, ValueError):
        return str(val)


def _ratio_pct(val, fallback="N/A") -> str:
    """比率キー（equityRatioOfficial=0.421 等の小数）を % 表示に変換する。"""
    if val is None:
        return fallback
    try:
        return f"{float(val) * 100:.1f} %"
    except (TypeError, ValueError):
        return str(val)


def _fmt_financials_table(financials: list[dict]) -> str:
    """get_financials の配列から財務時系列テーブルを生成。

    キー名は EDINET DB get_financials の実キーに合わせる
    （自己資本比率=equityRatioOfficial / ROE=roeOfficial /
      CF=cfOperating・cfInvesting・cfFinancing）。
    """
    if not financials:
        return "*財務時系列データなし*"

    rows = sorted(financials, key=lambda x: x.get("fiscalYear", 0))
    years = [str(r.get("fiscalYear", "?")) for r in rows]

    def row_line(label: str, key: str, fmt="{:,.0f}", unit="百万円") -> str:
        vals = []
        for r in rows:
            v = r.get(key)
            vals.append(_n(v, fmt, unit) if v is not None else "N/A")
        return f"| {label} | " + " | ".join(vals) + " |"

    def ratio_line(label: str, key: str) -> str:
        vals = [_ratio_pct(r.get(key)) for r in rows]
        return f"| {label} | " + " | ".join(vals) + " |"

    def yen_line(label: str, key: str) -> str:
        """円建てが確定している項目は必ず百万円へ換算する（_n のヒューリスティック回避）。"""
        vals = [_yen_to_mn(r.get(key)) for r in rows]
        return f"| {label} | " + " | ".join(vals) + " |"

    header = "| 指標 | " + " | ".join(years) + " |"
    sep    = "|------|" + "------|" * len(years)

    lines = [header, sep]
    lines.append(yen_line("売上高", "revenue"))
    lines.append(yen_line("売上総利益", "grossProfit"))
    lines.append(yen_line("営業利益", "operatingIncome"))
    lines.append(yen_line("経常利益", "ordinaryIncome"))
    lines.append(yen_line("純利益", "netIncome"))
    lines.append(yen_line("総資産", "totalAssets"))
    lines.append(yen_line("純資産", "netAssets"))
    lines.append(yen_line("自己資本", "shareholdersEquity"))
    lines.append(ratio_line("自己資本比率", "equityRatioOfficial"))
    lines.append(ratio_line("ROE",          "roeOfficial"))
    lines.append(row_line("EPS",          "eps",             "{:.1f}", "円"))
    lines.append(row_line("BPS",          "bps",             "{:.1f}", "円"))
    lines.append(row_line("PER",          "per",             "{:.1f}", "x"))
    lines.append(row_line("PBR",          "pbr",             "{:.1f}", "x"))
    lines.append(yen_line("営業CF", "cfOperating"))
    lines.append(yen_line("投資CF", "cfInvesting"))
    lines.append(yen_line("財務CF", "cfFinancing"))
    lines.append(yen_line("現預金", "cash"))
    lines.append(yen_line("設備投資", "capex"))
    lines.append(yen_line("減価償却費", "depreciation"))
    lines.append(yen_line("有利子負債(短期借入)", "shortTermLoans"))
    lines.append(yen_line("社債", "bondsPayable"))
    lines.append(yen_line("利益剰余金", "retainedEarnings"))
    lines.append(row_line("1株配当",      "dividendPerShare", "{:.1f}", "円"))
    lines.append(row_line("配当性向",     "payoutRatio",      "{:.1f}", "%"))
    lines.append(row_line("従業員数",     "numEmployees",     "{:,.0f}", "人"))
    return "\n".join(lines)


# get_text_blocks の content は「## 和文見出し (English Name)」で区切られた Markdown。
# 和文見出しは年度・企業で揺れる（例: 事業方針／経営方針、経営上の重要な契約等）ため、
# 括弧内の英語名で識別する。(出力ラベル, 英語名の識別キーワード, 文字数上限)
_TEXT_BLOCK_SECTIONS = [
    ("事業の内容",                 "business overview",              5000),
    ("MD&A（経営者による分析）",   "management discussion",          5000),
    ("事業等のリスク",             "business risks",                 4000),
    ("経営方針・経営環境",         "management policies",            3000),
    ("研究開発活動",               "research & development",         2000),
    ("関係会社の状況",             "affiliated entities",            1500),
    ("経営上の重要な契約",         "critical contracts",             1500),
    ("従業員の状況",               "employees",                       800),
]

_TEXT_BLOCK_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.M)


def _split_text_block_sections(content: str) -> dict[str, str]:
    """content を「## 見出し」で分割し、英語名（小文字）→本文 の辞書を返す。

    英語名が取れない見出しは和文見出しをそのままキーにする。
    """
    sections: dict[str, str] = {}
    matches = list(_TEXT_BLOCK_HEADING.finditer(content))
    for i, m in enumerate(matches):
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body  = content[start:end].strip()
        if not body:
            continue
        title = m.group("title").strip()
        en_m  = re.search(r"\(([^()]*)\)\s*$", title)
        key   = (en_m.group(1) if en_m else title).strip().lower()
        if key and key not in sections:
            sections[key] = body
    return sections


def _fmt_text_blocks(text_blocks: dict) -> list[str]:
    """get_text_blocks のレスポンス（content=Markdown）からセクション一覧を生成。

    見出しが見つからないセクションは、見出しごと出力しない
    （「*取得なし*」という未取得表記を誌面へ出さないため）。
    """
    content = str(text_blocks.get("content", "") or "")
    if not content.strip():
        print("  → 取得失敗: _fmt_text_blocks: content が空です", file=sys.stderr)
        return []

    sections = _split_text_block_sections(content)

    lines: list[str] = []
    sections_count = text_blocks.get("sections_count")
    full_text_url  = text_blocks.get("full_text_url", "")
    meta_bits = []
    if sections_count:
        meta_bits.append(f"有価証券報告書セクション数: {sections_count}")
    if text_blocks.get("fiscal_year"):
        meta_bits.append(f"対象年度: {text_blocks['fiscal_year']}")
    if full_text_url:
        meta_bits.append(f"全文: {full_text_url}")
    if meta_bits:
        lines += [f"> {'  /  '.join(meta_bits)}", ""]

    emitted = 0
    for label, en_key, limit in _TEXT_BLOCK_SECTIONS:
        body = ""
        for key, val in sections.items():
            if en_key in key:
                body = val
                break
        if not body:
            continue  # 見出しごと出力しない
        emitted += 1
        lines += [f"## {label}", "", body[:limit], ""]

    if emitted == 0:
        # セクション分割に失敗した場合のみ、content 先頭を丸ごと提示する
        print("  → 取得失敗: _fmt_text_blocks: 既知の見出しに一致するセクションがありません",
              file=sys.stderr)
        lines += ["## 有価証券報告書（抜粋）", "", content[:8000], ""]

    return lines


def fetch_segments(client, edinet_code: str) -> list[dict]:
    """EDINET DB get_segments からセグメント別の売上・利益を取得する。

    EdinetDBClient に専用メソッドがあればそれを使い、なければ MCP を直接呼ぶ。
    レスポンスは {"segments": [...]} 形式（fiscalYear / segmentName / segmentType /
    revenue / operatingIncome / revenueShare / oiMargin / revenueYoy / oiYoy）。
    """
    try:
        if hasattr(client, "get_segments"):
            data = client.get_segments(edinet_code)
        else:
            data = client._call("get_segments", {"edinet_code": edinet_code})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("segments", data.get("data", []))
        return []
    except Exception as e:
        print(f"  → 取得失敗: fetch_segments({edinet_code}): {e}", file=sys.stderr)
        return []


def fetch_major_shareholders(client, edinet_code: str, max_periods: int = 2) -> list[dict]:
    """EDINET DB get_major_shareholders から「大株主の状況」（上位10名）を取得する。

    ⚠️ 大量保有報告書（get_shareholders）とは別物。大量保有報告書は提出時点の
    株数・比率が固定され、その後の発行済株式数の変動（合併・増資等）を反映しない。
    比率の正本は必ず有価証券報告書・半期報告書のこの「大株主の状況」を使う。

    レスポンスは {"majorShareholders": [...]} 形式（fiscalYear / quarter / period /
    docID / rank / holderName / holderType / holderAddress / sharesHeld / ratioPct）。
    quarter=None は有報（通期）スナップショット、quarter=2 は半期報告書（H1）。

    Returns:
        直近 max_periods 期分（新しい順）の行のみを返す。取得できなければ空リスト。
        期の新しさは (fiscalYear, quarter) の降順で判定する（同一 fiscalYear に
        有報と半期報告書の両方が存在しうるため、fiscalYear だけでは決まらない）。
    """
    try:
        if hasattr(client, "get_major_shareholders"):
            data = client.get_major_shareholders(edinet_code, period="all")
        else:
            data = client._call("get_major_shareholders",
                                {"edinet_code": edinet_code, "period": "all"})
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("majorShareholders", data.get("data", []))
        else:
            return []
        rows = [r for r in rows if isinstance(r, dict)]
        if not rows:
            return []

        # (fiscalYear, quarter) で期を束ねる。quarter=None（有報）は同一 fiscalYear の
        # 半期報告書（quarter=2）より後の基準日なので、None を大きい値として扱う。
        def period_key(r: dict) -> tuple:
            fy = r.get("fiscalYear")
            q  = r.get("quarter")
            return (fy if fy is not None else -1, 99 if q is None else q)

        periods = sorted({period_key(r) for r in rows}, reverse=True)[:max_periods]
        keep = set(periods)
        picked = [r for r in rows if period_key(r) in keep]
        picked.sort(key=lambda r: (period_key(r), -(r.get("rank") or 0)), reverse=True)
        return picked
    except Exception as e:
        print(f"  → 取得失敗: fetch_major_shareholders({edinet_code}): {e}", file=sys.stderr)
        return []


def fetch_directors(client, edinet_code: str) -> list[dict]:
    """EDINET DB get_directors から役員一覧を取得する（株主名との突合用）。

    レスポンスは {"directors": [...]} 形式（fiscalYear / officerName /
    officialTitle / dateOfBirth / sharesHeld / termOfOffice）。
    sharesHeld=None は有報原文の「－」（未開示または非保有・曖昧）、
    sharesHeld=0 は明示的なゼロ開示であり、両者は区別される（補完しない）。

    Returns:
        最新年度の役員行のみ。取得できなければ空リスト。
    """
    try:
        if hasattr(client, "get_directors"):
            data = client.get_directors(edinet_code)
        else:
            data = client._call("get_directors", {"edinet_code": edinet_code})
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("directors", data.get("data", []))
        else:
            return []
        rows = [r for r in rows if isinstance(r, dict)]
        if not rows:
            return []
        years = [r.get("fiscalYear") for r in rows if r.get("fiscalYear") is not None]
        if years:
            latest = max(years)
            rows = [r for r in rows if r.get("fiscalYear") == latest]
        return rows
    except Exception as e:
        print(f"  → 取得失敗: fetch_directors({edinet_code}): {e}", file=sys.stderr)
        return []


def fetch_ownership_relations(client, edinet_code: str) -> dict:
    """資本関係・取引先の材料を取得する（株主の「会社との関係」判定用）。

    - get_parent_companies: 親会社・その他関係会社（self_reported / cross_reported）
    - get_subsidiaries:     関係会社の状況（連結子会社・持分法適用関連会社等）
    - get_main_customers:   主要販売先（10%超の顧客。無い企業は空で正常）

    いずれか1つが失敗しても他は返す（個別に try で囲む）。取れないものは
    空リストのまま残し、推定で埋めない。
    """
    result: dict = {"parents": [], "subsidiaries": [], "customers": []}

    def _rows(data, *keys) -> list[dict]:
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, list):
                    return [r for r in v if isinstance(r, dict)]
        return []

    for label, tool, keys in [
        ("parents",      "get_parent_companies", ("data", "parents")),
        ("subsidiaries", "get_subsidiaries",     ("data", "subsidiaries")),
        ("customers",    "get_main_customers",   ("mainCustomers", "data", "customers")),
    ]:
        try:
            if hasattr(client, tool):
                data = getattr(client, tool)(edinet_code)
            else:
                data = client._call(tool, {"edinet_code": edinet_code})
            result[label] = _rows(data, *keys)
        except Exception as e:
            print(f"  → 取得失敗: fetch_ownership_relations.{tool}({edinet_code}): {e}",
                  file=sys.stderr)
    return result


def fetch_large_holdings(client, edinet_code: str) -> list[dict]:
    """EDINET DB get_shareholders から大量保有報告書（5%超）の保有目的を取得する。

    ⚠️ 比率の正本にしてはならない。大量保有報告書は提出時点で数値が固定され、
    その後の発行済株式数の変動（合併・増資等）を反映しないため、
    比率が実態から大きく乖離することがある。ここでは「保有目的」
    （安定株主・純投資・経営参画等）を読むためだけに取得する。

    レスポンスは {"filings": [{..., "holders": [...]}]} 形式。
    holding_ratio は小数（0.0708 = 7.08%）で、ratioPct（%）とはスケールが違う。

    Returns:
        提出日・提出者・保有者・保有目的を平坦化した行のリスト。
    """
    try:
        if hasattr(client, "get_shareholders"):
            data = client.get_shareholders(edinet_code)
        else:
            data = client._call("get_shareholders", {"edinet_code": edinet_code})
        if not isinstance(data, dict):
            return []
        rows: list[dict] = []
        for f in data.get("filings", []):
            if not isinstance(f, dict):
                continue
            for h in f.get("holders", []):
                if not isinstance(h, dict):
                    continue
                rows.append({
                    "submit_date":      f.get("submit_date"),
                    "doc_type":         f.get("doc_type"),
                    "is_change_report": f.get("is_change_report"),
                    "filer_name":       f.get("filer_name"),
                    "holder_name":      h.get("holder_name"),
                    "holding_ratio":    h.get("holding_ratio"),
                    "shares_held":      h.get("shares_held"),
                    "purpose":          h.get("purpose"),
                })
        rows.sort(key=lambda r: (r.get("submit_date") or ""), reverse=True)
        return rows
    except Exception as e:
        print(f"  → 取得失敗: fetch_large_holdings({edinet_code}): {e}", file=sys.stderr)
        return []


def _yen_to_mn(val, fallback="N/A") -> str:
    """円建ての実数を必ず百万円へ換算して表示する。

    _n() の「1,000,000 以上なら円建て」ヒューリスティックは、セグメントの
    小さな損益（例: -515,000 円）を円のまま百万円と表示してしまうため、
    単位が円と確定している用途ではこちらを使う。
    """
    if val is None:
        return fallback
    try:
        return f"{float(val) / 1_000_000:,.0f} 百万円"
    except (TypeError, ValueError):
        return str(val)


def _fmt_segments(segments: list[dict], max_years: int = 3) -> list[str]:
    """get_segments のレスポンスからセグメント別テーブルを生成。

    取得できなかった場合は空リストを返し、セクションごと出力しない。
    """
    if not segments:
        return []

    years = sorted({s.get("fiscalYear") for s in segments if s.get("fiscalYear")},
                   reverse=True)[:max_years]
    if not years:
        return []

    lines = ["## セグメント情報（EDINET DB・有報セグメント注記）", ""]
    for fy in years:
        rows = [s for s in segments if s.get("fiscalYear") == fy]
        rows.sort(key=lambda s: s.get("revenue") or 0, reverse=True)
        lines += [
            f"### FY{fy}",
            "",
            "| セグメント | 区分 | 売上高 | 構成比 | 営業利益 | 営業利益率 | 売上YoY | 営業利益YoY |",
            "|------------|------|--------|--------|----------|-----------|---------|-------------|",
        ]
        for s in rows:
            name = s.get("segmentName") or s.get("segmentNameEn") or "?"
            lines.append(
                f"| {name} | {s.get('segmentType', '')} | "
                f"{_yen_to_mn(s.get('revenue'))} | {_ratio_pct(s.get('revenueShare'))} | "
                f"{_yen_to_mn(s.get('operatingIncome'))} | {_ratio_pct(s.get('oiMargin'))} | "
                f"{_ratio_pct(s.get('revenueYoy'))} | {_ratio_pct(s.get('oiYoy'))} |"
            )
        lines.append("")
    return lines


def _shares(val, fallback="N/A") -> str:
    """保有株数を整形する。単位は提出会社により株／千株が揺れるため換算しない。"""
    if val is None:
        return fallback
    try:
        return f"{float(val):,.0f}"
    except (TypeError, ValueError):
        return str(val)


def _pct_pt(val, fallback="N/A") -> str:
    """既に % スケールで来る比率（ratioPct=29.2）をそのまま % 表示する。"""
    if val is None:
        return fallback
    try:
        return f"{float(val):.2f}%"
    except (TypeError, ValueError):
        return str(val)


def _ratio_frac_pct(val, fallback="N/A") -> str:
    """小数スケールの比率（holding_ratio=0.5487）を % 表示に変換する。"""
    if val is None:
        return fallback
    try:
        return f"{float(val) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(val)


_PERIOD_LABEL = {"annual": "有価証券報告書（通期）", "interim_h1": "半期報告書（第2四半期）"}


def _period_heading(fy, quarter, period) -> str:
    """大株主セクションの見出し。どの報告書のいつ時点かを必ず明記する。"""
    doc = _PERIOD_LABEL.get(period)
    if not doc:
        doc = "半期報告書（第2四半期）" if quarter == 2 else "有価証券報告書（通期）"
    fy_s = f"FY{fy}" if fy is not None else "年度不明"
    return f"{fy_s} {doc}"


def _norm_name(name: str) -> str:
    """株主名・役員名の突合用に空白（全角含む）を除去して正規化する。"""
    return re.sub(r"[\s　]+", "", str(name or ""))


def _fmt_major_shareholders(
    major_shareholders: list[dict],
    directors: list[dict] | None = None,
    relations: dict | None = None,
    large_holdings: list[dict] | None = None,
) -> list[str]:
    """「大株主の状況」（有報・半期報告書）と関係判定の材料をセクション化する。

    取得できなかった場合は空リストを返し、セクションごと出力しない。
    比率の正本は有報・半期報告書であり、大量保有報告書は保有目的の参考としてのみ
    併記する（同じ数値を比率として使わせないための注記を必ず添える）。
    """
    if not major_shareholders:
        return []

    directors = directors or []
    relations = relations or {}
    large_holdings = large_holdings or []

    # 役員名 → 役職。株主名と突き合わせて「役員かどうか」を機械的に示す。
    director_by_name = {
        _norm_name(d.get("officerName")): d
        for d in directors if d.get("officerName")
    }
    # 大量保有報告書の保有目的（比率ではなく目的だけを引く）
    purpose_by_name: dict[str, str] = {}
    for h in large_holdings:
        key = _norm_name(h.get("holder_name"))
        if key and key not in purpose_by_name and h.get("purpose"):
            purpose_by_name[key] = str(h["purpose"]).strip()
    # 資本関係のある法人名（親会社・関係会社）
    related_corp: dict[str, str] = {}
    for p in relations.get("parents", []):
        nm = _norm_name(p.get("reportingCompanyName"))
        if nm:
            related_corp.setdefault(nm, "自社を関係会社として報告している会社")
    for s in relations.get("subsidiaries", []):
        nm = _norm_name(s.get("subsidiaryName") or s.get("subsidiaryResolvedName"))
        if nm:
            related_corp.setdefault(nm, str(s.get("relationType") or "関係会社"))
    # 主要販売先
    customer_names = {
        _norm_name(c.get("customerName")) for c in relations.get("customers", [])
        if c.get("customerName")
    }

    def relation_of(holder_name: str) -> str:
        """株主名から会社との関係を判定する。判定材料がなければ空欄のまま。"""
        key = _norm_name(holder_name)
        bits: list[str] = []
        d = director_by_name.get(key)
        if d:
            bits.append(str(d.get("officialTitle") or "役員").strip())
        for corp_name, rel in related_corp.items():
            if corp_name and (corp_name in key or key in corp_name):
                bits.append(rel)
                break
        if key in customer_names:
            bits.append("主要販売先")
        purpose = purpose_by_name.get(key)
        if purpose:
            bits.append(f"保有目的: {purpose}")
        return " / ".join(bits)

    # 期ごとに束ねる（新しい順）。
    def period_key(r: dict) -> tuple:
        fy = r.get("fiscalYear")
        q  = r.get("quarter")
        return (fy if fy is not None else -1, 99 if q is None else q)

    periods = sorted({period_key(r) for r in major_shareholders}, reverse=True)

    lines = [
        "## 大株主の状況（EDINET DB・有価証券報告書／半期報告書）", "",
        "> **出典は有価証券報告書・半期報告書の「大株主の状況」**（上位10名）。"
        "決算期ごとに全体が更新されるため、持株比率はこの数値を正本として使うこと。",
        "> **大量保有報告書（5%超の株主が提出）の比率をここで使ってはならない。**"
        "大量保有報告書は提出時点の株数・比率が固定され、その後の合併・増資による"
        "発行済株式数の変動が反映されないため、時間が経つほど実態から乖離する。",
        "> 保有株数の単位は提出会社により株／千株が揺れる（EDINET DB 注記）。"
        "会社をまたぐ比較には比率（%）を使うこと。",
        "",
    ]

    for fy, q in periods:
        rows = [r for r in major_shareholders if period_key(r) == (fy, q)]
        rows.sort(key=lambda r: r.get("rank") or 999)
        if not rows:
            continue
        head = rows[0]
        # period_key のソート用センチネル（fy=-1 / q=99）を表示へ漏らさない
        quarter = None if q == 99 else q
        fy = head.get("fiscalYear")
        doc_id = head.get("docID") or ""
        lines += [
            f"### 基準: {_period_heading(fy, quarter, head.get('period'))}"
            + (f"（書類ID: {doc_id}）" if doc_id else ""),
            "",
            "| 順位 | 株主名 | 区分 | 保有株数 | 保有比率 | 会社との関係（判定材料） |",
            "|------|--------|------|----------|----------|--------------------------|",
        ]
        for r in rows:
            lines.append(
                f"| {r.get('rank') or ''} | {r.get('holderName') or ''} | "
                f"{r.get('holderType') or ''} | {_shares(r.get('sharesHeld'))} | "
                f"{_pct_pt(r.get('ratioPct'))} | {relation_of(r.get('holderName', ''))} |"
            )
        lines.append("")

    if len(periods) >= 2:
        lines += [
            "> 上の2期は基準日が異なる。比率の差は売買だけでなく発行済株式数の変動"
            "（合併・増資・自己株式の消却等）でも生じるため、"
            "株数と比率の両方を見て増減を判断すること。",
            "",
        ]

    # --- 役員一覧（株主名との突合材料） ---
    if directors:
        fy_d = directors[0].get("fiscalYear")
        lines += [
            f"### 役員一覧（{('FY' + str(fy_d)) if fy_d is not None else '年度不明'}"
            "・有価証券報告書「役員の状況」）",
            "",
            "| 役職 | 氏名 | 保有株数 |",
            "|------|------|----------|",
        ]
        for d in directors:
            lines.append(
                f"| {d.get('officialTitle') or ''} | {d.get('officerName') or ''} | "
                f"{_shares(d.get('sharesHeld'), '未開示')} |"
            )
        lines += [
            "",
            "> 保有株数の「未開示」は有報原文が「－」の行"
            "（未開示か非保有かを区別できない）。ゼロと断定しないこと。",
            "",
        ]

    # --- 資本関係 ---
    parents = relations.get("parents", [])
    subs    = relations.get("subsidiaries", [])
    if parents or subs:
        lines += ["### 資本関係（親会社・関係会社）", ""]
        if parents:
            lines += [
                "| 報告元 | 関係 | 議決権比率 | 出典 |",
                "|--------|------|------------|------|",
            ]
            for p in parents:
                lines.append(
                    f"| {p.get('reportingCompanyName') or ''} | "
                    f"{p.get('relationType') or ''} | "
                    f"{_pct_pt(p.get('votingRightsPct'))} | {p.get('source') or ''} |"
                )
            lines.append("")
        if subs:
            lines += [
                "| 会社名 | 関係区分 | 議決権比率 | 事業内容 |",
                "|--------|----------|------------|----------|",
            ]
            for s in subs:
                lines.append(
                    f"| {s.get('subsidiaryName') or ''} | {s.get('relationType') or ''} | "
                    f"{_pct_pt(s.get('votingRightsPct'))} | "
                    f"{s.get('subsidiaryBusiness') or ''} |"
                )
            lines.append("")

    # --- 主要販売先 ---
    customers = relations.get("customers", [])
    if customers:
        lines += [
            "### 主要販売先（有報「主要な顧客ごとの情報」）", "",
            "| 年度 | 顧客名 | セグメント | 売上高 | 売上構成比 | 確度 |",
            "|------|--------|------------|--------|------------|------|",
        ]
        for c in customers:
            share = c.get("salesSharePct")
            if share is None:
                share = c.get("salesSharePctFilled")
            share_s = f"{float(share):.1f}%" if isinstance(share, (int, float)) else "N/A"
            lines.append(
                f"| {c.get('fiscalYear') or ''} | {c.get('customerName') or ''} | "
                f"{c.get('segment') or ''} | {_yen_to_mn(c.get('salesAmountYen'))} | "
                f"{share_s} | {c.get('confidence') or ''} |"
            )
        lines += [
            "",
            "> 主要販売先が空でも「大口顧客なし」を意味しない"
            "（10%超の顧客がない場合や記載省略の場合がある）。",
            "",
        ]

    # --- 大量保有報告書（保有目的のみ・比率は参考外） ---
    if large_holdings:
        lines += [
            "### 大量保有報告書（保有目的の参考・⚠️ 比率は使用しないこと）", "",
            "| 提出日 | 書類種別 | 提出者 | 保有者 | 提出時点の比率 | 提出時点の株数 | 保有目的 |",
            "|--------|----------|--------|--------|----------------|----------------|----------|",
        ]
        for h in large_holdings:
            lines.append(
                f"| {h.get('submit_date') or ''} | {h.get('doc_type') or ''} | "
                f"{h.get('filer_name') or ''} | {h.get('holder_name') or ''} | "
                f"{_ratio_frac_pct(h.get('holding_ratio'))} | "
                f"{_shares(h.get('shares_held'))} | {h.get('purpose') or ''} |"
            )
        lines += [
            "",
            "> **この表の比率・株数は提出日時点で固定された値であり、現在の持株比率ではない。**"
            "レポート本文の持株比率には必ず上の「大株主の状況」の値を使うこと。"
            "保有目的（安定株主・純投資・経営参画等）を読む目的でのみ参照する。",
            "",
        ]

    return lines


# ---------------------------------------------------------------------------
# 株価時系列（日足 OHLCV・yfinance）
# ---------------------------------------------------------------------------

def fetch_price_history(code4: str, retries: int = 2):
    """yfinance で日足 OHLCV を取得する（HTTP のみ・MCP 非依存・GHA でも動く）。

    fetch_position_quotes.py と同じ取得作法に合わせる。
      - シンボルは ``{code}.T``
      - auto_adjust=False（終値は分割調整済み・配当未調整。TradingView の
        SMA/BB と同じ基準に揃え、配当調整で過去終値がずれるのを防ぐ）

    Returns:
        DatetimeIndex を持つ DataFrame（Open/High/Low/Close/Volume）。
        取得できなければ None を返す（推定・補完は一切しない）。
    """
    try:
        import yfinance as yf
    except Exception as e:
        print(f"  → 取得失敗: yfinance import: {e}", file=sys.stderr)
        return None

    hist = None
    last_err: object = None
    for _ in range(retries + 1):
        try:
            ticker = yf.Ticker(f"{code4}.T")
            hist = ticker.history(period=PRICE_HISTORY_PERIOD, interval="1d",
                                  auto_adjust=False)
            if hist is not None and not hist.empty:
                break
        except Exception as e:
            last_err = e
        time.sleep(0.6)

    if hist is None or hist.empty:
        print(f"  → 取得失敗: fetch_price_history({code4}): {last_err}", file=sys.stderr)
        return None

    cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in hist.columns]
    if "Close" not in cols:
        print(f"  → 取得失敗: fetch_price_history({code4}): Close 列なし", file=sys.stderr)
        return None
    df = hist[cols].dropna(subset=["Close"])
    return df if not df.empty else None


def _levels_from_prices(df) -> dict:
    """日足から水準（直近高値・安値・移動平均・ボリンジャーバンド）を算出する。

    期間が足りず算出できない水準は None のまま残し、推定で埋めない。
    """
    close = df["Close"]
    high  = df["High"] if "High" in df.columns else close
    low   = df["Low"] if "Low" in df.columns else close
    n     = len(close)
    last  = float(close.iloc[-1])

    levels: dict = {}

    # 直近高値・安値
    for win in PRICE_RANGE_WINDOWS:
        if n < win:
            levels[f"{win}日高値"] = None
            levels[f"{win}日安値"] = None
            continue
        levels[f"{win}日高値"] = float(high.iloc[-win:].max())
        levels[f"{win}日安値"] = float(low.iloc[-win:].min())

    # 移動平均
    for win in PRICE_MA_WINDOWS:
        levels[f"{win}日移動平均"] = (
            float(close.rolling(win).mean().iloc[-1]) if n >= win else None
        )

    # ボリンジャーバンド（25日・±2σ / ±3σ）
    bb_win = PRICE_BB_WINDOW
    if n >= bb_win:
        ma_last = float(close.rolling(bb_win).mean().iloc[-1])
        sd_last = float(close.rolling(bb_win).std(ddof=0).iloc[-1])
        for k in (2, 3):
            levels[f"BB+{k}シグマ"] = ma_last + k * sd_last
            levels[f"BB-{k}シグマ"] = ma_last - k * sd_last
    else:
        for k in (2, 3):
            levels[f"BB+{k}シグマ"] = None
            levels[f"BB-{k}シグマ"] = None

    return {"last_close": last, "levels": levels}


def _touch_stats(df, level, horizons=(5, 10)) -> dict:
    """ある価格水準に過去何回到達し、到達後どう動いたかを集計する。

    到達の定義: その日の安値〜高値レンジが水準を跨いだ日（Low <= level <= High）。
    連続到達は1回にまとめる（間に PRICE_TOUCH_GAP_DAYS 営業日以上あけば別回）。
    到達後の値動きは、到達日終値を基準にした N 営業日後終値の騰落率。
    先行きが N 営業日に満たない到達は、その horizon の集計から除外する。
    """
    if level is None or level != level:  # None / NaN
        return {"touches": 0, "reactions": {}}

    high  = df["High"] if "High" in df.columns else df["Close"]
    low   = df["Low"] if "Low" in df.columns else df["Close"]
    close = df["Close"]
    n = len(close)

    touch_idx: list[int] = []
    last_i = None
    for i in range(n):
        h = high.iloc[i]
        l = low.iloc[i]
        if h != h or l != l:
            continue
        if float(l) <= level <= float(h):
            if last_i is None or (i - last_i) >= PRICE_TOUCH_GAP_DAYS:
                touch_idx.append(i)
            last_i = i

    reactions: dict = {}
    for hz in horizons:
        rets = []
        for i in touch_idx:
            j = i + hz
            if j >= n:
                continue
            base = float(close.iloc[i])
            if base == 0:
                continue
            rets.append((float(close.iloc[j]) / base - 1.0) * 100.0)
        if rets:
            rets_sorted = sorted(rets)
            reactions[hz] = {
                "samples": len(rets),
                "avg_pct": sum(rets) / len(rets),
                "median_pct": rets_sorted[len(rets_sorted) // 2],
                "up_ratio_pct": sum(1 for r in rets if r > 0) / len(rets) * 100.0,
                "max_pct": max(rets),
                "min_pct": min(rets),
            }
        else:
            reactions[hz] = None

    return {"touches": len(touch_idx), "reactions": reactions}


def build_price_level_stats(df) -> dict | None:
    """各水準の到達回数と到達後の値動きを集計した dict を返す。

    df が None（取得失敗）なら None を返し、呼び出し側でセクションごと省略する。
    """
    if df is None or len(df) == 0:
        return None
    base = _levels_from_prices(df)
    stats = {}
    for label, level in base["levels"].items():
        stats[label] = {"level": level, **_touch_stats(df, level)}
    return {
        "last_close": base["last_close"],
        "bars": len(df),
        "first_date": df.index[0].strftime("%Y-%m-%d"),
        "last_date": df.index[-1].strftime("%Y-%m-%d"),
        "stats": stats,
    }


def _lv(val, fallback="N/A") -> str:
    """価格水準を円建てで整形する。取れていなければ fallback。"""
    if val is None or val != val:
        return fallback
    try:
        return f"{float(val):,.1f} 円"
    except (TypeError, ValueError):
        return fallback


def _sp(val, fallback="N/A") -> str:
    """騰落率（%）を符号付きで整形する。"""
    if val is None or val != val:
        return fallback
    try:
        return f"{float(val):+.1f}%"
    except (TypeError, ValueError):
        return fallback


def _rate(val, fallback="N/A") -> str:
    """勝率（%）を整形する。"""
    if val is None or val != val:
        return fallback
    try:
        return f"{float(val):.0f}%"
    except (TypeError, ValueError):
        return fallback


def _fmt_price_levels(price_stats: dict | None) -> list[str]:
    """株価水準の実績テーブルを生成する。取得できなければ空リストを返す。"""
    if not price_stats or not price_stats.get("stats"):
        return []

    last = price_stats["last_close"]
    lines = [
        "## 株価水準の実績（日足・yfinance・生成時ライブ取得）",
        "",
        f"- **対象期間**: {price_stats['first_date']} 〜 {price_stats['last_date']}"
        f"（{price_stats['bars']} 営業日）",
        f"- **直近終値**: {_lv(last)}",
        "- **到達の定義**: その日の安値〜高値レンジがその水準を跨いだ日。"
        f"連続到達は1回に束ね、{PRICE_TOUCH_GAP_DAYS} 営業日以上あいたら別回として数える。",
        "- **到達後の値動き**: 到達日の終値を基準にした N 営業日後終値の騰落率"
        "（先行きが N 営業日に満たない到達はその集計から除外）。",
        "",
        "| 水準 | 価格 | 現値乖離 | 到達回数 | 5日後平均 | 5日後上昇率 | 5日後件数 "
        "| 10日後平均 | 10日後上昇率 | 10日後件数 |",
        "|------|------|----------|----------|-----------|------------|-----------"
        "|-----------|-------------|-----------|",
    ]

    for label, s in price_stats["stats"].items():
        lv = s.get("level")
        if lv is None or lv != lv:
            lines.append(f"| {label} | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
            continue
        gap = (last / lv - 1.0) * 100.0 if lv else None
        reactions = s.get("reactions") or {}
        r5  = reactions.get(5)
        r10 = reactions.get(10)
        lines.append(
            f"| {label} | {_lv(lv)} | {_sp(gap)} | {s.get('touches', 0)} 回 "
            f"| {_sp(r5.get('avg_pct')) if r5 else 'N/A'} "
            f"| {_rate(r5.get('up_ratio_pct')) if r5 else 'N/A'} "
            f"| {r5.get('samples') if r5 else 'N/A'} "
            f"| {_sp(r10.get('avg_pct')) if r10 else 'N/A'} "
            f"| {_rate(r10.get('up_ratio_pct')) if r10 else 'N/A'} "
            f"| {r10.get('samples') if r10 else 'N/A'} |"
        )

    lines += [
        "",
        "> 到達回数 0 回の水準は対象期間中に一度も機能していない"
        "（レポートで「機能した水準」として扱わないこと）。",
        "> 件数が5件未満の水準は統計として弱いことを本文で明示すること。",
        "> N/A は算出に必要な期間が足りず取得できなかったことを示す（推定値を入れない）。",
        "",
    ]
    return lines


# ---------------------------------------------------------------------------
# 取得経路（provenance）と EDINET 公式 API フォールバック
# ---------------------------------------------------------------------------

def _safe(label: str, prov: dict, key: str, fn, *, source: str = PROV_DB, default=None):
    """fn() を呼び、成功したら provenance[key]=source を立てる。

    どの取得が失敗しても run 全体を止めない。例外は握りつぶさず、
    stderr へ理由を出し provenance に「取得不可（理由）」を残す。
    """
    try:
        val = fn()
    except Exception as e:
        print(f"  → 取得失敗: {label}: {e}", file=sys.stderr)
        prov[key] = f"{PROV_NONE}（{type(e).__name__}: {str(e)[:120]}）"
        return default
    empty = val is None or (isinstance(val, (list, dict, str)) and len(val) == 0)
    if empty:
        prov[key] = f"{PROV_NONE}（{source} が空を返しました）"
        return default if val is None else val
    prov[key] = source
    return val


def fetch_edinet_annual_pdf(code: str) -> dict:
    """EDINET 公式 API から最新の有価証券報告書 PDF を1回だけ取得しキャッシュする。

    Returns:
        {"meta": dict|None, "pdf_bytes": bytes|None, "sections": dict, "error": str}
        meta は documents.json の1件（filerName / edinetCode / docID / submitDateTime 等）。
        取得できなければ pdf_bytes=None・error に理由を入れる。
    """
    result = {"meta": None, "pdf_bytes": None, "sections": {}, "error": ""}
    api_key = os.getenv("EDINET_API_KEY", "")
    if not api_key:
        result["error"] = ".env に EDINET_API_KEY がありません"
        print(f"  → 取得失敗: fetch_edinet_annual_pdf: {result['error']}", file=sys.stderr)
        return result

    cache_dir  = DEEP_DIVE_CACHE_DIR / code
    cache_pdf  = cache_dir / "annual_report.pdf"
    cache_meta = cache_dir / "annual_report_meta.json"

    if cache_pdf.exists() and cache_meta.exists():
        try:
            import json
            result["meta"] = json.loads(cache_meta.read_text(encoding="utf-8"))
            result["pdf_bytes"] = cache_pdf.read_bytes()
            print(f"  → 有報 PDF をキャッシュから読み込み: {cache_pdf}")
        except Exception as e:
            print(f"  → キャッシュ読み込み失敗（再取得します）: {e}", file=sys.stderr)
            result["meta"] = None
            result["pdf_bytes"] = None

    if result["pdf_bytes"] is None:
        try:
            meta = edinet_client.find_latest_filing(
                code, api_key,
                doc_type_codes=[edinet_client.DOC_TYPE_ANNUAL],
                lookback_days=400,
            )
        except Exception as e:
            result["error"] = f"find_latest_filing: {e}"
            print(f"  → 取得失敗: fetch_edinet_annual_pdf: {result['error']}", file=sys.stderr)
            return result
        if not meta:
            result["error"] = "直近400日に有価証券報告書の提出がありません"
            print(f"  → 取得失敗: fetch_edinet_annual_pdf: {result['error']}", file=sys.stderr)
            return result
        result["meta"] = meta
        try:
            buf = edinet_client.download_document(
                meta["docID"], api_key, file_type=edinet_client.FILE_TYPE_PDF
            )
            result["pdf_bytes"] = buf.getvalue()
        except Exception as e:
            result["error"] = f"download_document: {e}"
            print(f"  → 取得失敗: fetch_edinet_annual_pdf: {result['error']}", file=sys.stderr)
            return result
        try:
            import json
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_pdf.write_bytes(result["pdf_bytes"])
            cache_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception as e:
            print(f"  → キャッシュ保存失敗（処理は継続）: {e}", file=sys.stderr)

    try:
        from io import BytesIO as _BIO
        sections = edinet_pdf_extractor.extract_sections_from_bytes(_BIO(result["pdf_bytes"]))
        if isinstance(sections, dict) and sections.get("error"):
            result["error"] = str(sections["error"])
            print(f"  → セクション抽出失敗: {result['error']}", file=sys.stderr)
        else:
            result["sections"] = sections or {}
    except Exception as e:
        result["error"] = f"extract_sections_from_bytes: {e}"
        print(f"  → 取得失敗: {result['error']}", file=sys.stderr)

    return result


def build_text_blocks_from_pdf(sections: dict) -> dict:
    """有報 PDF のセクション dict を get_text_blocks 相当の {"content": Markdown} へ変換する。

    _fmt_text_blocks は content を「## 見出し（english key）」で分割するため、
    同じ形式の Markdown を組み立てる。取れなかったセクションは書かない。
    """
    mapping = [
        ("business_overview",     "事業の内容 (business overview)"),
        ("business_detail",       "事業の状況 (business overview detail)"),
        ("mda",                   "MD&A（経営者による分析） (management discussion)"),
        ("risk_factors",          "事業等のリスク (business risks)"),
        ("management_policy",     "経営方針・経営環境 (management policies)"),
    ]
    parts: list[str] = []
    for key, heading in mapping:
        body = (sections.get(key) or "").strip()
        if not body:
            continue
        parts.append(f"## {heading}\n\n{body}")
    if not parts:
        return {}
    return {"content": "\n\n".join(parts)}


# ---------------------------------------------------------------------------
# フォワードガイダンス（会社公表の将来見込み）の取得
#   経路1: EDINET DB get_ir_documents（中期経営計画・決算説明資料のアーカイブ）
#   経路2: TDNet 適時開示（直近 TDNET_DAYS 日・ただし PDF 実体は約30日で消える）
#   経路3: 会社 IR ページのスクレイピング（古い資料の最後の砦）
#   経路4: 有価証券報告書（経営方針・対処すべき課題）
# ---------------------------------------------------------------------------

def _pdf_fulltext(pdf_bytes: bytes) -> str:
    """PDF バイト列から全文テキストを抽出する。失敗時は空文字。"""
    try:
        from io import BytesIO as _BIO
        from pdfminer.high_level import extract_text
    except ImportError as e:
        print(f"  → 取得失敗: pdfminer が使えません: {e}", file=sys.stderr)
        return ""
    try:
        t = extract_text(_BIO(pdf_bytes)) or ""
    except Exception as e:
        print(f"  → 取得失敗: _pdf_fulltext: {e}", file=sys.stderr)
        return ""
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _text_is_extractable(text: str) -> tuple[bool, str]:
    """抽出テキストが実用に足るかを判定する。

    画像ベース PDF・CID 崩れの PDF は本文が取れないため、その旨を記録して
    「取れなかった事実」を data.md に残す（黙って空にしない）。
    """
    body = re.sub(r"\s", "", text or "")
    if len(body) < 200:
        return False, "テキスト抽出不可（画像ベースPDFの可能性・文字数200未満）"
    if text.count("(cid:") > 50:
        return False, "テキスト抽出不可（フォント埋め込みによるCID崩れ）"
    return True, "抽出成功"


def _normalize_fy(m: "re.Match") -> str:
    """年度表現のマッチを「FY2029」「2029/3期」等の短い表記へ正規化する。"""
    g = m.groups()
    if g[0]:                                   # FY2029 / FY2035-37
        return f"FY{g[0]}" + (f"-{g[1]}" if g[1] else "")
    if g[2]:                                   # 2029年3月期
        return f"{g[2]}/{int(g[3])}期"
    if g[4]:                                   # 2029年度
        return f"{g[4]}年度"
    if g[5]:                                   # 2029/3期
        y = g[5] if len(g[5]) == 4 else f"20{g[5]}"
        return f"{y}/{int(g[6])}期"
    return ""


def extract_guidance_targets(text: str, source: str = "", published: str = "") -> dict:
    """全文テキストから将来目標を機械的に抽出する（LLM を使わない）。

    トークン節約のため、PDF 全文はここ（Python のメモリ上）でだけ扱い、
    data.md へ渡すのは以下の絞り込み結果のみとする。

    Returns:
        {"rows": [{"fy","metric","value","source","published","line"}],
         "quotes": [str]}   rows = 表に落ちた数値目標 / quotes = 落ちなかった記述
    """
    out = {"rows": [], "quotes": []}
    if not text:
        return out

    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]

    hit_idx: list[int] = []
    for i, ln in enumerate(lines):
        if not ln or len(ln) < 4:
            continue
        has_val    = bool(GUIDANCE_VALUE_PATTERN.search(ln))
        has_metric = bool(GUIDANCE_METRIC_PATTERN.search(ln))
        has_fy     = bool(GUIDANCE_FY_PATTERN.search(ln))
        has_goal   = bool(GUIDANCE_GOAL_PATTERN.search(ln))
        # 「年度＋指標＋数値」または「目標語＋数値」を将来目標の候補とする
        if (has_val and has_metric and (has_fy or has_goal)) or (has_goal and has_val and has_fy):
            hit_idx.append(i)

    if not hit_idx:
        return out

    # マッチ行 ± GUIDANCE_CONTEXT_LINES 行を文脈として取り、重なる範囲はマージする
    ranges: list[list[int]] = []
    for i in hit_idx:
        lo = max(0, i - GUIDANCE_CONTEXT_LINES)
        hi = min(len(lines) - 1, i + GUIDANCE_CONTEXT_LINES)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])

    seen_rows: set[tuple] = set()
    seen_quotes: set[str] = set()

    for lo, hi in ranges:
        chunk = " ".join(x for x in lines[lo:hi + 1] if x).strip()
        if not chunk:
            continue

        # 「年度 + 指標 + 数値」が同じ文脈に揃う場合は表の行に落とす
        fy_hits = [(_normalize_fy(m), m.start()) for m in GUIDANCE_FY_PATTERN.finditer(chunk)]
        fy_hits = [(f, p) for f, p in fy_hits if f]
        made_row = False
        if fy_hits:
            for mm in GUIDANCE_METRIC_PATTERN.finditer(chunk):
                metric = mm.group(0)
                # 指標名の直後にある数値を目標値とみなす（直後64文字以内）
                tail = chunk[mm.end():mm.end() + 64]
                vm = GUIDANCE_VALUE_PATTERN.search(tail)
                if not vm:
                    continue
                value = re.sub(r"\s+", "", vm.group(0))
                # 指標に最も近い年度表現を採用する
                fy = min(fy_hits, key=lambda t: abs(t[1] - mm.start()))[0]
                key = (fy, metric, value)
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                out["rows"].append({"fy": fy, "metric": metric, "value": value,
                                    "source": source, "published": published})
                made_row = True
                if len(out["rows"]) >= GUIDANCE_MAX_TARGET_ROWS:
                    break
        if len(out["rows"]) >= GUIDANCE_MAX_TARGET_ROWS:
            break

        # 表に落とせなかった記述だけを原文引用として残す
        if not made_row and GUIDANCE_GOAL_PATTERN.search(chunk):
            q = chunk[:GUIDANCE_QUOTE_CHARS]
            k = re.sub(r"[\s、。，．]", "", q)[:50]
            if k not in seen_quotes:
                seen_quotes.add(k)
                out["quotes"].append(q)

    return out


def extract_guidance_snippets(text: str) -> list[str]:
    """後方互換用。将来目標に該当する記述を短い引用のリストで返す。"""
    r = extract_guidance_targets(text)
    quotes = list(r.get("quotes") or [])
    for row in (r.get("rows") or []):
        quotes.append(f"{row['fy']} {row['metric']} {row['value']}")
    return quotes[:GUIDANCE_MAX_QUOTES]


def _title_is_guidance(title: str) -> bool:
    """タイトル文字列が決算説明資料・中期経営計画等に該当するか。"""
    t = title or ""
    return any(k in t for k in IR_TITLE_KEYWORDS)


def fetch_guidance_from_edinetdb(client, edinet_code: str) -> list[dict]:
    """EDINET DB の IR ドキュメントアーカイブから資料を取得する（経路1）。

    TDNet の PDF 削除の影響を受けない唯一の経路。ただし EDINET DB が
    レート上限（429）の場合は空リストを返し、呼び出し側は他経路へ倒す。
    """
    if client is None or not edinet_code:
        return []
    docs: list[dict] = []
    try:
        raw = client._call("get_ir_documents", {
            "company": [edinet_code],
            "type": ["midterm", "decks", "integrated"],
            "limit": 20,
        })
    except Exception as e:
        print(f"  → 取得失敗: get_ir_documents: {e}", file=sys.stderr)
        return []

    items = []
    if isinstance(raw, dict):
        for k in ("documents", "items", "data", "results"):
            if isinstance(raw.get(k), list):
                items = raw[k]
                break
    elif isinstance(raw, list):
        items = raw

    for it in items[:GUIDANCE_MAX_DOCS]:
        if not isinstance(it, dict):
            continue
        doc_id = it.get("doc_id") or it.get("id") or ""
        title = it.get("title") or it.get("document_title") or ""
        pub = str(it.get("published_at") or it.get("published") or "")[:10]
        dtype = it.get("type") or it.get("document_type") or ""
        url = ""
        if doc_id:
            try:
                sig = client._call("get_ir_pdf_url", {"doc_id": doc_id})
                if isinstance(sig, dict):
                    url = sig.get("url") or sig.get("signed_url") or sig.get("download_url") or ""
            except Exception as e:
                print(f"  → 取得失敗: get_ir_pdf_url({doc_id}): {e}", file=sys.stderr)
        docs.append({"title": title, "published": pub, "pdf_url": url,
                     "source": "EDINET DB (IRアーカイブ)", "doc_type": dtype})
    return docs


def fetch_company_website(code4: str) -> str:
    """証券コードから会社の公式サイト URL を取得する（会社 IR ページ探索の起点）。"""
    try:
        r = requests.get(f"https://kabutan.jp/stock/?code={code4}",
                         timeout=IR_SITE_TIMEOUT, headers=_YAHOO_HEADERS)
        r.raise_for_status()
        m = re.search(r'会社サイト</th>\s*<td><a href="(https?://[^"]+)"', r.text)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"  → 取得失敗: fetch_company_website({code4}): {e}", file=sys.stderr)
    return ""


def fetch_guidance_from_ir_site(code4: str) -> dict:
    """会社 IR ページを巡回し、決算説明資料・中期経営計画の PDF を探す（経路3）。

    Returns: {"site": str, "docs": [...], "note": str}
    """
    from urllib.parse import urljoin, urlparse

    out = {"site": "", "docs": [], "note": ""}
    base = fetch_company_website(code4)
    out["site"] = base
    if not base:
        out["note"] = "会社の公式サイト URL を特定できませんでした。"
        return out

    host = urlparse(base).netloc
    seen_pages: set[str] = set()
    frontier = [base]
    found: dict[str, dict] = {}

    for _depth in range(IR_SITE_MAX_DEPTH + 1):
        nxt: list[str] = []
        for page in frontier:
            if page in seen_pages or len(seen_pages) >= IR_SITE_MAX_PAGES:
                continue
            seen_pages.add(page)
            try:
                r = requests.get(page, timeout=IR_SITE_TIMEOUT, headers=_YAHOO_HEADERS)
                r.encoding = r.apparent_encoding or r.encoding
                if r.status_code != 200:
                    continue
                html = r.text
            except Exception:
                continue
            for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.{0,200}?)</a>', html, re.S | re.I):
                target = urljoin(page, m.group(1))
                label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
                if ".pdf" in target.lower():
                    if _title_is_guidance(label) or _title_is_guidance(target):
                        if target not in found:
                            found[target] = {"title": label or target.rsplit("/", 1)[-1],
                                             "published": "", "pdf_url": target,
                                             "source": f"会社IRページ（{host}）",
                                             "doc_type": ""}
                    continue
                if urlparse(target).netloc != host:
                    continue
                low = target.lower()
                if any(h in low for h in IR_SITE_URL_HINTS) and target not in seen_pages:
                    nxt.append(target)
        frontier = nxt
        if not frontier or len(seen_pages) >= IR_SITE_MAX_PAGES:
            break

    out["docs"] = list(found.values())[:GUIDANCE_MAX_DOCS]
    if not out["docs"]:
        out["note"] = (f"会社サイト {base} を最大 {len(seen_pages)} ページ巡回しましたが、"
                       "決算説明資料・中期経営計画の PDF リンクを検出できませんでした"
                       "（IR ページが JavaScript 描画の場合は静的取得できません）。")
    return out


def collect_forward_guidance(code4: str, client, edinet_code: str,
                             tdnet_entries: list[dict] | None) -> dict:
    """4経路を順に試し、会社公表の将来見込み資料を取得・テキスト抽出する。

    Returns:
        {"docs": [{"title","published","pdf_url","source","extracted",
                   "extract_note","chars","rows","quotes"}],
         "routes": {経路名: 状況}, "site": str, "note": str}
    """
    result: dict = {"docs": [], "routes": {}, "site": "", "note": ""}
    candidates: list[dict] = []

    # 経路1: EDINET DB IR アーカイブ
    db_docs = fetch_guidance_from_edinetdb(client, edinet_code)
    result["routes"]["EDINET DB IRアーカイブ"] = (
        f"{len(db_docs)} 件" if db_docs else "0件（未対応・レート上限・該当なしのいずれか）")
    candidates += [d for d in db_docs if d.get("pdf_url")]

    # 経路2: TDNet（直近 TDNET_DAYS 日）。PDF 実体は約30日で消えるため
    #        「タイトルは拾えるが本文は取れない」ケースを明示的に記録する。
    td_docs = [
        {"title": e.get("title", ""), "published": str(e.get("published", ""))[:10],
         "pdf_url": e.get("pdf_url", ""), "source": f"TDNet（直近{TDNET_DAYS}日）",
         "doc_type": ""}
        for e in (tdnet_entries or [])
        if _title_is_guidance(e.get("title", "")) and e.get("pdf_url")
    ]
    result["routes"][f"TDNet（直近{TDNET_DAYS}日）"] = f"{len(td_docs)} 件"
    candidates += td_docs

    # 経路3: 会社 IR ページ
    site = fetch_guidance_from_ir_site(code4)
    result["routes"]["会社IRページ"] = (
        f"{len(site['docs'])} 件" if site.get("docs") else (site.get("note") or "0件"))
    result["site"] = site.get("site", "")
    candidates += site.get("docs", [])

    # 重複 URL を除去しつつ、取得可能な資料から順にテキスト抽出する。
    tried: set[str] = set()
    ok_docs = 0
    for c in candidates:
        url = c.get("pdf_url", "")
        if not url or url in tried:
            continue
        tried.add(url)
        if ok_docs >= GUIDANCE_MAX_DOCS:
            break
        rec = dict(c)
        rec.update({"extracted": False, "extract_note": "", "chars": 0,
                    "snippets": [], "rows": [], "quotes": []})
        try:
            r = requests.get(url, timeout=60, headers=_YAHOO_HEADERS)
            if r.status_code != 200:
                tdnet_note = ("／TDNet は開示から約30日で PDF 実体を削除します"
                              if "tdnet" in url.lower() else "")
                rec["extract_note"] = f"PDF 取得不可（HTTP {r.status_code}）{tdnet_note}"
                result["docs"].append(rec)
                continue
            pdf_bytes = r.content
        except Exception as ex:
            rec["extract_note"] = f"PDF 取得失敗: {type(ex).__name__}"
            result["docs"].append(rec)
            continue

        text = _pdf_fulltext(pdf_bytes)
        okflag, note = _text_is_extractable(text)
        rec["extract_note"] = note
        rec["chars"] = len(text)
        if okflag:
            rec["extracted"] = True
            # 全文はここ（メモリ上）で捨てる。data.md へ渡すのは絞り込み結果のみ。
            parsed = extract_guidance_targets(
                text, source=_clean_title(rec.get("title"), 30),
                published=rec.get("published") or "")
            rec["rows"] = parsed["rows"]
            rec["quotes"] = parsed["quotes"]
            rec["snippets"] = parsed["quotes"]
            ok_docs += 1
        result["docs"].append(rec)
        time.sleep(REQUEST_SLEEP)

    if not result["docs"]:
        result["note"] = ("4経路（EDINET DB IRアーカイブ／TDNet／会社IRページ／有報）"
                          "のいずれからも決算説明資料・中期経営計画に該当する開示を"
                          "確認できませんでした。")
    return result


def _clean_title(title: object, limit: int = 70) -> str:
    """資料タイトルを1行へ整形する。"""
    return re.sub(r"\s+", " ", str(title or "")).strip()[:limit] or "（無題）"


def _fmt_forward_guidance(guidance: dict | None, pdf_sections: dict | None,
                          text_blocks: dict | None) -> list[str]:
    """フォワードガイダンスのセクションを組み立てる。

    トークン節約のため、PDF 全文は載せない。Python 側で機械抽出した
    「目標年度 × 指標 × 目標値」の表を主体とし、表に落ちなかった記述だけを
    上限付きで原文引用する。セクション全体で GUIDANCE_SECTION_MAX_CHARS を超えたら
    年度付き数値目標を優先して打ち切る。
    取得できなかった場合もセクションを消さず「確認できなかった」と残す。
    """
    g = guidance or {}
    docs = g.get("docs") or []

    lines = ["## 会社が公表した将来見込み（フォワードガイダンス）", ""]
    lines += ["> 決算短信に載らない「会社自身が公表した来期以降・複数年の業績目標」を"
              "必須インプットとして収集したセクションです。数値は資料原文からの機械抽出で、"
              "推定・補完は行っていません。", ""]

    # --- 取得した資料の一覧 ---
    lines += ["### 取得した資料", ""]
    routes = g.get("routes") or {}
    if routes:
        lines += ["- 経路別の取得状況: " + " / ".join(f"{k}={v}" for k, v in routes.items()), ""]
    if docs:
        lines += ["| 資料名 | 開示日 | 取得元 | テキスト抽出 |", "|---|---|---|---|"]
        for d in docs:
            mark = (f"成功（{d.get('chars', 0):,}字→目標{len(d.get('rows') or [])}件）"
                    if d.get("extracted") else (d.get("extract_note") or "不明"))
            lines.append(f"| {_clean_title(d.get('title'), 40)} | {d.get('published') or '不明'} "
                         f"| {d.get('source') or '不明'} | {mark} |")
        lines.append("")
    else:
        lines += ["*該当する開示が確認できなかった*", ""]
        if g.get("note"):
            lines += [f"> {g['note']}", ""]

    # --- 中期経営計画・長期目標の数値（表が主役） ---
    lines += ["### 中期経営計画・長期目標の数値", ""]
    rows: list[dict] = []
    seen: set[tuple] = set()
    for d in docs:
        for r in (d.get("rows") or []):
            key = (r.get("fy"), r.get("metric"), r.get("value"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
    # 有報の経営方針からも同じ抽出をかける（説明資料が取れない銘柄の受け皿）
    policy = ""
    for key in ("management_policy", "policy", "business_policy"):
        v = (pdf_sections or {}).get(key)
        if v and str(v).strip():
            policy = str(v).strip()
            break
    if not policy:
        content = str((text_blocks or {}).get("content", "") or "")
        secs = _split_text_block_sections(content) if content else {}
        for k, v in secs.items():
            if any(w in k for w in ("経営方針", "対処すべき課題", "経営環境")):
                policy = str(v).strip()
                break
    policy_parsed = extract_guidance_targets(policy, source="有価証券報告書") if policy else {}
    for r in (policy_parsed.get("rows") or []):
        key = (r.get("fy"), r.get("metric"), r.get("value"))
        if key not in seen:
            seen.add(key)
            rows.append(r)

    if rows:
        lines += ["| 目標年度 | 指標 | 目標値 | 出典資料 | 公表日 |", "|---|---|---|---|---|"]
        for r in rows[:GUIDANCE_MAX_TARGET_ROWS]:
            lines.append(f"| {r.get('fy') or '不明'} | {r.get('metric') or ''} "
                         f"| {r.get('value') or ''} | {r.get('source') or ''} "
                         f"| {r.get('published') or '不明'} |")
        lines.append("")
        lines += ["> 上表は資料本文からの機械抽出です。指標名の直後にある数値を目標値として"
                  "拾っているため、実績値が混じる場合があります。誌面へ書く前に下の原文引用で"
                  "「目標」か「実績」かを確認してください。", ""]
    else:
        lines += ["*年度付きの数値目標を抽出できなかった*", ""]

    # --- 表に落とせなかった記述（原文引用・上限付き） ---
    quotes: list[str] = []
    qseen: set[str] = set()
    for d in docs:
        for q in (d.get("quotes") or []):
            k = re.sub(r"[\s、。，．]", "", q)[:50]
            if k not in qseen:
                qseen.add(k)
                quotes.append(f"（{_clean_title(d.get('title'), 24)}）{q}")
    for q in (policy_parsed.get("quotes") or []):
        k = re.sub(r"[\s、。，．]", "", q)[:50]
        if k not in qseen:
            qseen.add(k)
            quotes.append(f"（有報・経営方針）{q}")

    if quotes:
        lines += ["### 目標に関する記述（原文引用・表に落ちなかった分）", ""]
        used = sum(len(x) for x in lines)
        shown = 0
        for q in quotes[:GUIDANCE_MAX_QUOTES]:
            if used + len(q) > GUIDANCE_SECTION_MAX_CHARS:
                break
            lines.append(f"- 「{q}」")
            used += len(q) + 4
            shown += 1
        if shown < len(quotes):
            lines.append(f"- 以下省略（該当箇所が多いため、年度付き数値目標を優先して掲載。"
                         f"全 {len(quotes)} 件中 {shown} 件を掲載）")
        lines.append("")

    # --- 経営方針・対処すべき課題（有報・要点のみ） ---
    lines += ["### 経営方針・対処すべき課題（有報より）", ""]
    if policy:
        lines += [f"- 有報の該当セクションを {len(policy):,} 字取得済み"
                  "（全文は載せず、上の数値目標・原文引用へ機械抽出済み）。", ""]
    else:
        lines += ["*有価証券報告書から経営方針・対処すべき課題の本文を取得できなかった*", ""]

    return lines


# ---------------------------------------------------------------------------
# 決算説明資料・成長可能性資料の重要ページ画像化
# ---------------------------------------------------------------------------

def _pdfminer_page_texts(pdf_bytes: bytes) -> list[str]:
    """PDF をページ単位でテキスト抽出する。失敗時は空リスト。"""
    try:
        from io import BytesIO as _BIO
        from pdfminer.high_level import extract_text
        from pdfminer.pdfpage import PDFPage
    except ImportError as e:
        print(f"  → 取得失敗: pdfminer が使えません: {e}", file=sys.stderr)
        return []
    try:
        n_pages = len(list(PDFPage.get_pages(_BIO(pdf_bytes))))
    except Exception as e:
        print(f"  → 取得失敗: ページ数を取得できません: {e}", file=sys.stderr)
        return []
    texts: list[str] = []
    for i in range(n_pages):
        try:
            t = extract_text(_BIO(pdf_bytes), page_numbers=[i]) or ""
        except Exception:
            t = ""
        texts.append(t)
    return texts


def fetch_ir_deck_pages(code: str, tdnet_entries: list[dict]) -> dict:
    """決算説明資料・成長可能性資料の重要ページを PNG 化する。

    Returns:
        {"fitz_available": bool, "docs": [{"title", "pdf_url", "pages", "images", "note"}],
         "note": str}
    """
    out = {"fitz_available": False, "docs": [], "note": ""}

    fitz = None
    try:
        import fitz  # PyMuPDF
        out["fitz_available"] = True
    except ImportError:
        out["note"] = ("PyMuPDF (fitz) が未インストールのためページ画像化を行いませんでした"
                       "（pip install は実行していません）。")
        print(f"  → {out['note']}", file=sys.stderr)

    targets = [
        e for e in (tdnet_entries or [])
        if e.get("pdf_url") and any(k in (e.get("title") or "") for k in IR_TITLE_KEYWORDS)
    ][:IR_MAX_DOCS]

    if not targets:
        out["note"] = (out["note"] + " " if out["note"] else "") + \
            "TDNet 直近30日に決算説明資料・成長可能性資料の開示がありませんでした。"
        return out

    save_dir = DEEP_DIVE_CACHE_DIR / code
    for e in targets:
        doc_rec = {"title": e.get("title", ""), "pdf_url": e.get("pdf_url", ""),
                   "pages": 0, "images": [], "note": ""}
        try:
            r = requests.get(e["pdf_url"], timeout=60, headers=_YAHOO_HEADERS)
            r.raise_for_status()
            pdf_bytes = r.content
        except Exception as ex:
            doc_rec["note"] = f"PDF 取得失敗: {ex}"
            print(f"  → 取得失敗: fetch_ir_deck_pages: {ex}", file=sys.stderr)
            out["docs"].append(doc_rec)
            continue

        page_texts = _pdfminer_page_texts(pdf_bytes)
        doc_rec["pages"] = len(page_texts)
        if not page_texts:
            doc_rec["note"] = "ページテキストを取得できませんでした（ページ画像は未生成）。"
            out["docs"].append(doc_rec)
            continue

        total_chars = sum(len(re.sub(r"\s", "", t)) for t in page_texts)
        cid_hits    = sum(t.count("(cid:") for t in page_texts)
        cid_broken  = (total_chars < 200) or (cid_hits > 50)

        hit_pages = [i for i, t in enumerate(page_texts) if IR_PAGE_KEYWORDS.search(t)]

        # 抽出したページテキストを捨てずに保持する。PNG は執筆側が読めないため、
        # data.md にはこのテキストを必ず併記する（改修前はここで破棄していた）。
        if cid_broken:
            doc_rec["page_text"] = ""
            doc_rec["text_note"] = ("テキスト抽出不可（画像ベースPDFまたはCID崩れ）。"
                                    "OCR は未実装のため本文は取得できていません。")
        else:
            _keep = hit_pages if hit_pages else list(range(min(len(page_texts), IR_MAX_PAGES)))
            _chunks = []
            for _i in _keep[:IR_MAX_PAGES]:
                _t = re.sub(r"\n{3,}", "\n\n", (page_texts[_i] or "")).strip()
                if _t:
                    _chunks.append(f"--- p.{_i + 1} ---\n{_t}")
            doc_rec["page_text"] = "\n\n".join(_chunks)
            doc_rec["text_note"] = ("抽出成功" if _chunks else "該当ページの本文が空でした")

        if cid_broken and not hit_pages:
            # テキストが CID 崩れで読めない場合、キーワードでの選別ができない。
            # 推測でページを選ばず、資料の先頭から IR_MAX_PAGES ページを
            # 「選別なし」と明示して画像化する（テキスト判定は行わない）。
            doc_rec["selection"] = "テキスト不能のため先頭ページを無選別で画像化"
            hit_pages = list(range(min(doc_rec["pages"], IR_MAX_PAGES)))
        else:
            doc_rec["selection"] = "キーワード一致ページ"

        hit_pages = hit_pages[:IR_MAX_PAGES]
        if not hit_pages:
            doc_rec["note"] = (f"全{doc_rec['pages']}ページにキーワード"
                               f"（市場規模/TAM/SAM/中期/中計/受注残/生産能力/KPI/目標）"
                               f"を含むページがありませんでした。")
            out["docs"].append(doc_rec)
            continue

        if not out["fitz_available"]:
            doc_rec["note"] = (f"該当 {len(hit_pages)} ページ"
                               f"（{', '.join(str(i + 1) for i in hit_pages)}）を検出しましたが、"
                               f"PyMuPDF 未導入のため画像は未生成。")
            out["docs"].append(doc_rec)
            continue

        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            slug = re.sub(r"[^0-9]+", "", str(e.get("published", ""))[:10]) or "deck"
            for i in hit_pages:
                page = doc.load_page(i)
                pix = page.get_pixmap(dpi=144)
                fpath = save_dir / f"{slug}_p{i + 1:03d}.png"
                pix.save(str(fpath))
                doc_rec["images"].append(str(fpath.resolve()))
            doc.close()
            doc_rec["note"] = (f"全{doc_rec['pages']}ページ中 {len(doc_rec['images'])} ページを画像化"
                               f"（選別基準: {doc_rec.get('selection', '')}）。")
        except Exception as ex:
            doc_rec["note"] = f"画像化失敗: {ex}"
            print(f"  → 取得失敗: fetch_ir_deck_pages(PNG): {ex}", file=sys.stderr)

        out["docs"].append(doc_rec)
        time.sleep(REQUEST_SLEEP)

    return out


# ---------------------------------------------------------------------------
# 新株予約権・有報 PDF 追加セクションの整形
# ---------------------------------------------------------------------------

def _fmt_share_warrants(warrants: list[dict], split_info) -> list[str]:
    """parse_share_warrants の結果を表にする。空なら空リスト。"""
    if not warrants:
        return []
    lines = ["## 新株予約権の状況（有報 PDF・自動抽出）", ""]
    if split_info:
        lines.append(
            "> 本文から検出した株式分割: "
            + " × ".join(f"1:{r}" for r in split_info["ratios"])
            + f"（累積 1:{split_info['cumulative']}）。調整後行使価額を併記する。"
        )
    else:
        lines.append("> 本文から株式分割の比率を検出できなかった。各回号の「分割調整後」列に"
                     "有報自身の記載（調整済みか分割前か）をそのまま示す。")
    notes = sorted({w.get("split_note", "") for w in warrants if w.get("split_note")})
    if notes:
        lines.append("")
        lines.append("> 調整状態: " + " / ".join(notes))
    lines += [
        "",
        "| 回号 | 目的株式数（期末） | 目的株式数（提出日前月末） | 行使価額 | 分割調整後 | 行使期間 | 割当先 |",
        "|------|-----------|-----------|---------|-----------|---------|-------|",
    ]
    for w in warrants:
        sh  = f"{w['shares']:,}株" if w.get("shares") is not None else "N/A"
        shl = f"{w['shares_latest']:,}株" if w.get("shares_latest") is not None else "同左/未記載"
        pr  = f"{w['exercise_price']:,}円" if w.get("exercise_price") is not None else "N/A"
        adj = (f"{w['exercise_price_adjusted']:,.2f}円"
               if w.get("exercise_price_adjusted") is not None else w.get("split_note", "N/A"))
        per = w.get("exercise_period") or "N/A"
        gr  = (w.get("grantee") or "N/A").replace("|", "／")[:60]
        lines.append(f"| {w['round']} | {sh} | {shl} | {pr} | {adj} | {per} | {gr} |")
    lines += [
        "",
        "> 自動抽出のため回号・数値の取りこぼしがありうる。"
        "本文執筆時は「有価証券報告書 PDF 追加セクション」の原文で必ず照合すること。",
        "> N/A は PDF から読み取れなかったことを示す（推定値を入れていない）。",
        "",
    ]
    return lines


def _fmt_pdf_extra_sections(sections: dict) -> list[str]:
    """有報 PDF から抽出した追加セクション（本文）を出力する。"""
    labels = [
        ("shares_issued_history", "発行済株式総数・資本金等の推移", 3000),
        ("facilities_plan",       "設備の新設、除却等の計画",       3000),
        ("production_orders",     "生産、受注及び販売の実績",       3000),
        ("share_warrants",        "新株予約権等の状況（原文）",      6000),
        ("segment",               "セグメント情報（原文）",          5000),
        ("shareholder",           "大株主の状況（原文）",            4000),
    ]
    body_lines: list[str] = []
    for key, label, limit in labels:
        body = (sections or {}).get(key) or ""
        body = body.strip()
        if not body:
            continue
        body_lines += [f"### {label}", "", "```", body[:limit], "```", ""]
    if not body_lines:
        return []
    return ["## 有価証券報告書 PDF 追加セクション（EDINET公式API）", ""] + body_lines


def _fmt_ir_decks(ir_decks: dict | None) -> list[str]:
    """決算説明資料の重要ページ画像化結果を出力する。"""
    if not ir_decks:
        return []
    lines = ["## 決算説明資料・成長可能性資料（重要ページ画像）", ""]
    if ir_decks.get("note"):
        lines += [f"> {ir_decks['note']}", ""]
    if not ir_decks.get("docs"):
        lines += ["*対象資料なし*", ""]
        return lines
    for d in ir_decks["docs"]:
        lines += [f"### {d.get('title', '')}", ""]
        if d.get("pdf_url"):
            lines.append(f"- PDF: {d['pdf_url']}")
        lines.append(f"- ページ数: {d.get('pages', 0)}")
        if d.get("selection"):
            lines.append(f"- ページ選別基準: {d['selection']}")
        if d.get("note"):
            lines.append(f"- 状況: {d['note']}")
        imgs = d.get("images") or []
        if imgs:
            # パスの羅列もトークンを食うため、列挙は上限まで。
            lines.append(f"- 保存 PNG（{len(imgs)} 枚・パス列挙は最大"
                         f"{IR_DECK_MAX_IMAGE_PATHS}件）:")
            for p in imgs[:IR_DECK_MAX_IMAGE_PATHS]:
                lines.append(f"    - {p}")
            if len(imgs) > IR_DECK_MAX_IMAGE_PATHS:
                lines.append(f"    - （他 {len(imgs) - IR_DECK_MAX_IMAGE_PATHS} 枚は"
                             f"{DEEP_DIVE_CACHE_DIR.resolve()} 配下に保存済み）")
        else:
            lines.append(f"- 保存 PNG: なし（保存先 {DEEP_DIVE_CACHE_DIR.resolve()}・未生成）")
        lines.append("")
        # PNG は執筆エージェントが読めないため、抽出テキストから将来目標だけを機械抽出して
        # 併記する（全文は載せない＝トークンを食わせない）。
        page_text = (d.get("page_text") or "").strip()
        if page_text:
            parsed = extract_guidance_targets(page_text)
            rws, qts = parsed.get("rows") or [], parsed.get("quotes") or []
            if rws:
                lines.append("- 抽出した数値目標: "
                             + " / ".join(f"{r['fy']} {r['metric']} {r['value']}"
                                          for r in rws[:8]))
            if qts:
                lines.append("- 目標に関する記述:")
                for q in qts[:3]:
                    lines.append(f"    - 「{q[:GUIDANCE_QUOTE_CHARS]}」")
            if not rws and not qts:
                lines.append("- 抽出テキストに将来目標の記述は検出されませんでした。")
            lines.append("")
        elif d.get("text_note"):
            lines += [f"- 抽出テキスト: {d['text_note']}", ""]
    return lines


def _fmt_provenance(prov: dict) -> list[str]:
    """取得経路サマリーを1行で出す。"""
    if not prov:
        return []
    order = [
        ("company",        "会社情報"),
        ("financials",     "財務時系列"),
        ("text_blocks",    "定性テキスト"),
        ("segments",       "セグメント"),
        ("shareholders",   "大株主"),
        ("directors",      "役員"),
        ("relations",      "資本関係"),
        ("large_holdings", "大量保有"),
        ("warrants",       "新株予約権"),
        ("pdf_extra",      "有報PDF追加セクション"),
        ("ir_decks",       "決算説明資料"),
        ("guidance",       "将来見込み(中計・説明資料)"),
    ]
    known = {k for k, _ in order}
    bits = [f"{label}={prov[key]}" for key, label in order if key in prov]
    bits += [f"{k}={v}" for k, v in prov.items() if k not in known]
    return ["## 取得経路サマリー", "", "- " + " / ".join(bits), ""]


# ---------------------------------------------------------------------------
# Markdown 生成
# ---------------------------------------------------------------------------
# 反応スコア対象日の外部環境（PM 2026-09-06 指示）
#
# 目的: 「特定できる材料が確認できなかった」で終わる記述を無くす。
#   執筆エージェントは data.md に無い事実を書けない（GHA では Web も MCP も使えない）。
#   そこで「その日の値動きの原因になりうる事実」を機械で先に揃えて data.md へ入れる。
#
# 揃える事実（対象は反応スコア上位 REACTION_TOP_N 日のみ）:
#   1. 自社の開示の有無（当日・前営業日の引け後）… TDNet
#   2. 国内指数の当日騰落率 … 日経平均（yfinance）・TOPIX / 東証グロース市場250（J-Quants）
#   3. 米国市場の前営業日の騰落率 … SOX・ナスダック総合・S&P500（yfinance）
#   4. 為替の当日変化率 … ドル円（yfinance）
#   5. 同業他社の当日騰落率と当日の開示 … peers.yml ＋ yfinance ＋ TDNet
#   6. 当日のマクロ・動意レポートで当該銘柄／セクターに触れた段落
#   7. 需給（PM 2026-09-06 追加指示）… reaction_supply_demand.py
#      機関投資家の空売り残高と日次増減（機関名別）・信用取引の売残/買残と増減・
#      立会外分売/自己株取得/大量保有報告書・信用規制・配当/分割の権利落ち日。
#      3168 で 9/2 の -9.17% を「権利落ち」と誤って説明した原因が、需給が
#      機械で揃える事実の中に無かったことにあるため追加した。
# ---------------------------------------------------------------------------


def _pct_change(cur, prev):
    """前日比 %。どちらか欠ければ None（推定も補完もしない）。"""
    try:
        if cur is None or prev is None:
            return None
        prev = float(prev)
        if prev == 0:
            return None
        return (float(cur) / prev - 1.0) * 100.0
    except (TypeError, ValueError):
        return None


def _signed_pct(val, fallback="取得できず") -> str:
    """符号付きパーセント表記。欠損は fallback を返し、0 で埋めない。"""
    if val is None:
        return fallback
    try:
        return f"{float(val):+.2f}%"
    except (TypeError, ValueError):
        return fallback


def compute_reaction_days(price_df, top_n: int = REACTION_TOP_N) -> list:
    """反応スコア（日中値幅 × 出来高5日平均比）の上位日を返す。

    反応スコア = (高値 − 安値) / 前日終値 × 100  ×  当日出来高 / 直近5日平均出来高
    順位付けの定義は agents/stock_analyst.md の反応スコア条文に合わせる
    （終値騰落率のみでの順位付けはしない）。

    Returns:
        [{"date": date, "score": float, "range_pct": float, "vol_ratio": float,
          "close_pct": float, "close": float}] をスコア降順で最大 top_n 件。
        算出できなければ空リスト。
    """
    if price_df is None or len(price_df) < REACTION_VOL_WINDOW + 2:
        return []
    try:
        df = price_df.copy()
        need = {"High", "Low", "Close"}
        if not need.issubset(set(df.columns)):
            return []
        prev_close = df["Close"].shift(1)
        range_pct = (df["High"] - df["Low"]) / prev_close * 100.0
        close_pct = (df["Close"] / prev_close - 1.0) * 100.0
        if "Volume" in df.columns:
            vol_avg = df["Volume"].shift(1).rolling(REACTION_VOL_WINDOW).mean()
            vol_ratio = df["Volume"] / vol_avg
        else:
            vol_ratio = None

        # 対象期間は直近 REACTION_LOOKBACK_DAYS 日。それ以前は誌面の関心外。
        cutoff = df.index.max() - pd_timedelta(days=REACTION_LOOKBACK_DAYS)
        mask = df.index >= cutoff

        rows = []
        for idx in df.index[mask]:
            rp = range_pct.get(idx)
            if rp is None or rp != rp:      # NaN
                continue
            vr = None
            if vol_ratio is not None:
                v = vol_ratio.get(idx)
                if v is not None and v == v:
                    vr = float(v)
            score = float(rp) * (vr if vr is not None else 1.0)
            cp = close_pct.get(idx)
            rows.append({
                "date": idx.date() if hasattr(idx, "date") else idx,
                "score": score,
                "range_pct": float(rp),
                "vol_ratio": vr,
                "close_pct": (float(cp) if cp is not None and cp == cp else None),
                "close": float(df["Close"].get(idx)),
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        picked = rows[:top_n]

        # 「直近で大きく動いたものは必ず含める」（agents/stock_analyst.md の条文）。
        # 期間全体のスコア上位だけを取ると、直近の大きな値動きが漏れる。
        # 直近 REACTION_RECENT_DAYS 日の中で最もスコアが高い日が未採用なら、
        # 上位の末尾と入れ替えて必ず含める。
        recent_floor = max(r["date"] for r in rows) - timedelta(days=REACTION_RECENT_DAYS)
        recent = [r for r in rows if r["date"] >= recent_floor]
        if recent and picked:
            top_recent = max(recent, key=lambda r: r["score"])
            if all(r["date"] != top_recent["date"] for r in picked):
                picked[-1] = top_recent

        picked.sort(key=lambda r: r["score"], reverse=True)
        return picked
    except Exception as e:
        print(f"  → 取得失敗: compute_reaction_days: {e}", file=sys.stderr)
        return []


def pd_timedelta(days: int):
    """pandas.Timedelta の薄いラッパ（import を1か所に閉じる）。"""
    import pandas as pd
    return pd.Timedelta(days=days)


def load_peers(code4: str) -> list:
    """peers.yml から当該銘柄の同業を読む。未定義・空なら空リスト。

    peers が空の銘柄（例: 285A）は同業比較が構造的に不可能なので、
    その事実を呼び出し側が区別できるよう空リストで返す（0 埋めしない）。
    """
    try:
        import yaml
        if not PEERS_YAML_PATH.exists():
            return []
        data = yaml.safe_load(PEERS_YAML_PATH.read_text(encoding="utf-8")) or {}
        entry = data.get(code4) or data.get(str(code4))
        if not isinstance(entry, dict):
            return []
        peers = entry.get("peers") or []
        out = []
        for p in peers:
            if not isinstance(p, dict):
                continue
            c = str(p.get("code") or "").strip()
            if c:
                out.append({"code": c, "name": str(p.get("name") or "").strip()})
        return out
    except Exception as e:
        print(f"  → 取得失敗: load_peers({code4}): {e}", file=sys.stderr)
        return []


def _fetch_multi_daily_closes(tickers: list, start, end) -> dict:
    """yfinance で複数ティッカーの日足終値をまとめて1回で取得する。

    銘柄ごと・日ごとに呼ばない（トークンではなく実行時間とレート制限の問題）。

    Returns:
        {ticker: {date: close}}。取れなかったティッカーはキーごと落とす。
    """
    if not tickers:
        return {}
    try:
        import yfinance as yf
        import pandas as pd
    except Exception as e:
        print(f"  → 取得失敗: yfinance import（外部環境）: {e}", file=sys.stderr)
        return {}

    out: dict = {}
    try:
        df = yf.download(
            tickers, start=start, end=end, interval="1d",
            auto_adjust=False, progress=False, group_by="column", threads=True,
        )
        if df is None or df.empty:
            return {}
        close = df["Close"] if "Close" in df.columns.get_level_values(0) else None
        if close is None:
            return {}
        if isinstance(close, pd.Series):
            close = close.to_frame(name=tickers[0])
        for t in close.columns:
            ser = close[t].dropna()
            if ser.empty:
                continue
            out[str(t)] = {
                (i.date() if hasattr(i, "date") else i): float(v)
                for i, v in ser.items()
            }
    except Exception as e:
        print(f"  → 取得失敗: _fetch_multi_daily_closes: {e}", file=sys.stderr)
    return out


def _prev_available(series: dict, day):
    """series（{date: 値}）から day の直前の日付と値を返す。無ければ (None, None)。"""
    prior = [d for d in series if d < day]
    if not prior:
        return None, None
    d = max(prior)
    return d, series[d]


def _fetch_jq_index_closes(days: list) -> dict:
    """J-Quants v2 /indices/bars/daily から TOPIX・グロース250 の終値を取得する。

    yfinance には TOPIX・東証グロース市場250 の「指数」が無く、ETF（1306・2516 等）
    しか取れない。_cr §1 は ETF を内部参照も含めて避ける方針のため、指数そのものを
    返す J-Quants を使う。HTTP のみなので GHA でも動く。
    JQUANTS_API_KEY が無い場合は空 dict を返し、run は止めない（欠損として扱う）。

    Args:
        days: 取得したい日付（date）のリスト。1日1リクエストのため対象日と
              その前営業日のみに絞って渡すこと。
    Returns:
        {date: {指数コード: 終値}}
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key or not days:
        return {}
    try:
        import jquantsapi
        from jq_client_utils import fetch_paginated_v2
        client = jquantsapi.ClientV2(api_key=api_key)
    except Exception as e:
        print(f"  → 取得失敗: J-Quants クライアント生成（外部環境）: {e}", file=sys.stderr)
        return {}

    wanted = set(REACTION_JQ_INDEX_CODES.values())
    out: dict = {}
    for d in sorted(set(days)):
        try:
            rows = fetch_paginated_v2(
                client, "/indices/bars/daily", params={"date": d.isoformat()}
            )
        except Exception as e:
            print(f"  → 取得失敗: J-Quants 指数 {d}: {e}", file=sys.stderr)
            continue
        vals: dict = {}
        for r in rows:
            # Date 不一致の行は採用しない（古い日付での黙ったフォールバックを避ける）
            if str(r.get("Date", ""))[:10] != d.isoformat():
                continue
            code = str(r.get("Code", ""))
            close = r.get("C")   # v2 のフィールド名は C
            if code in wanted and close is not None:
                try:
                    vals[code] = float(close)
                except (TypeError, ValueError):
                    continue
        if vals:
            out[d] = vals
    return out


def _fetch_jq_short_sale_daily(code4: str, days: list, shares_out=None) -> dict:
    """J-Quants /markets/short-sale-report から、対象銘柄の機関空売り残の日次増減を取得する。

    空売り残高報告制度（発行済株式総数の 0.5% 以上の空売りポジションに報告義務）の
    開示を、反応スコア対象日の前後について取得する。screening_master.parquet の
    週次スナップショットは対象日時点の増減を持たないため、ここでライブ取得する。
    PM 2026-09-06 指示: 反応スコアの需給要因として機関空売りの増減を必ず確認するため。

    CalcDate（計算年月日 = 実際に空売り残高が動いた日）で日付を引き当てる。
    DiscDate（公表日）は CalcDate の 2 営業日後になるため、値動きの当日と結びつけるには
    CalcDate を使う必要がある。

    JQUANTS_API_KEY が無い場合・取得失敗時は空 dict を返し、run は止めない（欠損扱い）。

    Args:
        code4: 4桁銘柄コード。
        days: 反応スコア対象日（date のリスト）。
        shares_out: 発行済株式総数（対発行済% の算出に使う。無ければ開示比率をそのまま使う）。
    Returns:
        {date: {"rows": [ ... 各機関の残高と増減 ... ], "total_shares": int, "total_pct": float}}
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key or not days:
        return {}
    try:
        import jquantsapi
        from jq_client_utils import fetch_paginated_v2
        client = jquantsapi.ClientV2(api_key=api_key)
    except Exception as e:
        print(f"  → 取得失敗: J-Quants クライアント生成（空売り残）: {e}", file=sys.stderr)
        return {}

    # 公表日は計算日の約2営業日後。対象日の前後に十分な余裕を取って走査する。
    lo = min(days) - timedelta(days=SHORT_SALE_SCAN_BACK_DAYS)
    hi = max(days) + timedelta(days=SHORT_SALE_SCAN_FWD_DAYS)
    rows: list = []
    d_scan = lo
    while d_scan <= hi:
        disc = d_scan.isoformat()
        try:
            got = fetch_paginated_v2(
                client, "/markets/short-sale-report",
                params={"disc_date": disc}, sleep_seconds=0.4,
            )
        except Exception as e:
            print(f"  → 取得失敗: J-Quants 空売り残 {disc}: {e}", file=sys.stderr)
            d_scan += timedelta(days=1)
            continue
        for r in (got or []):
            if str(r.get("Code", ""))[:4] != code4:
                continue
            rows.append(r)
        d_scan += timedelta(days=1)

    if not rows:
        return {}

    def _to_date(s):
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    # CalcDate ごと・機関ごとに整理する。同一機関の同一 CalcDate は最後の1行を採る。
    by_day: dict = {}
    for r in rows:
        cd = _to_date(r.get("CalcDate"))
        if cd is None:
            continue
        inst = str(r.get("SSName") or r.get("DiscretionaryInvestmentContractorName") or "").strip()
        by_day.setdefault(cd, {})[inst] = r

    all_days = sorted(by_day)
    out: dict = {}
    for day in sorted(set(days)):
        # 対象日そのもの、無ければ対象日以前で最新の CalcDate を使う（残高は継続するため）。
        cur_day = day if day in by_day else next(
            (x for x in reversed(all_days) if x <= day), None
        )
        if cur_day is None:
            continue
        # 直前の CalcDate（増減の比較対象）
        prev_day = next((x for x in reversed(all_days) if x < cur_day), None)
        prev_map = by_day.get(prev_day, {}) if prev_day else {}
        cur_map = by_day[cur_day]

        detail: list = []
        for inst, r in cur_map.items():
            shares = r.get("ShrtPosShares", r.get("ShortPositionsInSharesNumber"))
            ratio = r.get("ShrtPosToSO", r.get("ShortPositionsToSharesOutstandingRatio"))
            try:
                shares = float(shares) if shares is not None else None
            except (TypeError, ValueError):
                shares = None
            try:
                ratio = float(ratio) if ratio is not None else None
            except (TypeError, ValueError):
                ratio = None
            # 増減は前 CalcDate の残高から。無ければ PrevRptRatio から逆算する。
            delta = None
            prev_r = prev_map.get(inst)
            if prev_r is not None:
                try:
                    ps = float(prev_r.get("ShrtPosShares", prev_r.get("ShortPositionsInSharesNumber")))
                    if shares is not None:
                        delta = shares - ps
                except (TypeError, ValueError):
                    pass
            if delta is None and shares is not None:
                try:
                    pr = float(r.get("PrevRptRatio")) if r.get("PrevRptRatio") is not None else None
                except (TypeError, ValueError):
                    pr = None
                if pr is not None and ratio:
                    # 前回報告比率から前回株数を逆算する（同一の発行済株式数を前提）。
                    delta = shares - (shares / ratio * pr)
                elif str(r.get("PrevRptDate") or "").strip() in ("", "-"):
                    # 前回報告が無い = 新規に報告義務が生じた（0.5% を超えた）
                    delta = shares
            detail.append({
                "inst": inst,
                "shares": shares,
                "ratio": ratio,
                "delta": delta,
                "calc_date": cur_day,
                "is_new": str(r.get("PrevRptDate") or "").strip() in ("", "-"),
            })

        tot = sum(x["shares"] for x in detail if x["shares"] is not None)
        tot_delta = sum(x["delta"] for x in detail if x["delta"] is not None)
        pct = None
        if shares_out:
            try:
                pct = tot / float(shares_out) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                pct = None
        if pct is None:
            rs = [x["ratio"] for x in detail if x["ratio"] is not None]
            pct = sum(rs) * 100.0 if rs else None
        delta_pct = None
        if shares_out:
            try:
                delta_pct = tot_delta / float(shares_out) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                delta_pct = None
        out[day] = {
            "rows": sorted(detail, key=lambda x: -(x["shares"] or 0)),
            "total_shares": tot,
            "total_pct": pct,
            "total_delta": tot_delta,
            "total_delta_pct": delta_pct,
            "calc_date": cur_day,
            "is_stale": cur_day != day,
        }
    return out


def _tdnet_titles_on(tdnet_entries: list, day, include_prev_evening: bool = True) -> list:
    """TDNet 一覧から指定日（および前営業日の引け後）の開示表題を返す。

    「前営業日の引け後」は前日 15:00 以降の開示とみなす。日付を解釈できない開示は
    その日の判定に使えないため落とす（時点の取り違えを防ぐ）。
    """
    hits: list = []
    for e in (tdnet_entries or []):
        dt = _parse_pub_datetime(e.get("published", ""))
        if dt is None:
            continue
        d = dt.date()
        if d == day:
            hits.append({"when": "当日", "title": e.get("title", ""), "time": dt.strftime("%H:%M")})
        elif include_prev_evening and (day - d).days == 1 and dt.hour >= 15:
            hits.append({"when": "前営業日の引け後", "title": e.get("title", ""),
                         "time": dt.strftime("%H:%M")})
    return hits


def _fetch_peer_tdnet_titles(peer_code: str, day) -> list:
    """同業1社の TDNet 開示のうち、指定日および前営業日引け後のものを返す。

    連想での波及（同業の開示で当該銘柄が動く）を検出するために使う。
    取得失敗時は空リストを返し、run を止めない。
    """
    url = _TDNET_ATOM_URL.format(code=peer_code)
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  → 取得失敗: 同業TDNet({peer_code}): {e}", file=sys.stderr)
        return []
    entries: list = []
    for entry in root.findall("a:entry", _NS):
        pub = ""
        for tag in ("a:published", "a:issued", "a:created", "a:modified", "a:updated"):
            pub = (entry.findtext(tag, "", _NS) or "").strip()
            if pub:
                break
        entries.append({"title": (entry.findtext("a:title", "", _NS) or "").strip(),
                        "published": pub})
    return _tdnet_titles_on(entries, day)


def build_reaction_context(code4: str, price_df, tdnet_entries: list,
                           sector_name: str = "", shares_out=None) -> dict:
    """反応スコア上位日について、値動きの原因になりうる外部環境を機械的に集める。

    yfinance は「全ティッカー × 全対象日」を1回の download でまとめて取得する
    （銘柄ごと・日ごとには呼ばない）。取得失敗した項目は欠損のまま残し、run は止めない。

    Returns:
        {"days": [ ... 各日の事実 ... ], "peers": [...], "note": str}
    """
    days = compute_reaction_days(price_df)
    if not days:
        return {"days": [], "peers": [], "note": "株価時系列を取得できず反応スコアを算出できませんでした"}

    peers = load_peers(code4)
    target_dates = [d["date"] for d in days]
    lo = min(target_dates) - timedelta(days=12)   # 前営業日・連休を跨ぐ余裕
    hi = max(target_dates) + timedelta(days=3)

    tickers = (
        list(REACTION_JP_YF_TICKERS.values())
        + list(REACTION_US_TICKERS.values())
        + list(REACTION_FX_TICKERS.values())
        + [f"{p['code']}.T" for p in peers]
    )
    closes = _fetch_multi_daily_closes(tickers, lo, hi)

    # J-Quants は 1 日 1 リクエストなので、対象日とその前営業日候補のみに絞る。
    jq_days: list = []
    for d in target_dates:
        jq_days.append(d)
        for back in range(1, 6):
            jq_days.append(d - timedelta(days=back))
    jq = _fetch_jq_index_closes(sorted(set(jq_days)))

    # 機関空売り残の日次増減（需給要因の主因判定に必須。PM 2026-09-06 指示）。
    # 週次スナップショットの screening_master では対象日の増減が取れないためライブ取得する。
    short_sale = _fetch_jq_short_sale_daily(code4, target_dates, shares_out=shares_out)

    # 需給のうち空売り以外（信用残・権利落ち日・需給に関わる開示）。
    # 3168 で 9/2 の -9.17% を「権利落ち」と誤って説明した原因は、
    # 権利落ち日そのものを機械で持っていなかったことにある（実際は 8/28）。
    # API 呼び出しは銘柄あたり空売り1回・信用残1回・権利落ち1回のみ。
    try:
        supply_demand = rsd.build_supply_demand(code4, target_dates)
    except Exception as e:  # noqa: BLE001
        print(f"  → 取得失敗: 需給データ({code4}): {e}", file=sys.stderr)
        supply_demand = {"days": {}, "shares_outstanding": None, "actions": [],
                         "reports": [], "margin": [], "sources": ["需給=取得できず"]}

    # 同業の開示は「同業社数 × 対象日」ではなく「同業社数」回だけ取得する。
    peer_tdnet: dict = {}
    for p in peers:
        url_code = p["code"]
        try:
            r = requests.get(_TDNET_ATOM_URL.format(code=url_code), timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            ents = []
            for entry in root.findall("a:entry", _NS):
                pub = ""
                for tag in ("a:published", "a:issued", "a:created", "a:modified", "a:updated"):
                    pub = (entry.findtext(tag, "", _NS) or "").strip()
                    if pub:
                        break
                ents.append({"title": (entry.findtext("a:title", "", _NS) or "").strip(),
                             "published": pub})
            peer_tdnet[url_code] = ents
        except Exception as e:
            print(f"  → 取得失敗: 同業TDNet({url_code}): {e}", file=sys.stderr)
            peer_tdnet[url_code] = []
        time.sleep(REQUEST_SLEEP)

    out_days: list = []
    for d in days:
        day = d["date"]
        rec: dict = {
            "date": day,
            "score": d["score"],
            "range_pct": d["range_pct"],
            "vol_ratio": d["vol_ratio"],
            "close_pct": d["close_pct"],
        }

        # 1) 自社の開示（当日・前営業日引け後）
        rec["own_disclosures"] = _tdnet_titles_on(tdnet_entries, day)

        # 2) 国内指数（当日騰落率）
        jp: dict = {}
        for label, tkr in REACTION_JP_YF_TICKERS.items():
            ser = closes.get(tkr) or {}
            cur = ser.get(day)
            _, prev = _prev_available(ser, day)
            jp[label] = _pct_change(cur, prev)
        jq_cur = jq.get(day) or {}
        # J-Quants の前営業日は「対象日より前で取得できた最新日」を使う。
        jq_prior_days = sorted([x for x in jq if x < day], reverse=True)
        jq_prev = jq.get(jq_prior_days[0]) if jq_prior_days else {}
        for label, icode in REACTION_JQ_INDEX_CODES.items():
            jp[label] = _pct_change(jq_cur.get(icode), (jq_prev or {}).get(icode))
        rec["jp_indices"] = jp

        # 3) 米国市場（日本の当日から見た前営業日 = 米国側の直近営業日）
        us: dict = {}
        for label, tkr in REACTION_US_TICKERS.items():
            ser = closes.get(tkr) or {}
            # 日本時間の当日朝に反映されるのは「日本の前日」の米国終値。
            us_days = sorted([x for x in ser if x < day], reverse=True)
            if len(us_days) >= 2:
                us[label] = _pct_change(ser[us_days[0]], ser[us_days[1]])
                us[label + "_日付"] = us_days[0].isoformat()
            else:
                us[label] = None
        rec["us_indices"] = us

        # 4) 為替（当日変化率）
        fx: dict = {}
        for label, tkr in REACTION_FX_TICKERS.items():
            ser = closes.get(tkr) or {}
            cur = ser.get(day)
            _, prev = _prev_available(ser, day)
            fx[label] = _pct_change(cur, prev)
        rec["fx"] = fx

        # 5) 同業の当日騰落率と当日の開示
        peer_rows: list = []
        for p in peers:
            ser = closes.get(f"{p['code']}.T") or {}
            cur = ser.get(day)
            _, prev = _prev_available(ser, day)
            peer_rows.append({
                "code": p["code"], "name": p["name"],
                "pct": _pct_change(cur, prev),
                "disclosures": _tdnet_titles_on(peer_tdnet.get(p["code"], []), day),
            })
        rec["peers"] = peer_rows

        # 6) 機関空売り残の当日残高と増減（需給要因）
        rec["short_sale"] = short_sale.get(day)

        # 7) 当日のマクロ・動意レポートで当該銘柄／セクターに触れた段落
        rec["market_excerpt"] = _market_report_excerpt(day, code4, sector_name)

        # 8) 需給のうち空売り以外（信用残・権利落ち日・需給に関わる開示）。
        #    空売りは rec["short_sale"] で既に出しているため include_short=False。
        try:
            sd_disc = rsd.supply_demand_disclosures(
                tdnet_entries, day, _parse_pub_datetime
            )
            rec["supply_demand_rows"] = rsd.fmt_supply_demand_for_day(
                supply_demand, day, sd_disc, include_short=False
            )
        except Exception as e:  # noqa: BLE001
            print(f"  → 取得失敗: 需給の整形({code4} {day}): {e}", file=sys.stderr)
            rec["supply_demand_rows"] = []

        out_days.append(rec)

    note = ""
    if not peers:
        note = "peers.yml に同業の定義がないため同業比較は実施していません（内部注記・誌面には書かない）"
    return {
        "days": out_days,
        "peers": peers,
        "note": note,
        "supply_demand": supply_demand,
    }


def _fmt_reaction_context(ctx: dict) -> list:
    """反応スコア対象日の外部環境を data.md 用の Markdown 行にする。

    1日あたり十数行・3日で50行程度に収める（トークン効率のため全期間には広げない）。
    取れなかった項目は「取得できず」と明記し、0 や推定値で埋めない。
    """
    lines: list = ["## 反応スコア対象日の外部環境", ""]
    if not ctx or not ctx.get("days"):
        lines.append("*反応スコア対象日を算出できませんでした（株価時系列の取得失敗）*")
        lines.append("")
        return lines

    lines += [
        "> 反応スコア = 日中値幅（高値−安値の前日終値比%）× 出来高5日平均比。",
        "> その日の値動きの原因になりうる事実を機械的に集めたもの。",
        "> **自社の開示が無い日は、需給（機関空売り残の増減 → 信用残の増減 → 信用規制 → 権利落ち）"
        "→ 同業 → 米国指数 → 国内指数 → ドル円 → 当日レポート言及 の順に該当を探し、"
        "最初に該当したものを主因として書くこと。**",
        "> **機関空売り残に増減がある日は、それを需給の主因として必ず書く"
        "（対発行済%を併記する）。テクニカルの水準を原因にしてはならない。**",
        "> **「権利落ち」「権利付最終日」を主因として書けるのは、下の『権利落ち』行が"
        "「当日と一致する」と書いている日だけである。一致しない日に権利落ちを主因として"
        "書くことを禁止する（基準日と権利落ち日の取り違えを防ぐため）。**",
        "> **信用残は週次（毎週金曜時点）のため対象日そのものの残高は存在しない。"
        "対象日を挟む2回の公表値とその増減を示してある。信用倍率は使わない。**",
        "",
    ]
    if ctx.get("note"):
        lines += [f"> 注（内部・誌面には書かない）: {ctx['note']}", ""]

    for d in ctx["days"]:
        day = d["date"]
        vr = d.get("vol_ratio")
        vr_s = f"{vr:.1f}倍" if vr is not None else "取得できず"
        lines += [
            f"### {day.isoformat()}（反応スコア {d['score']:.1f}"
            f" ／ 日中値幅 {d['range_pct']:.2f}% × 出来高 {vr_s}"
            f" ／ 終値 前日比 {_signed_pct(d.get('close_pct'))}）",
            "",
            "| 項目 | 値 |",
            "|---|---|",
        ]

        own = d.get("own_disclosures") or []
        if own:
            joined = "／".join(f"{o['when']} {o['time']} {_clean_title(o['title'], 40)}" for o in own[:3])
            lines.append(f"| 自社の開示 | あり（{joined}） |")
        else:
            lines.append("| 自社の開示 | なし（当日・前営業日引け後とも TDNet に開示なし） |")

        for label in ("日経平均", "TOPIX", "東証グロース市場250"):
            lines.append(f"| {label} | {_signed_pct((d.get('jp_indices') or {}).get(label))} |")

        us = d.get("us_indices") or {}
        for label in ("SOX指数", "ナスダック総合", "S&P500"):
            dt_s = us.get(label + "_日付")
            suffix = f"（{dt_s}）" if dt_s else ""
            lines.append(f"| {label}（前営業日）{suffix} | {_signed_pct(us.get(label))} |")

        for label in ("ドル円",):
            lines.append(f"| {label} | {_signed_pct((d.get('fx') or {}).get(label))} |")

        # 機関空売り残（需給要因）。増減がある日はそれ自体が主因になりうる。
        ss = d.get("short_sale")
        if ss:
            calc_s = ss["calc_date"].isoformat()
            stale = "（対象日の開示なし・直近の残高）" if ss.get("is_stale") else ""
            tot_pct = ss.get("total_pct")
            tot_pct_s = f"（対発行済 {tot_pct:.2f}%）" if tot_pct is not None else ""
            lines.append(
                f"| 機関空売り残 合計（計算日 {calc_s}）{stale} "
                f"| {ss['total_shares']:,.0f}株{tot_pct_s} |"
            )
            dlt = ss.get("total_delta")
            if dlt:
                dp = ss.get("total_delta_pct")
                dp_s = f"（対発行済 {dp:+.2f}pt）" if dp is not None else ""
                lines.append(f"| 機関空売り残 前回報告比 増減 | {dlt:+,.0f}株{dp_s} |")
            for row in ss["rows"][:5]:
                sh = f"{row['shares']:,.0f}株" if row["shares"] is not None else "取得できず"
                rt = f" 対発行済 {row['ratio'] * 100:.2f}%" if row["ratio"] is not None else ""
                dl = ""
                if row["delta"] is not None:
                    dl = f" 前回比 {row['delta']:+,.0f}株"
                    if row.get("is_new"):
                        dl += "（新規に報告義務が生じた）"
                lines.append(f"| 機関空売り {row['inst']} | {sh}{rt}{dl} |")
        else:
            lines.append(
                "| 機関空売り残 | 対象日の前後に空売り残高報告の開示なし"
                "（発行済の0.5%未満のため報告義務が生じていない） |"
            )

        # 信用残・権利落ち日・需給に関わる開示（PM 2026-09-06 追加指示）。
        # 「権利落ち」を主因にする場合はここの日付と一致することが必須。
        lines += (d.get("supply_demand_rows") or [])

        peer_rows = d.get("peers") or []
        if peer_rows:
            for p in peer_rows[:5]:
                lines.append(f"| 同業 {p['code']} {p['name']} | {_signed_pct(p.get('pct'))} |")
            pd_hits = []
            for p in peer_rows:
                for o in (p.get("disclosures") or [])[:1]:
                    pd_hits.append(f"{p['code']}: {_clean_title(o['title'], 36)}")
            lines.append(
                f"| 同業の開示 | {'あり（' + '／'.join(pd_hits[:3]) + '）' if pd_hits else 'なし'} |"
            )
        else:
            lines.append("| 同業 | peers 未定義のため比較なし（内部注記・誌面には書かない） |")

        lines.append("")
        ex = d.get("market_excerpt") or []
        if ex:
            lines.append("当日のマクロ・動意レポートでの言及:")
            lines += ex
            lines.append("")

    return lines


def _bbs_stamp(post: dict) -> str:
    """投稿1件の日時表記。日付を解釈できなかった投稿は必ず「日付不明」と書く。

    日付不明の投稿を特定日の値動きの説明に使ってはならないことを、
    誌面を書く側が判別できるようにするための表記（PM 2026-09-06 指示）。
    """
    dt = post.get("dt")
    if dt is None:
        return "日付不明"
    stamp = dt.strftime("%Y/%m/%d %H:%M")
    return stamp + "（年は推定）" if post.get("date_estimated") else stamp


def _fmt_bbs_posts(bbs_data: dict) -> list:
    """掲示板の投稿を日付つきで出力する。

    - 反応スコア対象日の前後1日の投稿を「対象日周辺」として別掲し優先的に載せる
    - セクション全体は BBS_SECTION_MAX_CHARS 以内。超える場合は対象日周辺を優先し、
      直近の投稿の件数を絞る
    - 日付を解釈できなかった投稿は「日付不明」と明記する
    """
    posts     = bbs_data.get("posts", []) or []
    day_posts = bbs_data.get("day_posts", {}) or {}
    oldest    = bbs_data.get("oldest")
    note      = bbs_data.get("lookback_note", "")

    lines: list = []
    span = ""
    if oldest:
        span = f"取得できた最も古い投稿日: {oldest.isoformat()}"
    undated = sum(1 for p in posts if not p.get("dt"))
    header = [
        "> 各投稿には投稿日時を付けている。**日付が付いていない投稿（「日付不明」）を"
        "特定日の値動きの説明に使ってはならない。**",
        "> 掲示板は原則として分析日時点のセンチメントに使う。過去日の反応の理由として"
        "引く場合は、その日付の投稿のみを根拠にすること。",
    ]
    if span:
        header.append("> " + span)
    if undated:
        header.append(f"> 日付を解釈できなかった投稿: {undated} 件（特定日の説明には使えない）")
    if note:
        header.append(f"> 取得上の制約: {note}")
    header += [
        ">",
        "> **スパム定義（以下に該当する投稿は無視する）**: LINE・Telegram 等への登録誘導／"
        "根拠なし煽り文句のみ／無意味な短文・記号羅列／明らかなコピペ／個人攻撃・荒らし。",
        "",
    ]
    lines += header

    used = sum(len(x) for x in lines)
    budget = BBS_SECTION_MAX_CHARS

    # 1) 反応スコア対象日の周辺（優先して載せる）
    if day_posts:
        lines.append("### 反応スコア対象日の前後の投稿")
        lines.append("")
        for day in sorted(day_posts.keys()):
            block = [f"**{day}**", ""]
            for i, p in enumerate(day_posts[day], 1):
                row = f"{i}. [{_bbs_stamp(p)}] {p['body']}"
                if used + len(row) > budget:
                    break
                block.append(row)
                used += len(row)
            block.append("")
            if len(block) > 3:
                lines += block
        lines.append("")

    # 2) 直近の投稿（残りの予算で載せる）
    lines.append("### 直近の投稿（分析日時点のセンチメント）")
    lines.append("")
    if posts:
        shown = 0
        for p in posts:
            row = f"{shown + 1}. [{_bbs_stamp(p)}] {p['body']}"
            if used + len(row) > budget:
                lines.append(f"*（文字数上限のため残り {len(posts) - shown} 件は省略）*")
                break
            lines.append(row)
            used += len(row)
            shown += 1
    else:
        lines.append("*投稿なし*")

    return lines


# ---------------------------------------------------------------------------

def build_data_markdown(
    code: str,
    company_data: dict,
    financials: list[dict],
    text_blocks: dict,
    segments: list[dict] | None = None,
    major_shareholders: list[dict] | None = None,
    directors: list[dict] | None = None,
    relations: dict | None = None,
    large_holdings: list[dict] | None = None,
    bbs_data: dict | None = None,
    tdnet_entries: list | None = None,
    news_items: list | None = None,
    supply_demand: dict | None = None,
    sector_context: str = "",
    macro_context: str = "",
    past_research: str = "",
    price_stats: dict | None = None,
    provenance: dict | None = None,
    pdf_sections: dict | None = None,
    warrants: list[dict] | None = None,
    split_info: dict | None = None,
    ir_decks: dict | None = None,
    sd_axes: dict | None = None,
    guidance: dict | None = None,
    reaction_ctx: dict | None = None,
) -> str:
    company_name = (
        company_data.get("companyName")
        or company_data.get("filerName")
        or company_data.get("name")
        or "不明"
    )
    edinet_code  = company_data.get("edinetCode", "")
    industry     = company_data.get("industryName") or company_data.get("industry", "")
    account_std  = company_data.get("accountingStandard", "")
    health_score = company_data.get("healthScore") or company_data.get("financialHealthScore")
    today_str    = date.today().strftime("%Y-%m-%d")

    # 最新期財務（get_company の latestFY または直接フィールド）
    lfy: dict = company_data.get("latestFY") or {}
    if not lfy:
        # latestFY がない場合はトップレベルから取得
        for k in ["revenue", "operatingIncome", "netIncome", "totalAssets"]:
            if company_data.get(k) is not None:
                lfy = company_data
                break

    # TDNet最新決算短信（get_company の latestEarnings）
    latest_earnings: dict = company_data.get("latestEarnings") or {}

    lines = [
        f"# {company_name}（{code}）Deep Dive データ",
        f"",
        f"- **EDINETコード**: {edinet_code}",
        f"- **業種**: {industry}",
        f"- **会計基準**: {account_std}",
        f"- **財務健全性スコア**: {health_score if health_score is not None else 'N/A'} / 100",
        f"- **収集日**: {today_str}",
        f"",
        f"---",
        f"",
    ]

    # 取得経路サマリー（冒頭・執筆側が最初に見る）
    lines += _fmt_provenance(provenance or {})
    if provenance:
        lines += ["---", ""]

    lines += [
        f"## 財務サマリー（最新期）",
        f"",
        f"| 指標 | 値 |",
        f"|------|----|",
        f"| 売上高 | {_n(lfy.get('revenue'))} |",
        f"| 営業利益 | {_n(lfy.get('operatingIncome'))} |",
        f"| 純利益 | {_n(lfy.get('netIncome'))} |",
        f"| 総資産 | {_n(lfy.get('totalAssets'))} |",
        f"| 純資産 | {_n(lfy.get('netAssets') or lfy.get('equity'))} |",
        f"| 自己資本比率 | {_pct(lfy.get('equityRatio'))} |",
        f"| ROE | {_pct(lfy.get('roe'))} |",
        f"| EPS | {_n(lfy.get('eps'), '{:.1f}', '円')} |",
        f"| BPS | {_n(lfy.get('bps'), '{:.1f}', '円')} |",
        f"| PER | {_n(lfy.get('per'), '{:.1f}', 'x')} |",
        f"| PBR | {_n(lfy.get('pbr'), '{:.1f}', 'x')} |",
        f"| 時価総額 | {_n(lfy.get('marketCap'), '{:,.0f}', '百万円')} |",
        f"",
    ]

    # TDNet最新決算短信
    if latest_earnings:
        lines += [
            f"## TDNet 最新決算短信（EDINET DB）",
            f"",
            f"| 指標 | 値 | YoY |",
            f"|------|----|----|",
            f"| 売上高 | {_n(latest_earnings.get('revenue'))} | {_pct(latest_earnings.get('revenueYoy'))} |",
            f"| 営業利益 | {_n(latest_earnings.get('operatingIncome'))} | {_pct(latest_earnings.get('operatingIncomeYoy'))} |",
            f"| 純利益 | {_n(latest_earnings.get('netIncome'))} | {_pct(latest_earnings.get('netIncomeYoy'))} |",
            f"| EPS | {_n(latest_earnings.get('eps'), '{:.1f}', '円')} | - |",
            f"",
        ]

    lines += [
        f"---",
        f"",
        f"## 財務時系列（EDINET DB・過去{len(financials)}期）",
        f"",
        _fmt_financials_table(financials),
        f"",
        f"---",
        f"",
    ]

    # セグメント情報（取得できなかった場合はセクションごと出力しない）
    segment_lines = _fmt_segments(segments or [])
    if segment_lines:
        lines += segment_lines
        lines += ["---", ""]

    # 大株主の状況（取得できなかった場合はセクションごと出力しない）
    shareholder_lines = _fmt_major_shareholders(
        major_shareholders or [], directors, relations, large_holdings
    )
    if shareholder_lines:
        lines += shareholder_lines
        lines += ["---", ""]

    # 定性テキスト（有報セクション）
    lines += _fmt_text_blocks(text_blocks)

    lines += ["---", ""]

    # 有報 PDF から取得した追加セクション（設備計画・生産受注販売・資本推移など）
    pdf_extra_lines = _fmt_pdf_extra_sections(pdf_sections or {})
    if pdf_extra_lines:
        lines += pdf_extra_lines
        lines += ["---", ""]

    # 新株予約権（希薄化の材料）
    warrant_lines = _fmt_share_warrants(warrants or [], split_info)
    if warrant_lines:
        lines += warrant_lines
        lines += ["---", ""]

    # TDNet 適時開示
    # 収集は TDNET_DAYS 日（説明資料・中期経営計画を拾うため）まで広げているが、
    # このセクションの体裁は従来どおり直近 TDNET_RECENT_DISPLAY_DAYS 日に絞る。
    lines += [f"## TDNet 適時開示（直近{TDNET_RECENT_DISPLAY_DAYS}日）", ""]
    _recent_cut = datetime.now().astimezone() - timedelta(days=TDNET_RECENT_DISPLAY_DAYS)
    _recent_entries = []
    for _e in (tdnet_entries or []):
        _dt = _parse_pub_datetime(_e.get("published", ""))
        if _dt is None or _dt >= _recent_cut:
            _recent_entries.append(_e)
    if _recent_entries:
        for e in _recent_entries:
            lines.append(f"- **{e['title']}** ({e['published'][:10]})")
            if e.get("pdf_text"):
                # 切り捨ては TDNET_PDF_MAX に一本化（ここでの二重切り捨てはしない）
                lines.append("")
                lines.append("```")
                lines.append(e["pdf_text"])
                lines.append("```")
                lines.append("")
    else:
        lines.append("*開示なし*")

    lines += ["", "---", ""]

    # 決算説明資料・成長可能性資料の重要ページ
    deck_lines = _fmt_ir_decks(ir_decks)
    if deck_lines:
        lines += deck_lines
        lines += ["---", ""]

    # 会社が公表した将来見込み（フォワードガイダンス）。
    # 取得できなかった場合もセクションごと消さず「確認できなかった」と記録する。
    lines += _fmt_forward_guidance(guidance, pdf_sections, text_blocks)
    lines += ["---", ""]

    # Yahoo Finance ニュース
    lines += ["## Yahoo Finance ニュース", ""]
    if news_items:
        for n in news_items:
            lines.append(f"- {n['date']} {n['title']}（{n['source']}）")
    else:
        lines.append("*ニュースなし*")

    lines += ["", "---", ""]

    # 需給3軸（誌面のゲート項目）
    sd_lines = _fmt_supply_demand_axes(sd_axes)
    if sd_lines:
        lines += sd_lines
        lines += ["---", ""]

    # ETL財務・需給データ（screening_master）
    lines += ["## ETLデータ（スクリーニングマスター）", ""]
    if supply_demand:
        skip = {"sector", "market", "company_name",
                "_section_valuation", "_section_pl", "_section_bs",
                "_section_liquidity", "_section_supply"}
        section_titles = {
            "_section_valuation": "### バリュエーション",
            "_section_pl":        "### 損益推移（百万円）",
            "_section_bs":        "### バランスシート",
            "_section_liquidity": "### 流動性",
            "_section_supply":    "### 需給（信用残・空売り残）",
        }
        lines.append("| 項目 | 値 |")
        lines.append("|------|-----|")
        for k, v in supply_demand.items():
            if k in skip:
                continue
            if k in section_titles:
                lines += ["", section_titles[k], "", "| 項目 | 値 |", "|------|-----|"]
                continue
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("*取得できませんでした（screening_masterが見つからないか未収録）*")

    lines += ["", "---", ""]

    # 株価水準の実績（取得できなかった場合はセクションごと出力しない）
    price_lines = _fmt_price_levels(price_stats)
    if price_lines:
        lines += price_lines
        lines += ["---", ""]

    # 反応スコア対象日の外部環境（自社開示・国内指数・米国指数・為替・同業）。
    # 「特定できる材料が確認できなかった」で終わらせないための材料。
    lines += _fmt_reaction_context(reaction_ctx or {})
    lines += ["---", ""]

    # セクター週次コンテキスト
    lines += ["## セクター週次コンテキスト", ""]
    if sector_context:
        lines.append(sector_context)
    else:
        lines.append("*取得できませんでした（ETL未完走またはセクター不明）*")

    lines += ["", "---", ""]

    # Yahoo 掲示板（個人投資家センチメント）
    lines += ["## Yahoo掲示板（個人投資家センチメント）", ""]
    if bbs_data:
        sentiment = bbs_data.get("sentiment", "")
        posts     = bbs_data.get("posts", [])
        error     = bbs_data.get("error", "")
        if error:
            lines.append(f"> 取得エラー: {error}")
        else:
            if sentiment:
                lines.append(f"**みんなの評価（直近1週間）**: {sentiment}")
                lines.append("")
            lines += _fmt_bbs_posts(bbs_data)
    else:
        lines.append("*取得できませんでした*")

    # 直近マクロレポート
    if macro_context:
        lines += ["", "---", "", "## 直近マクロレポート（参考）", "", macro_context]

    # 過去 Deep Dive・Perplexity
    if past_research:
        lines += ["", "---", "", "## 過去レポート（Deep Dive・Perplexity）", "", past_research]

    return "\n".join(lines)


def main() -> None:
    load_dotenv(_ENV_PATH)

    parser = argparse.ArgumentParser(description="銘柄 Deep Dive データ収集（EDINET DB ベース）")
    parser.add_argument("--code",  required=True, help="証券コード（4桁）例: 7256")
    parser.add_argument("--years", type=int, default=5, help="財務時系列の取得年数（デフォルト: 5）")
    args = parser.parse_args()

    code = normalize_code(args.code)

    # 取得経路の記録。どの経路で取れたかを data.md 冒頭に必ず出す。
    provenance: dict = {}

    # EDINET DB クライアント生成（429 等で失敗しても run を止めない）
    client = None
    try:
        client = EdinetDBClient()
    except Exception as e:
        print(f"  → 取得失敗: EdinetDBClient(): {e}", file=sys.stderr)

    # 1) 証券コード → EDINETコード変換
    print(f"[1/9] {code} の EDINETコードを検索中...")
    edinet_code = ""
    if client is not None:
        edinet_code = _safe("code_to_edinet", provenance, "edinet_code",
                            lambda: client.code_to_edinet(code), default="") or ""
    else:
        provenance["edinet_code"] = f"{PROV_NONE}（EDINET DB クライアントを生成できません）"
    if edinet_code:
        print(f"  → EDINETコード: {edinet_code}")
    else:
        print("  → ⚠️ EDINET DB から EDINETコードを取得できませんでした（公式 API へフォールバックします）")

    # 1-2) 有報 PDF（EDINET 公式 API）。EDINET DB が欠けている項目をここで賄う。
    #      EDINET DB が全て健全な場合でも、有報 PDF 固有のセクション
    #      （設備計画・生産受注販売・新株予約権）は EDINET DB 経路に存在しないため取得する。
    print("[1/9] 有価証券報告書 PDF を取得中（EDINET 公式 API・1回のみ）...")
    annual = fetch_edinet_annual_pdf(code)
    pdf_sections: dict = annual.get("sections") or {}
    if annual.get("meta"):
        print(f"  → {annual['meta'].get('filerName', '')}"
              f" / docID {annual['meta'].get('docID', '')}"
              f" / 提出 {str(annual['meta'].get('submitDateTime', ''))[:10]}")
    if pdf_sections:
        provenance["pdf_extra"] = PROV_EDINET
    else:
        provenance["pdf_extra"] = f"{PROV_NONE}（{annual.get('error') or 'セクション抽出0件'}）"

    # 新株予約権の構造化（有報 PDF 由来）
    warrants: list[dict] = []
    split_info = None
    try:
        warrant_text = (pdf_sections.get("share_warrants") or "")
        if warrant_text.strip():
            # 分割の記載は新株予約権の節以外（発行済株式総数の推移等）にもあるため
            # 有報の主要セクションを結合して比率を探す。
            split_hunt = "\n".join(
                str(v) for k, v in pdf_sections.items()
                if k in ("share_warrants", "shares_issued_history", "shareholder")
            )
            split_info = edinet_pdf_extractor.detect_split_ratio(split_hunt)
            warrants = edinet_pdf_extractor.parse_share_warrants(warrant_text)
    except Exception as e:
        print(f"  → 取得失敗: parse_share_warrants: {e}", file=sys.stderr)
    provenance["warrants"] = PROV_EDINET if warrants else \
        f"{PROV_NONE}（有報 PDF に新株予約権の節を検出できませんでした）"
    print(f"  → 新株予約権 {len(warrants)} 回号"
          f" / 分割比率 {('1:' + str(split_info['cumulative'])) if split_info else '未検出'}")

    # 2) 企業基本情報（最新期財務 + TDNet 最新決算短信）
    print("[2/9] 企業基本情報を取得中（EDINET DB）...")
    company_data: dict = {}
    if client is not None and edinet_code:
        company_data = _safe("get_company", provenance, "company",
                             lambda: client.get_company(edinet_code), default={}) or {}
    else:
        provenance["company"] = f"{PROV_NONE}（EDINETコード未取得）"
    if not isinstance(company_data, dict):
        company_data = {}

    # EDINET DB が使えない場合、会社名・EDINETコードを公式 API の返り値から埋める
    meta = annual.get("meta") or {}
    if not company_data.get("companyName") and meta.get("filerName"):
        company_data["companyName"] = meta["filerName"]
        provenance["company"] = PROV_EDINET
    if not company_data.get("edinetCode"):
        fallback_ec = edinet_code or meta.get("edinetCode") or ""
        if fallback_ec:
            company_data["edinetCode"] = fallback_ec
    if not edinet_code and meta.get("edinetCode"):
        edinet_code = meta["edinetCode"]

    company_name = (
        company_data.get("companyName")
        or company_data.get("filerName")
        or company_data.get("name")
        or "不明"
    )
    print(f"  → {company_name}")

    # 3) 財務時系列
    print(f"[3/9] 財務時系列を取得中（{args.years}期分）...")
    financials: list[dict] = []
    if client is not None and edinet_code:
        fin_raw = _safe("get_financials", provenance, "financials",
                        lambda: client.get_financials(edinet_code, years=args.years),
                        default=None)
        if isinstance(fin_raw, list):
            financials = fin_raw
        elif isinstance(fin_raw, dict):
            financials = fin_raw.get("data", []) or []
    else:
        provenance["financials"] = f"{PROV_NONE}（EDINETコード未取得）"
    print(f"  → {len(financials)} 期分取得")

    # 4) 定性テキスト（有報セクション）
    print("[4/9] 定性テキストを取得中（有報セクション）...")
    text_blocks: dict = {}
    if client is not None and edinet_code:
        tb_raw = _safe("get_text_blocks", provenance, "text_blocks",
                       lambda: client.get_text_blocks(edinet_code), default=None)
        if isinstance(tb_raw, dict):
            text_blocks = tb_raw if "content" in tb_raw else (tb_raw.get("sections", {}) or {})
    else:
        provenance["text_blocks"] = f"{PROV_NONE}（EDINETコード未取得）"

    if not str(text_blocks.get("content", "") or "").strip():
        # フォールバック: 有報 PDF から text_blocks 相当を組み立てる
        tb_pdf = build_text_blocks_from_pdf(pdf_sections)
        if tb_pdf:
            text_blocks = tb_pdf
            provenance["text_blocks"] = PROV_EDINET
        else:
            provenance["text_blocks"] = f"{PROV_NONE}（EDINET DB・有報 PDF ともに取得不可）"

    _content = str(text_blocks.get("content", "") or "")
    _sections = _split_text_block_sections(_content)
    print(f"  → セクション {len(_sections)} 件 / content {len(_content):,} 字"
          f"（経路: {provenance.get('text_blocks')}）")

    # 4-2) セグメント情報
    print("[5/9] セグメント情報を取得中...")
    segments: list[dict] = []
    if client is not None and edinet_code:
        segments = _safe("fetch_segments", provenance, "segments",
                         lambda: fetch_segments(client, edinet_code), default=[]) or []
    else:
        provenance["segments"] = f"{PROV_NONE}（EDINETコード未取得）"
    if not segments:
        # フォールバック: 有報 PDF のセグメント本文を PDF 追加セクションとして出す
        if (pdf_sections.get("segment") or "").strip():
            provenance["segments"] = f"{PROV_EDINET}（原文テキストのみ・構造化なし）"
    print(f"  → セグメント {len(segments)} 行（経路: {provenance.get('segments')}）")

    # 4-3) 大株主の状況と、株主の関係判定に使う材料
    print("[6/9] 大株主・役員・資本関係・主要販売先を取得中...")
    major_shareholders: list[dict] = []
    directors: list[dict] = []
    relations: dict = {"parents": [], "subsidiaries": [], "customers": []}
    large_holdings: list[dict] = []
    if client is not None and edinet_code:
        major_shareholders = _safe("fetch_major_shareholders", provenance, "shareholders",
                                   lambda: fetch_major_shareholders(client, edinet_code),
                                   default=[]) or []
        directors = _safe("fetch_directors", provenance, "directors",
                          lambda: fetch_directors(client, edinet_code), default=[]) or []
        relations = _safe("fetch_ownership_relations", provenance, "relations",
                          lambda: fetch_ownership_relations(client, edinet_code),
                          default=None) or {"parents": [], "subsidiaries": [], "customers": []}
        # fetch_ownership_relations は3キーの dict を常に返すため、_safe の
        # 「空なら取得不可」判定が効かない。中身が全て空なら取得不可へ倒す。
        if not any(relations.get(k) for k in ("parents", "subsidiaries", "customers")):
            provenance["relations"] = f"{PROV_NONE}（親会社・関係会社・主要販売先とも0件）"
        large_holdings = _safe("fetch_large_holdings", provenance, "large_holdings",
                               lambda: fetch_large_holdings(client, edinet_code),
                               default=[]) or []
    else:
        for k in ("shareholders", "directors", "relations", "large_holdings"):
            provenance[k] = f"{PROV_NONE}（EDINETコード未取得）"

    if not major_shareholders and (pdf_sections.get("shareholder") or "").strip():
        provenance["shareholders"] = f"{PROV_EDINET}（原文テキストのみ・構造化なし）"

    if major_shareholders:
        _top = major_shareholders[0]
        _basis = _period_heading(_top.get("fiscalYear"), _top.get("quarter"), _top.get("period"))
        print(f"  → 大株主 {len(major_shareholders)} 行（基準: {_basis}）"
              f"  筆頭: {_top.get('holderName', '?')} {_pct_pt(_top.get('ratioPct'))}")
    else:
        print(f"  → ⚠️ 大株主（構造化）を取得できませんでした（経路: {provenance.get('shareholders')}）")
    print(f"  → 役員 {len(directors)} 名 / 親会社等 {len(relations.get('parents', []))} 件"
          f" / 関係会社 {len(relations.get('subsidiaries', []))} 件"
          f" / 主要販売先 {len(relations.get('customers', []))} 件"
          f" / 大量保有報告書 {len(large_holdings)} 行")

    # 6) TDNet・Yahooニュース
    print("[8/9] TDNet・Yahooニュースを取得中...")
    tdnet_entries = fetch_tdnet(code)
    time.sleep(REQUEST_SLEEP)
    news_items = fetch_yahoo_news(code)
    news_status = f"{len(news_items)} 件" if news_items else "⚠️ 0件（取得失敗の可能性あり・再実行を推奨）"
    print(f"  → TDNet {len(tdnet_entries)} 件 / ニュース {news_status}")

    # 6-2) 決算説明資料・成長可能性資料の重要ページ画像化
    print("[8/9] 決算説明資料の重要ページを画像化中...")
    ir_decks = fetch_ir_deck_pages(code, tdnet_entries)
    _png = sum(len(d.get("images") or []) for d in ir_decks.get("docs", []))
    if _png:
        provenance["ir_decks"] = f"TDNet PDF（PNG {_png} 枚）"
    else:
        provenance["ir_decks"] = f"{PROV_NONE}（{ir_decks.get('note') or 'ページ画像未生成'}）"
    print(f"  → 対象 {len(ir_decks.get('docs', []))} 件 / PNG {_png} 枚")

    # 6-2b) 会社公表の将来見込み（中期経営計画・決算説明資料）を必須インプットとして収集。
    #       EDINET DB IRアーカイブ → TDNet → 会社IRページ の順に試し、
    #       取得した PDF は全文テキスト抽出まで行う。
    print("[8/9] 会社公表の将来見込み（中計・決算説明資料）を収集中...")
    guidance = collect_forward_guidance(code, client, edinet_code, tdnet_entries)
    _g_ok = sum(1 for d in guidance.get("docs", []) if d.get("extracted"))
    _g_sn = sum(len(d.get("rows") or []) for d in guidance.get("docs", []))
    _g_q  = sum(len(d.get("quotes") or []) for d in guidance.get("docs", []))
    if _g_ok:
        provenance["guidance"] = (f"資料 {len(guidance.get('docs', []))} 件中 "
                                  f"{_g_ok} 件をテキスト抽出"
                                  f"（数値目標 {_g_sn} 件 / 目標記述 {_g_q} 件）")
    else:
        provenance["guidance"] = (f"{PROV_NONE}（"
                                  + " / ".join(f"{k}:{v}" for k, v in
                                               (guidance.get("routes") or {}).items())
                                  + "）")
    print(f"  → 資料 {len(guidance.get('docs', []))} 件 / 抽出成功 {_g_ok} 件"
          f" / 数値目標 {_g_sn} 件 / 目標記述 {_g_q} 件")

    # 6-3) 株価時系列（日足 OHLCV・yfinance）と水準の実績集計
    print("[9/9] 株価時系列を取得中（yfinance 日足）...")
    price_df = fetch_price_history(code)
    price_stats = build_price_level_stats(price_df)
    if price_stats is None:
        print("  → ⚠️ 取得失敗（株価水準セクションは出力しません）")
    else:
        bars = price_stats["bars"]
        warn = "" if bars >= PRICE_MIN_BARS else f"（⚠️ {PRICE_MIN_BARS} 営業日未満）"
        print(f"  → {bars} 営業日分取得{warn}"
              f"  {price_stats['first_date']} 〜 {price_stats['last_date']}")

    # 7) 需給・セクター・マクロ・過去レポート（ローカルファイル）
    print("[9/9] 需給・セクター・マクロ・過去レポートを読み込み中...")
    supply_demand  = load_supply_demand(code)
    sd_axes        = build_supply_demand_axes(code)
    sector_name    = supply_demand.get("sector", "")
    sector_context = load_sector_context(sector_name)
    macro_context  = load_macro_context()
    past_research  = load_past_research(code)
    print(f"  → セクター: {sector_name or '不明'}"
          f"  需給3軸: {'算出済' if sd_axes.get('available') else '未収録'}"
          f"  過去レポート: {len([s for s in past_research.split('---') if s.strip()])} 件")

    # 7-2) 反応スコア対象日の外部環境。
    #      「特定できる材料が確認できなかった」で終わらせないため、値動きの原因に
    #      なりうる事実（自社開示・国内指数・米国指数・為替・同業）を機械で揃える。
    print("[9/9] 反応スコア対象日の外部環境を取得中（yfinance・J-Quants・TDNet・空売り残）...")
    _shares_out = (sd_axes.get("raw") or {}).get("発行済株式総数")
    reaction_ctx = build_reaction_context(
        code, price_df, tdnet_entries, sector_name, shares_out=_shares_out
    )
    _rdays = [d["date"] for d in reaction_ctx.get("days", [])]
    if _rdays:
        print("  → 対象日: " + " / ".join(d.isoformat() for d in _rdays))
    else:
        print("  → ⚠️ 反応スコア対象日を算出できませんでした")

    # 7-3) Yahoo 掲示板。反応スコア対象日が確定した後に取得し、その前後1日の投稿も
    #      日付つきで拾う（過去日の値動きの説明に当日以外の投稿を使わせないため）。
    print("[9/9] Yahoo掲示板を取得中（投稿日時つき・対象日周辺も遡る）...")
    bbs_data = fetch_bbs_for_deep_dive(code, target_days=_rdays)
    if bbs_data.get("error"):
        print(f"  → エラー: {bbs_data['error']}")
    else:
        _dp = bbs_data.get("day_posts", {}) or {}
        _oldest = bbs_data.get("oldest")
        print(f"  → 直近 {len(bbs_data.get('posts', []))} 件"
              f" / 対象日周辺 {sum(len(v) for v in _dp.values())} 件（{len(_dp)} 日分）"
              f" / 最古 {_oldest.isoformat() if _oldest else '不明'}"
              f"  感情: {bbs_data.get('sentiment', 'なし')}")

    # Markdown 生成・保存
    print("Markdown を生成・保存中...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().strftime("%Y-%m-%d")
    out_path  = OUTPUT_DIR / f"{code}_{today_str}_data.md"

    md = build_data_markdown(
        code, company_data, financials, text_blocks,
        segments=segments,
        major_shareholders=major_shareholders,
        directors=directors,
        relations=relations,
        large_holdings=large_holdings,
        bbs_data=bbs_data,
        tdnet_entries=tdnet_entries,
        news_items=news_items,
        supply_demand=supply_demand,
        sector_context=sector_context,
        macro_context=macro_context,
        past_research=past_research,
        price_stats=price_stats,
        provenance=provenance,
        pdf_sections=pdf_sections,
        warrants=warrants,
        split_info=split_info,
        ir_decks=ir_decks,
        sd_axes=sd_axes,
        guidance=guidance,
        reaction_ctx=reaction_ctx,
    )
    out_path.write_text(md, encoding="utf-8")
    char_count = len(md)
    token_est  = char_count // 3
    print(f"\n保存完了: {out_path}")
    print(f"文字数: {char_count:,}  推定トークン: {token_est:,}")
    print("取得経路: " + " / ".join(f"{k}={v}" for k, v in provenance.items()))
    print("→ Claudeにこのファイルを渡してDeep Diveレポートを依頼してください。")


if __name__ == "__main__":
    main()
