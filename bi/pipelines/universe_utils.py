# -*- coding: utf-8 -*-
"""
母集団（ユニバース）判定の唯一の正本。

【なぜ必要か】
同じ「個別株パネル」を作る2本のスクリプトが、別々のインラインなコード判定を
持っていた。

    build_adjusted_prices.py  : screening_master の Code が ^\\d{4}$ のものだけ
                                （= 4桁数字 ∩ screening_master。意図どおりの母集団）
    update_adjusted_panel.py  : J-Quants の生コードが「5文字かつ末尾が 0」
                                （= 4桁銘柄も英字銘柄も ETF も全部通る緩い判定）

後者は screening_master と突き合わせないため、日次更新が走った日を境に ETF・
英字コードが一斉にパネルへ流入し、日次売買代金合計が跳ね上がった。同じ母集団を
2箇所で別々に定義したことが事故の直接原因なので、判定を本モジュールに一本化する。

【モード】
    digit4 : ^[0-9]{4}$          ∩ screening_master   -- 現行の意図どおりの母集団
    equity : ^[0-9]{4}$ または ^[0-9]{3}[A-Z]$
                                  ∩ screening_master   -- 英字コードまで含めた拡張母集団

screening_master 自体が ETF・REIT を除外済みなので、積集合を取るだけで ETF は落ちる。
一方 screening_master には英字コード（^[0-9]{3}[A-Z]$。2024年以降に採番が始まった
新形式）が含まれるため、英字を入れるか外すかは shape 側で決める必要がある。

2026-08 の Stage 3b で **生産スクリプト2本とも equity へ切り替えた**
（build_adjusted_prices.py / update_adjusted_panel.py）。digit4 のままだと英字コードが
日次更新のたびにパネルから追い出され、対象銘柄が静かに欠け続けるため。
digit4 は移行前の挙動を再現・比較するために残してある。

【コードの正規化】
J-Quants の日次バーは 5 文字コード（4桁銘柄 13050 / 英字銘柄 285A0）で返る。
判定の前に必ず jq_client_utils.normalize_code_4 で先頭4文字へ落としてから
screening_master と突き合わせる（正規化前に突き合わせると全件不一致になる）。

Reads : 引数で渡された Code 列 / screening_master.parquet（load_screening_master_codes 使用時のみ）
Writes: なし
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from jq_client_utils import normalize_code_4

# 出力ルートはリポジトリ位置から導出する（端末依存の絶対パスを埋め込まない）。
# bi/pipelines/universe_utils.py -> pipelines -> bi -> リポルート
OUTROOT = Path(__file__).resolve().parents[2] / "bi" / "outputs"
DEFAULT_SCREENING_MASTER = str(OUTROOT / "screening_master.parquet")

# --- コード形状の正規表現（本モジュールが唯一の定義箇所） -----------------------
RE_DIGIT4 = re.compile(r"^[0-9]{4}$")        # 従来からの4桁数字コード
RE_LETTER = re.compile(r"^[0-9]{3}[A-Z]$")   # 2024年以降の英字入りコード（例 285A）

#: 対応モードと、そのモードが受け付けるコード形状
UNIVERSE_MODES: dict[str, tuple[re.Pattern[str], ...]] = {
    "digit4": (RE_DIGIT4,),
    "equity": (RE_DIGIT4, RE_LETTER),
}
DEFAULT_MODE = "digit4"

# --- 除外理由のラベル ---------------------------------------------------------
REASON_INCLUDED = "included"
#: 形状は条件を満たすが screening_master に無い（ETF・REIT・上場廃止・非対象市場など）
REASON_NOT_IN_SM = "not_in_screening_master"
#: 英字コードだが、モードが digit4 なので形状で落ちた
REASON_LETTER_CODE = "letter_code"
#: 4桁数字でも英字コードでもない形状（想定外の桁数・記号付きなど）
REASON_OTHER_SHAPE = "non_4digit_or_etf_shaped"

EXCLUSION_REASONS: tuple[str, ...] = (REASON_NOT_IN_SM, REASON_LETTER_CODE, REASON_OTHER_SHAPE)


def _patterns(mode: str) -> tuple[re.Pattern[str], ...]:
    try:
        return UNIVERSE_MODES[mode]
    except KeyError:
        raise ValueError(
            f"未知の mode: {mode!r}（使えるのは {sorted(UNIVERSE_MODES)}）") from None


def _matches_shape(code: str, mode: str) -> bool:
    return any(p.match(code) for p in _patterns(mode))


def normalize_codes(codes: Iterable[object]) -> pd.Series:
    """
    任意のコード列を判定用の4文字コードへ正規化した Series を返す。

    J-Quants の 5 文字コード（13050 / 285A0）を 1305 / 285A に落とす。
    既に4文字のものはそのまま通る。欠損は空文字にはせず "nan" 等の文字列化を避けるため
    NaN のまま残さず、str 化した上で正規化する（判定側で形状不一致として落ちる）。
    """
    s = pd.Series(list(codes), dtype="object") if not isinstance(codes, pd.Series) else codes
    return s.astype(str).map(normalize_code_4)


def load_screening_master_codes(path: str | Path = DEFAULT_SCREENING_MASTER) -> set[str]:
    """screening_master.parquet の Code 列を正規化済みの集合として読む（読むだけ）。"""
    sm = pd.read_parquet(path, columns=["Code"])
    return set(normalize_codes(sm["Code"]))


def universe_codes(screening_codes: Iterable[str], mode: str = DEFAULT_MODE) -> set[str]:
    """
    screening_master のコード集合から、指定モードの母集団そのものを作る。

    「パネルの行を絞る」のではなく「母集団の名簿を作る」用途（build_adjusted_prices.py が
    price_history をこの名簿で絞り込むのに使う）。
    """
    return {c for c in normalize_codes(list(screening_codes)) if _matches_shape(c, mode)}


def in_universe(
    codes: Iterable[object],
    screening_codes: Iterable[str],
    mode: str = DEFAULT_MODE,
    *,
    normalize: bool = True,
) -> np.ndarray:
    """
    コード列に対する真偽マスク（形状条件 ∩ screening_master）を返す。

    normalize=False は、既に正規化済みだと分かっている列で二度手間を避けるための逃げ道。
    """
    sm = set(normalize_codes(list(screening_codes)))
    s = normalize_codes(codes) if normalize else pd.Series(list(codes)).astype(str)
    shape_ok = s.map(lambda c: _matches_shape(c, mode))
    return (shape_ok & s.isin(sm)).to_numpy(dtype=bool)


def filter_to_universe(
    df: pd.DataFrame,
    screening_codes: Iterable[str],
    mode: str = DEFAULT_MODE,
    *,
    code_col: str = "Code",
    normalize: bool = True,
) -> pd.DataFrame:
    """
    DataFrame を母集団の行だけに絞り、code_col を正規化済み4文字コードへ揃えて返す。

    元の DataFrame は書き換えない（コピーを返す）。値の補完・推定は一切しない。
    """
    if code_col not in df.columns:
        raise ValueError(f"列 {code_col!r} がありません（実際の列: {list(df.columns)}）")
    out = df.copy()
    if normalize:
        out[code_col] = normalize_codes(out[code_col])
    else:
        out[code_col] = out[code_col].astype(str)
    mask = in_universe(out[code_col], screening_codes, mode, normalize=False)
    return out.loc[mask].reset_index(drop=True)


def explain_exclusions(
    codes: Iterable[object],
    screening_codes: Iterable[str],
    mode: str = DEFAULT_MODE,
) -> pd.DataFrame:
    """
    ユニークなコードごとに採否と除外理由を付けた明細を返す（監査・報告用）。

    Returns
    -------
    DataFrame
        raw_code  入力そのままの文字列（5文字コードならそのまま）
        code      正規化後の4文字コード
        included  母集団に入るか
        reason    included / not_in_screening_master / letter_code / non_4digit_or_etf_shaped

    Notes
    -----
    理由の判定順は「形状 -> 名簿」。形状で落ちたものは英字コードか否かで
    letter_code / non_4digit_or_etf_shaped に振り分け、形状を通ったのに落ちたものだけを
    not_in_screening_master とする。ETF（1305 等）は4桁数字なので形状は通り、
    screening_master に居ないことで not_in_screening_master に入る。
    """
    sm = set(normalize_codes(list(screening_codes)))
    raw = pd.Series(pd.unique(pd.Series(list(codes)).astype(str)), name="raw_code")
    norm = raw.map(normalize_code_4)

    reasons = []
    for c in norm:
        if not _matches_shape(c, mode):
            reasons.append(REASON_LETTER_CODE if RE_LETTER.match(c) else REASON_OTHER_SHAPE)
        elif c not in sm:
            reasons.append(REASON_NOT_IN_SM)
        else:
            reasons.append(REASON_INCLUDED)

    out = pd.DataFrame({"raw_code": raw, "code": norm, "reason": reasons})
    out["included"] = out["reason"] == REASON_INCLUDED
    return out[["raw_code", "code", "included", "reason"]]


def summarize_exclusions(detail: pd.DataFrame) -> pd.Series:
    """explain_exclusions の明細を理由別の件数へ畳む（0件の理由も 0 で明示する）。"""
    counts = detail["reason"].value_counts()
    index = [REASON_INCLUDED, *EXCLUSION_REASONS]
    return counts.reindex(index).fillna(0).astype(int)


def format_exclusion_report(detail: pd.DataFrame, *, sample: int = 8) -> str:
    """理由別件数と代表コードを1つの文字列にまとめる（ログにそのまま流す用）。"""
    summary = summarize_exclusions(detail)
    lines = []
    for reason, n in summary.items():
        codes: Sequence[str] = sorted(detail.loc[detail["reason"] == reason, "code"])
        head = ", ".join(codes[:sample]) + (" ..." if len(codes) > sample else "")
        lines.append(f"  {reason:26s} {n:6,d}  {head}")
    return "\n".join(lines)
