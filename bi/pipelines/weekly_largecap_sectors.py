"""
週次大型株速報 v2 — セクター選定 + データ組み立てヘルパー。

決算起点ではなく「セクターマップのキープレイヤー」を母集団に、22セクターを
台帳（sector_ledger.json）でローテーション管理する。

母集団:
  research/earnings/coverage_stocks.csv の sources に "sector_map" を含む行
  （watchlist / positions は除外）。sectors 列が 22 セクター識別子（stem）。
  カンマ区切りで複数セクター併記の行は「最初のセクター」に属するとみなす。

2モード:
  --survey [--date YYYY-MM-DD]
    全22セクターについて素材を算出し sector_survey_{date}.csv に出力 + stdout。
    台帳は更新しない（軸B 未発信 + 軸A 参考データ）。

  --select stem1,stem2,... --date YYYY-MM-DD
    指定セクターのキープレイヤー全社について、市場系数値（時価総額・PER・PBR・
    株価・前日比）を yfinance で生成時ライブ取得し、財務（売上・営業利益・純利益）は
    screening_master の年次実績を据え置いて sector_data_{date}.json に出力。
    yfinance は HTTP のみで GHA（クラウド）でも動く（MCP 非依存）。yfinance が
    取れない銘柄のみ screening_master の価格由来値にフォールバック。
    出力後、台帳の last_featured を --date に更新。

例:
  python weekly_largecap_sectors.py --survey --date 2026-06-28
  python weekly_largecap_sectors.py --select 03_finance,02_electronics --date 2026-06-28
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from datetime import date as date_cls
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

COVERAGE_CSV = ROOT / "research" / "earnings" / "coverage_stocks.csv"
SECTORS_DIR = ROOT / "research" / "sectors"
SCREENING_MASTER = ROOT / "bi" / "outputs" / "screening_master.parquet"
SECTOR_WEEKLY = ROOT / "bi" / "outputs" / "sector_weekly.parquet"

LARGECAP_DIR = ROOT / "research" / "largecap"
LEDGER_PATH = LARGECAP_DIR / "sector_ledger.json"

# 22 セクター stem -> 日本語名（research/sectors/*.md の冒頭見出しより）
SECTOR_NAMES: dict[str, str] = {
    "01_automotive": "自動車・輸送用機器",
    "02_electronics": "電機・精密・半導体",
    "03_finance": "銀行・金融",
    "04_trading_companies": "商社",
    "05_telecom": "通信",
    "06_chemicals": "化学",
    "07_pharmaceutical": "医薬品・ヘルスケア",
    "08_it_services": "IT・ネット・サービス",
    "09_heavy_industry": "重工・機械・防衛",
    "10_leisure_consumer": "レジャー消費",
    "11_food_beverage": "食品飲料",
    "12_non_ferrous": "非鉄金属",
    "13_insurance": "保険",
    "14_real_estate": "不動産",
    "15_retail": "小売",
    "16_other_materials": "その他素材",
    "17_construction": "建設住宅",
    "18_railways": "鉄道",
    "19_logistics": "運輸物流",
    "20_energy": "エネルギー",
    "21_utilities": "電力ガス",
    "22_steel": "鉄鋼",
}

# MarketCap 異常値ガード（株式分割未反映バグ等。例: 285A キオクシアHD が 56兆円表示）
MARKETCAP_MAX_YEN = 50e12

logger = logging.getLogger("weekly_largecap_sectors")


# ---------------------------------------------------------------------------
# 共通ロード
# ---------------------------------------------------------------------------
def _today_str() -> str:
    return date_cls.today().isoformat()


def load_keyplayers() -> pd.DataFrame:
    """coverage_stocks.csv から sector_map キープレイヤーのみ抽出。

    返り値の列: code(str), name(str), sector_stem(str=最初のセクター)
    """
    if not COVERAGE_CSV.exists():
        raise SystemExit(f"母集団 CSV がありません: {COVERAGE_CSV}")
    df = pd.read_csv(COVERAGE_CSV, dtype=str).fillna("")
    df["sources"] = df["sources"].astype(str)
    mask = df["sources"].str.contains("sector_map", na=False)
    kp = df.loc[mask, ["code", "name", "sectors"]].copy()
    # 複数セクター併記は最初のセクターに属する
    kp["sector_stem"] = kp["sectors"].str.split(",").str[0].str.strip()
    kp["code"] = kp["code"].astype(str).str.strip()
    kp["name"] = kp["name"].astype(str).str.strip()
    kp = kp[kp["sector_stem"] != ""].reset_index(drop=True)
    return kp[["code", "name", "sector_stem"]]


def load_ledger() -> dict[str, object]:
    """台帳ロード。無ければ22セクター全て null で新規作成して保存。"""
    if LEDGER_PATH.exists():
        with LEDGER_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # 欠落セクターを null 補完（破壊はしない）
        changed = False
        for stem in SECTOR_NAMES:
            if stem not in data:
                data[stem] = None
                changed = True
        if changed:
            save_ledger(data)
        return data
    ledger = {stem: None for stem in SECTOR_NAMES}
    save_ledger(ledger)
    logger.info("台帳を新規作成: %s", LEDGER_PATH)
    return ledger


def save_ledger(ledger: dict[str, object]) -> None:
    LARGECAP_DIR.mkdir(parents=True, exist_ok=True)
    # stem 番号順に整列して書き出す（差分を見やすくする）
    ordered = {stem: ledger.get(stem) for stem in SECTOR_NAMES}
    # SECTOR_NAMES に無い stem も末尾に保持
    for k, v in ledger.items():
        if k not in ordered:
            ordered[k] = v
    with LEDGER_PATH.open("w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _weeks_since(last_featured: object, ref: date_cls) -> float | None:
    """last_featured(YYYY-MM-DD or None) から ref までの経過週数。

    None は「未発信」なので最優先扱いとして float('inf') を返す。
    """
    if last_featured in (None, "", "null"):
        return math.inf
    try:
        d = date_cls.fromisoformat(str(last_featured))
    except ValueError:
        return math.inf
    return (ref - d).days / 7.0


# ---------------------------------------------------------------------------
# 軸A 参考: sector_weekly の直近リターンを stem 単位に集約
# ---------------------------------------------------------------------------
def _build_recent_perf(kp: pd.DataFrame, master: pd.DataFrame) -> dict[str, float | None]:
    """各 stem の「直近1週リターン」を sector_weekly(Return_W01) から集約。

    sector_weekly は JIS17分類(Sector17CodeName)キーで、本レポートの22stemとは
    直接対応しない。そのため各 stem のキープレイヤーが screening_master 上で
    属する Sector17CodeName 群へマップし、その Return_W01 を社数加重平均する。
    sector_weekly が無い場合は全 stem None（空）。
    """
    if not SECTOR_WEEKLY.exists():
        logger.info("sector_weekly が無いため recent_perf はスキップ")
        return {stem: None for stem in SECTOR_NAMES}

    sw = pd.read_parquet(SECTOR_WEEKLY)
    if "Sector17CodeName" not in sw.columns or "Return_W01" not in sw.columns:
        logger.warning("sector_weekly に必要列が無いため recent_perf はスキップ")
        return {stem: None for stem in SECTOR_NAMES}
    w01 = sw.set_index("Sector17CodeName")["Return_W01"].to_dict()

    code_to_sec17 = (
        master.drop_duplicates("Code").set_index("Code")["Sector17CodeName"].to_dict()
        if "Sector17CodeName" in master.columns
        else {}
    )

    out: dict[str, float | None] = {}
    for stem in SECTOR_NAMES:
        codes = kp.loc[kp["sector_stem"] == stem, "code"].tolist()
        vals: list[float] = []
        for c in codes:
            sec17 = code_to_sec17.get(str(c))
            if sec17 is None:
                continue
            r = w01.get(sec17)
            if r is not None and not (isinstance(r, float) and math.isnan(r)):
                vals.append(float(r))
        out[stem] = (sum(vals) / len(vals)) if vals else None
    return out


# ---------------------------------------------------------------------------
# survey モード
# ---------------------------------------------------------------------------
def run_survey(date_str: str) -> Path:
    kp = load_keyplayers()
    ledger = load_ledger()
    master = pd.read_parquet(SCREENING_MASTER, columns=None)
    master["Code"] = master["Code"].astype(str)

    ref = date_cls.fromisoformat(date_str)
    recent_perf = _build_recent_perf(kp, master)

    rows = []
    for stem, jp in SECTOR_NAMES.items():
        n = int((kp["sector_stem"] == stem).sum())
        last = ledger.get(stem)
        ws = _weeks_since(last, ref)
        rp = recent_perf.get(stem)
        rows.append(
            {
                "sector_stem": stem,
                "sector_name": jp,
                "keyplayers": n,
                "last_featured": last if last not in (None, "") else "",
                "weeks_since": ws,
                "recent_perf": rp if rp is not None else "",
            }
        )

    df = pd.DataFrame(rows)
    # weeks_since 降順（inf=未発信が最優先で上に来る）
    df = df.sort_values("weeks_since", ascending=False, kind="stable").reset_index(drop=True)

    LARGECAP_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = LARGECAP_DIR / f"sector_survey_{date_str}.csv"
    # CSV では inf を空文字で表現せず、可読のため 'inf' を残す（未発信の意味）
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    # stdout（見やすく）
    print(f"\n=== 週次大型株 v2 セクターサーベイ ({date_str}) ===")
    print(f"母集団: sector_map キープレイヤー {int(kp.shape[0])} 社 / {len(SECTOR_NAMES)} セクター")
    print(f"{'stem':<20} {'セクター名':<16} {'社数':>4} {'未発信週':>10} {'last_featured':>13} {'直近1wk':>9}")
    print("-" * 78)
    for _, r in df.iterrows():
        ws = r["weeks_since"]
        ws_str = "未発信" if ws == math.inf else f"{ws:.1f}"
        rp = r["recent_perf"]
        rp_str = "" if rp == "" else f"{float(rp) * 100:+.1f}%"
        last_str = r["last_featured"] if r["last_featured"] else "-"
        print(
            f"{r['sector_stem']:<20} {r['sector_name']:<16} {r['keyplayers']:>4} "
            f"{ws_str:>10} {last_str:>13} {rp_str:>9}"
        )
    print("-" * 78)
    print(f"出力: {out_csv}")
    print("（注: 台帳は survey では更新しません）\n")
    return out_csv


# ---------------------------------------------------------------------------
# select モード
# ---------------------------------------------------------------------------
def _safe_num(v: object) -> float | None:
    """NaN / 非数を None に。推定補完はしない（欠損は None=NaN のまま）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _guard_marketcap(mcap: float | None, code: str, name: str) -> float | None:
    """MarketCap 異常値ガード。MARKETCAP_MAX_YEN 超は NaN(None) 化 + 警告。"""
    if mcap is None:
        return None
    if mcap > MARKETCAP_MAX_YEN:
        logger.warning(
            "MarketCap 異常値ガード発動: %s %s = %.3e 円 (> %.0e) -> NaN",
            code,
            name,
            mcap,
            MARKETCAP_MAX_YEN,
        )
        return None
    return mcap


