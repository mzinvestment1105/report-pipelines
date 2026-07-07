"""fetch_position_quotes.py — 保有ポジションの現値・テクニカル raw 生成（GHA/ローカル共通）

portfolio/positions.md の「## 保有ポジション」表から銘柄コードを抽出し、
yfinance（{code}.T・HTTP のみ・GHA でも動く・MCP 非依存）で現値・前日比・
当日高安・出来高・時価総額を取得、日足終値から SMA20/25/50/75 と
BB(20日) ±2σ/±3σ を算出して market/daily/{date}_position_quotes_raw.md に出力する。

/position-check の GHA 版（prompts/position-check.md）が本 raw を一次情報として
アラート機械照合（撤退ライン・SMA50 連動・BB+3σ 等）に使う。
{YYYY-MM-DD}_*_raw.md 命名のため rotate_report_archives.py の raw ローテ対象
（7日超で market/daily/archive/raw/ へ自動退避）。

使い方:
  python fetch_position_quotes.py [--date YYYY-MM-DD] [--session am|pm]

終了コード:
  0 = 正常（1 銘柄以上の取得成功・raw 出力済み）
  1 = positions.md 不在/パース失敗・保有銘柄ゼロ・全銘柄で yfinance 取得失敗
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("fetch_position_quotes")

REPO_ROOT = Path(__file__).resolve().parents[2]
POSITIONS_MD = REPO_ROOT / "portfolio" / "positions.md"
OUT_DIR = REPO_ROOT / "market" / "daily"

JST = ZoneInfo("Asia/Tokyo")
CODE_RE = re.compile(r"^[0-9][0-9A-Z]{3}$")


def _safe_num(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def parse_positions(md_text: str) -> list[dict]:
    """positions.md の「## 保有ポジション」表から保有銘柄行を抽出する。

    返り値: [{code, name, direction, quantity, avg_price}, ...]
    表が見つからない・銘柄ゼロなら空リスト（呼び出し側で exit 1）。
    """
    lines = md_text.splitlines()
    in_section = False
    rows: list[dict] = []
    for line in lines:
        if line.startswith("## "):
            in_section = "保有ポジション" in line
            continue
        if not in_section or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        code = cells[0]
        if not CODE_RE.match(code):
            continue  # ヘッダ行・区切り行・注記行
        qty_m = re.search(r"[\d,]+", cells[3])
        price_m = re.search(r"[\d,.]+", cells[4])
        rows.append(
            {
                "code": code,
                "name": cells[1],
                "direction": cells[2],
                "quantity": int(qty_m.group().replace(",", "")) if qty_m else None,
                "avg_price": float(price_m.group().replace(",", "")) if price_m else None,
            }
        )
    return rows


def _sma(closes, n: int) -> float | None:
    if len(closes) < n:
        return None
    return float(closes.tail(n).mean())


def fetch_quote(code: str, retries: int = 2) -> dict | None:
    """yfinance で 1 銘柄の現値・テクニカルを取得（HTTP のみ・MCP 非依存）。

    返り値 dict（取れないフィールドは None）。履歴すら取れない完全失敗は None。
    """
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        logger.error("yfinance import 失敗: %s", exc)
        return None

    ticker = None
    hist = None
    last_err: object = None
    for _ in range(retries + 1):
        try:
            ticker = yf.Ticker(f"{code}.T")
            # auto_adjust=False: 終値は分割調整済み・配当未調整（TradingView の
            # SMA/BB と同じ基準。配当調整で過去終値がずれるのを防ぐ）
            hist = ticker.history(period="9mo", interval="1d", auto_adjust=False)
            if hist is not None and not hist.empty:
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.6)
    if hist is None or hist.empty:
        logger.error("yfinance 履歴取得失敗 %s: %s", code, last_err)
        return None

    closes = hist["Close"].dropna()
    if closes.empty:
        logger.error("yfinance 終値が空 %s", code)
        return None

    last_bar_date = closes.index[-1].strftime("%Y-%m-%d")
    last_close = float(closes.iloc[-1])
    prev_bar_close = float(closes.iloc[-2]) if len(closes) >= 2 else None
    prev_bar_date = (
        closes.index[-2].strftime("%Y-%m-%d") if len(closes) >= 2 else None
    )

    info: dict = {}
    for _ in range(retries + 1):
        try:
            info = ticker.info or {}
            if info:
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.6)
    if not info:
        logger.warning("yfinance info 取得失敗 %s（履歴のみで継続）: %s", code, last_err)

    current = _safe_num(info.get("currentPrice")) or _safe_num(
        info.get("regularMarketPrice")
    )
    if current is None:
        current = last_close
    prev_close = _safe_num(info.get("previousClose"))
    if prev_close is None:
        prev_close = prev_bar_close

    chg = (current - prev_close) if prev_close is not None else None
    chg_pct = (chg / prev_close * 100) if (chg is not None and prev_close) else None

    day_high = _safe_num(info.get("dayHigh")) or _safe_num(
        info.get("regularMarketDayHigh")
    )
    day_low = _safe_num(info.get("dayLow")) or _safe_num(
        info.get("regularMarketDayLow")
    )
    volume = _safe_num(info.get("volume")) or _safe_num(
        info.get("regularMarketVolume")
    )
    if day_high is None and "High" in hist.columns:
        day_high = _safe_num(hist["High"].iloc[-1])
    if day_low is None and "Low" in hist.columns:
        day_low = _safe_num(hist["Low"].iloc[-1])
    if volume is None and "Volume" in hist.columns:
        volume = _safe_num(hist["Volume"].iloc[-1])
    market_cap = _safe_num(info.get("marketCap"))

    sma20 = _sma(closes, 20)
    sma25 = _sma(closes, 25)
    sma50 = _sma(closes, 50)
    sma75 = _sma(closes, 75)

    bb = {}
    if sma20 is not None and len(closes) >= 20:
        std20 = float(closes.tail(20).std(ddof=0))
        bb = {
            "p2": sma20 + 2 * std20,
            "p3": sma20 + 3 * std20,
            "m2": sma20 - 2 * std20,
            "m3": sma20 - 3 * std20,
        }

    # 次回決算日（yfinance ベストエフォート・取れなければ省略）
    next_earnings = None
    try:
        cal = ticker.calendar
        dates = None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date")
        elif cal is not None and hasattr(cal, "empty") and not cal.empty:
            dates = list(cal.loc["Earnings Date"]) if "Earnings Date" in cal.index else None
        if dates:
            first = dates[0] if isinstance(dates, (list, tuple)) else dates
            next_earnings = str(first)[:10]
    except Exception:  # noqa: BLE001
        pass

    return {
        "code": code,
        "current": current,
        "prev_close": prev_close,
        "change_abs": chg,
        "change_pct": chg_pct,
        "day_high": day_high,
        "day_low": day_low,
        "volume": volume,
        "market_cap": market_cap,
        "last_bar_date": last_bar_date,
        "last_close": last_close,
        "prev_bar_date": prev_bar_date,
        "prev_bar_close": prev_bar_close,
        "sma20": sma20,
        "sma25": sma25,
        "sma50": sma50,
        "sma75": sma75,
        "bb": bb,
        "next_earnings": next_earnings,
    }


def _fmt(v: float | None, unit: str = "円", nd: int = 2) -> str:
    if v is None:
        return ""
    return f"{v:,.{nd}f}{unit}"


def _gap_line(label: str, line_val: float | None, current: float | None) -> str | None:
    """「現値とラインの距離」を計算根拠付きで 1 行にする（距離% = (現値−ライン)÷現値）。"""
    if line_val is None or current in (None, 0):
        return None
    gap = current - line_val
    pct = gap / current * 100
    return (
        f"- {label}: {line_val:,.2f}円（現値−ライン = {gap:+,.2f}円・"
        f"距離 {pct:+.2f}%＝(現値−ライン)÷現値）"
    )


def build_markdown(
    positions: list[dict], quotes: dict[str, dict], date_str: str, session: str | None
) -> str:
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    ses = f"（session: {session}）" if session else ""
    lines = [
        f"# ポジション現値 raw {date_str}{ses}",
        "",
        f"- 取得時刻: {now_jst}",
        "- 取得手段: yfinance（{code}.T・HTTP・生成時ライブ取得）。SMA/BB は日足終値"
        "（分割調整済み・配当未調整）で算出。",
        "- 本ファイルは /position-check（GHA 版）の一次情報 raw。レポート本文ではない。",
        "",
    ]
    for pos in positions:
        code = pos["code"]
        q = quotes.get(code)
        lines.append(f"## {code} {pos['name']}")
        lines.append(
            f"- positions.md 記載: 方向 {pos['direction']}・数量 "
            f"{pos['quantity']:,}株・平均取得単価 {pos['avg_price']:,.0f}円"
            if pos["quantity"] is not None and pos["avg_price"] is not None
            else f"- positions.md 記載: 方向 {pos['direction']}"
        )
        if q is None:
            lines.append("- yfinance 取得失敗（本銘柄の数値はレポートで完全省略すること）")
            lines.append("")
            continue
        cur = q["current"]
        chg_txt = ""
        if q["change_abs"] is not None and q["change_pct"] is not None:
            chg_txt = f"（前日終値 {q['prev_close']:,.1f}円・前日比 {q['change_abs']:+,.1f}円 / {q['change_pct']:+.2f}%）"
        lines.append(f"- 現値: {cur:,.1f}円{chg_txt}")
        lines.append(
            f"- 日足終値（直近バー）: {q['last_close']:,.1f}円（{q['last_bar_date']}）"
            + (
                f"／前バー終値: {q['prev_bar_close']:,.1f}円（{q['prev_bar_date']}）"
                if q["prev_bar_close"] is not None
                else ""
            )
        )
        hl = []
        if q["day_high"] is not None:
            hl.append(f"当日高値 {q['day_high']:,.1f}円")
        if q["day_low"] is not None:
            hl.append(f"当日安値 {q['day_low']:,.1f}円")
        if q["volume"] is not None:
            hl.append(f"出来高 {q['volume']:,.0f}株")
        if hl:
            lines.append("- " + "／".join(hl))
        if q["market_cap"] is not None:
            mcap_oku = q["market_cap"] / 1e8
            lines.append(f"- 時価総額: {mcap_oku:,.1f}億円")
        sma_parts = []
        for label, key in (
            ("SMA20", "sma20"),
            ("SMA25", "sma25"),
            ("SMA50", "sma50"),
            ("SMA75", "sma75"),
        ):
            if q[key] is not None:
                sma_parts.append(f"{label} {q[key]:,.2f}円")
        if sma_parts:
            lines.append("- 移動平均（日足終値・直近バーまで）: " + "／".join(sma_parts))
        g = _gap_line("SMA50 との距離", q["sma50"], cur)
        if g:
            lines.append(g)
        bb = q["bb"]
        if bb:
            lines.append(
                f"- BB(20日): +2σ {bb['p2']:,.2f}円／+3σ {bb['p3']:,.2f}円／"
                f"-2σ {bb['m2']:,.2f}円／-3σ {bb['m3']:,.2f}円"
            )
            for label, key in (("BB+2σ との距離", "p2"), ("BB+3σ との距離", "p3")):
                g = _gap_line(label, bb[key], cur)
                if g:
                    lines.append(g)
        if pos["quantity"] is not None and pos["avg_price"] is not None:
            pnl_man = (cur - pos["avg_price"]) * pos["quantity"] / 1e4
            lines.append(
                f"- 参考含み損益: (現値 {cur:,.1f}円 − 取得単価 {pos['avg_price']:,.0f}円) × "
                f"{pos['quantity']:,}株 = {pnl_man:+,.1f}万円"
            )
        if q["next_earnings"]:
            lines.append(f"- 次回決算日（yfinance）: {q['next_earnings']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="保有ポジションの現値 raw を生成")
    parser.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（既定: JST 当日）")
    parser.add_argument(
        "--session", default=None, choices=["am", "pm"], help="am=寄り前 / pm=引け直後（ヘッダ記録用）"
    )
    args = parser.parse_args()

    date_str = args.date or datetime.now(JST).strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        logger.error("--date の形式が不正: %s", date_str)
        return 1

    if not POSITIONS_MD.exists():
        logger.error("positions.md が見つかりません: %s", POSITIONS_MD)
        return 1
    positions = parse_positions(POSITIONS_MD.read_text(encoding="utf-8"))
    if not positions:
        logger.error(
            "positions.md の「## 保有ポジション」表から保有銘柄を 1 件も抽出できませんでした"
            "（表の形式変更またはノーポジション）。処理を中止します: %s",
            POSITIONS_MD,
        )
        return 1
    logger.info("保有銘柄 %d 件: %s", len(positions), ", ".join(p["code"] for p in positions))

    quotes: dict[str, dict] = {}
    for pos in positions:
        q = fetch_quote(pos["code"])
        if q is not None:
            quotes[pos["code"]] = q
            logger.info(
                "%s %s: 現値 %s / SMA50 %s",
                pos["code"],
                pos["name"],
                f"{q['current']:,.1f}" if q["current"] is not None else "-",
                f"{q['sma50']:,.2f}" if q["sma50"] is not None else "-",
            )
    if not quotes:
        logger.error("全銘柄で yfinance 取得に失敗しました。raw を出力せず中止します")
        return 1

    md = build_markdown(positions, quotes, date_str, args.session)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{date_str}_position_quotes_raw.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"出力: {out_path}（{len(quotes)}/{len(positions)} 銘柄取得成功）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
