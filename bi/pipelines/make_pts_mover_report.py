"""
夜間PTS動意レポート raw 生成 v5（日次動意と同一プロセス・値動きデータのみ PTS 差し替え）
========================================================================
日次動意レポート（make_mover_report.py）の**材料生成プロセスをそのまま流用**し、
差し替えるのは「値動きデータ」だけ：
  動意 = JQuants EOD の前日比ランキング
  PTS  = カブラボ（値上がり表・値下がり表・AI材料解説）の当日終値比ランキング

値動きデータ元は**カブラボ1本**（値上がり表・値下がり表の2表を取る）。株探は GHA の
クラウド IP から 405 で恒常的に弾かれ、値下がり・売買代金・テーマが全滅していたため、
値下がりはカブラボの値下がり表へ移し、売買代金 Top セクションと事業テーマ欄は廃止した。

材料（＝動意と完全に同じ関数を流用）:
  - TDNet 銘柄別 atom + PDF本文 …… make_mover_report.fetch_tdnet_batch
  - EDINET事業内容 + Yahoo!ファイナンス ニュース + Yahoo掲示板 …… make_mover_report.fetch_yahoo_batch
  - 過去リサーチ（Deep Dive）…… make_mover_report.load_research_context
  - 需給ブロック（信用残・機関空売り・株価水準）…… make_mover_report.build_supply_block（60営業日OHLC）
  - 時価総額 …… screening_master（当日終値×発行済株数）

指標は「当日終値比」のみ（前日比は使わない）。3市場を1本に束ねるため各銘柄行に市場を付す。
本文（何の会社/なぜ動いた）はローカルの Claude が prompts/pts-mover-report.md に従い執筆し、
send_report_pdf_discord.py --kind pts_movers で PDF 配信する。

出力:
  market/daily/YYYY-MM-DD_pts_movers_raw.md          …… raw 本体
  market/daily/YYYY-MM-DD_pts_quality_flags.txt      …… 誌面へ転記する品質注記のみ（階層1）
  market/daily/YYYY-MM-DD_pts_internal_flags.txt     …… 内部フラグのみ（階層2・誌面へ出さない）

実行:
  cd bi/pipelines
  python make_pts_mover_report.py --date 2026-07-16
  python make_pts_mover_report.py --fast   # PDF本文・掲示板・OHLC等の重い取得をスキップ
"""

from __future__ import annotations

import argparse
import io
import os
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from jq_client_utils import normalize_code_4
from make_mover_report import (
    build_supply_block,
    fetch_ohlc_history,
    fetch_tdnet_batch,
    fetch_yahoo_batch,
    fetch_yahoo_disclosures,
    filter_by_days,
    _BROWSER_HEADERS,
    _get_with_retry,
    filter_same_day_disclosures,
    load_research_context,
    merge_disclosure_entries,
    news_block_label,
    estimate_tokens,
    log_token_usage,
    DEFAULT_TDNET_DAYS,
    FETCH_ERRORS,
    JST,
    MARKET_DAILY_DIR,
    SCREENING_MASTER_PATH,
)
from theme_radar import (
    build_desc_lookup,
    build_material_lookup,
    compute_theme_heat_v2,
    detect_night,
    render_heat_section,
    render_internal_flags,
    render_reason_material,
    render_today_candidates,
)

BASE_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# 抽出パラメータ（当日終値比のみ）
# ---------------------------------------------------------------------------
UP_PCT_MIN = 3.0
DOWN_PCT_MAX = -3.0
VALUE_MIN = 3_000_000
UP_MAX = 10
DOWN_MAX = 5

KABURABO_URL = "https://kabu-lab.com/pts/"
# 株探は GHA のクラウド IP から 405（WAF 拒否）が常態のため値下がりの予備経路にのみ残す。
# 通常はカブラボの値下がり表で満たされるためアクセス自体が発生しない。
_KABUTAN_DOWN_URL = "https://kabutan.jp/warning/pts_night_price_decrease"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Referer": "https://kabutan.jp/",
}

_SHARES_COL = "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"
# 需給ブロック（build_supply_block）が参照する screening_master 列（動意と同一）
_SUPPLY_COLS = [
    "LongMarginTradeVolume", "ShortMarginTradeVolume",
    *[f"LongMargin_WkSeq0{i}" for i in range(1, 9)],
    *[f"ShortMargin_WkSeq0{i}" for i in range(1, 9)],
    "Scr_LongMargin_to_SharesOutstanding", "Scr_LongMargin_to_AvgVol5d",
    "ShortPositionsToSharesOutstandingRatio",
    *[f"ShortSale_WkSeq0{i}" for i in range(5, 9)],
    "AvgDailyVolume5d", "AvgDailyValue5d", _SHARES_COL,
]

