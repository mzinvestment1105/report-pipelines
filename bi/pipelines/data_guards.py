# -*- coding: utf-8 -*-
"""
データ健全性ガード v1: パイプラインが黙って壊れたデータを流すのを防ぐ再利用可能な検査。

他パイプラインから `from data_guards import check_market_turnover_jump, ...` で読み込む
ことを想定した純関数群。関数内で sys.exit / 例外送出はせず、判定結果を GuardResult に
詰めて返す（呼び出し側が続行するか止めるかを決める）。止めたい場合は raise_if_failed()
の薄いラッパを重ねる。

本モジュールはデータを一切書き換えない（読むだけ・欠損の補完も推定値の穴埋めもしない）。

--------------------------------------------------------------------------------
Guard A: check_market_turnover_jump  -- 日次売買代金合計の不連続を検知する
--------------------------------------------------------------------------------
Date/Code/Turnover のパネルから母集団全体の日次売買代金合計を作り、前日比が
threshold（既定 20%）を超えた日を拾う。

【ただの ±20% しきい値では役に立たない】
本番パネル（adj_close_panel・2022-01-04〜2026-07-31 の 1118 遷移）で試すと
|前日比| > 20% の日は 210 日ある。SQ 日・FOMC 翌日・決算集中日など、実勢として
売買代金が跳ねる日は珍しくないためで、しきい値だけを鳴らしてもオペレータは
「またか」で無視するようになる。

そこで本ガードは検知した日ごとに **ジャンプの内訳を分解** する:
  - 当日にだけ現れて前日にはいなかった銘柄（= 母集団の定義変更・ETL の混入）
  - 両日に共通して存在する銘柄（= 実勢の値動き）
前者が増分の過半を説明する日を cause="universe_change"、そうでない日を
cause="market_move" とラベルする。上記 210 日のうち universe_change は
2026-07-07 の 1 日だけで、残り 209 日は新規銘柄の寄与が増分の 4% 未満だった。
オペレータが見るべきは cause="universe_change" の行だけになる。

--------------------------------------------------------------------------------
Guard B: check_adjustment_factor_consistency  -- 分割・併合係数と生値の整合を検知する
--------------------------------------------------------------------------------
price_history 形状（Date/Code/Close/AdjustmentFactor）で AdjustmentFactor != 1.0 の
行について、権利落ち境界をまたぐ **生の終値比** が係数と整合するかを見る。

【AdjustmentFactor の規約は実データから実測した（記憶・通説では決めていない）】
bi/outputs/price_history/2025.parquet の AdjustmentFactor != 1.0 は 238 行。
うち同ファイル内に直前営業日の終値がある 237 行で 2 仮説を突き合わせた。
    仮説A: Close_t / Close_{t-1} ≒ AdjustmentFactor
    仮説B: Close_t / Close_{t-1} ≒ 1 / AdjustmentFactor
相対偏差 |観測比 / 期待比 - 1| の実測分布:
    仮説A  中央値 0.0206 / 75%点 0.0371 / 90%点 0.0659 / 95%点 0.0910 /
           99%点 0.1869 / 最大 0.8000
    仮説B  最小 0.1024 / 中央値 0.8832 / 最大 371.59
    行ごとの勝ち負け（偏差が小さいほう）は 237 行すべてで仮説A。
    許容 0.10 に収まる行数は 仮説A 226 行 / 仮説B 0 行。
したがって本モジュールは **仮説A** を規約として実装する。
    1:2 分割 -> factor 0.5 で当日終値がほぼ半値、20:1 併合 -> factor 20 で約20倍。
同ファイルの AdjC 列とも整合する（権利落ち前の AdjC = 権利落ち前 Close x factor）。

【許容幅 tolerance の既定 0.30 の根拠】
上記 237 行のうち 1773/2025-05-26（偏差 0.8000）を除く 236 行の偏差は最大 0.1900
（6574/2025-08-28。1:10 分割の権利落ち日に実勢 +19% の値動きが重なった例）。
偏差 0.1900 と 0.8000 の間に観測値は 1 件もなく、しきい値を (0.19, 0.80) の
どこに置いても 2025 年の判定は変わらない。既定 0.30 は
  - 実勢最大 0.1900 に対して約 1.6 倍の余裕がある
  - 日本株の通常の値幅制限（低位株では概ね ±30%）が権利落ちに重なっても誤検知しない
という2点から採った。分割と実勢の値動きが重なる正常系を通し、
「係数 5.0 なのに生の終値が 64 -> 64 で横ばい」のような **総崩れの矛盾だけ** を拾う。

--------------------------------------------------------------------------------
Reads : 引数で渡された DataFrame のみ（CLI 実行時は下記を読むだけ・書き込みなし）
        analysis/star_hunter/adj_close_panel.parquet (Date/Code/AdjClose/Turnover)
        price_history/{year}.parquet                (Date/Code/Close/AdjustmentFactor ほか)
Writes: なし
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# 出力ルートはリポジトリ位置から導出する（端末依存の絶対パスを埋め込まない）。
# bi/pipelines/data_guards.py -> pipelines -> bi -> リポルート
OUTROOT = Path(__file__).resolve().parents[2] / "bi" / "outputs"
DEFAULT_PANEL = str(OUTROOT / "analysis" / "star_hunter" / "adj_close_panel.parquet")
DEFAULT_PRICE_HISTORY = str(OUTROOT / "price_history" / "2025.parquet")

# Guard A: 前日比のしきい値（|pct| がこれを超えた日を検査対象に拾う）
DEFAULT_TURNOVER_THRESHOLD = 0.20
# Guard A: 増分のうち母集団構成変化が説明する割合がこれを超えたら universe_change
DEFAULT_MAJORITY_SHARE = 0.50
# Guard B: 観測比と係数の相対偏差の許容幅（既定の根拠はモジュール docstring 参照）
DEFAULT_FACTOR_TOLERANCE = 0.30


class DataGuardError(RuntimeError):
    """ガードが致命的な不整合を検知したことを示す例外（raise_if_failed が送出する）。"""


@dataclass
class GuardResult:
    """
    ガード1本の判定結果。

    name       : ガード名
    passed     : 致命的な検知が 0 件なら True（n_critical == 0）
    n_checked  : 判定にかけた件数（Guard A=前日比を計算できた日数 / Guard B=係数イベント数）
    n_flagged  : 一次スクリーニングに引っかかった件数
                 （Guard A=しきい値超えの日数 / Guard B=n_checked と同じ）
    n_critical : 致命的と判定した件数
                 （Guard A=cause が universe_change の日数 / Guard B=verdict が inconsistent の行数）
    n_skipped  : 前日データ欠損などで判定不能だった件数（合否には数えない）
    detail     : 明細 DataFrame（Guard A=しきい値超えの日 / Guard B=係数イベント全行）
    params     : 実行時パラメータ
    summary    : 1行サマリ（ログにそのまま流せる文字列）
    """

    name: str
    passed: bool
    n_checked: int
    n_flagged: int
    n_critical: int
    n_skipped: int
    detail: pd.DataFrame
    params: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    @property
    def critical(self) -> pd.DataFrame:
        """detail のうち致命的判定の行だけを返す（オペレータが見るべき行）。"""
        if self.detail.empty:
            return self.detail
        if "cause" in self.detail.columns:
            return self.detail[self.detail["cause"] == "universe_change"]
        if "verdict" in self.detail.columns:
            return self.detail[self.detail["verdict"] == "inconsistent"]
        return self.detail.iloc[0:0]

    def __str__(self) -> str:  # ログ用
        mark = "OK" if self.passed else "NG"
        return f"[{mark}] {self.name}: {self.summary}"


def raise_if_failed(result: GuardResult, *, max_rows: int = 10) -> GuardResult:
    """
    GuardResult を受けて、致命的検知があれば DataGuardError を送出する薄いラッパ。

    ガード本体（check_*）は判定するだけで例外を投げない。パイプラインを止めたい
    呼び出し側だけがこの関数を挟む、という責務分離のための関数。
    合格時は result をそのまま返すので `res = raise_if_failed(check_...(df))` と書ける。
    """
    if result.passed:
        return result
    head = result.critical.head(max_rows).to_string(index=False)
    raise DataGuardError(f"{result.name}: {result.summary}\n{head}")


def _require_columns(df: pd.DataFrame, cols: list[str], who: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{who}: 必要な列がありません {missing}（実際の列: {list(df.columns)}）")


# =============================================================================
# Guard A: 日次売買代金合計の不連続
# =============================================================================
def check_market_turnover_jump(
    panel: pd.DataFrame,
    threshold: float = DEFAULT_TURNOVER_THRESHOLD,
    *,
    majority_share: float = DEFAULT_MAJORITY_SHARE,
    date_col: str = "Date",
    code_col: str = "Code",
    turnover_col: str = "Turnover",
) -> GuardResult:
    """
    母集団全体の日次売買代金合計の前日比ジャンプを検知し、その原因を分解する。

    Parameters
    ----------
    panel : DataFrame
        Date / Code / Turnover を持つ日次パネル。(Date, Code) が重複していても
        銘柄単位で合算してから扱うため問題ない。値の書き換えはしない。
    threshold : float
        |前日比| がこれを超えた日を検査対象に拾う（既定 0.20 = 20%）。
    majority_share : float
        増分のうち母集団構成変化（新規登場銘柄 - 消滅銘柄）が説明する割合が
        これを超えたら cause="universe_change"（既定 0.50 = 過半）。

    Returns
    -------
    GuardResult
        detail は しきい値超えの日ごとに1行:
          date                            対象日
          total_prev / total_now          前営業日 / 当日の売買代金合計
          pct_change                      前日比（total_now / total_prev - 1）
          n_codes_prev / n_codes_now      前営業日 / 当日に存在した銘柄数
          n_new_codes                     当日にだけ現れた銘柄数（前日は不在）
          turnover_from_new_codes         その新規銘柄が当日に稼いだ売買代金
          share_of_delta_from_new_codes   増分に占める新規銘柄の割合
          cause                           universe_change / market_move
          delta                           total_now - total_prev
          n_dropped_codes                 前営業日にいて当日消えた銘柄数
          turnover_from_dropped_codes     その消滅銘柄が前営業日に稼いだ売買代金
          share_of_delta_from_composition (新規 - 消滅) / 増分。cause の判定に使う値
          common_turnover_prev/now        両日に共通して存在した銘柄の売買代金合計
          common_pct_change               共通銘柄だけで見た前日比（= 実勢の値動き）

    Notes
    -----
    増分は 増分 = (新規銘柄の当日分) - (消滅銘柄の前日分) + (共通銘柄の増減) に
    厳密に分解される（銘柄集合の直和分割なので残差は出ない）。
    cause は第1項と第2項の合計、すなわち母集団の入れ替えが増分の何割を占めるかで決める。
    """
    _require_columns(panel, [date_col, code_col, turnover_col], "check_market_turnover_jump")
    if not 0 < majority_share <= 1:
        raise ValueError(f"majority_share は (0, 1] で指定してください: {majority_share}")

    cols = ["date", "total_prev", "total_now", "pct_change", "n_codes_prev", "n_codes_now",
            "n_new_codes", "turnover_from_new_codes", "share_of_delta_from_new_codes", "cause",
            "delta", "n_dropped_codes", "turnover_from_dropped_codes",
            "share_of_delta_from_composition", "common_turnover_prev", "common_turnover_now",
            "common_pct_change"]

    daily = panel.groupby(date_col)[turnover_col].sum().sort_index()
    pct = daily.pct_change(fill_method=None)
    n_checked = int(pct.notna().sum())

    dates = list(daily.index)
    prev_of = {dates[i]: dates[i - 1] for i in range(1, len(dates))}
    hit = [d for d in dates[1:] if pd.notna(pct.loc[d]) and abs(pct.loc[d]) > threshold]

    if not hit:
        empty = pd.DataFrame(columns=cols)
        return GuardResult(
            name="market_turnover_jump", passed=True, n_checked=n_checked, n_flagged=0,
            n_critical=0, n_skipped=0, detail=empty,
            params={"threshold": threshold, "majority_share": majority_share},
            summary=f"{n_checked}遷移を検査。|前日比|>{threshold:.0%} の日なし。",
        )

    # --- しきい値超えの日とその前営業日だけを取り出して銘柄単位で突き合わせる ---
    hit_set = set(hit)
    prev_set = {prev_of[d] for d in hit}
    next_of = {prev_of[d]: d for d in hit}  # 前営業日 -> 対象日（1対1）

    sub = panel.loc[panel[date_col].isin(hit_set | prev_set), [date_col, code_col, turnover_col]]
    agg = sub.groupby([date_col, code_col], as_index=False)[turnover_col].sum()

    now_side = agg[agg[date_col].isin(hit_set)].copy()
    now_side["key_date"] = now_side[date_col]
    now_side["present_now"] = 1
    now_side = now_side.rename(columns={turnover_col: "t_now"})[
        ["key_date", code_col, "t_now", "present_now"]]

    prev_side = agg[agg[date_col].isin(prev_set)].copy()
    prev_side["key_date"] = prev_side[date_col].map(next_of)
    prev_side["present_prev"] = 1
    prev_side = prev_side.rename(columns={turnover_col: "t_prev"})[
        ["key_date", code_col, "t_prev", "present_prev"]]

    m = now_side.merge(prev_side, on=["key_date", code_col], how="outer")
    # 「銘柄が存在したか」は present_* フラグで持つ（売買代金が NaN の行と区別するため）。
    for c in ("present_now", "present_prev"):
        m[c] = m[c].fillna(0).astype(int)
    for c in ("t_now", "t_prev"):
        m[c] = m[c].fillna(0.0)

    is_new = (m["present_now"] == 1) & (m["present_prev"] == 0)
    is_dropped = (m["present_now"] == 0) & (m["present_prev"] == 1)
    is_common = (m["present_now"] == 1) & (m["present_prev"] == 1)

    idx = pd.Index(hit, name="key_date")

    def _sum(mask: pd.Series, col: str) -> pd.Series:
        return m.loc[mask].groupby("key_date")[col].sum().reindex(idx).fillna(0.0)

    def _cnt(mask: pd.Series) -> pd.Series:
        return m.loc[mask].groupby("key_date").size().reindex(idx).fillna(0).astype(int)

    out = pd.DataFrame(index=idx)
    out["total_prev"] = [daily.loc[prev_of[d]] for d in hit]
    out["total_now"] = [daily.loc[d] for d in hit]
    out["pct_change"] = [pct.loc[d] for d in hit]
    out["n_codes_prev"] = m.groupby("key_date")["present_prev"].sum().reindex(idx).fillna(0).astype(int)
    out["n_codes_now"] = m.groupby("key_date")["present_now"].sum().reindex(idx).fillna(0).astype(int)
    out["n_new_codes"] = _cnt(is_new)
    out["turnover_from_new_codes"] = _sum(is_new, "t_now")
    out["delta"] = out["total_now"] - out["total_prev"]
    out["n_dropped_codes"] = _cnt(is_dropped)
    out["turnover_from_dropped_codes"] = _sum(is_dropped, "t_prev")
    out["common_turnover_prev"] = _sum(is_common, "t_prev")
    out["common_turnover_now"] = _sum(is_common, "t_now")

    safe_delta = out["delta"].where(out["delta"] != 0)
    out["share_of_delta_from_new_codes"] = out["turnover_from_new_codes"] / safe_delta
    composition = out["turnover_from_new_codes"] - out["turnover_from_dropped_codes"]
    out["share_of_delta_from_composition"] = composition / safe_delta
    safe_common_prev = out["common_turnover_prev"].where(out["common_turnover_prev"] != 0)
    out["common_pct_change"] = out["common_turnover_now"] / safe_common_prev - 1.0

    # 母集団の入れ替えが増分の過半を説明する日だけを universe_change と呼ぶ。
    out["cause"] = np.where(
        out["share_of_delta_from_composition"] > majority_share, "universe_change", "market_move")

    out = out.reset_index().rename(columns={"key_date": "date"})[cols].sort_values("date")
    n_critical = int((out["cause"] == "universe_change").sum())

    return GuardResult(
        name="market_turnover_jump",
        passed=n_critical == 0,
        n_checked=n_checked,
        n_flagged=len(out),
        n_critical=n_critical,
        n_skipped=0,
        detail=out.reset_index(drop=True),
        params={"threshold": threshold, "majority_share": majority_share},
        summary=(f"{n_checked}遷移を検査。|前日比|>{threshold:.0%} が {len(out)}日、"
                 f"うち母集団構成変化が主因（universe_change）は {n_critical}日。"),
    )


# =============================================================================
# Guard B: AdjustmentFactor と生の終値比の整合
# =============================================================================
def check_adjustment_factor_consistency(
    df: pd.DataFrame,
    tolerance: float = DEFAULT_FACTOR_TOLERANCE,
    *,
    date_col: str = "Date",
    code_col: str = "Code",
    close_col: str = "Close",
    factor_col: str = "AdjustmentFactor",
    neutral_factor: float = 1.0,
) -> GuardResult:
    """
    AdjustmentFactor != 1.0 の行で、生の終値比が係数と整合しているかを検査する。

    規約は 2025 年実データで実測した **観測比 = 係数**（モジュール docstring 参照）:
        observed_ratio = Close_t / Close_{t-1}
        expected_ratio = AdjustmentFactor
        deviation      = |observed_ratio / expected_ratio - 1|
    deviation > tolerance の行を verdict="inconsistent" として拾う。

    Parameters
    ----------
    df : DataFrame
        price_history 形状（Date / Code / Close / AdjustmentFactor）。書き換えはしない。
    tolerance : float
        相対偏差の許容幅（既定 0.30）。権利落ちと実勢の値動きが重なる正常系を通し、
        総崩れの矛盾だけを拾うため広めに取る。既定値の根拠はモジュール docstring 参照。
    neutral_factor : float
        調整なしを表す係数（既定 1.0）。この値の行は検査対象外。

    Returns
    -------
    GuardResult
        detail は係数イベント全行（正常系も含む）で、列は:
          code, date, factor, close_prev, close_now,
          observed_ratio, expected_ratio, deviation, verdict, prev_date
        verdict は
          ok                -- deviation <= tolerance
          inconsistent      -- deviation > tolerance（要調査）
          insufficient_data -- 直前の終値が無い / 終値や係数が非正で比が計算できない
                               （上場初日・年跨ぎのファイル境界・上場前の空行など。
                                 合否には数えず、推定での穴埋めもしない）

    Notes
    -----
    直前の終値は同じ df 内で Code ごとに1行前の Close を取る。年別ファイルを
    1年ぶんだけ渡すと年初の権利落ちが insufficient_data になるため、境界を厳密に
    見たい場合は前年ぶんを連結してから渡す。
    """
    _require_columns(df, [date_col, code_col, close_col, factor_col],
                     "check_adjustment_factor_consistency")
    if tolerance <= 0:
        raise ValueError(f"tolerance は正の値で指定してください: {tolerance}")

    work = df[[date_col, code_col, close_col, factor_col]].copy()
    work[code_col] = work[code_col].astype(str)
    work = work.sort_values([code_col, date_col], kind="mergesort")
    grp = work.groupby(code_col, sort=False)
    work["close_prev"] = grp[close_col].shift(1)
    work["prev_date"] = grp[date_col].shift(1)

    f = work[factor_col]
    ev = work[f.notna() & (f != neutral_factor)].copy()

    ev = ev.rename(columns={code_col: "code", date_col: "date",
                            close_col: "close_now", factor_col: "factor"})
    ev["expected_ratio"] = ev["factor"]

    computable = (
        ev["close_prev"].notna() & (ev["close_prev"] > 0)
        & ev["close_now"].notna() & (ev["close_now"] > 0)
        & (ev["expected_ratio"] > 0)
    )
    ev["observed_ratio"] = np.where(computable, ev["close_now"] / ev["close_prev"], np.nan)
    ev["deviation"] = np.where(
        computable, np.abs(ev["observed_ratio"] / ev["expected_ratio"] - 1.0), np.nan)

    ev["verdict"] = np.where(
        ~computable, "insufficient_data",
        np.where(ev["deviation"] > tolerance, "inconsistent", "ok"))

    cols = ["code", "date", "factor", "close_prev", "close_now",
            "observed_ratio", "expected_ratio", "deviation", "verdict", "prev_date"]
    ev = ev[cols].sort_values(["date", "code"]).reset_index(drop=True)

    n_events = len(ev)
    n_skipped = int((ev["verdict"] == "insufficient_data").sum())
    n_critical = int((ev["verdict"] == "inconsistent").sum())
    n_ok = int((ev["verdict"] == "ok").sum())

    return GuardResult(
        name="adjustment_factor_consistency",
        passed=n_critical == 0,
        n_checked=n_events - n_skipped,
        n_flagged=n_events,
        n_critical=n_critical,
        n_skipped=n_skipped,
        detail=ev,
        params={"tolerance": tolerance, "neutral_factor": neutral_factor,
                "convention": "Close_t / Close_prev = AdjustmentFactor"},
        summary=(f"係数!={neutral_factor} の {n_events}行を検査。"
                 f"整合 {n_ok}行 / 不整合 {n_critical}行 / 判定不能 {n_skipped}行"
                 f"（許容 {tolerance:.2f}）。"),
    )


# =============================================================================
# CLI（本番ファイルを読むだけ・書き込みなし）
# =============================================================================
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="データ健全性ガードを本番ファイルに対して実行する（読み取り専用）。")
    ap.add_argument("--panel", default=DEFAULT_PANEL,
                    help="Guard A の入力パネル parquet（Date/Code/Turnover）。")
    ap.add_argument("--price-history", default=DEFAULT_PRICE_HISTORY,
                    help="Guard B の入力 price_history parquet（Date/Code/Close/AdjustmentFactor）。")
    ap.add_argument("--threshold", type=float, default=DEFAULT_TURNOVER_THRESHOLD,
                    help="Guard A の前日比しきい値（既定 0.20）。")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_FACTOR_TOLERANCE,
                    help="Guard B の相対偏差の許容幅（既定 0.30）。")
    ap.add_argument("--strict", action="store_true",
                    help="致命的検知があれば DataGuardError を送出して異常終了する。")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 40)

    print(f"[in] panel          = {args.panel}")
    panel = pd.read_parquet(args.panel)
    a = check_market_turnover_jump(panel, threshold=args.threshold)
    print(a)
    if a.n_critical:
        show = ["date", "total_prev", "total_now", "pct_change", "n_codes_prev", "n_codes_now",
                "n_new_codes", "turnover_from_new_codes", "share_of_delta_from_new_codes",
                "common_pct_change", "cause"]
        print(a.critical[show].to_string(index=False))

    print(f"\n[in] price_history  = {args.price_history}")
    ph = pd.read_parquet(args.price_history,
                         columns=["Date", "Code", "Close", "AdjustmentFactor"])
    b = check_adjustment_factor_consistency(ph, tolerance=args.tolerance)
    print(b)
    if b.n_critical:
        print(b.critical.to_string(index=False))
    if b.n_skipped:
        print("[skip] 判定不能（直前終値なし）:")
        print(b.detail[b.detail["verdict"] == "insufficient_data"].to_string(index=False))

    if args.strict:
        raise_if_failed(a)
        raise_if_failed(b)


if __name__ == "__main__":
    main()
