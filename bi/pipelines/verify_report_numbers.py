"""個別銘柄レポートの数値整合を機械検算する（PM 2026-08-30 承認）。

背景: サブエージェントが返した数値を一次情報で検算せずに誌面へ載せ、発行済株式総数を
実際の約19倍で記載した事故が発生した（4477）。ルール文だけでは「検算を忘れる」経路を
塞げないため、送信スクリプトから機械的に呼び出して不整合なら送信を止める。

検査するのは「レポート本文の数値どうし・および screening_master の実値との整合」のみ。
値そのものの正しさ（一次情報との一致）は本スクリプトの守備範囲外だが、発行済株式総数を
取り違えると BPS・保有比率・時価総額のいずれかが必ず桁で合わなくなるため、
本検査で実際に検出できる。

使い方:
    python verify_report_numbers.py --code 7256 --date 2026-08-30
    python verify_report_numbers.py --md research/stocks/4011/2026-08-30_notarget.md --code 4011

exit 0 = 合格 / exit 1 = 不整合あり（送信を止める）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path

# Windows 既定の cp932 だと日本語メッセージを print した瞬間に UnicodeEncodeError で
# 落ち、判定行（NUMBER VERIFY: OK / FAILED）が出力されない。標準出力・標準エラーを
# UTF-8（変換不能文字は置換）へ張り替える。失敗しても検査は続行する（フェイルオープン）。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]
MASTER_PATH = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"
JST = _dt.timezone(_dt.timedelta(hours=9))

# 許容誤差（レポートは四捨五入した値を載せるため、丸め分は通す）
TOL_SHARES = 0.01   # 発行済株式総数 1%
TOL_MCAP = 0.02     # 時価総額 2%
TOL_MCAP_REF = 0.05  # 検査2b: 時価総額 vs 照合先（master または再取得値）5%
TOL_BPS = 0.05      # BPS × 発行済 = 自己資本 5%
TOL_RATIO = 0.15    # 大株主の保有比率 0.15pt

# 検査2b: screening_master の時価総額は更新時点の株価基準のため、対象日から
# この暦日数を超えて離れていたら「古い」とみなし、対象日の終値で取り直して照合する
# （2026-10-04 に master が 3 週間古く、正しい誌面値が偽 error になった事例への対処）。
MASTER_STALE_DAYS = 3


def _to_float(s: str) -> float:
    return float(s.replace(",", "").replace("▲", "-").replace("△", "-"))


def _find_shares(md: str) -> list[tuple[int, float]]:
    """発行済株式総数の記載を全て拾う（行番号, 株数）。"""
    out = []
    pat = re.compile(r"発行済(?:株式総数|株式数)?[^0-9\n]{0,12}([0-9][0-9,]{5,})\s*株")
    for i, line in enumerate(md.splitlines(), 1):
        for m in pat.finditer(line):
            out.append((i, _to_float(m.group(1))))
    return out


def _find_one(md: str, pat: str) -> float | None:
    m = re.search(pat, md)
    return _to_float(m.group(1)) if m else None


def _load_master(code: str) -> dict:
    try:
        import pandas as pd
    except ImportError:
        return {}
    p = MASTER_PATH
    if not p.exists():
        return {}
    df = pd.read_parquet(p)
    sub = df[df["Code"].astype(str) == str(code)]
    if sub.empty:
        return {}
    row = sub.iloc[0]
    out = {}
    for key, col in [
        ("mcap", "MarketCap"),
        ("shares", "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"),
        ("equity", "Equity_LatestFY"),
    ]:
        if col in sub.columns:
            v = row[col]
            if v == v:  # not NaN
                out[key] = float(v)
    return out


def _target_date_from_md(md: str) -> _dt.date | None:
    """誌面の冒頭（タイトル・ヘッダ）にある YYYY-MM-DD を対象日とみなす。"""
    head = "\n".join(md.splitlines()[:5])
    m = re.search(r"(20[0-9]{2})-([0-9]{2})-([0-9]{2})", head)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _master_date() -> _dt.date | None:
    """screening_master の鮮度基準日。

    git の最終コミット日を優先する（checkout では mtime が取得時刻になり鮮度を表さないため）。
    shallow clone では履歴が切れてコミット日が当てにならないので使わない。
    取れなければ mtime（JST）、それも無理なら None。
    """
    try:
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if shallow == "false":
            out = subprocess.run(
                ["git", "log", "-1", "--format=%cs", "--",
                 MASTER_PATH.relative_to(REPO_ROOT).as_posix()],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", out):
                return _dt.date.fromisoformat(out)
    except Exception:  # noqa: BLE001
        pass
    try:
        return _dt.datetime.fromtimestamp(MASTER_PATH.stat().st_mtime, JST).date()
    except Exception:  # noqa: BLE001
        return None


def _fetch_mcap_oku(code: str, shares: float, target_date: _dt.date) -> float | None:
    """yfinance で対象日（休場なら直前の営業日）の終値を取り、× 誌面の発行済株式数 で
    時価総額（億円）を返す。取れなければ None。"""
    try:
        import yfinance as yf
        start = target_date - _dt.timedelta(days=10)
        end = target_date + _dt.timedelta(days=1)
        hist = yf.Ticker(f"{code}.T").history(
            start=start.isoformat(), end=end.isoformat(), auto_adjust=False,
        )
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        closes = hist["Close"].dropna()
        closes = closes[[d.date() <= target_date for d in closes.index]]
        if closes.empty:
            return None
        return float(closes.iloc[-1]) * shares / 1e8
    except Exception:  # noqa: BLE001
        return None


def verify(md: str, code: str, target_date: _dt.date | str | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings) を返す。errors が空なら送信可。

    errors  = 送信を止める（誌面の数値が一次データと矛盾している＝事故）
    warnings= ログに出すだけ（分母の取り方など、正当な理由で差が出うる項目）

    target_date = 誌面の対象日（検査2b の取り直しに使う）。None なら誌面冒頭の
    YYYY-MM-DD、取れなければ今日（JST）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    master = _load_master(code)
    if isinstance(target_date, str):
        try:
            target_date = _dt.date.fromisoformat(target_date)
        except ValueError:
            target_date = None
    if target_date is None:
        target_date = _target_date_from_md(md) or _dt.datetime.now(JST).date()

    shares_hits = _find_shares(md)
    # 最頻値を「その銘柄の発行済株式総数」とみなす（合併前の株数などを本文が併記するため）
    if shares_hits:
        from collections import Counter
        shares = Counter(v for _, v in shares_hits).most_common(1)[0][0]
    else:
        shares = None

    # 検査5: 本文中の発行済株式総数が全箇所で同一か（併記は正当なので警告どまり）
    if shares_hits:
        distinct = {v for _, v in shares_hits}
        if len(distinct) > 1:
            detail = ", ".join(f"L{i}:{v:,.0f}株" for i, v in shares_hits)
            warnings.append(f"[検査5] 発行済株式総数の記載が複数ある（併記の意図を確認）: {detail}")

    # 検査1: 発行済株式総数が screening_master の実値と一致するか
    if shares and master.get("shares"):
        ref = master["shares"]
        if abs(shares - ref) / ref > TOL_SHARES:
            errors.append(
                f"[検査1] 発行済株式総数が screening_master と不一致: "
                f"誌面 {shares:,.0f}株 vs 実値 {ref:,.0f}株（{shares / ref:.2f}倍）"
            )

    # 株価・時価総額は「基本情報」表の行から取る（本文中の別の株価に引っ張られないため）
    price = _find_one(md, r"\|\s*株価[^|\n]*\|\s*\**([0-9][0-9,]*)\s*\**\s*(?:円)?\s*\|")
    if price is None:
        price = _find_one(md, r"株価（[^）]*終値[^）]*）[^0-9\n]{0,10}([0-9][0-9,]*)\s*円")
    mcap_oku = _find_one(md, r"\|\s*時価総額[^|\n]*\|\s*\**([0-9][0-9,]*\.?[0-9]*)\s*\**\s*億円")

    # 検査2: 時価総額 ÷ 株価 = 発行済株式総数
    check2 = "未実施"  # 検査2b の warning 文に結果を添えるため保持する
    if price and mcap_oku and shares:
        implied = mcap_oku * 1e8 / price
        check2 = "通過"
        if abs(implied - shares) / shares > TOL_MCAP + TOL_SHARES:
            check2 = "不整合"
            errors.append(
                f"[検査2] 時価総額÷株価が発行済株式総数と不整合: "
                f"{mcap_oku}億円÷{price:,.0f}円={implied:,.0f}株 vs 誌面 {shares:,.0f}株"
            )

    # 検査2b: 時価総額が screening_master と一致するか
    # master が対象日から MASTER_STALE_DAYS 暦日を超えて離れていれば、対象日の終値 ×
    # 誌面の発行済株式数（検査1で master と照合済み）で取り直して照合する。取り直しにも
    # 失敗したら比較が成立しないため warning にとどめる（PM 2026-10-05 承認・選択肢 1）。
    if mcap_oku and master.get("mcap"):
        ref_oku: float | None = master["mcap"] / 1e8
        src = "screening_master"
        m_date = _master_date()
        age = abs((target_date - m_date).days) if m_date else None
        if age is not None and age > MASTER_STALE_DAYS:
            live = _fetch_mcap_oku(code, shares, target_date) if shares else None
            if live is not None:
                ref_oku, src = live, "yfinance 再取得"
                warnings.append(
                    f"[検査2b] master が {age} 日古いため yfinance 再取得"
                    f"（{target_date}以前の直近終値×誌面株数={live:.1f}億円）と照合"
                )
            else:
                ref_oku = None
                note = {
                    "通過": "検査2 の誌面内整合は通過",
                    "不整合": "検査2 の誌面内整合も不一致",
                    "未実施": "検査2 は株価等が読めず未実施",
                }[check2]
                warnings.append(
                    f"[検査2b] master が {age} 日古く再取得も失敗のため時価総額照合をスキップ"
                    f"（{note}）"
                )
        if ref_oku and abs(mcap_oku - ref_oku) / ref_oku > TOL_MCAP_REF:
            errors.append(
                f"[検査2b] 時価総額が {src} と不一致: "
                f"誌面 {mcap_oku}億円 vs 実値 {ref_oku:.1f}億円"
            )

    # 検査3: PER の整合（株価 ÷ EPS = PER）
    per = _find_one(md, r"\|\s*PER\s*\|[^0-9\n]{0,10}([0-9]+\.?[0-9]*)\s*倍")
    eps = _find_one(md, r"EPS\s*([0-9]+\.?[0-9]*)\s*円")
    if per and eps and price and eps > 0:
        implied_per = price / eps
        if abs(implied_per - per) / per > 0.05:
            errors.append(
                f"[検査3] PER が株価÷EPS と不整合: "
                f"{price:,.0f}円÷{eps}円={implied_per:.1f}倍 vs 誌面 {per}倍"
            )

    # 検査4: BPS × 発行済 = 自己資本
    # screening_master の自己資本は直近本決算のため、期中の増資・合併があるとずれる。警告どまり。
    bps = _find_one(md, r"(?:BPS|1株当たり純資産)[^0-9\n]{0,10}([0-9][0-9,]*\.?[0-9]*)\s*円")
    if bps and shares and master.get("equity"):
        implied_equity = bps * shares
        ref = master["equity"]
        if ref > 0 and abs(implied_equity - ref) / ref > TOL_BPS:
            warnings.append(
                f"[検査4] BPS×発行済が直近本決算の自己資本と不一致（期中増資なら正常）: "
                f"{bps:,.0f}円×{shares:,.0f}株={implied_equity / 1e6:,.0f}百万円 "
                f"vs 実値 {ref / 1e6:,.0f}百万円"
            )

    # 検査6: 大株主の保有株数 ÷ 発行済 = 記載の保有比率
    # 「大株主」見出し配下の表に限定する（セグメント売上表など、株数でない表を誤検出しないため）
    if shares:
        lines = md.splitlines()
        in_holder = False
        row_pat = re.compile(
            r"^\|[^|]*\|[^|]*?([0-9][0-9,]{3,})\s*株\s*\|[^|0-9]*([0-9]+\.[0-9]+)\s*%"
        )
        alt_pat = re.compile(
            r"^\|[^|]*\|[^|0-9]*([0-9]+\.[0-9]+)\s*%\s*\|[^|]*?([0-9][0-9,]{3,})\s*株"
        )
        bad = []
        ratios: list[float] = []
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith("#"):
                in_holder = "大株主" in s or "株主構成" in s
                continue
            if not in_holder or not s.startswith("|"):
                continue
            m = row_pat.match(s)
            if m:
                held, pct = _to_float(m.group(1)), _to_float(m.group(2))
            else:
                m = alt_pat.match(s)
                if not m:
                    continue
                pct, held = _to_float(m.group(1)), _to_float(m.group(2))
            implied = held / shares * 100
            ratios.append(implied / pct if pct else 0)
            if abs(implied - pct) > TOL_RATIO:
                bad.append(f"L{i}: {held:,.0f}株→{implied:.2f}% vs 誌面{pct}%")
        if bad:
            # 全行が同じ倍率でずれている＝分母が発行済でない（議決権数・自己株控除後）。
            # これは有報の記載どおりであり正当なので警告。バラバラにずれていれば誤りなので停止。
            uniform = len(ratios) >= 3 and (max(ratios) - min(ratios)) < 0.02
            msg = "[検査6] 大株主の保有比率が発行済と不整合: " + " / ".join(bad[:5])
            if uniform:
                warnings.append(msg + f"（全行が一律 {sum(ratios) / len(ratios):.3f} 倍。分母が議決権数等の可能性）")
            else:
                errors.append(msg)

    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="銘柄コード")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--md", help="レポートの相対パス（--date より優先）")
    args = ap.parse_args()

    if args.md:
        md_path = REPO_ROOT / args.md
    else:
        md_path = REPO_ROOT / "research" / "stocks" / args.code / f"{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: report not found: {md_path}")
        return 1

    errors, warnings = verify(md_path.read_text(encoding="utf-8"), args.code, args.date)
    for w in warnings:
        print("NUMBER VERIFY WARNING: " + w)
    if errors:
        print("NUMBER VERIFY: FAILED（送信を中止します）")
        for e in errors:
            print("  " + e)
        return 1
    print("NUMBER VERIFY: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