_MARKET_MAP = {
    "東Ｐ": "プライム", "東Ｓ": "スタンダード", "東Ｇ": "グロース",
    "東Ｅ": "ETF/ETN", "名Ｍ": "名証メイン", "名Ｎ": "名証ネクスト",
    "札": "札証", "福": "福証",
}
_ETF_MARKET = {"東Ｅ"}
_ETF_REIT_NAME = re.compile(
    r"ETF|ETN|上場投信|上場投資信託|投信|NEXT FUNDS|iShares|MAXIS|ダイワ上場|"
    r"日経レバ|日経ダブル|指数連動|連動型上場|レバレッジ|インバース|"
    r"ブル\d|ベア\d|J-REIT|REIT|リート|不動産投資法人|投資法人|インフラファンド"
)
_CELL_RE = re.compile(r"^\s*(\S+?)\s+([0-9]{3}[0-9A-Za-z])\s*(.+?)\s*$")

# 当日開示ブロックの判定時刻（引け後＝15時以降を「本日の適時開示」とする）
DISCLOSURE_SINCE_HOUR = 15
# 株探の取得失敗（GHA からは 405 で弾かれる）を無警告にしないための記録
_KABUTAN_ERRORS: list[str] = []
_KABUTAN_SESSION: requests.Session | None = None
# 一度ブロックされたら以降の株探アクセスを丸ごと止める（405 へのリトライで raw 生成が伸びるため）
_KABUTAN_BLOCKED = False


def _get_kabutan_session() -> requests.Session:
    """株探用 requests.Session（ブラウザ相当ヘッダー + トップ訪問で cookie 取得）。

    GHA からの 405 は WAF による拒否であり GET/URL 自体は正しい（ローカルからは 200）。
    セッション cookie とブラウザ相当ヘッダーで bot 判定を回避できるかを試す経路。
    """
    global _KABUTAN_SESSION, _KABUTAN_BLOCKED
    if _KABUTAN_SESSION is not None:
        return _KABUTAN_SESSION
    s = requests.Session()
    s.headers.update(_BROWSER_HEADERS | {"Referer": "https://kabutan.jp/"})
    try:
        r = s.get("https://kabutan.jp/", timeout=20)
        if r.status_code != 200:
            _KABUTAN_BLOCKED = True
            _KABUTAN_ERRORS.append(f"top: HTTP {r.status_code}")
    except Exception as e:
        print(f"  [WARN] 株探トップのセッション初期化失敗: {e}")
        _KABUTAN_ERRORS.append(f"top: {e}")
        _KABUTAN_BLOCKED = True
    _KABUTAN_SESSION = s
    return s


def _num(v) -> float | None:
    s = str(v).replace(",", "").replace("円", "").replace("株", "").strip()
    if s in ("", "－", "-", "nan", "None"):
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def is_etf_reit(market_raw: str, name: str) -> bool:
    return (str(market_raw) in _ETF_MARKET) or bool(_ETF_REIT_NAME.search(str(name)))


class SourceError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 値動きデータ（PTS）: カブラボ + 株探
# ---------------------------------------------------------------------------

def _parse_kaburabo_table(table: pd.DataFrame) -> pd.DataFrame:
    """カブラボのランキング表1本を共通スキーマへ変換する。

    値上がり表・値下がり表はカラム構成が同一（順位 / 市場・コード・銘柄名 / 当日終値 /
    PTS価格 / 値上がり(値下がり) / 騰落率 / 出来高 / 上昇(下落)の理由）のため同じ位置指定で読める。
    """
    rows = []
    for _, r0 in table.iterrows():
        cell = str(r0.iloc[1])
        m = _CELL_RE.match(cell)
        if m:
            market_raw, code, name = m.group(1), m.group(2).upper(), m.group(3).strip()
        else:
            cm = re.search(r"([0-9]{3}[0-9A-Za-z])", cell)
            if not cm:
                continue
            code, market_raw, name = cm.group(1).upper(), cell[:cm.start()].strip(), cell[cm.end():].strip()
        rows.append({"Code": code, "Name": name, "MarketRaw": market_raw,
                     "Close": _num(r0.iloc[2]), "PtsPrice": _num(r0.iloc[3]),
                     "DiffPct": _num(r0.iloc[5]), "Volume": _num(r0.iloc[6]),
                     "Reason": str(r0.iloc[7]).strip() if pd.notna(r0.iloc[7]) else ""})
    return pd.DataFrame(rows)


def _kaburabo_direction(table: pd.DataFrame, parsed: pd.DataFrame) -> str:
    """ランキング表が値上がり／値下がりのどちらかを判定する（見出し優先・騰落率符号で保険）。"""
    joined = "".join(str(c) for c in table.columns)
    if "値下がり" in joined or "下落" in joined:
        return "down"
    if "値上がり" in joined or "上昇" in joined:
        return "up"
    s = pd.to_numeric(parsed.get("DiffPct"), errors="coerce").dropna()
    return "down" if (not s.empty and s.median() < 0) else "up"


