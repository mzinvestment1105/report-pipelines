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
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 許容誤差（レポートは四捨五入した値を載せるため、丸め分は通す）
TOL_SHARES = 0.01   # 発行済株式総数 1%
TOL_MCAP = 0.02     # 時価総額 2%
TOL_BPS = 0.05      # BPS × 発行済 = 自己資本 5%
TOL_RATIO = 0.15    # 大株主の保有比率 0.15pt


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
    p = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"
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


def verify(md: str, code: str) -> tuple[list[str], list[str]]:
    """(errors, warnings) を返す。errors が空なら送信可。

    errors  = 送信を止める（誌面の数値が一次データと矛盾している＝事故）
    warnings= ログに出すだけ（分母の取り方など、正当な理由で差が出うる項目）
    """
    errors: list[str] = []
    warnings: list[str] = []
    master = _load_master(code)

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
    if price and mcap_oku and shares:
        implied = mcap_oku * 1e8 / price
        if abs(implied - shares) / shares > TOL_MCAP + TOL_SHARES:
            errors.append(
                f"[検査2] 時価総額÷株価が発行済株式総数と不整合: "
                f"{mcap_oku}億円÷{price:,.0f}円={implied:,.0f}株 vs 誌面 {shares:,.0f}株"
            )

    # 検査2b: 時価総額が screening_master と一致するか
    if mcap_oku and master.get("mcap"):
        ref_oku = master["mcap"] / 1e8
        if abs(mcap_oku - ref_oku) / ref_oku > 0.05:
            errors.append(
                f"[検査2b] 時価総額が screening_master と不一致: "
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

    errors, warnings = verify(md_path.read_text(encoding="utf-8"), args.code)
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