def _fetch_live_market(code: str, retries: int = 2) -> dict[str, float | None]:
    """yfinance で市場系数値を生成時ライブ取得（HTTP のみ・GHA でも動く・MCP 非依存）。

    返り値: market_cap / per / pbr / close / prev_close / change_abs / change_pct。
    取れないフィールドは None。完全失敗時は空 dict（呼び出し側で screening_master に
    フォールバック）。screening_master スナップショットは数日古いため、ここで取れた
    現値を優先する。
    """
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance import 失敗（screening_master にフォールバック）: %s", exc)
        return {}
    last_err: object = None
    for _ in range(retries + 1):
        try:
            info = yf.Ticker(f"{code}.T").info or {}
            close = _safe_num(info.get("currentPrice")) or _safe_num(
                info.get("regularMarketPrice")
            )
            prev = _safe_num(info.get("previousClose"))
            chg = (close - prev) if (close is not None and prev is not None) else None
            chg_pct = (
                (chg / prev * 100.0)
                if (chg is not None and prev not in (None, 0))
                else None
            )
            return {
                "market_cap": _safe_num(info.get("marketCap")),
                "per": _safe_num(info.get("trailingPE")),
                "pbr": _safe_num(info.get("priceToBook")),
                "close": close,
                "prev_close": prev,
                "change_abs": chg,
                "change_pct": chg_pct,
            }
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.6)
    logger.warning("yfinance ライブ取得失敗 %s（フォールバック）: %s", code, last_err)
    return {}