def scrape_kaburabo() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """カブラボ PTS ページから値上がり表・値下がり表の両方を取得する。

    戻り値: (値上がり df, 値下がり df, 更新時刻)。値下がり表が無い日は空 df を返す。
    """
    try:
        r = requests.get(KABURABO_URL, headers=_HEADERS, timeout=25)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
    except Exception as e:
        raise SourceError(f"カブラボ取得失敗: {e}") from e
    ts_m = re.search(r"20\d\d/\d\d/\d\d\s*\d\d:\d\d\s*更新", r.text)
    updated = ts_m.group().strip() if ts_m else ""
    try:
        tables = pd.read_html(io.StringIO(r.text))
    except ValueError as e:
        raise SourceError(f"カブラボ解析失敗: {e}") from e
    cands = [t for t in tables
             if not t.empty
             and "コード" in "".join(str(c) for c in t.columns)
             and ("騰落率" in "".join(str(c) for c in t.columns) or "PTS" in "".join(str(c) for c in t.columns))]
    if not cands:
        raise SourceError("カブラボ ランキングテーブルなし")
    up_df, down_df = pd.DataFrame(), pd.DataFrame()
    for t in cands:
        parsed = _parse_kaburabo_table(t)
        if parsed.empty:
            continue
        if _kaburabo_direction(t, parsed) == "down":
            if down_df.empty:
                down_df = parsed
        elif up_df.empty:
            up_df = parsed
    if up_df.empty:
        raise SourceError("カブラボ 行解析0件")
    return up_df, down_df, updated


def scrape_kabutan(url: str, max_pages: int = 3) -> tuple[pd.DataFrame, str]:
    """株探のPTSランキングを取得する（カブラボの値下がり表が取れない日の予備経路）。

    GHA のクラウド IP からは WAF に 405 で弾かれるのが常態のため、一度ブロックを検知したら
    以降は即座に空を返してリトライ・ページ送りを打ち切る（raw 生成時間の無駄を作らない）。
    """
    global _KABUTAN_BLOCKED
    rows, stamp = [], ""
    if _KABUTAN_BLOCKED:
        return pd.DataFrame(), ""
    for page in range(1, max_pages + 1):
        u = url if page == 1 else f"{url}?page={page}"
        try:
            r = _get_with_retry(_get_kabutan_session(), u, referer=url, timeout=20, tries=1)
            r.raise_for_status()
            r.encoding = r.apparent_encoding
        except Exception as e:
            print(f"  [WARN] 株探取得失敗 {u}: {e}")
            _KABUTAN_ERRORS.append(f"{u}: {e}")
            _KABUTAN_BLOCKED = True
            break
        if _KABUTAN_BLOCKED:
            break
        if not stamp:
            mt = re.search(r"(\d{1,2})月(\d{1,2})日.*?(\d{1,2}:\d{2})現在", r.text, re.S)
            if mt:
                stamp = f"{int(mt.group(1))}月{int(mt.group(2))}日 {mt.group(3)}現在"
        try:
            tables = pd.read_html(io.StringIO(r.text))
        except ValueError:
            break
        tbl = None
        for t in tables:
            if t.shape[1] >= 13:
                fc = "".join(str(c) for c in (t.columns[0] if isinstance(t.columns[0], tuple) else [t.columns[0]]))
                if "コード" in fc:
                    tbl = t
                    break
        if tbl is None:
            break
        added = 0
        for _, row in tbl.iterrows():
            code = str(row.iloc[0]).strip().upper()[:4]
            if not re.match(r"^[0-9A-Z]{4}$", code):
                continue
            rows.append({"Code": code, "Name": str(row.iloc[1]).strip(), "MarketRaw": str(row.iloc[2]).strip(),
                         "Close": _num(row.iloc[5]), "PtsPrice": _num(row.iloc[6]), "DiffPct": _num(row.iloc[8]),
                         "Volume": _num(row.iloc[9]), "Reason": ""})
            added += 1
        time.sleep(1.0)
        if added == 0:
            break
    return pd.DataFrame(rows), stamp


# 事業テーマ欄（株探個別ページ）は廃止。株探が GHA から取得できず全銘柄で空振りしていた上、
# 「何の会社」は EDINET の事業内容で賄えるため、テーマ取得の HTTP アクセスごと止めた。


# ---------------------------------------------------------------------------
# raw 組み立て（動意 _append_detail をミラー・PTS 価格ヘッダに差し替え）
# ---------------------------------------------------------------------------

