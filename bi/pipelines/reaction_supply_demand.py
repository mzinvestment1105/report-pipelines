"""反応スコア対象日の需給データ（PM 2026-09-06 指示）。

背景（この仕組みが必要になった経緯）:
  3168 の 2026-09-02 の株価 -9.17% を「8/31 基準日の期末配当が 9/1 に権利落ちした」と
  説明したが誤りだった。実際の権利落ち日は 2026-08-28 であり、9/2 の真因は機関投資家の
  空売り残高の積み増し（JPモルガン +1.3万株・モルガン・スタンレー +5.0万株＝計 +6.3万株）
  だった。値動きの最も直接的な原因である需給が、機械で揃える事実の中に無かったため、
  執筆側が推測で埋めてしまった。

そこで本モジュールは、反応スコア対象日ごとに次の需給の事実を機械で揃える:
  1. 機関投資家の空売り残高と日次増減（機関名別）
     … J-Quants v2 /markets/short-sale-report。JPX の大量空売りポジション報告
       （発行済株式数の 0.5% 以上で報告義務）が原データ。
  2. 信用取引の売残・買残と増減（対発行済株式数 %）
     … J-Quants v2 /markets/margin-interest（週次・毎週金曜時点）。
       信用倍率は使わない（_cr / feedback_data_accuracy_rules）。
  3. 立会外分売・自己株取得・大量保有報告書の提出（対象日前後）
     … TDNet の開示表題から機械的に抽出する。
  4. 信用規制の変更（日々公表・増担保規制の指定／解除）
     … TDNet／取引所の開示表題から機械的に抽出する。
  5. 株式分割・配当の権利付最終日／権利落ち日
     … yfinance の corporate actions（Dividends / Stock Splits）。
       権利落ち日そのものを機械で持つことで、「権利落ち」を主因として書く際に
       対象日と一致するかを検証できるようにする（3168 の誤りの再発防止）。

すべて HTTP 経由（J-Quants REST・yfinance・TDNet Atom）であり、ローカル MCP に
依存しないため GitHub Actions のクラウド環境でも動く（_cr §14）。
取得できなかった項目は欠損のまま残し、0 や推定値で埋めない。
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

# 空売り残高報告の発生／消失ライン（発行済株式数の 0.5%）。
# short_sale_utils.DISCLOSURE_THRESHOLD と同じ定義。
DISCLOSURE_THRESHOLD = 0.005

# 需給の推移を見る前後の暦日数（対象日の前後この日数ぶんを走査する）。
SD_WINDOW_DAYS = 5

# 空売り残高の日次増減を「特筆すべき」とみなす下限。
# 発行済株式数比でこの値以上の増減があった日は、誌面で言及する義務を課す
# （gate_stock_report.py の検査と同じ閾値を使う）。
SD_NOTABLE_RATIO = 0.001          # 発行済株式数の 0.1%
SD_NOTABLE_GROWTH = 0.50          # または直前残高比 +50%

# 需給に関わる開示を表題から拾うためのキーワード。
# 表題の文言は発行会社ごとに揺れるため、語幹だけで拾う。
DISCLOSURE_KEYWORDS = {
    "立会外分売": ("立会外分売",),
    "自己株式の取得": ("自己株式の取得", "自己株取得", "自己株式取得"),
    "大量保有報告書": ("大量保有", "変更報告書"),
    "売出し・公募": ("売出", "公募", "第三者割当", "新株予約権"),
    "株式分割": ("株式分割",),
    "配当": ("配当予想", "配当の修正", "剰余金の配当"),
}

# 信用規制（日々公表・増担保）に関わる表題のキーワード。
# 取引所が指定／解除を公表するため、銘柄の TDNet に載らないことがある。
# 載っていた場合に限り拾い、無い場合は「TDNet の範囲では確認できず」と明記する。
REGULATION_KEYWORDS = (
    "日々公表",
    "増担保",
    "委託保証金",
    "信用取引",
    "規制",
)


def _f(v):
    """数値化。できなければ None（0 で埋めない）。"""
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _d(s):
    """YYYY-MM-DD 文字列を date に。解釈できなければ None。"""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. 機関投資家の空売り残高（J-Quants /markets/short-sale-report）
# ---------------------------------------------------------------------------

def fetch_short_positions(code4: str) -> list:
    """当該銘柄の空売り残高報告を CalcDate 昇順で返す。

    J-Quants v2 の /markets/short-sale-report は code 指定だと全期間を返す
    （from/to は効かない。実測 2026-09-06）。呼び出し側で期間を絞ること。

    Returns:
        [{"calc_date": date, "disc_date": date, "inst": str,
          "shares": float, "ratio": float,
          "prev_date": date|None, "prev_ratio": float|None}]
        取得できなければ空リスト（run は止めない）。
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        print("  → 取得できず: 空売り残高（JQUANTS_API_KEY 未設定）", file=sys.stderr)
        return []
    try:
        import jquantsapi
        from jq_client_utils import fetch_paginated_v2
        client = jquantsapi.ClientV2(api_key=api_key)
        rows = fetch_paginated_v2(
            client, "/markets/short-sale-report", params={"code": str(code4)},
            sleep_seconds=0.8,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  → 取得失敗: 空売り残高({code4}): {e}", file=sys.stderr)
        return []

    out: list = []
    for r in rows or []:
        cd = _d(r.get("CalcDate"))
        if cd is None:
            continue
        inst = str(r.get("SSName") or "").strip()
        dic = str(r.get("DICName") or "").strip()
        if dic and dic not in ("-", "nan", "None"):
            inst = dic
        out.append({
            "calc_date": cd,
            "disc_date": _d(r.get("DiscDate")),
            "inst": inst or "（報告者名を取得できず）",
            "shares": _f(r.get("ShrtPosShares")),
            "ratio": _f(r.get("ShrtPosToSO")),
            "prev_date": _d(r.get("PrevRptDate")),
            "prev_ratio": _f(r.get("PrevRptRatio")),
        })
    out.sort(key=lambda x: (x["calc_date"], x["disc_date"] or x["calc_date"]))
    return out


def _shares_outstanding_from_reports(reports: list):
    """報告の「株数 ÷ 発行済比率」から発行済株式数を推定する。

    空売り残高報告は株数と対発行済株式数比の両方を持つため、その比から
    発行済株式数を逆算できる。複数報告の中央値を採り、外れ値の影響を避ける。
    比率が小数第4位に丸められているため厳密値ではなく概算である。
    """
    vals = []
    for r in reports:
        sh, ra = r.get("shares"), r.get("ratio")
        if sh and ra and ra > 0:
            vals.append(sh / ra)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def short_position_timeline(reports: list, lo: date, hi: date) -> list:
    """[lo, hi] の各報告について、同一報告者の直前の残高からの増減を付けて返す。

    Returns:
        [{"calc_date": date, "inst": str, "shares": float, "ratio": float,
          "delta": float|None, "prev_shares": float|None, "prev_date": date|None,
          "is_new": bool}]  calc_date 昇順。
    """
    by_inst: dict = {}
    for r in reports:
        by_inst.setdefault(r["inst"], []).append(r)

    rows: list = []
    for inst, seq in by_inst.items():
        seq = sorted(seq, key=lambda x: x["calc_date"])
        for i, r in enumerate(seq):
            if not (lo <= r["calc_date"] <= hi):
                continue
            prev = seq[i - 1] if i > 0 else None
            prev_sh = prev["shares"] if prev else None
            delta = None
            if r["shares"] is not None and prev_sh is not None:
                delta = r["shares"] - prev_sh
            elif r["shares"] is not None and r.get("prev_ratio") is None:
                # 直前の報告が無い＝新規に報告義務が発生した（0.5% を超えた）。
                # 残高そのものが新規の積み増しにあたるため増減として扱う。
                delta = r["shares"]
            rows.append({
                "calc_date": r["calc_date"],
                "inst": inst,
                "shares": r["shares"],
                "ratio": r["ratio"],
                "delta": delta,
                "prev_shares": prev_sh,
                "prev_date": prev["calc_date"] if prev else None,
                "is_new": prev is None,
            })
    rows.sort(key=lambda x: (x["calc_date"], x["inst"]))
    return rows


def total_short_balance_on(reports: list, day: date):
    """day 時点で有効な空売り残高の合計（報告者ごとの最新値の和）を返す。

    報告義務消失（最新比率が 0.5% 未満）の報告者は合計から外す
    （short_sale_utils と同じロジック。カバー済みの古い残高を足し続けない）。
    """
    latest: dict = {}
    for r in reports:
        if r["calc_date"] > day:
            continue
        latest[r["inst"]] = r
    tot = 0.0
    got = False
    for r in latest.values():
        ra, sh = r.get("ratio"), r.get("shares")
        if ra is not None and ra < DISCLOSURE_THRESHOLD:
            continue
        if sh is not None:
            tot += sh
            got = True
    return tot if got else None


# ---------------------------------------------------------------------------
# 2. 信用取引の売残・買残（J-Quants /markets/margin-interest・週次）
# ---------------------------------------------------------------------------

def fetch_margin_interest(code4: str) -> list:
    """当該銘柄の週次信用残（売残・買残）を Date 昇順で返す。

    J-Quants v2 /markets/margin-interest。毎週金曜時点の残高が翌週に公表される。
    日次の信用残（/markets/daily-margin-interest）は上位プランのみで
    本環境では 403 になるため使わない（実測 2026-09-06）。

    Returns:
        [{"date": date, "short": float|None, "long": float|None}]
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        import jquantsapi
        from jq_client_utils import fetch_paginated_v2
        client = jquantsapi.ClientV2(api_key=api_key)
        rows = fetch_paginated_v2(
            client, "/markets/margin-interest", params={"code": str(code4)},
            sleep_seconds=0.8,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  → 取得失敗: 信用残({code4}): {e}", file=sys.stderr)
        return []

    out: list = []
    for r in rows or []:
        d = _d(r.get("Date"))
        if d is None:
            continue
        out.append({
            "date": d,
            "short": _f(r.get("ShrtVol")),
            "long": _f(r.get("LongVol")),
        })
    out.sort(key=lambda x: x["date"])
    return out


def margin_around(margin: list, day: date) -> dict:
    """day を挟む直近2回の信用残と、その間の増減を返す。

    週次のため対象日そのものの残高は存在しない。対象日の直前の公表日と、
    対象日より後の最初の公表日を並べ、対象日を含む週の増減として示す。
    """
    before = [m for m in margin if m["date"] <= day]
    after = [m for m in margin if m["date"] > day]
    prev = before[-1] if before else None
    prev2 = before[-2] if len(before) >= 2 else None
    nxt = after[0] if after else None
    out: dict = {"prev": prev, "prev2": prev2, "next": nxt}
    if prev and nxt:
        out["short_delta"] = (
            None if (prev["short"] is None or nxt["short"] is None)
            else nxt["short"] - prev["short"]
        )
        out["long_delta"] = (
            None if (prev["long"] is None or nxt["long"] is None)
            else nxt["long"] - prev["long"]
        )
    return out


# ---------------------------------------------------------------------------
# 3. 権利落ち日（yfinance の corporate actions）
# ---------------------------------------------------------------------------

def fetch_corporate_actions(code4: str) -> list:
    """配当・株式分割の権利落ち日を返す（yfinance）。

    yfinance の actions のインデックスは権利落ち日（ex-date）である。
    権利付最終日はその1営業日前にあたる。

    3168 の誤りはここを機械で持っていなかったことに起因する
    （実際の権利落ち日 2026-08-28 を、9/1 と取り違えて主因に据えた）。

    Returns:
        [{"ex_date": date, "kind": "配当"|"株式分割", "value": float}]
        ex_date 昇順。取得できなければ空リスト。
    """
    try:
        import yfinance as yf
        t = yf.Ticker(f"{code4}.T")
        acts = t.actions
    except Exception as e:  # noqa: BLE001
        print(f"  → 取得失敗: 権利落ち日({code4}): {e}", file=sys.stderr)
        return []
    if acts is None or len(acts) == 0:
        return []

    out: list = []
    try:
        for idx, row in acts.iterrows():
            ex = idx.date() if hasattr(idx, "date") else None
            if ex is None:
                continue
            div = _f(row.get("Dividends"))
            spl = _f(row.get("Stock Splits"))
            if div:
                out.append({"ex_date": ex, "kind": "配当", "value": div})
            if spl:
                out.append({"ex_date": ex, "kind": "株式分割", "value": spl})
    except Exception as e:  # noqa: BLE001
        print(f"  → 取得失敗: 権利落ち日の整形({code4}): {e}", file=sys.stderr)
        return []
    out.sort(key=lambda x: x["ex_date"])
    return out


def ex_date_hit(actions: list, day: date, window: int = 1):
    """day の前後 window 暦日に該当する権利落ちを返す。無ければ None。"""
    for a in actions:
        if abs((a["ex_date"] - day).days) <= window:
            return a
    return None


# ---------------------------------------------------------------------------
# 4. 需給に関わる開示・信用規制（TDNet の表題から抽出）
# ---------------------------------------------------------------------------

def supply_demand_disclosures(tdnet_entries: list, day: date,
                              parse_dt, window: int = 3) -> list:
    """TDNet 表題から、対象日前後 window 日の需給関連の開示を抜き出す。

    Args:
        tdnet_entries: [{"title": str, "published": str}]（deep_dive が取得済みのもの）
        parse_dt: published 文字列を datetime にする関数（deep_dive の実装を渡す）
    Returns:
        [{"date": date, "kind": str, "title": str}]
    """
    hits: list = []
    for e in (tdnet_entries or []):
        dt = parse_dt(e.get("published", ""))
        if dt is None:
            continue
        d = dt.date()
        if abs((d - day).days) > window:
            continue
        title = str(e.get("title") or "")
        matched = False
        for kind, kws in DISCLOSURE_KEYWORDS.items():
            if any(k in title for k in kws):
                hits.append({"date": d, "kind": kind, "title": title})
                matched = True
                break
        if not matched and any(k in title for k in REGULATION_KEYWORDS):
            hits.append({"date": d, "kind": "信用規制の関連", "title": title})
    hits.sort(key=lambda x: x["date"])
    return hits


# ---------------------------------------------------------------------------
# 集約と Markdown 化
# ---------------------------------------------------------------------------

def build_supply_demand(code4: str, target_days: list) -> dict:
    """反応スコア対象日ごとの需給データを一括で揃える。

    API 呼び出しは銘柄あたり空売り 1 回・信用残 1 回・権利落ち 1 回のみとし、
    対象日ごとには呼ばない（レート制限と実行時間のため）。

    Args:
        code4: 4桁銘柄コード
        target_days: 反応スコア対象日（date のリスト）
    Returns:
        {"days": {date: {...}}, "shares_outstanding": float|None,
         "actions": [...], "reports": [...], "margin": [...], "sources": [...]}
    """
    if not target_days:
        return {"days": {}, "shares_outstanding": None, "actions": [],
                "reports": [], "margin": [], "sources": []}

    reports = fetch_short_positions(code4)
    margin = fetch_margin_interest(code4)
    actions = fetch_corporate_actions(code4)
    so = _shares_outstanding_from_reports(reports)

    sources: list = []
    sources.append("空売り残高=J-Quants /markets/short-sale-report"
                   if reports else "空売り残高=取得できず")
    sources.append("信用残=J-Quants /markets/margin-interest（週次）"
                   if margin else "信用残=取得できず")
    sources.append("権利落ち日=yfinance corporate actions"
                   if actions else "権利落ち日=取得できず")

    days: dict = {}
    for day in target_days:
        lo = day - timedelta(days=SD_WINDOW_DAYS)
        hi = day + timedelta(days=SD_WINDOW_DAYS)
        tl = short_position_timeline(reports, lo, hi)
        on_day = [r for r in tl if r["calc_date"] == day]
        delta_sum = None
        for r in on_day:
            if r.get("delta") is not None:
                delta_sum = (delta_sum or 0.0) + r["delta"]
        days[day] = {
            "short_timeline": tl,
            "short_on_day": on_day,
            "short_delta_on_day": delta_sum,
            "short_total": total_short_balance_on(reports, day),
            "margin": margin_around(margin, day),
            "ex_action": ex_date_hit(actions, day, window=1),
        }
    return {
        "days": days,
        "shares_outstanding": so,
        "actions": actions,
        "reports": reports,
        "margin": margin,
        "sources": sources,
    }


def _man(v) -> str:
    """株数の増減を符号付きの万株で表記する。欠損は「取得できず」。"""
    if v is None:
        return "取得できず"
    if v == 0:
        return "増減なし"
    return f"{v / 10000:+.1f}万株"


def _man_abs(v) -> str:
    """株数を万株で表記する（符号なし）。欠損は「取得できず」。"""
    if v is None:
        return "取得できず"
    return f"{v / 10000:.1f}万株"


def _pct_of_so(v, so) -> str:
    """発行済株式数比 %。分母が無ければ空文字（推定で埋めない）。"""
    if v is None or not so:
        return ""
    return f"（発行済比 {v / so * 100:.2f}%）"


def is_notable_short_move(delta, prev_total, so) -> bool:
    """その日の空売り増減が誌面で言及すべき規模かを判定する。

    gate_stock_report.py の検査と同じ定義を使う（片方だけ変えないこと）:
      - 発行済株式数の SD_NOTABLE_RATIO（0.1%）以上の増減、または
      - 直前残高比 SD_NOTABLE_GROWTH（+50%）以上の増加
    """
    if delta is None:
        return False
    if so and abs(delta) >= so * SD_NOTABLE_RATIO:
        return True
    if prev_total and prev_total > 0 and delta / prev_total >= SD_NOTABLE_GROWTH:
        return True
    return False


def fmt_supply_demand_for_day(sd: dict, day: date, disclosures: list,
                              include_short: bool = True) -> list:
    """1 日ぶんの需給を data.md 用の Markdown 表の行にする（10 行前後に収める）。

    呼び出し側が開いた表（| 項目 | 値 |）の中へ差し込む前提の行を返す。

    Args:
        include_short: 空売り残高の行を含めるか。呼び出し側が別経路で
            空売りを既に出している場合に False にして重複を避ける。
    """
    if not sd or day not in (sd.get("days") or {}):
        return []
    d = sd["days"][day]
    so = sd.get("shares_outstanding")
    lines: list = []

    # --- 空売り（機関名別の日次増減）---
    if include_short:
        on_day = d.get("short_on_day") or []
        if on_day:
            for r in on_day:
                dl = r.get("delta")
                note = "・新規に報告義務が発生" if r.get("is_new") else ""
                lines.append(
                    f"| 空売り {r['inst']} | 残 {_man_abs(r['shares'])}"
                    f"{_pct_of_so(r['shares'], so)} ／ 増減 {_man(dl)}{note} |"
                )
            tot_dl = d.get("short_delta_on_day")
            if tot_dl is not None and len(on_day) > 1:
                lines.append(
                    f"| 空売り 当日合計の増減 | {_man(tot_dl)}{_pct_of_so(abs(tot_dl), so)} |"
                )
        else:
            lines.append("| 空売り残高（機関別） | 当日の報告なし＝増減なし |")

        tot = d.get("short_total")
        if tot is not None:
            lines.append(f"| 空売り残高 合計 | {_man_abs(tot)}{_pct_of_so(tot, so)} |")

    # --- 信用残（週次）---
    mg = d.get("margin") or {}
    prev, nxt = mg.get("prev"), mg.get("next")
    if prev:
        lines.append(
            f"| 信用残（{prev['date'].isoformat()} 時点） | "
            f"売残 {_man_abs(prev['short'])}{_pct_of_so(prev['short'], so)} ／ "
            f"買残 {_man_abs(prev['long'])}{_pct_of_so(prev['long'], so)} |"
        )
    if prev and nxt and (mg.get("short_delta") is not None or mg.get("long_delta") is not None):
        lines.append(
            f"| 信用残の増減（{prev['date'].isoformat()}→{nxt['date'].isoformat()}） | "
            f"売残 {_man(mg.get('short_delta'))} ／ 買残 {_man(mg.get('long_delta'))} |"
        )
    if not prev and not nxt:
        lines.append("| 信用残 | 取得できず |")

    # --- 権利落ち（3168 の誤りの再発防止。当日でなければ「該当なし」と明記する）---
    ex = d.get("ex_action")
    if ex:
        lines.append(
            f"| 権利落ち | {ex['ex_date'].isoformat()} に{ex['kind']}の権利落ち"
            f"（{ex['value']}）。当日と一致するため主因として書いてよい |"
        )
    else:
        nearest = None
        for a in (sd.get("actions") or []):
            if nearest is None or abs((a["ex_date"] - day).days) < abs(
                (nearest["ex_date"] - day).days
            ):
                nearest = a
        near_s = (
            f"直近の権利落ちは {nearest['ex_date'].isoformat()}（{nearest['kind']}）"
            if nearest else "権利落ちの記録なし"
        )
        lines.append(
            f"| 権利落ち | 当日は権利落ち日ではない（{near_s}）。"
            "権利落ちを主因として書くことを禁止する |"
        )

    # --- 需給関連の開示・信用規制 ---
    if disclosures:
        joined = "／".join(f"{h['date'].isoformat()} {h['kind']}" for h in disclosures[:4])
        lines.append(f"| 需給に関わる開示 | あり（{joined}） |")
    else:
        lines.append("| 需給に関わる開示 | 前後3日に立会外分売・自己株取得・"
                     "大量保有報告書・信用規制の開示なし |")

    return lines