def _company_record(
    row: pd.Series, live: dict | None = None, code: str | None = None
) -> dict[str, object]:
    # master_by_code は Code をインデックス化しているため row には "Code" 列が無い。
    # 呼び出し側のループ変数 c を code 引数で受け取り確実に埋める。
    code = str(code if code is not None else row.get("Code", "")).strip()
    name = str(row.get("CompanyName", "")).strip()
    live = live or {}

    # 市場系数値はライブ（yfinance）優先。取れない銘柄のみ screening_master に
    # フォールバック（数日古い価格由来値）。どちらも異常値ガードを通す。
    mcap_live = _guard_marketcap(_safe_num(live.get("market_cap")), code, name)
    mcap_master = _guard_marketcap(_safe_num(row.get("MarketCap")), code, name)
    market_cap = mcap_live if mcap_live is not None else mcap_master

    per_live = _safe_num(live.get("per"))
    per = per_live if per_live is not None else _safe_num(row.get("PER_Trailing"))
    pbr_live = _safe_num(live.get("pbr"))
    pbr = pbr_live if pbr_live is not None else _safe_num(row.get("PBR_Trailing"))
    close_live = _safe_num(live.get("close"))
    close = close_live if close_live is not None else _safe_num(row.get("Close"))

    price_source = "yfinance" if mcap_live is not None else "screening_master"

    rec = {
        "code": code,
        "name": name,
        "market_cap": market_cap,
        "per": per,
        "pbr": pbr,
        "close": close,
        "prev_close": _safe_num(live.get("prev_close")),
        "change_abs": _safe_num(live.get("change_abs")),
        "change_pct": _safe_num(live.get("change_pct")),
        # 財務は年次実績（screening_master / EDINET・決算時しか変わらないため据え置き）
        "net_sales": _safe_num(row.get("NetSales_LatestYear_Actual")),
        "operating_profit": _safe_num(row.get("OperatingProfit_LatestYear_Actual")),
        "net_income": _safe_num(row.get("Profit_LatestYear_Actual")),
        "price_source": price_source,
    }
    return rec