def _fmt_value(v) -> str:
    if v is None or pd.isna(v):
        return "─"
    if v >= 1e8:
        return f"{v/1e8:.1f}億円"
    if v >= 1e6:
        return f"{v/1e6:.0f}百万円"
    return f"約{v/1e4:.0f}万円"


def _reason_lines(errors: list[str], limit: int = 5) -> list[str]:
    """内部フラグ用に失敗理由（HTTP status 等）を1件1行で残す（いつから壊れたかの追跡用）。"""
    out = [f"  - {str(e).replace(chr(10), ' ')[:200]}" for e in errors[:limit]]
    if len(errors) > limit:
        out.append(f"  - …他{len(errors) - limit}件")
    return out


def _pts_reason_material_for(code4: str, disc_ctx: dict, tdnet_data: dict,
                             yahoo_data: dict, reason_by_code: dict) -> list[str]:
    """テーマ表の主導銘柄について、raw 内に既にある夜間の動意理由テキストを集めて返す。

    出典は raw の各銘柄ブロックと同一（当日15時以降の適時開示・カブラボ解説・
    TDNet 開示タイトル・Yahoo!ファイナンス ニュース見出し）で、新たな取得は行わない。
    """
    out: list[str] = []
    ctx = (disc_ctx or {}).get(code4, {}) or {}
    for e in (ctx.get("today") or [])[:3]:
        title = str(e.get("title") or "").strip()
        if title:
            out.append(f"本日15時以降の開示　{str(e.get('time_label') or '')} {title}".strip())
    kb = str(reason_by_code.get(code4) or "").strip()
    if kb:
        out.append(f"カブラボ解説　{kb}")
    if not out:
        for e in (ctx.get("recent") or (tdnet_data.get(code4, {}) or {}).get("entries", []))[:2]:
            title = str(e.get("title") or "").strip()
            if title:
                out.append(f"TDNet {str(e.get('published') or '')[:10]}　{title}")
        for n in ((yahoo_data.get(code4, {}) or {}).get("news", []))[:2]:
            title = str(n.get("title") or "").strip()
            if title:
                out.append(f"ニュース　{title}")
    return out


def _stock_block(rec: dict, srow, hist_df, tdnet_data: dict, yahoo_data: dict,
                 real_value, fast: bool,
                 disc_ctx: dict | None = None, target: date | None = None) -> list[str]:
    code4 = normalize_code_4(rec["Code"])
    name = rec.get("CompanyName") if srow is not None and pd.notna(rec.get("CompanyName")) else rec["Name"]
    market = _MARKET_MAP.get(rec.get("MarketRaw", ""), rec.get("MarketRaw", ""))
    sector = rec.get("Sector17CodeName", "")
    cap_oku = rec.get("MarketCapOku")
    cap_str = (f"{cap_oku/10000:.1f}兆円" if pd.notna(cap_oku) and cap_oku >= 10000
               else (f"{cap_oku:,.0f}億円" if pd.notna(cap_oku) else "─"))
    vol = rec.get("Volume")
    vol_str = (f"{vol/1e4:.1f}万株" if pd.notna(vol) and vol >= 1e4 else (f"{int(vol):,}株" if pd.notna(vol) else "─"))
    if real_value is not None:
        val_str = _fmt_value(real_value)
    elif pd.notna(rec.get("PtsPrice")) and pd.notna(vol):
        val_str = _fmt_value(rec["PtsPrice"] * vol) + "（概算=PTS価格×出来高）"
    else:
        val_str = "─"

    ctx = (disc_ctx or {}).get(code4, {})
    today_disc = ctx.get("today")
    if today_disc is None and target is not None:
        today_disc = filter_same_day_disclosures(ctx.get("recent", []), target, DISCLOSURE_SINCE_HOUR)
    today_disc = today_disc or []

    lines = [
        f"### {code4} {name}　PTS {rec['DiffPct']:+.2f}%　[{market}]",
        "",
    ]
    # 当日開示ブロック（本修正の核心）: 引け後の材料の有無を執筆側が機械判定できるよう
    # 0件でも必ず出す。過去日の開示を当日の理由に流用させないための土台。
    if today_disc:
        lines.append(f"**本日の適時開示（{DISCLOSURE_SINCE_HOUR}:00以降・{len(today_disc)}件）:**")
        lines.append("")
        for e in today_disc:
            lines.append(f"- {e.get('time_label', '')} ｜ {e['title']}")
        lines.append("")
    else:
        lines += [f"**本日の適時開示（{DISCLOSURE_SINCE_HOUR}:00以降）:** なし", ""]

    lines += [
        f"- 市場: {market}　セクター: {sector if pd.notna(sector) else '─'}　時価総額: {cap_str}",
        f"- PTS株価: {rec['PtsPrice']:,.1f}円　当日終値: {rec['Close']:,.0f}円　当日終値比: {rec['DiffPct']:+.2f}%",
        f"- 夜間出来高: {vol_str}　夜間売買代金: {val_str}",
    ]
    desc = yahoo_data.get(code4, {}).get("description", "")
    if desc:
        lines.append(f"- 事業内容(EDINET): {desc}")
    if rec.get("Reason"):
        _dir = "下落" if (rec.get("DiffPct") or 0) < 0 else "上昇"
        lines.append(f"- カブラボ解説（夜間{_dir}の理由）: {rec['Reason']}")
    lines.append("")

    # 需給ブロック（動意と同一関数）
    if srow is not None:
        try:
            lines += build_supply_block(srow, hist_df)
        except Exception:
            pass

    # 過去リサーチ
    research = load_research_context(code4, sector if isinstance(sector, str) else "")
    if research:
        lines += ["**過去リサーチ:**", "", research, ""]

    # TDNet（動意と同一・銘柄別atom+PDF）＋ Yahoo適時開示タブをマージした過去材料
    tdnet = tdnet_data.get(code4, {})
    entries = ctx.get("recent") or tdnet.get("entries", [])
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

    # 銘柄別ニュース（Yahoo!ファイナンス ニュースタブ）
    news = yahoo_data.get(code4, {}).get("news", [])
    if news:
        lines.append(f"**{news_block_label(news)}（{len(news)}件）:**")
        lines.append("")
        for n in news:
            lines.append(f"- {n['title']}")
        lines.append("")
    else:
        lines += ["**Yahoo!ファイナンス ニュース:** なし", ""]

    # Yahoo掲示板（材料の裏取り用）
    bbs = yahoo_data.get(code4, {}).get("bbs", {})
    sent, posts = bbs.get("sentiment", ""), bbs.get("posts", [])
    if sent or posts:
        lines.append("**Yahoo掲示板（材料の裏取り用・本文には転記しない）:**")
        if sent:
            lines.append(f"- みんなの評価: {sent}")
        for p in posts[:4]:
            body = p.get("body", "") if isinstance(p, dict) else str(p)
            if body:
                lines.append(f"> {body[:120]}")
        lines.append("")
    lines.append("")
    return lines


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description="夜間PTS動意レポート raw 生成 v5（動意と同一プロセス）")
    parser.add_argument("--date", default=None, help="対象取引日 YYYY-MM-DD（省略時: 午前7時まで=前日）")
    parser.add_argument("--fast", action="store_true", help="PDF本文・掲示板・OHLC等の重い取得をスキップ")
    parser.add_argument("--out-dir", default=None,
                        help="出力先ディレクトリ（省略時 market/daily・検証時に本番を汚さないため）")
    args = parser.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else MARKET_DAILY_DIR

    if args.date:
        target = date.fromisoformat(args.date)
    else:
        now = datetime.now()
        target = now.date() - timedelta(days=1) if now.time() < dtime(7, 0) else now.date()
    print(f"対象取引日: {target}")

    # 階層1: 誌面へ転記する品質注記（raw 冒頭の ⚠️ 行 + {date}_pts_quality_flags.txt）。
    #   顧客提出水準の定型文のみ。例外文字列・URL・HTTPステータス・取得元名を入れてはならない
    #   （内部フラグが誌面へ漏出した 2026-08-17/18 の再発防止）。
    quality_notes: list[str] = []
    # 階層2: 内部フラグのみ（raw 末尾セクション + {date}_pts_internal_flags.txt。誌面へは出さない）
    internal_flags: list[str] = []

    # === 値動きデータ（PTS）= カブラボ1本（値上がり表 + 値下がり表）===
    try:
        up_raw, down_kb, kb_updated = scrape_kaburabo()
        print(f"カブラボ: 値上がり {len(up_raw)} 件 / 値下がり {len(down_kb)} 件（{kb_updated}）")
    except SourceError as e:
        up_raw, down_kb, kb_updated = pd.DataFrame(), pd.DataFrame(), ""
        quality_notes.append("値上がりランキングを取得できず、本日は掲載銘柄が通常より少なくなっています。")
        internal_flags.append(f"- 値上がりデータ元（カブラボ）取得失敗: {str(e)[:200]}")
        print(f"  [ERROR] {e}")

    up_df = up_raw.copy()
    if not up_df.empty:
        up_df["_etf"] = up_df.apply(lambda r: is_etf_reit(r["MarketRaw"], r["Name"]), axis=1)
        up_df["_val"] = pd.to_numeric(up_df["PtsPrice"], errors="coerce") * pd.to_numeric(up_df["Volume"], errors="coerce")
        up_df = up_df[(pd.to_numeric(up_df["DiffPct"], errors="coerce") >= UP_PCT_MIN)
                      & (up_df["_val"].fillna(0) >= VALUE_MIN) & (~up_df["_etf"])] \
            .sort_values("DiffPct", ascending=False).head(UP_MAX).reset_index(drop=True)
    print(f"値上がり: {len(up_df)} 件")

    # 値下がりはカブラボの値下がり表を正とする。株探は GHA から 405 で恒常的に取れないため、
    # カブラボ側が空の日だけ予備として叩く（ブロック検知後は即スキップ）。
    down_raw, kt_stamp = down_kb.copy(), ""
    if down_raw.empty:
        down_raw, kt_stamp = scrape_kabutan(_KABUTAN_DOWN_URL)
    down_df = down_raw.copy()
    if not down_df.empty:
        down_df["_etf"] = down_df.apply(lambda r: is_etf_reit(r["MarketRaw"], r["Name"]), axis=1)
        down_df["_val"] = pd.to_numeric(down_df["PtsPrice"], errors="coerce") * pd.to_numeric(down_df["Volume"], errors="coerce")
        down_df = down_df[(pd.to_numeric(down_df["DiffPct"], errors="coerce") <= DOWN_PCT_MAX)
                          & (down_df["_val"].fillna(0) >= VALUE_MIN) & (~down_df["_etf"])] \
            .sort_values("DiffPct").head(DOWN_MAX).reset_index(drop=True)
    # 値下がりが 0 件でも誌面注記は出さず該当セクションを省略する（主役は値上がり）。
    print(f"値下がり: {len(down_df)} 件")

    # 売買代金 Top セクションは廃止（円建て実額の唯一の取得元だった株探が GHA から取れず、
    # カブラボに売買代金の列が無いため。夜間売買代金は各銘柄行の「PTS価格×出来高」の概算で出す）。
    value_lookup: dict[str, float] = {}

    all_codes: list[str] = []
    for d in (up_df, down_df):
        if not d.empty:
            all_codes += d["Code"].tolist()
    all_codes = list(dict.fromkeys(all_codes))
    codes4 = list(dict.fromkeys(normalize_code_4(c) for c in all_codes))

    # === 材料（動意と同一の関数を流用）===
    master = pd.read_parquet(SCREENING_MASTER_PATH)
    master["Code"] = master["Code"].astype(str).str[:4]
    meta_cols = ["Code", "CompanyName", "Sector17CodeName", "MarketCodeName", "MarketCap"] + _SUPPLY_COLS
    meta_cols = [c for c in meta_cols if c in master.columns]
    meta = master[meta_cols].drop_duplicates("Code").set_index("Code")

    client = None
    hist_df = None
    if not args.fast:
        api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
        if api_key:
            try:
                import jquantsapi
                client = jquantsapi.ClientV2(api_key=api_key)
                hist_df = fetch_ohlc_history(client, set(codes4), target, n_days=60)
                print(f"60営業日OHLC: {len(hist_df) if hist_df is not None else 0} 行")
            except Exception as e:
                print(f"  [WARN] OHLC取得失敗（需給の株価水準スキップ）: {e}")
        else:
            print("  [WARN] JQUANTS_API_KEY 未設定（需給の株価水準スキップ）")

    print("TDNet 銘柄別取得中...")
    tdnet_data = fetch_tdnet_batch(codes4, no_pdf=args.fast) if codes4 else {}
    print("EDINET/Yahoo!ファイナンス ニュース/Yahoo掲示板 取得中...")
    yahoo_data = fetch_yahoo_batch(codes4) if (codes4 and not args.fast) else {}

    # Yahoo適時開示タブ（時刻付き）。yanoshin の TDnet ミラーは銘柄単位で当日分を
    # 落とすことがあるため、当日開示の有無はこちらで裏取りする。
    print("Yahoo適時開示タブ 取得中...")
    disc_ctx: dict[str, dict] = {}
    for c in codes4:
        res = fetch_yahoo_disclosures(c)
        merged = merge_disclosure_entries(tdnet_data.get(c, {}).get("entries", []), res["entries"])
        disc_ctx[c] = {
            "recent": filter_by_days(merged, DEFAULT_TDNET_DAYS),
            "today": filter_same_day_disclosures(merged, target, DISCLOSURE_SINCE_HOUR),
            "error": res["error"],
        }
        time.sleep(0.5)
    n_no_today = sum(1 for c in codes4 if not disc_ctx[c]["today"])
    if codes4:
        print(f"当日{DISCLOSURE_SINCE_HOUR}時以降の適時開示: {len(codes4) - n_no_today}/{len(codes4)} 銘柄で検出")

    # === 品質注記（無警告劣化の可視化・2階層）===
    # 階層1（誌面転記）: 当日開示の有無を判定できない＝今回の再発防止の要。通常は成功する。
    # 顧客提出水準のため取得元名・URL・HTTPステータス・例外文字列を書かない（詳細は階層2へ）。
    n_disc_err = len({e.split(":")[0] for e in FETCH_ERRORS["yahoo_disclosure"]})
    if n_disc_err:
        quality_notes.append(f"当日の開示情報を確認できなかった銘柄が{n_disc_err}件あります。")
        internal_flags.append(f"- Yahoo適時開示タブ取得失敗 {n_disc_err}銘柄（当日開示の裏取り不可）")
        internal_flags += _reason_lines(FETCH_ERRORS["yahoo_disclosure"])

    # 階層2（内部フラグのみ）: 株探405・当日開示0件は常態のため誌面に出すとノイズになり、
    # 本当に材料が欠けた日の警告が埋もれる。運用追跡用に理由付きで残す。
    if FETCH_ERRORS["news"]:
        lost = {e.split(":")[0].strip() for e in FETCH_ERRORS["news"]}
        internal_flags.append(f"- 銘柄別ニュース未取得 {len(lost)}銘柄（材料が一部欠落）")
        internal_flags += _reason_lines(FETCH_ERRORS["news"])
    if _KABUTAN_ERRORS:
        internal_flags.append(f"- 株探取得失敗 {len(_KABUTAN_ERRORS)}件")
        internal_flags += _reason_lines(_KABUTAN_ERRORS)
    if codes4 and n_no_today:
        no_today_codes = [c for c in codes4 if not disc_ctx[c]["today"]]
        internal_flags.append(
            f"- 当日{DISCLOSURE_SINCE_HOUR}時以降の適時開示が0件: {n_no_today}/{len(codes4)}銘柄")
        internal_flags += _reason_lines([f"{c}: 当日開示なし" for c in no_today_codes])

    # === raw 出力 ===
    lines = [
        f"# 夜間PTS動意レポート 生データ ({target})",
        "",
        "> 値動きデータ＝カブラボ（値上がり表・値下がり表・AI材料解説）の当日終値比。",
        "> 材料（TDNet銘柄別/EDINET/Yahoo!ファイナンス/Yahoo掲示板/過去リサーチ/需給）は日次動意と同一プロセス。",
        "> Claude が prompts/pts-mover-report.md に従い「何の会社」「なぜ動いた」を執筆する。",
        f"- **生成日時**: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST",
        f"- **PTSデータ時点**: カブラボ {kb_updated or '─'}" + (f" / 株探 {kt_stamp}" if kt_stamp else ""),
        f"- **対象取引日**: {target}（当日終値比・ナイトタイムセッション17:00〜翌6:00）",
        "",
    ]
    if quality_notes:
        lines += [f"> ⚠️ **品質注記**: {'／'.join(quality_notes)}", ""]

    def _emit(df: pd.DataFrame) -> list[str]:
        out = []
        for _, r in df.iterrows():
            rec = r.to_dict()
            code4 = normalize_code_4(rec["Code"])
            srow = None
            if code4 in meta.index:
                srow = meta.loc[code4].copy()
                srow["Code"] = code4
                srow["Close_T"] = rec.get("Close")
                rec["CompanyName"] = srow.get("CompanyName")
                rec["Sector17CodeName"] = srow.get("Sector17CodeName")
                shares = srow.get(_SHARES_COL)
                if pd.notna(shares) and pd.notna(rec.get("Close")):
                    rec["MarketCapOku"] = rec["Close"] * shares / 1e8
                else:
                    rec["MarketCapOku"] = pd.NA
            else:
                rec["MarketCapOku"] = pd.NA
            out += _stock_block(rec, srow, hist_df, tdnet_data, yahoo_data,
                                value_lookup.get(rec["Code"]), args.fast,
                                disc_ctx, target)
        return out

    # カブラボ解説（夜間の動意理由）をコード別に引けるようにする（理由素材の材料）
    reason_by_code: dict[str, str] = {}
    for _df in (up_df, down_df):
        if _df is None or _df.empty:
            continue
        for _, _r in _df.iterrows():
            _rs = str(_r.get("Reason") or "").strip()
            if _rs:
                reason_by_code.setdefault(normalize_code_4(_r["Code"]), _rs)

    # === テーマ2部（値上がりセクションの前に置く）===
    # 当日部 = 当夜の PTS 上昇銘柄。熱量部 = 昼の動意蓄積10営業日をそのまま使う
    # （当夜の PTS を昼の蓄積へ合算しない）。誌面構成は昼の動意レポートと同一。
    # テーマ表の鮮度警告は内部フラグへ回し、誌面には出さない（内部情報の誌面漏出を防ぐ）。
    try:
        pts_risers = []
        if not up_df.empty:
            for _, r in up_df.iterrows():
                code4 = normalize_code_4(r["Code"])
                name = r["Name"]
                if code4 in meta.index:
                    cn = meta.loc[code4].get("CompanyName")
                    if pd.notna(cn):
                        name = cn
                _vol = r.get("Volume")
                _px = r.get("PtsPrice")
                _tv = (float(_px) * float(_vol)) if (pd.notna(_px) and pd.notna(_vol)) else None
                pts_risers.append({
                    "code": code4,
                    "name": name,
                    "pts_pct": float(r["DiffPct"]),
                    "return_pct": float(r["DiffPct"]),
                    "turnover": _tv,
                    "market": _MARKET_MAP.get(str(r.get("MarketRaw", "")).strip(), r.get("MarketRaw")),
                })
        night = detect_night(pts_risers)
        # 熱量は昼の蓄積のみ（当夜分を重ねない）
        heat = compute_theme_heat_v2(codes_today=None, trade_date=str(target))
        _material_today = lambda code: _pts_reason_material_for(
            code, disc_ctx, tdnet_data, yahoo_data, reason_by_code
        )
        # 当夜部は候補リスト（最大15件・主導銘柄ごとに材料テキスト付き）。
        # テーマ名の付け直し・共通材料の判定・行の絞り込みは Claude が行う（_cr §38）。
        # PTS は当夜騰落率で並ぶため pct_key を pts_pct にする。
        # 「何の会社」欄の素材。出典は EDINET DB の事業概要（yahoo_data の description）。
        _desc_today = lambda code: (
            (yahoo_data.get(normalize_code_4(code), {}) or {}).get("description", "")
        )
        # フォールバック連鎖つき lookup（当夜 → 直近10営業日の昼蓄積 → 業種名）。
        # 夜間 PTS は昼の蓄積 parquet を読むだけで、当夜分の書き込みはしない
        # （昼の動意レポートが同日分を既に保存しているため上書きしない）。
        _desc = build_desc_lookup(primary=_desc_today, trade_date=str(target))
        _material = build_material_lookup(primary=_material_today, trade_date=str(target))
        lines += render_today_candidates(night, _material, pct_key="pts_pct", desc_lookup=_desc)
        # 熱量表は熱量降順。当夜1位テーマは必ず含める（night を渡す）。
        lines += render_heat_section(
            heat, today_result=night, desc_lookup=_desc, material_lookup=_material
        )
        lines += render_reason_material(night, heat, _material)
        internal_flags += render_internal_flags(night)
    except Exception as e:
        # テーマレーダーの失敗で本体レポートを止めない（配信絶対の原則）
        internal_flags.append(f"- テーマスコアラーの生成に失敗: {str(e)[:200]}")
        print(f"  [WARN] theme_radar: {e}")

    # 値上がり（主役）は必ず出す。値下がりは取得できた時だけセクションを出す
    # （空セクションも⚠️も出さない）。売買代金 Top セクションは廃止済み。
    lines += [f"## 夜間PTS 値上がり（当日終値比 +{UP_PCT_MIN:.0f}%以上・上位{UP_MAX}件）", ""]
    lines += _emit(up_df) if not up_df.empty else ["（該当なし）", ""]
    if not down_df.empty:
        lines += [f"## 夜間PTS 値下がり（当日終値比 {DOWN_PCT_MAX:.0f}%以下・下位{DOWN_MAX}件）", ""]
        lines += _emit(down_df)

    # 内部フラグは raw 末尾の専用セクションに置く（誌面へ転記させないため冒頭には出さない）
    if internal_flags:
        lines += ["---", "", "## 内部品質フラグ（運用追跡用・レポート本文へ転記しない）", ""]
        lines += internal_flags + [""]

    body = "\n".join(lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target}_pts_movers_raw.md"
    out_path.write_text(body, encoding="utf-8")
    # 誌面用フラグ（階層1のみ）と内部フラグ（階層2のみ）を**別ファイル**に分ける。
    # 同一ファイルへ [internal] 区切りで連結すると workflow が全文を誌面へ挿入して
    # 内部情報が配信される（2026-08-17/18 の障害）。連結は恒久禁止。
    flag_text = "／".join(quality_notes)
    (out_dir / f"{target}_pts_quality_flags.txt").write_text(
        (flag_text + "\n") if flag_text else "", encoding="utf-8")
    (out_dir / f"{target}_pts_internal_flags.txt").write_text(
        ("\n".join(internal_flags) + "\n") if internal_flags else "", encoding="utf-8")
    tokens = estimate_tokens(body)
    log_token_usage(target, "make_pts_mover_report", tokens, len(body))
    print(f"\n出力: {out_path}")
    print(f"推定トークン数: {tokens:,}")


if __name__ == "__main__":
    main()