def run_select(stems: list[str], date_str: str) -> Path:
    kp = load_keyplayers()
    ledger = load_ledger()
    master = pd.read_parquet(SCREENING_MASTER)
    master["Code"] = master["Code"].astype(str)
    master_by_code = master.drop_duplicates("Code").set_index("Code")

    unknown = [s for s in stems if s not in SECTOR_NAMES]
    if unknown:
        raise SystemExit(f"未知のセクター stem: {unknown}（有効: {list(SECTOR_NAMES)}）")

    sectors_out = []
    for stem in stems:
        jp = SECTOR_NAMES[stem]
        codes = kp.loc[kp["sector_stem"] == stem, "code"].tolist()
        companies = []
        for c in codes:
            c = str(c)
            if c not in master_by_code.index:
                logger.warning("screening_master 未収録: %s (stem=%s)", c, stem)
                continue
            live = _fetch_live_market(c)
            companies.append(_company_record(master_by_code.loc[c], live, code=c))
        # MarketCap 降順（None は末尾）
        companies.sort(
            key=lambda r: (r["market_cap"] is None, -(r["market_cap"] or 0.0))
        )
        sectors_out.append(
            {"sector_stem": stem, "sector_name": jp, "companies": companies}
        )

    payload = {
        "date": date_str,
        "marketcap_guard_yen": MARKETCAP_MAX_YEN,
        "market_numbers_source": "yfinance(live) / fallback: screening_master",
        "sectors": sectors_out,
    }

    LARGECAP_DIR.mkdir(parents=True, exist_ok=True)
    out_json = LARGECAP_DIR / f"sector_data_{date_str}.json"
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # 台帳更新（指定セクターの last_featured を date に）
    for stem in stems:
        ledger[stem] = date_str
    save_ledger(ledger)

    print(f"\n=== 週次大型株 v2 セクターデータ抽出 ({date_str}) ===")
    for s in sectors_out:
        n = len(s["companies"])
        with_mcap = sum(1 for c in s["companies"] if c["market_cap"] is not None)
        live_n = sum(1 for c in s["companies"] if c.get("price_source") == "yfinance")
        print(f"  {s['sector_stem']:<20} {s['sector_name']:<16} {n:>3} 社 "
              f"(MarketCap 有効 {with_mcap} / ライブ {live_n})")
    print(f"出力: {out_json}")
    print(f"台帳更新: {LEDGER_PATH}（last_featured = {date_str}）\n")
    return out_json


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    p = argparse.ArgumentParser(
        description="週次大型株速報 v2 のセクター選定 + データ組み立て"
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--survey",
        action="store_true",
        help="全22セクターの素材を sector_survey_{date}.csv に出力（台帳は更新しない）",
    )
    mode.add_argument(
        "--select",
        type=str,
        default=None,
        metavar="stem1,stem2,...",
        help="指定セクターの数値を sector_data_{date}.json に出力し台帳を更新",
    )
    p.add_argument("--date", type=str, default=None, help="基準日 YYYY-MM-DD（省略時は本日）")
    args = p.parse_args()

    date_str = args.date or _today_str()
    # 妥当性チェック
    try:
        date_cls.fromisoformat(date_str)
    except ValueError:
        raise SystemExit(f"--date が不正です: {date_str}（YYYY-MM-DD）") from None

    if not SCREENING_MASTER.exists():
        raise SystemExit(f"screening_master がありません: {SCREENING_MASTER}")

    if args.survey:
        run_survey(date_str)
    else:
        stems = [s.strip() for s in args.select.split(",") if s.strip()]
        if not stems:
            raise SystemExit("--select に有効な stem がありません")
        run_select(stems, date_str)


if __name__ == "__main__":
    main()
