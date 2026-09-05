#!/usr/bin/env python3
"""テーマ資金流入スコアラー（v2・2026-08-31 全面改修）

みんかぶテーマタグ表（theme_master_minkabu.parquet）に対し、動意上位銘柄／PTS 上昇銘柄の
「資金量」をテーマへ按分配分し、当日スコアと直近10営業日の熱量スコアを算出する。

v1（テーマ初動レーダー・テーマ熱量）からの変更点:
1. 資金テーマでない括り（IPO 年次・株主優待・高配当・市場区分・親子上場・指数構成・
   地域名のみ 等）を正規表現で除外する。
2. 銘柄の寄与を「所属テーマ数」で按分する（多テーマ所属の大型株が全テーマを均等に
   押し上げる歪みを消す）。寄与 = log 圧縮した売買代金 × 騰落率の正の部分 ÷ 所属テーマ数。
3. 点灯銘柄集合が重複するテーマ（Jaccard >= 0.5 または包含関係）を1行へ統合し、
   構成銘柄数が最小＝最も具体的なテーマ名を代表名とする。
4. 出力は「本日のテーマ」（当日スコア上位5）と「直近2週間の熱いテーマ」
   （10営業日の熱量スコア上位5・局面を機械判定）の2部のみ。

本モジュールは検知ロジックと md セクション文字列の生成のみを担い、ファイル書き出し・
Discord 送信は呼び出し側（make_mover_report.py / make_pts_mover_report.py）が行う。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
JST = timezone(timedelta(hours=9))

# テーマ蓄積 parquet（movers_top100_daily / stock_context_daily / early_candidates_daily /
# own_themes_daily）の出力先ディレクトリ。既定は本番 bi/outputs/analysis/theme_radar/。
# 環境変数 THEME_RADAR_OUT_DIR が設定されている場合のみそちらへ差し替える（検証用のロー
# カルコピーで本番 parquet を触らずに raw を再生成するため）。未設定時の挙動は不変。
_THEME_RADAR_OUT_DIR = (
    Path(os.environ["THEME_RADAR_OUT_DIR"])
    if os.environ.get("THEME_RADAR_OUT_DIR")
    else (BASE_DIR / ".." / "outputs" / "analysis" / "theme_radar")
)

# テーマタグ表（みんかぶ・月次更新）
THEME_MASTER_PATH = BASE_DIR / ".." / "outputs" / "theme_master_minkabu.parquet"

# 動意上位100銘柄の日次蓄積先
MOVERS_HISTORY_PATH = _THEME_RADAR_OUT_DIR / "movers_top100_daily.parquet"

# 銘柄コンテキスト（「何の会社」＋「なぜ動いた」材料）の日次蓄積先。
# 2026-08-31 PM 指示。当日の動意上位100銘柄しか EDINET 事業概要・材料テキストを取得しない
# ため、熱量テーマの主導銘柄が当日 Top100 圏外だと「何の会社」が空欄（―）になり、材料も
# 取れず「材料不明」で一律除外されていた。本 parquet に日次で蓄積し、当日取れない銘柄は
# 直近 CONTEXT_LOOKBACK_DAYS 営業日以内の記述を日付付きで再利用する。
# 本番 parquet への列追加ではなく独立ファイル（file_safety_rules 準拠）。
STOCK_CONTEXT_PATH = _THEME_RADAR_OUT_DIR / "stock_context_daily.parquet"

# スクリーニングマスター（業種名の最終フォールバック用・読み取りのみ）
SCREENING_MASTER_PATH = BASE_DIR / ".." / "outputs" / "screening_master.parquet"

# 自前括りテーマ（GHA 側 Claude が材料から確定させた当日テーマ）の日次蓄積先。
# 2026-09-02 PM 承認の改修4。辞書（みんかぶタグ）に無いテーマは辞書ベースの継続性計算に
# 一切乗らないため、Claude が材料から作った括り（テーマ名＋銘柄コード集合＋共通材料）を
# 誌面 md の機械可読ブロックから回収して日次で貯める。翌日以降、辞書ベースの熱量行の
# 銘柄集合と一致する自前テーマがあれば、そのテーマ名を優先表示する（段階導入）。
# 本番 parquet への列追加ではなく独立ファイル（file_safety_rules 準拠）。
EARLY_CANDIDATES_PATH = _THEME_RADAR_OUT_DIR / "early_candidates_daily.parquet"
OWN_THEMES_PATH = _THEME_RADAR_OUT_DIR / "own_themes_daily.parquet"

# --- パラメータ ---
# 構成銘柄がこれを超える巨大テーマは母数が大きく偶然の同時掲載が起きるため除外。
MAX_THEME_SIZE = 100
# 当日テーマの点灯条件（1銘柄テーマは出さない）
MIN_CODES_FOR_ALERT = 2
# 熱量スコアの集計窓（営業日）と、比較する前の窓
HEAT_WINDOW_DAYS = 10
# テーマ統合の閾値（点灯銘柄集合の Jaccard 係数）
MERGE_JACCARD = 0.5
# 統合には最低この銘柄数の重複を要求する（1銘柄だけの重なりでは統合しない）
MERGE_MIN_OVERLAP = 2
# 誌面表示ベースの二次統合の閾値（2026-09-04 PM 指示）。
# merge_overlapping_themes は「点灯銘柄集合」で統合するが、誌面に実際に出る銘柄は
# 欄ごとに絞られる（初動候補は +3%以上の銘柄のみ、2週間欄は当日点灯銘柄のみ）ため、
# 統合後でも「誌面上は同じ銘柄群の別名テーマ」が複数枠を占めた（9/3 の
# 自動運転車 / フィジカルAI / MaaS）。表示対象集合で見て**小さい方の集合の
# DISPLAY_MERGE_RATIO 以上が重なる**場合も1行へ統合する。
DISPLAY_MERGE_RATIO = 0.5
# 表示ベース統合でも最低この銘柄数の重複を要求する（1銘柄の重なりでは統合しない）
DISPLAY_MERGE_MIN_OVERLAP = 2
# 候補行のうちこの割合以上に登場する銘柄は「多テーマ所属の大型株」とみなし、
# 表示ベース統合の判定根拠から除外する（総合商社・電力の共有だけで無関係なテーマが
# 芋づるに潰れるのを防ぐ）。
DISPLAY_PROMISCUOUS_RATIO = 0.34
# 大きい方の集合の側にも要求する重複率。小さい方の過半だけで統合すると、構成2〜3銘柄の
# 小テーマが大テーマへ次々と吸収されて1行が肥大する。
DISPLAY_MERGE_OUTER_RATIO = 0.34
# 片側だけの重なりで統合を認める「包含」の閾値。小さい方の集合のこの割合以上が
# 大きい方に含まれる場合、その行は大テーマの部分集合として別枠を占めているだけと見なす。
DISPLAY_MERGE_CONTAIN_RATIO = 0.6
# 新規性フィルタ（2026-09-04 PM 指示）: 誌面の各欄で、その行の表示銘柄のうち
# 「上位行に未掲載の銘柄」がこの数に満たない行は、新しい情報が無いので落として次点を
# 繰り上げる。9/3 実測で初動候補 4位 ナノテクノロジーが 三菱商事（1位既出）＋
# テクセンドフォトマスク（2位既出）の2銘柄だけになり、枠を消費して何も伝えなかった。
MIN_NEW_CODES = 2
# 「統合 → 銘柄の絞り込み → 新規性判定」を繰り返す最大回数（2026-09-04 PM 指示）。
# 絞り込みで表示銘柄が減ると新たな重複が生まれるため、変化がなくなるまで回す。
# 上限を置くのは、無制限だと全テーマが1行へ収束し得るため（実測）。
MERGE_REFINE_PASSES = 4


def _pct_at_least(rec: dict, pct: float) -> bool:
    """レコードの騰落率が pct 以上か（取れない場合は False）。"""
    try:
        return float(rec.get("return_pct") or 0) >= float(pct)
    except (TypeError, ValueError):
        return False

# 汎用銘柄の判定（2026-09-04 PM 指示）。上位プールのうちこの数以上のテーマに現れる銘柄は
# 商社・電力・大型汎用株であり、そのテーマを説明しないので表示・n3・新規性判定から外す。
# 9/3 実測で 三菱商事(8058・辞書上46テーマ所属)・住友商事(8053・48テーマ) が
# 自動運転車／EUV／再生可能エネルギーの3行すべてに並んだ。
GENERIC_CODE_MIN_THEMES = 3


def _self_codes(entry: dict, code_to_themes: dict | None) -> set:
    """その行の**代表テーマ自身**の辞書構成銘柄だけを返す（2026-09-04 PM 指示）。

    merge_by_display_overlap / merge_overlapping_themes は統合行の codes を和集合にする。
    和集合をそのまま誌面へ出すと、吸収した別名テーマにしか属さない銘柄が代表テーマの
    行に並ぶ（9/3 実測で 自動運転車 の行に レーザーテック(6920) が出た。6920 は辞書上
    自動運転車に属さず、吸収した EUV 系テーマ由来だった）。別名テーマは
    「（…を含む）」の名前表記だけで示し、銘柄は代表テーマ自身のものに限定する。

    code_to_themes が無い場合は判定できないので全件を返す（従来動作）。
    """
    theme = entry.get("theme")
    if not code_to_themes or not theme:
        return {str(c.get("code")) for c in (entry.get("codes") or [])}
    return {
        str(c.get("code"))
        for c in (entry.get("codes") or [])
        if theme in (code_to_themes.get(str(c.get("code"))) or [])
    }


def generic_codes(
    entries: list[dict],
    code_to_themes: dict | None = None,
    min_themes: int = GENERIC_CODE_MIN_THEMES,
) -> set:
    """上位プールの min_themes 以上のテーマに現れる「汎用銘柄」の集合を返す。

    数えるのは「誌面の何行に出得るか」であり、各行の代表テーマ自身の構成銘柄
    （_self_codes）を跨いで数える。統合後の codes（和集合）を数えると吸収した別名
    テーマ由来の銘柄まで数に入り、辞書テーマ数で数えるとプールの別名テーマが母数に
    入って本来の主役まで汎用扱いになる（9/3 実測でいずれも誤判定）。

    誌面の表示・n3・新規性判定から外すためのもので、ゲート判定の n_up（単一テーマとして
    何社同時に動いたか）からは外さない（PM 指示）。
    """
    from collections import Counter

    # 数えるのは「その銘柄が誌面の**何行**に出得るか」。行の掲載可否は代表テーマ自身の
    # 構成銘柄かどうかで決まる（_self_codes）ため、その集合で行を跨いだ出現数を数える。
    # 統合前の辞書テーマ数で数えると、プールの別名テーマまで母数に入って 593A のような
    # 本来の主役まで汎用扱いになる（9/3 実測でプール104テーマ・593A が 6 テーマ該当）。
    cnt: Counter = Counter()
    for e in entries:
        cnt.update(_self_codes(e, code_to_themes))
    return {c for c, n in cnt.items() if n >= int(min_themes) and c}




def filter_new_codes(
    rows: list[dict],
    display_codes,
    max_rows: int | None = None,
    min_new: int = MIN_NEW_CODES,
) -> list[dict]:
    """上位行に未掲載の銘柄が min_new 件未満の行を落とす（2026-09-04 PM 指示）。

    上から順に見て、その行の表示銘柄のうち「ここまでに採用した行で既に出た銘柄」を
    除いた残りが min_new 件未満なら、その行を採用しない（既出銘柄の寄せ集めであり
    誌面に新しい情報を足さないため）。落とした分は後続の候補が自動的に繰り上がる。
    枠（max_rows）を満たせない場合は少ない行数のまま返す（水増し禁止・§25）。

    Args:
        rows: 優先順（上位が先頭）に並んだ候補行。
        display_codes: row -> 誌面に出る銘柄コードの集合（または iterable）。
        max_rows: 採用する最大行数。None なら全件を判定する。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        try:
            disp = {str(c) for c in (display_codes(r) or []) if str(c).strip()}
        except Exception:
            disp = set()
        if not disp:
            continue
        if len(disp - seen) < int(min_new):
            continue
        out.append(r)
        seen |= disp
        if max_rows is not None and len(out) >= max(int(max_rows), 0):
            break
    return out


def refine_rows_by_display(
    rows: list[dict],
    display_codes,
    max_rows: int | None = None,
    score_key: str = "score",
    min_new: int = MIN_NEW_CODES,
    passes: int = MERGE_REFINE_PASSES,
) -> list[dict]:
    """「表示ベース統合 → 新規性判定」を行数が変化しなくなるまで繰り返す。

    2026-09-04 PM 指示。銘柄の絞り込み（代表テーマ限定・汎用銘柄除外）で表示銘柄が
    減った結果として新たに生じた重複は、絞り込み前に1度統合しただけでは畳めない
    （9/3 実測で初動候補の 2位・3位 がともに九州電力(9508)を表示した）。
    初動候補・2週間欄・本日のテーマで共通に使う。
    """
    out = filter_new_codes(rows, display_codes, max_rows=max_rows, min_new=min_new)
    for _ in range(max(int(passes), 0)):
        before = len(out)
        out = merge_by_display_overlap(out, display_codes, score_key=score_key)
        out.sort(
            key=lambda r: (
                -float(r.get(score_key, 0.0) or 0.0),
                str(r.get("theme") or ""),
            )
        )
        out = filter_new_codes(out, display_codes, max_rows=max_rows, min_new=min_new)
        if len(out) == before:
            break
    return out


# 各部の最大表示行数
# 「本日のテーマ」は機械が確定させない。機械は候補を TODAY_CANDIDATES 件まで出し、
# レポート作成 Claude が「主導銘柄2社以上に共通する材料があるテーマ」だけを残して
# 最大 MAX_ROWS_TODAY 行へ絞り込む（誌面の最終行数は Claude 判定後の結果）。
# v12（2026-09-01 PM 承認）: 当日3件・2週間3件を**目標**とする。
# 材料が積極的にテーマを支持する銘柄が2社未満のテーマは落とすため、材料が無い日は
# 3件に届かなくてよい（水増し禁止）。
MAX_ROWS_TODAY = 3
# v15（2026-09-02 PM 承認・材料起点への転換）: 当日テーマは 3〜5 件を目標とする。
# 辞書タグ起点では束ねられる候補がタグの粒度に縛られて 1〜3 件へ張り付いていたが、
# 材料起点では同じ出来事で動いた銘柄を自由に束ねられるため上限を 5 へ広げる。
MAX_ROWS_TODAY_MAX = 5
TODAY_CANDIDATES = 15
# 誌面の熱量テーマ**目標**件数（v12・2026-09-01 PM 承認で 5 → 3）。
# 支持2銘柄未満のテーマを落として次候補へ繰り上げ、この件数に届くまで補充する。
MAX_ROWS_HEAT = 3
# 熱量部は「熱量上位 HEAT_CANDIDATE_POOL 件」へ先に絞ってから局面順に並べる
# （局面順を先に適用すると熱量の低いテーマが「新規」だけで上位を占める）
# 2026-08-31: 帰属判定で落ちた行の繰り上げ用に HEAT_CANDIDATES(20) 件を raw へ出すため、
# 上流のプールも 20 へ広げる（15 のままだと raw が 15 件で頭打ちになる）。
HEAT_CANDIDATE_POOL = 20
# 熱量部の raw 掲載件数（2026-08-31 PM 確定・帰属判定での繰り上げ用）。
# 誌面は MAX_ROWS_HEAT 件だが、Claude が「主導銘柄の材料がテーマに合わない行」を落として
# 次の熱量候補へ繰り上げるため、raw には誌面件数より多い候補を出す。
HEAT_CANDIDATES = 20
# 主導銘柄の表示件数（売買代金上位）。誌面の主導銘柄表の目安行数でもある。
LEAD_CODES = 3
# raw へ出す主導銘柄の**候補**件数（2026-08-31 PM 確定・6 -> 12 へ拡大）。
# 誌面は最大 MAX_LEAD_ROWS 行だが、raw を売買代金上位数件で固定すると「材料（なぜ動いた）の
# 裏が取れている銘柄」がテーマ内で下位に居る場合に raw へ出ず、_cr §38 の積極支持要件
# （材料がテーマの共通材料を支持する銘柄2社以上でテーマ維持）を満たせないまま行が落ちる。
# 8/28 実測では 336A が自動運転車の6位・3987 がフィジカルAIの9位に居て raw から漏れ、
# 直近2週間の熱いテーマが1テーマまで痩せた。材料保有銘柄を優先して候補を広げ、
# どれを誌面へ載せるかは GHA 側 Claude の積極支持判定に委ねる。
LEAD_CANDIDATES = 12
# 誌面の主導銘柄表の上限行数（2026-08-31 PM 確定）。
# 「フィジカルAIがこんな2銘柄なわけない」（PM 指摘）。積極支持と判定した銘柄は
# 全部載せる方針へ変更し、上限だけを置く。2〜MAX_LEAD_ROWS 行。
MAX_LEAD_ROWS = 8
# 「何の会社」欄の目安字数。
# BIZ_DESC_TARGET_CHARS = 誌面で1行に収まる目安（Claude が原文からこの長さへ要約する）。
# BIZ_DESC_SOURCE_CHARS = raw へ載せる原文の上限（機械は切るだけで要約しない）。
BIZ_DESC_TARGET_CHARS = 15
BIZ_DESC_SOURCE_CHARS = 120
# 「加速」判定の閾値（前10営業日比の増加率）
ACCEL_DELTA_RATIO = 0.5
# テーマ表がこの日数より古ければ内部フラグを立てる
THEME_MASTER_STALE_DAYS = 30
# 「何の会社」・材料テキストを遡って再利用する営業日数（2026-08-31 PM 指示）
CONTEXT_LOOKBACK_DAYS = 10

# --- v11（2026-09-01 PM 承認）: 熱量指標を「継続性ベース」へ ---
# 旧指標（heat = 直近10営業日の当日スコア累計）は、当日大きく動いたテーマの当日スコアが
# そのまま累計に乗るため、当日セクションの上位テーマが自動的に2週間側の上位も占めた
# （PM 指摘: 当日3件と2週間3件が順番違いの同一テーマ）。
# v11 の主軸 = sustain_score = 「当日を除く直近10営業日」の点灯日数 × 平均点灯日スコア。
# 単日の急騰では点灯日数が 1 にしかならず上位に入れない。
# 1日を「点灯」とみなす最小構成銘柄数（score_one_day の codes 件数）
SUSTAIN_MIN_CODES = 2
# v12（2026-09-01 PM 承認）: 当日掲載テーマへの降格ペナルティを**廃止**した。
# v11 は当日掲載テーマの sustain へ 0.35 を掛けて下げたが、副作用として当日に点灯した
# 継続テーマ（8/28 の「自動運転車」等）が2週間側の上位から消えた（PM 指摘: 自動運転車が
# 直近2週間に出ないのは異常）。v12 は**純粋な継続性順**（当日を除く点灯日数 × 平均点灯日
# スコア）で並べ、当日掲載テーマが2週間側に出ることを許容する。点灯日数を掛ける構造は
# 維持するため、単日急騰型（点灯日数 1 前後）は上位化しない。
TODAY_SHOWN_PENALTY = 1.0  # 互換のため名前だけ残す（実質無効。新規コードで参照しない）

# --- v16（2026-09-03）: 初動候補テーマ（機械抽出）の判定定数 ---
# 「複数銘柄が同時に点灯し、かつそこへ実弾（売買代金）が入っているテーマ」だけを機械的に
# 拾う欄。ティアフォー（593A）が 8/20 に単独点灯 → 8/27 に +20.4% → 8/28 に 336A・4667 と
# 3銘柄同時点灯へ広がった動きを、当日中に「自動運転車」というテーマ名で拾えるかを基準に
# バックテストして採用した条件（案E）。
# 比率型（n_up 比・売買代金比・順位ジャンプ幅）は、構成銘柄が数社しかない無名テーマが
# 分母の小ささで常に上位を占め、実弾の入っていないテーマばかりが並んだため採用しない。
# 当日 score 降順で候補プールに入れる件数
EARLY_TOP_POOL = 10
# 候補プールから採るための最低点灯銘柄数（同時に動いていることの担保）
EARLY_MIN_NUP = 4
# --- E5（2026-09-03 PM 指示）: 判定を「実際に動いた銘柄」だけで見る形へ変更 ---
# 旧条件（点灯銘柄全体の売買代金合計 >= 500億）は、ほぼ動いていない大型株が代金の大半を
# 占めるテーマ（9/2 の防衛=1989億は伊藤忠+0.4%・グローバルサウス=1613億はニッスイ+0.5%
# 等が寄与）を通してしまった。E5 は「+3%以上動いた銘柄」に絞って本数と実弾を見るため、
# 8/27・8/28 の自動運転を維持したまま 9/2 の防衛・グローバルサウスを落とせる。
# 「実際に動いた」とみなす騰落率の下限（%）。_cr §38 の本日のテーマ側と同じ基準。
EARLY_MOVE_PCT = 3.0
# +3%以上で動いた銘柄の最低本数（1社の急騰だけでテーマ扱いしないための担保）
EARLY_MIN_NUP3 = 2
# +3%以上で動いた銘柄の売買代金合計の下限（億円・実弾が入っていることの担保）
EARLY_MIN_TURN3_OKU = 100
# 誌面へ出す最大件数（2026-09-03 PM 決定で枠5。3か月後に early_candidates_daily.parquet で再検証）
EARLY_MAX_ROWS = 5
# 局面判定に使う直近営業日数（当日を除く）。既存の lit_days と同じ窓を使う。
EARLY_HISTORY_WINDOW = HEAT_WINDOW_DAYS
# 点灯銘柄セルへ並べる最大件数（売買代金順）
EARLY_LEAD_CODES = 6

# ---------------------------------------------------------------------------
# v14（2026-09-02 PM 承認）: テーマ検知の母集団を「流動性・規模の足切り付き」へ刷新
# ---------------------------------------------------------------------------
# 旧母集団 = make_mover_report.extract_all_movers（全市場・当日騰落率の絶対値上位100・
# 売買代金下限なし・時価総額下限なし）。実測で点数の付く上昇銘柄の37%が時価総額100億円
# 未満、半数が売買代金5億円未満であり、小型株の派手な値動きがテーマ点灯に混ざっていた。
# v14 は「毎日見るに値する銘柄」を先に決めてからテーマ点灯を判定する。
#
#   1. 足切り: 当日売買代金 >= RADAR_MIN_TURNOVER_OKU 億円
#              かつ 時価総額 >= RADAR_MIN_MCAP_OKU 億円
#              かつ 当日騰落率 > 0（資金流入の事実のみ見る）
#              権利落ち（HasCorporateAction）は除外。ETF/REIT/上場投信は上流で除外済み。
#   2. グロース・スタンダード: 足切り通過銘柄を全件（実測 約60銘柄/日）
#   3. プライム: 足切り通過銘柄を stock_weight（= log10(1+売買代金[億円]) × 騰落率）の
#      高い順に RADAR_PRIME_TOP_N 件（プライムは足切り通過が約400/日と多く、全件だと
#      大型株の小幅高がテーマ点灯を埋め尽くすため点数上位で絞る）
#
# 点数の式・按分・点灯判定（MIN_CODES_FOR_ALERT）・継続性スコアは一切変更しない。
RADAR_MIN_TURNOVER_OKU = 5      # 当日売買代金の下限（億円）
RADAR_MIN_MCAP_OKU = 100        # 時価総額の下限（億円）
RADAR_PRIME_TOP_N = 50          # プライムの採用上限（点数上位）

# 全件採用する市場（グロース・スタンダード）。MarketCodeName の部分一致で判定する。
RADAR_FULL_MARKETS = ("グロース", "スタンダード")
# 点数上位で絞る市場（プライム）。
RADAR_RANKED_MARKETS = ("プライム",)


def _radar_market_bucket(name) -> str:
    """MarketCodeName を radar の市場バケット（full / ranked / other）へ写す。"""
    s = "" if name is None else str(name)
    for m in RADAR_FULL_MARKETS:
        if m in s:
            return "full"
    for m in RADAR_RANKED_MARKETS:
        if m in s:
            return "ranked"
    return "other"


def extract_radar_universe(
    full_df,
    min_turnover_oku: float = RADAR_MIN_TURNOVER_OKU,
    min_mcap_oku: float = RADAR_MIN_MCAP_OKU,
    prime_top_n: int = RADAR_PRIME_TOP_N,
):
    """テーマ早期検知レーダーの母集団を返す（v14・PM 承認済みロジック）。

    入力は make_mover_report が組み立てた全銘柄 DataFrame（ETF/REIT 除外済み・
    MarketCapOku はライブ時価総額で再計算済み・Turnover は円単位）。

    返す DataFrame は入力の列をそのまま保持し、`_radar_bucket`（full/ranked）と
    `_radar_score`（stock_weight）を付与する。件数は概ね 110 銘柄/日。
    """
    if full_df is None or len(full_df) == 0:
        return pd.DataFrame()

    df = full_df.copy()
    if "DailyReturn" not in df.columns:
        return pd.DataFrame()

    # 権利落ちは値動きが実態を表さないため除外
    if "HasCorporateAction" in df.columns:
        df = df[~df["HasCorporateAction"].fillna(False)]

    ret = pd.to_numeric(df["DailyReturn"], errors="coerce")
    df = df[ret > 0]
    if df.empty:
        return pd.DataFrame()

    # 売買代金（円）→ 億円。Turnover は make_mover_report が全銘柄へ付与済み。
    turn_oku = pd.to_numeric(df.get("Turnover"), errors="coerce") / 1e8
    # 時価総額（億円）。MarketCapOku はライブ時価総額（Close_T × 発行済株数）由来。
    if "MarketCapOku" in df.columns:
        mcap_oku = pd.to_numeric(df["MarketCapOku"], errors="coerce")
    else:
        mcap_oku = pd.to_numeric(df.get("MarketCap"), errors="coerce") / 1e8

    keep = (turn_oku >= float(min_turnover_oku)) & (mcap_oku >= float(min_mcap_oku))
    df = df[keep.fillna(False)]
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    if "MarketCodeName" in df.columns:
        df["_radar_bucket"] = df["MarketCodeName"].map(_radar_market_bucket)
    else:
        df["_radar_bucket"] = "other"
    df["_radar_score"] = [
        stock_weight(t, r)
        for t, r in zip(
            pd.to_numeric(df.get("Turnover"), errors="coerce"),
            pd.to_numeric(df["DailyReturn"], errors="coerce"),
        )
    ]

    full_part = df[df["_radar_bucket"] == "full"]
    ranked_part = df[df["_radar_bucket"] == "ranked"]
    if len(ranked_part) > prime_top_n:
        ranked_part = ranked_part.nlargest(prime_top_n, "_radar_score")

    out = pd.concat([full_part, ranked_part], ignore_index=True)
    if out.empty:
        return pd.DataFrame()
    return out.drop_duplicates("Code").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 資金テーマでない括りの除外パターン（2026-08-31 確定）
# みんかぶのテーマタグには「資金がそのテーマへ向かった」ことを意味しない分類上の括りが
# 多数含まれる。これらは動意上位に載った銘柄が偶然同じ属性を持っていただけであり、
# 誌面に出すと「意味のないテーマ」になるため機械除外する。
# ---------------------------------------------------------------------------
EXCLUDE_PATTERNS = [
    # 上場・IPO・市場区分・指数構成（銘柄の属性であってテーマではない）
    r"IPO",
    r"^\d{4}年の",              # 2018年のIPO 〜 2026年のIPO
    r"上場",                    # 親子上場・東証再編系（「上場投信」は元々テーマ表に無い）
    r"^あえて",                 # あえてスタンダード
    r"東証再編",
    r"JPX",
    r"日経\d",                  # 日経225・日経400 等
    r"TOPIX",
    r"MSCI|ラッセル|Russell",
    r"指数$|指数構成|シャリア指数",
    r"^株式市場$",
    r"^01銘柄$",
    # 配当・優待・決算といった投資家属性の括り
    r"配当",                    # 好配当・高配当・連続増配（増配も下で除外）
    r"増配",
    r"優待",
    r"^\d{1,2}月決算",
    r"決算$",
    # 投資・ファンド運用そのもの（事業テーマではない）
    r"^投資事業$",
    r"^事業承継$",
    # 表彰・認定など属性ラベル
    r"なでしこ銘柄|攻めのIT経営銘柄|健康経営",
    r"銘柄$",                   # 京都銘柄 等の地域ラベル銘柄群
    # 国・地域名のみのテーマ（その国に関係があるだけで資金テーマではない）
    r"^(中東|ロシア|韓国|ブラジル|アフリカ|台湾|ミャンマー|オーストラリア|"
    r"サウジアラビア|ドバイ|トルコ|マレーシア|モンゴル|イラク|沖縄|京都|"
    r"チャインドネシア|インド|中国|米国|欧州|アメリカ|ベトナム|タイ|"
    r"インドネシア|フィリピン|シンガポール|メキシコ|カナダ|ドイツ|"
    r"フランス|イギリス|北朝鮮|ウクライナ|イスラエル|イラン|エジプト|"
    r"ナイジェリア|南アフリカ|アルゼンチン|チリ|ペルー|ミャンマー)関連$",
    # 為替・金利の方向という括り（銘柄横断の感応度であってテーマではない）
    r"^(円安|円高|ドル高|ドル安|ユーロ高|ユーロ安|金利上昇|金利低下)",
    # 単なる規模・カテゴリラベル
    r"^グローバルニッチ$|^国際優良株$|^インフラ$",
]
_EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS))


def is_excluded_theme(name: str) -> bool:
    """資金テーマでない括りなら True。"""
    return bool(_EXCLUDE_RE.search(str(name or "")))


# --------------------------------------------------------------------------
# テーマタグ表
# --------------------------------------------------------------------------
def load_theme_map(path: Path | str | None = None, max_theme_size: int = MAX_THEME_SIZE):
    """コード -> テーマ名リスト の辞書と、テーマ名 -> 構成銘柄数 を返す。

    構成銘柄数が max_theme_size 以下 かつ 除外パターンに該当しないテーマのみを対象にする。

    Returns:
        (code_to_themes, theme_size, stale_note, excluded_count)
    """
    p = Path(path) if path else THEME_MASTER_PATH
    if not p.exists():
        return {}, {}, f"テーマ表が見つかりません: {p}", 0

    df = pd.read_parquet(p)
    if df.empty:
        return {}, {}, "テーマ表が空です", 0

    sizes = df.groupby("theme_name")["code"].nunique()
    small = sizes[sizes <= max_theme_size]
    keep = [t for t in small.index if not is_excluded_theme(t)]
    excluded_count = len(small) - len(keep)
    target = df[df["theme_name"].isin(keep)]

    code_to_themes: dict[str, list[str]] = defaultdict(list)
    for code, theme in zip(target["code"].astype(str), target["theme_name"]):
        code_to_themes[code].append(theme)

    stale_note = _check_freshness(df)
    return dict(code_to_themes), small[keep].to_dict(), stale_note, excluded_count


def _check_freshness(df: pd.DataFrame) -> str | None:
    """fetched_at が古ければ内部フラグ用の文字列を返す（レポート本文には出さない）。"""
    if "fetched_at" not in df.columns or df.empty:
        return None
    try:
        latest = pd.to_datetime(df["fetched_at"], format="ISO8601", utc=True).max()
    except Exception:
        try:
            latest = pd.to_datetime(df["fetched_at"], utc=True).max()
        except Exception:
            return None
    if pd.isna(latest):
        return None
    age_days = (pd.Timestamp.now(tz="UTC") - latest).days
    if age_days > THEME_MASTER_STALE_DAYS:
        return (
            f"テーマ表が古い可能性があります（最終取得 {latest.tz_convert(JST):%Y-%m-%d}・"
            f"{age_days}日前）。fetch_minkabu_themes.py での更新を推奨します。"
        )
    return None


# --------------------------------------------------------------------------
# 動意上位100銘柄の日次蓄積
# --------------------------------------------------------------------------
def append_movers_history(
    movers_df: pd.DataFrame,
    trade_date,
    path: Path | str | None = None,
) -> Path:
    """動意上位100銘柄を日次 parquet へ追記する（同一日付は上書き＝冪等）。"""
    p = Path(path) if path else MOVERS_HISTORY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)

    date_str = str(trade_date)
    cols = {
        "Code": "code",
        "CompanyName": "name",
        "DailyReturn": "return_pct",
        "Turnover": "turnover",
        "MarketCodeName": "market",
    }
    use = [c for c in cols if c in movers_df.columns]
    new = movers_df[use].rename(columns={k: v for k, v in cols.items() if k in use}).copy()
    new["date"] = date_str
    if "code" in new.columns:
        new["code"] = new["code"].astype(str)

    if p.exists():
        try:
            old = pd.read_parquet(p)
            old = old[old["date"] != date_str]  # 同一日付を捨てて冪等に
            new = pd.concat([old, new], ignore_index=True)
        except Exception:
            pass  # 既存が壊れていても当日分は必ず残す

    new.to_parquet(p, index=False)
    return p


def _load_history(path: Path | str | None):
    """蓄積 parquet を読む（上昇銘柄のみへ絞る）。失敗時は空 DataFrame。"""
    p = Path(path) if path else MOVERS_HISTORY_PATH
    if not Path(p).exists():
        return pd.DataFrame()
    try:
        hist = pd.read_parquet(p)
    except Exception:
        return pd.DataFrame()
    if hist.empty or "date" not in hist.columns or "code" not in hist.columns:
        return pd.DataFrame()
    hist = hist.copy()
    hist["code"] = hist["code"].astype(str)
    hist["date"] = hist["date"].astype(str)
    if "return_pct" in hist.columns:
        ret = pd.to_numeric(hist["return_pct"], errors="coerce")
        hist = hist[ret > 0]  # 資金流入の事実を見るため上昇銘柄のみ
    return hist


# --------------------------------------------------------------------------
# 銘柄コンテキスト（「何の会社」＋材料テキスト）の日次蓄積と遡り参照
# 2026-08-31 PM 指示。当日 raw に記述が無い銘柄でも誌面に「―」を出さないための土台。
# --------------------------------------------------------------------------
# 材料テキストとして蓄積してはいけない定型句（上流の raw に実在する）。
# 「材料不明」等をそのまま貯めると、遡り参照時に中身の無い文字列が「材料あり」として
# 積極支持の判定へ回ってしまうため、蓄積の時点で落とす（_cr §38 の材料支持判定を守る）。
_NON_MATERIAL_RE = re.compile(
    r"材料不明|理由不明|材料なし|特に材料|不明です|該当なし|情報なし"
)


def append_stock_context(
    records: list[dict],
    trade_date,
    path: Path | str | None = None,
) -> Path:
    """当日取得できた「何の会社」「材料テキスト」を日次 parquet へ追記する（同一日付は上書き）。

    Args:
        records: [{"code": "593A", "name": "...", "desc": "事業概要原文",
                   "materials": ["TDNet ...", "ニュース ..."]}] のリスト。
            desc・materials とも一次情報（EDINET DB 事業概要／raw の開示・ニュース見出し）
            のみを渡すこと。機械はここで新規生成も推測もしない。

    Returns:
        書き出した parquet のパス。
    """
    p = Path(path) if path else STOCK_CONTEXT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    date_str = str(trade_date)

    rows = []
    for r in records or []:
        code = str(r.get("code") or "").strip()
        if not code:
            continue
        desc = re.sub(r"\s+", " ", str(r.get("desc") or "")).strip()
        mats = [
            str(m).strip() for m in (r.get("materials") or [])
            if str(m).strip() and not _NON_MATERIAL_RE.search(str(m))
        ]
        if not desc and not mats:
            continue  # 何も無い行は貯めない（parquet を膨らませない）
        rows.append({
            "date": date_str,
            "code": code,
            "name": str(r.get("name") or "").strip(),
            "desc": desc,
            "materials": "\n".join(mats[:5]),
        })
    new = pd.DataFrame(rows, columns=["date", "code", "name", "desc", "materials"])

    if p.exists():
        try:
            old = pd.read_parquet(p)
            old = old[old["date"].astype(str) != date_str]
            new = pd.concat([old, new], ignore_index=True)
        except Exception:
            pass
    new.to_parquet(p, index=False)
    return p


def _load_stock_context(path: Path | str | None = None) -> pd.DataFrame:
    """蓄積済みの銘柄コンテキストを読む（失敗時は空 DataFrame）。"""
    p = Path(path) if path else STOCK_CONTEXT_PATH
    if not Path(p).exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(p)
    except Exception:
        return pd.DataFrame()
    if df.empty or "code" not in df.columns or "date" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["code"] = df["code"].astype(str)
    df["date"] = df["date"].astype(str)
    return df


def _load_sector_names(path: Path | str | None = None) -> dict:
    """screening_master から `code -> 業種名（33業種）` を返す（読み取りのみ）。

    「何の会社」の**最終フォールバック**。業種名は事業内容そのものではないため、誌面では
    これを素材に Claude が15字前後へ書く（_cr §38）。読み取り専用であり列追加はしない。
    """
    p = Path(path) if path else SCREENING_MASTER_PATH
    if not Path(p).exists():
        return {}
    try:
        df = pd.read_parquet(p, columns=["Code", "Sector33CodeName", "Sector17CodeName"])
    except Exception:
        try:
            df = pd.read_parquet(p)
        except Exception:
            return {}
    if df.empty or "Code" not in df.columns:
        return {}
    out = {}
    for _, r in df.iterrows():
        code = str(r.get("Code") or "").strip()
        if not code:
            continue
        name = str(r.get("Sector33CodeName") or "").strip()
        if not name or name.lower() == "nan":
            name = str(r.get("Sector17CodeName") or "").strip()
        if name and name.lower() != "nan":
            out[code] = name
            if len(code) > 4:
                out.setdefault(code[:4], name)
    return out


def build_desc_lookup(
    primary=None,
    trade_date=None,
    context_path: Path | str | None = None,
    screening_path: Path | str | None = None,
    lookback_days: int = CONTEXT_LOOKBACK_DAYS,
):
    """「何の会社」の素材を返す callable を、フォールバック連鎖付きで組み立てて返す。

    連鎖（上から順に、最初に取れたものを返す。すべて一次情報のみ）:
      1. primary        当日 raw の EDINET 事業概要（呼び出し側が供給）
      2. 蓄積コンテキスト  直近 lookback_days 営業日以内に取得済みの事業概要（同一銘柄）
      3. 業種名          screening_master の33業種名（`業種: {名前}` 形式で返す）

    3 まで落ちた場合も**空文字は返さない**ため、誌面の「何の会社」列が構造的に空欄
    （―）になることが無くなる。3 は事業内容そのものではないので、誌面を書く Claude は
    これを素材に材料テキストと合わせて15字前後へ言い換える（_cr §38）。

    Returns:
        code -> str（取れなければ空文字）の callable。
    """
    ctx = _load_stock_context(context_path)
    sectors = _load_sector_names(screening_path)

    # 蓄積側は「対象日以前・lookback 日以内」の最新行だけを引く辞書へ畳む
    ctx_desc = {}
    if not ctx.empty and "desc" in ctx.columns:
        sub = ctx[ctx["desc"].astype(str).str.strip() != ""]
        if trade_date is not None:
            dates = sorted({d for d in sub["date"].unique() if d <= str(trade_date)})
            keep = set(dates[-lookback_days:])
            sub = sub[sub["date"].isin(keep)]
        sub = sub.sort_values("date")
        for code, desc in zip(sub["code"], sub["desc"]):
            ctx_desc[str(code)] = str(desc)  # 後勝ち＝最新日

    def _lookup(code) -> str:
        c = str(code or "").strip()
        if not c:
            return ""
        if primary is not None:
            try:
                v = str(primary(c) or "").strip()
            except Exception:
                v = ""
            if v:
                return v
        v = ctx_desc.get(c) or ctx_desc.get(c[:4], "")
        if v:
            return v
        sec = sectors.get(c) or sectors.get(c[:4], "")
        if sec:
            # 半角中黒（情報･通信業）を全角へ揃えて誌面での表記ゆれを防ぐ。
            return "業種: " + sec.replace("･", "・").replace("·", "・")
        return ""

    return _lookup


def build_material_lookup(
    primary=None,
    trade_date=None,
    context_path: Path | str | None = None,
    lookback_days: int = CONTEXT_LOOKBACK_DAYS,
):
    """材料テキストを返す callable を、遡り収集付きで組み立てて返す（2026-08-31 PM 指示）。

    当日 raw に「なぜ動いた」記述がある銘柄は当日分をそのまま返す。当日分が無い銘柄は、
    直近 lookback_days 営業日以内でその銘柄が動意 raw に載った**最新日**の材料を
    `{M/D}時点の材料: ...` の形で返す。

    熱量テーマの主導銘柄の約半数が当日の値上がり Top10 圏外で当日 raw に材料ブロックを
    持たず、_cr §38 の「材料不明は行ごと外す」規約により一律除外されて誌面が激減していた
    （8/28 実測で5テーマ→1テーマ）。遡り材料を与えることで積極支持の判定を機能させる。
    日付を必ず前置し、誌面の理由文へ使う場合も日付を明示させる（_cr §38）。

    Returns:
        code -> list[str] の callable。
    """
    ctx = _load_stock_context(context_path)

    # code -> (date, [materials]) の最新1件
    ctx_mat = {}
    if not ctx.empty and "materials" in ctx.columns:
        sub = ctx[ctx["materials"].astype(str).str.strip() != ""]
        if trade_date is not None:
            dates = sorted({d for d in sub["date"].unique() if d < str(trade_date)})
            keep = set(dates[-lookback_days:])
            sub = sub[sub["date"].isin(keep)]
        sub = sub.sort_values("date")
        for code, date, mats in zip(sub["code"], sub["date"], sub["materials"]):
            items = [m.strip() for m in str(mats).split("\n") if m.strip()]
            if items:
                ctx_mat[str(code)] = (str(date), items)  # 後勝ち＝最新日

    def _lookup(code) -> list:
        c = str(code or "").strip()
        if not c:
            return []
        if primary is not None:
            try:
                items = [str(m).strip() for m in (primary(c) or []) if str(m).strip()]
            except Exception:
                items = []
            if items:
                return items
        hit = ctx_mat.get(c) or ctx_mat.get(c[:4])
        if not hit:
            return []
        date, items = hit
        label = _short_date(date)
        return [f"{label}時点の材料: {it}" for it in items[:3]]

    return _lookup


def _short_date(date_str: str) -> str:
    """`2026-08-25` -> `8/25`（変換できなければ元の文字列）。"""
    try:
        y, m, d = str(date_str)[:10].split("-")
        return f"{int(m)}/{int(d)}"
    except Exception:
        return str(date_str)


# --------------------------------------------------------------------------
# スコアリング
# --------------------------------------------------------------------------
def stock_weight(turnover, return_pct) -> float:
    """銘柄1件の資金量スコア w(s)。

    w(s) = log10(1 + 売買代金[億円]) × 騰落率の正の部分
    売買代金は log 圧縮して大型株1銘柄が全体を支配するのを防ぐ。
    騰落率が0以下の銘柄は寄与0（資金流入と見なさない）。
    """
    try:
        ret = float(return_pct or 0)
    except (TypeError, ValueError):
        return 0.0
    if ret <= 0:
        return 0.0
    try:
        oku = float(turnover or 0) / 1e8
    except (TypeError, ValueError):
        oku = 0.0
    if oku < 0:
        oku = 0.0
    return math.log10(1.0 + oku) * ret


def score_one_day(records, code_to_themes: dict) -> dict:
    """1営業日分の銘柄リストからテーマ別スコアと点灯銘柄を返す。

    銘柄 s のテーマ t への寄与 = w(s) / n_themes(s)
    n_themes(s) = その銘柄が所属する（除外後の）テーマ数。

    Args:
        records: [{"code","name","return_pct","turnover","market"}, ...]

    Returns:
        {theme: {"score": float, "codes": [rec, ...]}}
    """
    out: dict[str, dict] = defaultdict(lambda: {"score": 0.0, "codes": []})
    for rec in records:
        code = str(rec.get("code") or "")
        if not code:
            continue
        themes = code_to_themes.get(code, [])
        if not themes:
            continue
        w = stock_weight(rec.get("turnover"), rec.get("return_pct"))
        if w <= 0:
            continue
        share = w / len(themes)  # 所属テーマ数で按分
        for t in themes:
            out[t]["score"] += share
            out[t]["codes"].append(rec)
    return dict(out)


# --------------------------------------------------------------------------
# テーマ統合（重複除去）
# --------------------------------------------------------------------------
def merge_overlapping_themes(entries: list[dict], theme_size: dict) -> list[dict]:
    """点灯銘柄集合が重複するテーマ群を1行へ統合する。

    統合条件: Jaccard(A, B) >= MERGE_JACCARD または 片方が他方を包含。
    代表名: 構成銘柄数（theme_size）が最小＝最も具体的なテーマ名。
    統合された他テーマ名は merged_names に入れ、誌面では括弧内へ併記する。

    Args:
        entries: [{"theme","score","codes"(list[rec])}, ...]
    Returns:
        統合済み entries（"theme"=代表名 / "merged_names"=併記名 / "score"=最大値 /
        "codes"=和集合）
    """
    if not entries:
        return []

    items = []
    for e in entries:
        codes = {str(c.get("code")) for c in e["codes"]}
        items.append({**e, "_set": codes})

    # 代表を1つ選び、その代表と直接似ているテーマだけを吸収する（貪欲法）。
    # Union-Find による連結成分だと A⊂B・B⊂C の連鎖で A と C（無関係なテーマ同士）まで
    # 1行に潰れるため採用しない。代表との直接比較のみで判定する。
    def _similar(a: set, b: set) -> bool:
        if not a or not b:
            return False
        inter = len(a & b)
        # 重複が1銘柄だけの包含（例: 総合商社と天然ガスに同じ商社株が1つ入っている）は
        # 統合しない。多テーマ所属の大型株1銘柄を介して無関係なテーマが1行に潰れるため。
        if inter < MERGE_MIN_OVERLAP:
            return False
        union_n = len(a | b)
        jac = inter / union_n if union_n else 0.0
        contained = (inter == len(a)) or (inter == len(b))
        return jac >= MERGE_JACCARD or contained

    # 代表の選定順: 銘柄集合が大きい -> スコアが高い順に代表を立てる
    order = sorted(
        range(len(items)),
        key=lambda i: (-len(items[i]["_set"]), -items[i]["score"], items[i]["theme"]),
    )
    used: set[int] = set()
    groups_idx: list[list[int]] = []
    for i in order:
        if i in used:
            continue
        grp = [i]
        used.add(i)
        for j in order:
            if j in used:
                continue
            if _similar(items[i]["_set"], items[j]["_set"]):
                grp.append(j)
                used.add(j)
        groups_idx.append(grp)

    merged = []
    for members in groups_idx:
        grp = [items[i] for i in members]
        # 代表名 = そのグループで最も資金が入っているテーマ。同スコアなら構成銘柄数が
        # 小さい（より具体的な）方 -> 名前昇順。
        # 「構成銘柄数が最小＝最も具体的」だけで選ぶと、群の実体と無関係な極小テーマ
        # （例: MaaS/自動運転車の群を「養殖マグロ」と名付ける）が代表になるため、
        # まずスコアで実体を掴んでから具体性で割る。
        rep = sorted(
            grp,
            key=lambda g: (-g["score"], theme_size.get(g["theme"], 10**6), g["theme"]),
        )[0]
        others = [g["theme"] for g in grp if g["theme"] != rep["theme"]]
        others = sorted(set(others), key=lambda t: (theme_size.get(t, 10**6), t))
        # 銘柄は和集合（コード重複を除く）
        seen: dict[str, dict] = {}
        for g in grp:
            for c in g["codes"]:
                seen.setdefault(str(c.get("code")), c)
        merged.append(
            {
                "theme": rep["theme"],
                "merged_names": others,
                "theme_size": int(theme_size.get(rep["theme"], 0)),
                "score": max(g["score"] for g in grp),
                "codes": list(seen.values()),
                # 統合前の構成テーマそれぞれの点灯銘柄数。初動候補テーマ（v16）の n_up は
                # 和集合の件数ではなくこの最大値を使う（1銘柄ずつ点灯した無関係なテーマが
                # 統合されただけの行が件数で通ってしまうのを防ぐ）。
                "member_counts": [len(g["_set"]) for g in grp],
            }
        )
    return merged


def merge_by_display_overlap(
    rows: list[dict],
    display_codes,
    theme_size: dict | None = None,
    score_key: str = "score",
    ratio: float = DISPLAY_MERGE_RATIO,
    min_overlap: int = DISPLAY_MERGE_MIN_OVERLAP,
) -> list[dict]:
    """**誌面に実際に出る銘柄集合**が重なるテーマ行を1行へ統合する（2026-09-04 PM 指示）。

    merge_overlapping_themes は「点灯銘柄集合」で統合するため、点灯集合では別物でも
    誌面に載る銘柄（初動候補欄=+3%以上の点灯銘柄／2週間欄・本日=表示する点灯銘柄）が
    ほぼ同じテーマが複数枠を占めることがあった（9/3 の 自動運転車 / フィジカルAI /
    MaaS がいずれも 593A・336A・4667 の重複）。本関数は表示対象集合で見て
    **小さい方の集合の ratio 以上（既定 50%）が重なる**場合も統合する。

    統合の仕様（PM 指示）:
        名前   … 上位テーマ名＋「（吸収したテーマ名を含む）」（format_theme_label が付与）
        銘柄   … 和集合
        指標   … スコア・n_up・turn3 等は**上位側（残る行）の値**をそのまま引き継ぐ。
                 ただし銘柄を数え直す指標（turn3_oku 等）は呼び出し側が和集合から再集計する。

    Args:
        rows: 統合対象の行（"theme" / "merged_names" / score_key / "codes" を持つ dict）。
            入力順が優先順（先頭ほど上位）である必要はなく、score_key 降順で上位を決める。
        display_codes: row -> 誌面に出る銘柄コードの集合（または iterable）を返す callable。
        theme_size: theme -> 構成銘柄数（併記名の並び順にだけ使う）。
    Returns:
        統合済みの行リスト（入力の dict をコピーし "merged_names" と "codes" を更新）。
        並びは score_key 降順（入力の相対順は保たない）。
    """
    if not rows:
        return []
    theme_size = theme_size or {}

    def _score(r: dict) -> float:
        try:
            return float(r.get(score_key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    items = []
    for r in rows:
        try:
            disp = {str(c) for c in (display_codes(r) or []) if str(c).strip()}
        except Exception:
            disp = set()
        items.append({"row": r, "_disp": disp, "_key": set(disp)})

    # 多テーマに顔を出す銘柄（総合商社・電力・大手半導体など、辞書上ほぼ全テーマへ
    # ぶら下がる大型株）を除いた「そのテーマを特徴づける銘柄」の集合 `_key` を作る。
    # 重なりの割合判定は誌面に出る生の集合 `_disp` で行い（PM が誌面で見る重複は
    # 大型株の並びそのものなので、ここから大型株を抜くと重複が検知できない）、
    # `_key` は「特徴銘柄を1つも共有しない＝別テーマ」を弾く**拒否条件**にだけ使う。
    # これを入れないと 8058・9503 等を共有するだけで無関係なテーマが1行へ潰れる。
    if len(items) >= 3:
        from collections import Counter

        cnt: Counter = Counter()
        for it in items:
            cnt.update(it["_disp"])
        limit = max(2, int(len(items) * DISPLAY_PROMISCUOUS_RATIO))
        promiscuous = {c for c, n in cnt.items() if n >= limit}
        if promiscuous:
            for it in items:
                it["_key"] = it["_disp"] - promiscuous

    # 統合はスコア上位を代表とする**貪欲法**で行い、Union-Find（連結成分）は採らない。
    # 連結成分だと A⊂B・B⊂C の連鎖で無関係なテーマ（自動運転車とショッピングセンター）
    # まで1行へ潰れる（9/3 実測で全候補が1行に収束した）。代表と**直接**過半が重なる
    # 行だけを吸収し、吸収のたび代表の判定集合を和集合へ広げる（同じクラスタを2行が
    # 別々に吸収して結果的に重複するのを防ぐ）。
    order = sorted(
        range(len(items)),
        key=lambda i: (-_score(items[i]["row"]), str(items[i]["row"].get("theme") or "")),
    )

    def _hit(a: set, b: set) -> bool:
        """表示対象集合が「実質同じ銘柄群」か。

        小さい方の過半（ratio）が重なることに加え、**大きい方の側も
        DISPLAY_MERGE_OUTER_RATIO 以上**が重なることを要求する。小さい方だけを見ると、
        構成2〜3銘柄の小テーマが大テーマの部分集合になっているだけで統合され、
        代表テーマが無関係な銘柄まで飲み込む（9/3 実測で自動運転車が
        ショッピングセンター・銀行まで吸収した）。
        """
        if not a or not b:
            return False
        inter = len(a & b)
        if inter < int(min_overlap):
            return False
        smaller, larger = min(len(a), len(b)), max(len(a), len(b))
        if not smaller or not larger:
            return False
        if (inter / smaller) < float(ratio):
            return False
        # (i) 双方向の重なり: 両側とも一定割合が重なる＝実質同じ銘柄群。
        if (inter / larger) >= DISPLAY_MERGE_OUTER_RATIO:
            return True
        # (ii) 包含: 小さい方が大きい方へほぼ収まっている（大テーマの部分集合として
        #      別枠を占めているだけの行）。9/3 の 地図情報システム（593A・336A・4425 が
        #      いずれも自動運転車の主導銘柄）がこれに当たる。片側の割合だけで通すと
        #      構成2〜3銘柄の小テーマが無差別に吸収されるため、包含率を
        #      DISPLAY_MERGE_CONTAIN_RATIO まで引き上げて要求する。
        return (inter / smaller) >= DISPLAY_MERGE_CONTAIN_RATIO

    used: set[int] = set()
    out: list[dict] = []
    for i in order:
        if i in used:
            continue
        used.add(i)
        rep = items[i]
        absorbed: list[int] = []
        # 代表の判定集合は**固定**する（吸収して広げると芋づるに全テーマを飲み込む）。
        for j in order:
            if j in used:
                continue
            if _hit(rep["_disp"], items[j]["_disp"]) and (
                rep["_key"] & items[j]["_key"]
            ):
                absorbed.append(j)
                used.add(j)
        new = dict(rep["row"])
        if absorbed:
            names = list(new.get("merged_names") or [])
            seen_codes: dict[str, dict] = {
                str(c.get("code")): c for c in (new.get("codes") or [])
            }
            for j in absorbed:
                other = items[j]["row"]
                names.append(str(other.get("theme") or ""))
                names += [str(n) for n in (other.get("merged_names") or [])]
                for c in (other.get("codes") or []):
                    seen_codes.setdefault(str(c.get("code")), c)
            names = [n for n in names if n and n != new.get("theme")]
            new["merged_names"] = sorted(
                set(names), key=lambda t: (theme_size.get(t, 10**6), t)
            )
            new["codes"] = list(seen_codes.values())
            # member_counts は「単一テーマとして何社同時に動いたか」の最大値であり、
            # 吸収した側の内訳も候補に含める（和集合件数へは膨らませない）。
            mc = list(new.get("member_counts") or [])
            for j in absorbed:
                mc += list(items[j]["row"].get("member_counts") or [])
            if mc:
                new["member_counts"] = mc
            # 熱量行が持つ「当日点灯銘柄」も和集合にする（欄の見出し・局面判定の材料）。
            if "today_codes" in new:
                seen_today: dict[str, dict] = {
                    str(c.get("code")): c for c in (new.get("today_codes") or [])
                }
                for j in absorbed:
                    for c in (items[j]["row"].get("today_codes") or []):
                        seen_today.setdefault(str(c.get("code")), c)
                new["today_codes"] = list(seen_today.values())
        out.append(new)
    out.sort(key=lambda r: (-_score(r), str(r.get("theme") or "")))
    return out


def format_theme_label(entry: dict) -> str:
    """代表名（統合した他テーマ名を括弧内へ併記）。"""
    names = entry.get("merged_names") or []
    if not names:
        return entry["theme"]
    shown = names[:2]
    tail = "ほか" if len(names) > len(shown) else ""
    return f"{entry['theme']}（{'・'.join(shown)}{tail}を含む）"


# --------------------------------------------------------------------------
# 当日のテーマ
# --------------------------------------------------------------------------
def detect_today(
    codes_today,
    theme_master_path: Path | str | None = None,
    min_codes: int = MIN_CODES_FOR_ALERT,
):
    """当日の動意上位（上昇銘柄）から「本日のテーマ」を算出する。

    Returns:
        {"rows": [...], "stale_note": str|None, "excluded_count": int}
        rows の各要素: theme / merged_names / theme_size / score / codes（売買代金降順）
    """
    code_to_themes, theme_size, stale_note, excluded = load_theme_map(theme_master_path)
    if not code_to_themes:
        return {"rows": [], "stale_note": stale_note, "excluded_count": excluded}

    recs = []
    for r in codes_today:
        if not r.get("code"):
            continue
        try:
            if float(r.get("return_pct") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        recs.append(r)

    per_theme = score_one_day(recs, code_to_themes)
    entries = [
        {"theme": t, "score": v["score"], "codes": v["codes"]}
        for t, v in per_theme.items()
        if len({str(c.get("code")) for c in v["codes"]}) >= min_codes
    ]
    rows = merge_overlapping_themes(entries, theme_size)
    # 表示ベースの二次統合（2026-09-04 PM 指示）。本日のテーマ欄が誌面へ出す銘柄は
    # 点灯銘柄そのものなので、点灯集合の過半が重なる別名テーマを1行へ畳む。
    rows = merge_by_display_overlap(
        rows,
        lambda r: {str(c.get("code")) for c in (r.get("codes") or [])},
        theme_size=theme_size,
    )
    # 2026-09-04 PM 指示: 誌面へ出す銘柄を代表テーマ自身の構成銘柄に限定し、
    # 上位プールの汎用銘柄（商社・電力等）を外す。
    _generic_today = generic_codes(rows, code_to_themes)
    for r in rows:
        _keep = _self_codes(r, code_to_themes) - _generic_today
        _filtered = [c for c in (r.get("codes") or []) if str(c.get("code")) in _keep]
        if _filtered:
            r["codes"] = _filtered
    for r in rows:
        r["codes"] = sorted(
            r["codes"], key=lambda c: -float(c.get("turnover") or 0)
        )
    # 点灯条件（MIN_CODES_FOR_ALERT）は絞り込み後の銘柄数で判定し直す
    # （汎用銘柄だけで成立していた行を誌面へ出さない）。
    rows = [r for r in rows if len({str(c.get("code")) for c in r["codes"]}) >= min_codes]
    rows.sort(key=lambda r: (-r["score"], r["theme"]))
    return {"rows": rows, "stale_note": stale_note, "excluded_count": excluded}


# --------------------------------------------------------------------------
# 熱量（直近10営業日）
# --------------------------------------------------------------------------
def _phase(heat: float, prev_heat: float, lit_today: bool) -> str:
    """局面の機械判定。

    新規: 前10営業日のスコアが 0（この2週間で初めて資金が入った）
    加速: Δ が前10営業日比 +50% 以上
    継続: それ以外で当日点灯している
    減衰: Δ がマイナス かつ 当日点灯なし（誌面には出さない）
    """
    if prev_heat <= 0:
        return "新規"
    delta = heat - prev_heat
    if delta / prev_heat >= ACCEL_DELTA_RATIO:
        return "加速"
    if delta < 0 and not lit_today:
        return "減衰"
    return "継続"


_PHASE_ORDER = {"新規": 0, "加速": 1, "継続": 2, "減衰": 3}


def compute_theme_heat(
    codes_today=None,
    history_parquet: Path | str | None = None,
    trade_date=None,
    theme_master_path: Path | str | None = None,
    window: int = HEAT_WINDOW_DAYS,
    min_codes: int = MIN_CODES_FOR_ALERT,
):
    """直近 window 営業日のテーマ熱量スコアと局面を算出する。

    熱量スコア = 各営業日の当日スコアの合計（点灯条件は日ごとに適用しない。
    その日そのテーマへ流れた資金量をそのまま足す）。
    Δ = 直近 window 営業日の合計 − その前 window 営業日の合計。

    Returns:
        {"rows": [...], "window_used": int, "prev_window_used": int,
         "stale_note": str|None, "excluded_count": int}
    """
    code_to_themes, theme_size, stale_note, excluded = load_theme_map(theme_master_path)
    if not code_to_themes:
        return {"rows": [], "window_used": 0, "prev_window_used": 0,
                "stale_note": stale_note, "excluded_count": excluded}

    hist = _load_history(history_parquet)
    end = str(trade_date) if trade_date else datetime.now(JST).date().isoformat()

    # 当日分が蓄積前でも動くよう、渡された当日銘柄を履歴へ重ねる
    if codes_today:
        rows_today = []
        for r in codes_today:
            if not r.get("code"):
                continue
            try:
                if float(r.get("return_pct") or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            rows_today.append(
                {
                    "code": str(r.get("code")),
                    "name": r.get("name") or "",
                    "return_pct": r.get("return_pct"),
                    "turnover": r.get("turnover"),
                    "market": r.get("market"),
                    "date": end,
                }
            )
        if rows_today:
            if not hist.empty:
                hist = hist[hist["date"] != end]
            hist = pd.concat([hist, pd.DataFrame(rows_today)], ignore_index=True)

    if hist.empty:
        return {"rows": [], "window_used": 0, "prev_window_used": 0,
                "stale_note": stale_note, "excluded_count": excluded}

    dates = sorted(d for d in hist["date"].unique() if d <= end)
    if not dates:
        return {"rows": [], "window_used": 0, "prev_window_used": 0,
                "stale_note": stale_note, "excluded_count": excluded}

    cur_dates = dates[-window:]
    prev_dates = dates[-(window * 2): -window] if len(dates) > window else []

    def _sum_scores(target_dates):
        agg: dict[str, float] = defaultdict(float)
        for d in target_dates:
            day = hist[hist["date"] == d]
            recs = day.to_dict("records")
            for t, v in score_one_day(recs, code_to_themes).items():
                agg[t] += v["score"]
        return agg

    cur = _sum_scores(cur_dates)
    prev = _sum_scores(prev_dates) if prev_dates else {}

    # 当日点灯銘柄（min_codes 以上）
    today_recs = hist[hist["date"] == end].drop_duplicates(subset=["code"]).to_dict("records")
    today_per_theme = score_one_day(today_recs, code_to_themes)

    entries = []
    for theme, heat in cur.items():
        tt = today_per_theme.get(theme, {"codes": []})
        entries.append(
            {
                "theme": theme,
                "score": float(heat),
                "prev_score": float(prev.get(theme, 0.0)),
                "codes": tt["codes"],
            }
        )

    # 統合の判定は「そのテーマを構成する銘柄のうち、窓内で実際に資金が入った銘柄」の集合で行う。
    # 窓内の全掲載銘柄をそのままテーマへ展開すると、10営業日分の銘柄が積み上がって
    # ほぼ全テーマの集合が肥大し、無関係なテーマ同士（総合商社と自動運転車など）まで
    # 包含判定で1行に潰れる。テーマごとに寄与の大きい上位銘柄へ絞って比較する。
    MERGE_TOP_CODES = 8
    theme_contrib: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    sub = hist[hist["date"].isin(cur_dates)]
    for rec in sub.to_dict("records"):
        code = str(rec.get("code") or "")
        themes = code_to_themes.get(code, [])
        if not themes:
            continue
        w = stock_weight(rec.get("turnover"), rec.get("return_pct"))
        if w <= 0:
            continue
        share = w / len(themes)
        for t in themes:
            theme_contrib[t][code] += share

    def _top_codes(theme: str) -> list[str]:
        contrib = theme_contrib.get(theme, {})
        return [
            c for c, _ in sorted(contrib.items(), key=lambda kv: -kv[1])[:MERGE_TOP_CODES]
        ]

    # 窓内（直近 window 営業日）にそのテーマで実際に資金が入った銘柄の「累計売買代金・
    # 最新の騰落率・社名」を集める。主導銘柄プールの原資（2026-08-31 PM 指示）。
    # 旧実装は熱量行の codes を「当日点灯銘柄」だけにしていたため、テーマの主要銘柄が
    # 当日 Top100 圏外だとプールに入らず、誌面が2銘柄まで痩せた
    # （PM 指摘「フィジカルAIがこんな2銘柄なわけない」）。
    theme_pool: dict[str, dict[str, dict]] = defaultdict(dict)
    for rec in sub.sort_values("date").to_dict("records"):
        code = str(rec.get("code") or "")
        themes = code_to_themes.get(code, [])
        if not themes:
            continue
        if stock_weight(rec.get("turnover"), rec.get("return_pct")) <= 0:
            continue
        try:
            tv = float(rec.get("turnover") or 0)
        except (TypeError, ValueError):
            tv = 0.0
        for th in themes:
            slot = theme_pool[th].setdefault(
                code,
                {"code": code, "name": rec.get("name") or "", "turnover": 0.0,
                 "return_pct": rec.get("return_pct"), "market": rec.get("market"),
                 "last_date": rec.get("date")},
            )
            slot["turnover"] += tv          # 窓内の累計売買代金（プールの並び順）
            slot["return_pct"] = rec.get("return_pct")   # 後勝ち＝窓内で最も新しい日
            slot["last_date"] = rec.get("date")
            if rec.get("name"):
                slot["name"] = rec.get("name")

    merge_input = [
        {"theme": e["theme"], "score": e["score"],
         "codes": [{"code": c} for c in _top_codes(e["theme"])]}
        for e in entries
    ]
    merged = merge_overlapping_themes(merge_input, theme_size)

    by_theme = {e["theme"]: e for e in entries}
    rows = []
    for m in merged:
        group = [m["theme"]] + list(m.get("merged_names") or [])
        heat = max(by_theme[t]["score"] for t in group if t in by_theme)
        prev_h = max(by_theme[t]["prev_score"] for t in group if t in by_theme)
        # 当日点灯銘柄は代表テーマ群の和集合
        seen: dict[str, dict] = {}
        for t in group:
            for c in by_theme.get(t, {}).get("codes", []):
                seen.setdefault(str(c.get("code")), c)
        today_codes = sorted(seen.values(), key=lambda c: -float(c.get("turnover") or 0))
        lit_today = len(today_codes) >= min_codes
        # 主導銘柄プール = 当日点灯銘柄 ＋ 窓内で資金が入った同テーマ群の銘柄。
        # 当日点灯銘柄を先頭に置き（当日の騰落率がそのまま使える）、残りを窓内の
        # 累計売買代金降順で続ける。誌面へ載せるかは Claude の積極支持判定に委ねる
        # ため、機械はここで取捨選択をしない（§25 の銘柄除外禁止）。
        pool: dict[str, dict] = {str(c.get("code")): c for c in today_codes}
        extra: list[dict] = []
        for t in group:
            for code, slot in theme_pool.get(t, {}).items():
                if code in pool:
                    continue
                if not any(e["code"] == code for e in extra):
                    extra.append(slot)
        extra.sort(key=lambda c: -float(c.get("turnover") or 0))
        lead_pool = today_codes + extra
        rows.append(
            {
                "theme": m["theme"],
                "merged_names": m.get("merged_names") or [],
                "theme_size": m["theme_size"],
                "heat": heat,
                "prev_heat": prev_h,
                "delta": heat - prev_h,
                "phase": _phase(heat, prev_h, lit_today),
                "codes": lead_pool,
                "today_codes": today_codes,
                "today_count": len(today_codes),
            }
        )

    # 減衰は誌面に出さない
    rows = [r for r in rows if r["phase"] != "減衰"]
    # 表示ベースの二次統合（2026-09-04 PM 指示）。この欄が誌面へ出すのは主導銘柄表の
    # 上位 LEAD_CANDIDATES 件なので、その表示対象で過半が重なる別名テーマを1行へ畳む。
    # 統合はプール切り詰め（HEAT_CANDIDATE_POOL）の**前**に行い、空いた枠へ次点の
    # 非重複テーマが繰り上がるようにする。
    # 判定に使う集合は**誌面と同じ並び・同じ件数**でなければならない。lead_pool は
    # 「当日点灯銘柄→窓内の累計代金順」の合成順であり、誌面表と並びが違うため、
    # 統合の前に誌面と同じ累計売買代金降順へ揃えてから上位 LEAD_CANDIDATES 件を採る。
    for r in rows:
        r["codes"] = sorted(
            r["codes"], key=lambda c: -float(c.get("turnover") or 0)
        )
    # 判定集合は「当日点灯銘柄 ∪ 主導銘柄表の上位 LEAD_CANDIDATES 件」。当日点灯銘柄を
    # 必ず含めるのは、主導銘柄の並びが窓内の累計売買代金順であり、当日いちばん派手に
    # 動いた小型株（9/3 の 336A・4425）が大型株に押し出されて上位12件へ入らないため。
    # PM が誌面で見る重複はまさにその当日の主役銘柄の重なりであり、そこを判定へ入れないと
    # 「同じ銘柄群の別名テーマ」（自動運転車と地図情報システム）を検知できない。
    rows = merge_by_display_overlap(
        rows,
        lambda r: {
            str(c.get("code")) for c in (r.get("today_codes") or [])
        }
        | {str(c.get("code")) for c in (r.get("codes") or [])[:LEAD_CANDIDATES]},
        theme_size=theme_size,
        score_key="heat",
    )
    # 2026-09-04 PM 指示: 誌面へ出す銘柄を「代表テーマ自身の辞書構成銘柄」に限定し、
    # 上位プールで GENERIC_CODE_MIN_THEMES 以上のテーマに現れる汎用銘柄を外す。
    # 統合の和集合をそのまま出すと、吸収した別名テーマにしか属さない銘柄が代表テーマの
    # 行に並ぶ（9/3 の 自動運転車 に レーザーテック(6920)）。
    _generic_heat = generic_codes(rows, code_to_themes)
    for r in rows:
        _keep = _self_codes(r, code_to_themes) - _generic_heat
        _filtered = [c for c in (r.get("codes") or []) if str(c.get("code")) in _keep]
        # 全滅する行（辞書が引けない等）は従来どおり和集合を残す（配信絶対の原則）。
        if _filtered:
            r["codes"] = _filtered
        r["today_codes"] = [
            c for c in (r.get("today_codes") or []) if str(c.get("code")) in _keep
        ] or r.get("today_codes") or []
    # 統合で codes が和集合になったため、表示順と当日点灯銘柄を数え直す。
    for r in rows:
        r["codes"] = sorted(
            r["codes"], key=lambda c: -float(c.get("turnover") or 0)
        )
        _today_set = {str(c.get("code")) for c in (r.get("today_codes") or [])}
        r["today_count"] = len(_today_set)
    # 並び順は熱量降順を基本とする（2026-08-31 PM 確定）。
    # 旧実装は上位 HEAT_CANDIDATE_POOL 件を局面順（新規 -> 加速 -> 継続）へ並べ替えていたが、
    # そのため熱量上位でも「継続」局面のテーマが加速テーマに押し出されて誌面から落ちた
    # （2026-08-28 の自動運転車＝当日1位テーマが直近2週間の表に載らない事象）。
    # 局面はあくまで表示上の属性であり、並び順の主軸は熱量に戻す。
    rows.sort(key=lambda r: (-r["heat"], r["theme"]))
    rows = rows[:HEAT_CANDIDATE_POOL]
    return {
        "rows": rows,
        "window_used": len(cur_dates),
        "prev_window_used": len(prev_dates),
        "stale_note": stale_note,
        "excluded_count": excluded,
    }


def compute_theme_heat_v2(
    codes_today=None,
    history_parquet: Path | str | None = None,
    trade_date=None,
    theme_master_path: Path | str | None = None,
    window: int = HEAT_WINDOW_DAYS,
    min_codes: int = MIN_CODES_FOR_ALERT,
):
    """v11 の熱量指標（継続性ベース）。compute_theme_heat の結果へ列を足して並べ替える。

    追加列:
        lit_days      … **当日を除く**直近 window 営業日のうち、そのテーマが
                        SUSTAIN_MIN_CODES 銘柄以上で点灯した日数（0〜window）
        lit_window    … 判定に使った営業日数（当日を除く。誌面の「10日中N日点灯」の分母）
        sustain       … lit_days × その点灯日の平均スコア（並び順の主軸）
        heat          … 旧指標（累計スコア）。誌面の「熱量」表示と互換のため維持
    並び順は sustain 降順（同値は heat 降順）。当日掲載済みテーマの降格は
    select_heat_rows_v2 側で行う（この関数は素の継続性順を返す）。
    """
    base = compute_theme_heat(
        codes_today=codes_today,
        history_parquet=history_parquet,
        trade_date=trade_date,
        theme_master_path=theme_master_path,
        window=window,
        min_codes=min_codes,
    )
    rows = base.get("rows") or []
    if not rows:
        return base

    code_to_themes, _theme_size, _sn, _ex = load_theme_map(theme_master_path)
    hist = _load_history(history_parquet)
    end = str(trade_date) if trade_date else datetime.now(JST).date().isoformat()
    if codes_today:
        rows_today = []
        for r in codes_today:
            if not r.get("code"):
                continue
            try:
                if float(r.get("return_pct") or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            rows_today.append(
                {
                    "code": str(r.get("code")),
                    "name": r.get("name") or "",
                    "return_pct": r.get("return_pct"),
                    "turnover": r.get("turnover"),
                    "market": r.get("market"),
                    "date": end,
                }
            )
        if rows_today:
            if not hist.empty:
                hist = hist[hist["date"] != end]
            hist = pd.concat([hist, pd.DataFrame(rows_today)], ignore_index=True)
    if hist.empty:
        return base

    dates = sorted(d for d in hist["date"].unique() if d <= end)
    # **当日を除く**直近 window 営業日（v11 の核。当日の急騰を継続性指標から外す）
    past_dates = [d for d in dates if d != end][-window:]

    # 日別の点灯有無とスコアをテーマ単位で集計
    lit_days: dict[str, int] = defaultdict(int)
    lit_score: dict[str, float] = defaultdict(float)
    for d in past_dates:
        day = hist[hist["date"] == d].drop_duplicates(subset=["code"]).to_dict("records")
        for t, v in score_one_day(day, code_to_themes).items():
            if len(v.get("codes") or []) >= SUSTAIN_MIN_CODES:
                lit_days[t] += 1
                lit_score[t] += float(v.get("score") or 0.0)

    for r in rows:
        group = _theme_group_names(r)
        # 統合テーマは構成テーマのうち最も継続しているものを代表値にする
        days = max((lit_days.get(t, 0) for t in group), default=0)
        sc = max((lit_score.get(t, 0.0) for t in group), default=0.0)
        avg = (sc / days) if days else 0.0
        r["lit_days"] = int(days)
        r["lit_window"] = len(past_dates)
        r["sustain"] = float(days) * avg

    rows.sort(key=lambda r: (-r.get("sustain", 0.0), -r.get("heat", 0.0), r["theme"]))
    base["rows"] = rows[:HEAT_CANDIDATE_POOL]
    base["sustain_window"] = len(past_dates)
    return base


# --------------------------------------------------------------------------
# 初動候補テーマ（機械抽出・v16 / 2026-09-03）
# --------------------------------------------------------------------------
def compute_lit_history(
    history_parquet: Path | str | None = None,
    trade_date=None,
    theme_master_path: Path | str | None = None,
    window: int = EARLY_HISTORY_WINDOW,
) -> dict:
    """テーマ -> 「当日を除く」直近 window 営業日の点灯日数 を返す。

    compute_theme_heat_v2 が内部で持っている lit_days の計算をそのまま切り出したもの
    （SUSTAIN_MIN_CODES 銘柄以上で点灯した日を1日と数える）。初動候補テーマの局面判定
    （初出 / 継続N日目）が同じ定義で動くようにするための共有関数。
    """
    code_to_themes, _size, _stale, _exc = load_theme_map(theme_master_path)
    if not code_to_themes:
        return {}
    hist = _load_history(history_parquet)
    if hist.empty:
        return {}
    end = str(trade_date) if trade_date else datetime.now(JST).date().isoformat()
    dates = sorted(d for d in hist["date"].unique() if d <= end)
    past_dates = [d for d in dates if d != end][-int(window):]

    lit_days: dict[str, int] = defaultdict(int)
    for d in past_dates:
        day = hist[hist["date"] == d].drop_duplicates(subset=["code"]).to_dict("records")
        for t, v in score_one_day(day, code_to_themes).items():
            if len(v.get("codes") or []) >= SUSTAIN_MIN_CODES:
                lit_days[t] += 1
    return dict(lit_days)


def evaluate_early_pool(
    entries_merged: list[dict],
    lit_history: dict | None = None,
    top_pool: int = EARLY_TOP_POOL,
) -> list[dict]:
    """当日 score 上位 `top_pool` 件を**ゲート判定前のまま**評価して返す（記録用）。

    select_early_candidates と同じ指標（n_up / n3 / turn3_oku / 局面）を計算するが、
    E5 のゲート（n_up・n3・turn3）で**落とさず**、通過可否を `passed_gate` に持たせる。
    枠数や閾値を後から変えて再検証できるよう、日次 parquet へはこの全件を残す。
    """
    if not entries_merged:
        return []
    lit_history = lit_history or {}
    if code_to_themes is None:
        code_to_themes, _ts, _sn, _ex = load_theme_map()

    def _turnover(rec: dict) -> float:
        try:
            return float(rec.get("turnover") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(
        entries_merged,
        key=lambda e: (-float(e.get("score") or 0.0), str(e.get("theme") or "")),
    )
    out: list[dict] = []
    for rank, e in enumerate(ranked[: max(int(top_pool or 0), 0)], 1):
        codes = [c for c in (e.get("codes") or []) if str(c.get("code") or "").strip()]
        if not codes:
            continue
        member_counts = e.get("member_counts") or []
        n_up = max([int(x) for x in member_counts], default=len(codes))
        moved = []
        for c in codes:
            try:
                if float(c.get("return_pct") or 0) >= EARLY_MOVE_PCT:
                    moved.append(c)
            except (TypeError, ValueError):
                continue
        n3 = len(moved)
        turn3_oku = sum(_turnover(c) for c in moved) / 1e8
        passed = (
            n_up >= EARLY_MIN_NUP
            and n3 >= EARLY_MIN_NUP3
            and turn3_oku >= float(EARLY_MIN_TURN3_OKU)
        )
        group = {e.get("theme")} | set(e.get("merged_names") or [])
        days = max((int(lit_history.get(t, 0) or 0) for t in group), default=0)
        out.append({
            "theme": e.get("theme"),
            "label": format_theme_label(e),
            "rank": rank,
            "score": float(e.get("score") or 0.0),
            "n_up": int(n_up),
            "n3": int(n3),
            "turn3_oku": float(turn3_oku),
            "passed_gate": bool(passed),
            "phase": "初出" if days <= 0 else f"継続{days + 1}日目",
            "codes": sorted(codes, key=lambda c: -_turnover(c)),
            "hot_codes": sorted(moved, key=lambda c: -_turnover(c)),
        })
    return out


def append_early_candidates(
    pool_rows: list[dict],
    trade_date,
    shown_themes=None,
    lane: str = "mover",
    path: Path | str | None = None,
) -> Path | None:
    """初動候補の日次記録を parquet へ追記する（同一 date+lane+theme は上書き）。

    2026-09-03 PM 決定。枠5で誌面へ出しつつ、**ゲート通過前の上位10件全て**を残して
    3か月後に枠数・閾値を変えた再検証ができるようにする。誌面へ出た行は shown=True。

    列: date / lane / theme / rank / score / n_up / n3 / turn3_oku /
        passed_gate / shown / phase / codes / hot_codes / hot_detail
    """
    if not pool_rows:
        return None
    date_str = str(trade_date)
    shown = {str(s) for s in (shown_themes or set())}

    def _codes_str(recs) -> str:
        return ",".join(str(c.get("code") or "").strip() for c in (recs or [])
                        if str(c.get("code") or "").strip())

    def _detail(recs) -> str:
        parts = []
        for c in recs or []:
            code = str(c.get("code") or "").strip()
            if not code:
                continue
            try:
                pct = f"{float(c.get('return_pct')):+.1f}%"
            except (TypeError, ValueError):
                pct = ""
            try:
                tn = f"{float(c.get('turnover') or 0) / 1e8:.0f}億"
            except (TypeError, ValueError):
                tn = ""
            parts.append(f"{code}{pct}/{tn}")
        return " ".join(parts)

    rows = []
    for r in pool_rows:
        theme = str(r.get("theme") or "").strip()
        if not theme:
            continue
        rows.append({
            "date": date_str,
            "lane": str(lane),
            "theme": theme,
            "rank": int(r.get("rank") or 0),
            "score": float(r.get("score") or 0.0),
            "n_up": int(r.get("n_up") or 0),
            "n3": int(r.get("n3") or 0),
            "turn3_oku": float(r.get("turn3_oku") or 0.0),
            "passed_gate": bool(r.get("passed_gate")),
            "shown": bool(theme in shown),
            "phase": str(r.get("phase") or ""),
            "codes": _codes_str(r.get("codes")),
            "hot_codes": _codes_str(r.get("hot_codes")),
            "hot_detail": _detail(r.get("hot_codes")),
        })
    if not rows:
        return None

    cols = ["date", "lane", "theme", "rank", "score", "n_up", "n3", "turn3_oku",
            "passed_gate", "shown", "phase", "codes", "hot_codes", "hot_detail"]
    p = Path(path) if path else EARLY_CANDIDATES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows, columns=cols)
    if p.exists():
        try:
            old = pd.read_parquet(p)
            # 同一 date/lane/theme を除いてから連結する（再実行の冪等性）
            key_new = set(zip(new["date"], new["lane"], new["theme"]))
            mask = [
                (str(d), str(l), str(th)) not in key_new
                for d, l, th in zip(old["date"], old["lane"], old["theme"])
            ]
            new = pd.concat([old[mask], new], ignore_index=True)
        except Exception:
            pass
    new.to_parquet(p, index=False)
    return p


def select_early_candidates(
    entries_merged: list[dict],
    theme_size: dict | None = None,
    lit_history: dict | None = None,
    top_pool: int = EARLY_TOP_POOL,
    min_nup: int = EARLY_MIN_NUP,
    min_nup3: int = EARLY_MIN_NUP3,
    min_turn3_oku: float = EARLY_MIN_TURN3_OKU,
    max_rows: int = EARLY_MAX_ROWS,
    code_to_themes: dict | None = None,
) -> list[dict]:
    """当日の統合テーマ行から「初動候補テーマ」を機械抽出する（案E）。

    判定は当日の一次データだけで閉じており、Claude の解釈も辞書の意味づけも入らない。
        0. 誌面へ出す銘柄集合（+3%以上の点灯銘柄）が重なるテーマ行を統合する
        1. 当日 score 降順で上位 `top_pool` 件を候補プールにする
        2. そのうち n_up >= `min_nup` かつ n3 >= `min_nup3` かつ turn3 >= `min_turn3_oku` 億円
        3. 残りを score 順に最大 `max_rows` 件

    E5（2026-09-03 PM 指示）で判定を「実際に動いた銘柄」だけで見る形へ変えた。
        n3    … 点灯銘柄のうち騰落率 EARLY_MOVE_PCT(+3%) 以上の銘柄数
        turn3 … その +3%以上の銘柄群の当日売買代金合計（億円）
    旧条件（点灯銘柄**全体**の代金合計 >= 500億）は、ほぼ動いていない大型株が代金の
    大半を占めるテーマを通した（9/2 の防衛=1989億の主因は伊藤忠+0.4%、グローバル
    サウス=1613億の主因はニッスイ+0.5%）。E5 は 8/27・8/28 の自動運転を維持したまま
    この2件を落とす。

    n_up / turn の定義（統合行に対して）:
        codes … 構成テーマの点灯銘柄の**和集合**（merge_overlapping_themes が既に和集合を
                作っているため、統合行の "codes" をそのまま使う）
        n_up  … 構成テーマそれぞれの点灯銘柄数の**最大値**。和集合の件数ではない。
                和集合を使うと、たまたま1銘柄ずつ点灯した無関係なテーマが統合された行が
                件数だけ膨らんで通ってしまうため、「単一のテーマとして何社同時に動いたか」
                を表す最大値を採る。
        turn  … 統合行の点灯銘柄（和集合）の当日売買代金の合計（億円）。

    局面（既存の lit_days と同じ定義を流用する）:
        直前 `EARLY_HISTORY_WINDOW` 営業日（**当日を除く**）のうち、そのテーマが
        SUSTAIN_MIN_CODES 銘柄以上で点灯した日数 N から
            N == 0 … 「初出」
            N >= 1 … 「継続{N+1}日目」
        統合行では構成テーマのうち最も継続しているものの日数を代表値にする
        （compute_theme_heat_v2 の lit_days と同じ扱い）。

    本日のテーマ欄・直近2週間欄との重複は**除外しない**（PM 指示）。重複していれば
    誌面側で注記するため、行に "dup_today" / "dup_heat" のフラグを立てられるよう
    呼び出し側が後から書き込める素の dict を返す。

    Args:
        entries_merged: merge_overlapping_themes の戻り値
            [{"theme","merged_names","theme_size","score","codes"(list[rec])}, ...]
        theme_size: theme -> 構成銘柄数（load_theme_map の戻り値の2番目）。表示には
            使わないが将来の同点処理のため受け取る。
        lit_history: theme -> 当日を除く直近窓の点灯日数。compute_theme_heat_v2 が
            算出したものを渡す。None なら全テーマ 0 日（＝すべて「初出」）とみなす。

    Returns:
        [{"theme","merged_names","label","rank","score","n_up","n3","turn3_oku",
          "lit_days","phase","codes"(売買代金降順), "theme_size"}, ...]
    """
    if not entries_merged:
        return []
    lit_history = lit_history or {}
    if code_to_themes is None:
        code_to_themes, _ts0, _sn0, _ex0 = load_theme_map()

    def _turnover(rec: dict) -> float:
        try:
            return float(rec.get("turnover") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _moved_codes(e: dict) -> set:
        s_ = set()
        for c in (e.get("codes") or []):
            try:
                if float(c.get("return_pct") or 0) >= EARLY_MOVE_PCT:
                    s_.add(str(c.get("code") or ""))
            except (TypeError, ValueError):
                continue
        return {c for c in s_ if c}

    # 2026-09-04 PM 指示: 「統合 → 銘柄の絞り込み → 新規性判定」を**変化がなくなるまで
    # 繰り返す**。1回で済ませると、絞り込みで表示銘柄が減った後に生じた重複を畳めない
    # （9/3 実測で 初動候補 2位・3位 がともに九州電力(9508)を表示した）。
    # 各パスの入口で毎回、統合前の素の entries から作り直す（前パスの絞り込み結果を
    # 入力にすると銘柄が単調減少して痩せ続けるため）。
    def _build_pool(src: list[dict]) -> list[dict]:
        """統合 → プール切り出し → 表示銘柄の絞り込み まで進めた行を返す。"""
        merged = merge_by_display_overlap(src, _moved_codes, theme_size=theme_size)
        ranked_ = sorted(
            merged,
            key=lambda e: (-float(e.get("score") or 0.0), str(e.get("theme") or "")),
        )
        pool_ = ranked_[: max(int(top_pool or 0), 0)]
        # (a) 代表テーマ自身の辞書構成銘柄に限定（吸収した別名テーマの銘柄を混ぜない。
        #     9/3 の 自動運転車 の行に レーザーテック(6920) が出た原因）。
        # (b) 上位プールの GENERIC_CODE_MIN_THEMES 行以上に出得る汎用銘柄
        #     （商社・電力・大型汎用株）を外す。ゲート判定の n_up からは外さない。
        generic_ = generic_codes(pool_, code_to_themes)
        for e in pool_:
            keep = _self_codes(e, code_to_themes) - generic_
            e["_shown"] = [
                c for c in (e.get("codes") or []) if str(c.get("code")) in keep
            ]
        return pool_

    pool = _build_pool(entries_merged)

    out: list[dict] = []
    for e in pool:
        codes = [c for c in (e.get("codes") or []) if str(c.get("code") or "").strip()]
        if not codes:
            continue
        # 誌面へ出す銘柄（代表テーマ自身の構成銘柄・汎用銘柄を除く）
        shown = list(e.get("_shown") or [])
        # n_up: 構成テーマ単位の点灯銘柄数の最大値。merge_overlapping_themes は
        # 構成テーマ別の内訳を残さないため、統合前の内訳が渡らない場合は和集合件数へ
        # フォールバックする（呼び出し側が "member_counts" を積んでいればそれを使う）。
        member_counts = e.get("member_counts") or []
        n_up = max([int(x) for x in member_counts], default=len(codes))
        # E5: 判定は「実際に +3%以上動いた銘柄」だけで行う（本数 n3 と実弾 turn3）。
        # 2026-09-04: 母集団は誌面へ出す銘柄（shown）に揃える。誌面に出ない銘柄で
        # n3・turn3 を膨らませると、表と見出しの数字が食い違う。
        moved = []
        for c in shown:
            try:
                if float(c.get("return_pct") or 0) >= EARLY_MOVE_PCT:
                    moved.append(c)
            except (TypeError, ValueError):
                continue
        n3 = len(moved)
        turn3_oku = sum(_turnover(c) for c in moved) / 1e8
        if n_up < min_nup or n3 < int(min_nup3) or turn3_oku < float(min_turn3_oku):
            continue
        # 局面: 構成テーマのうち最も継続している日数を代表値にする
        group = {e.get("theme")} | set(e.get("merged_names") or [])
        days = max((int(lit_history.get(t, 0) or 0) for t in group), default=0)
        phase = "初出" if days <= 0 else f"継続{days + 1}日目"
        out.append(
            {
                "theme": e.get("theme"),
                "merged_names": list(e.get("merged_names") or []),
                "label": format_theme_label(e),
                # 順位＝**表示順**（掲載位置の連番）。プール内順位をそのまま出すと
                # 誌面が「1位・3位・5位・7位・8位」と歯抜けになるため（2026-09-04 PM 指示）。
                # 実値は新規性フィルタ適用後に振り直す。
                "rank": 0,
                "score": float(e.get("score") or 0.0),
                "n_up": int(n_up),
                "n3": int(n3),
                "turn3_oku": float(turn3_oku),
                "lit_days": int(days),
                "phase": phase,
                "theme_size": int(
                    e.get("theme_size")
                    or (theme_size or {}).get(e.get("theme"), 0)
                    or 0
                ),
                "codes": sorted(shown, key=lambda c: -_turnover(c)),
            }
        )
    # 新規性フィルタ（2026-09-04 PM 指示）。上位行に未掲載の +3%銘柄が MIN_NEW_CODES 件
    # 未満の行を落とし、次点を繰り上げる。ここで初めて max_rows へ切る（先に切ると
    # 落とした分の繰り上げ候補が残らない）。
    # 2026-09-04 PM 指示: 絞り込み後の表示銘柄で**統合と新規性判定を再適用**し、
    # 行数が変化しなくなるまで繰り返す。絞り込みで表示銘柄が減った結果として新たに
    # 生じた重複（9/3 の 2位・3位 がともに 9508 を表示）を畳むため。
    for _ in range(MERGE_REFINE_PASSES):
        before = len(out)
        # 絞り込み後の表示銘柄（+3%）で統合し直す。codes は既に絞り込み済みなので
        # merge_by_display_overlap の和集合も絞り込み後の銘柄だけで構成される。
        out = merge_by_display_overlap(out, _moved_codes, theme_size=theme_size)
        # 統合で消えた行のぶん turn3 / n3 を数え直す（見出しと表の数字を一致させる）。
        for r in out:
            _mv = [
                c
                for c in (r.get("codes") or [])
                if _pct_at_least(c, EARLY_MOVE_PCT)
            ]
            r["n3"] = len(_mv)
            r["turn3_oku"] = sum(_turnover(c) for c in _mv) / 1e8
            r["label"] = format_theme_label(r)
        out.sort(key=lambda r: (-float(r.get("score") or 0.0), str(r.get("theme") or "")))
        out = filter_new_codes(out, _moved_codes, max_rows=max(int(max_rows or 0), 0))
        if len(out) == before:
            break
    # 順位＝表示順。フィルタ後の掲載位置で振り直す。
    for _i, _r in enumerate(out, 1):
        _r["rank"] = _i
    return out


def _early_rows_once(
    entries_merged,
    code_to_themes,
    theme_size,
    lit_history,
    top_pool,
    min_nup,
    min_nup3,
    min_turn3_oku,
    max_rows,
):
    """後方互換のための薄いラッパ（現在は select_early_candidates 内で完結）。"""
    return select_early_candidates(
        entries_merged,
        theme_size=theme_size,
        lit_history=lit_history,
        top_pool=top_pool,
        min_nup=min_nup,
        min_nup3=min_nup3,
        min_turn3_oku=min_turn3_oku,
        max_rows=max_rows,
        code_to_themes=code_to_themes,
    )


_EARLY_FOOTER = (
    "この欄は当日に複数銘柄が同時に動いたテーマを機械抽出したものであり、"
    "売買を推奨するものではありません。"
)


def render_early_candidates(
    rows: list[dict],
    pct_key: str = "return_pct",
    today_labels: set | None = None,
    max_codes: int = EARLY_LEAD_CODES,
) -> list[str]:
    """`## 初動候補テーマ（機械抽出）` の raw ブロックを返す（v17・2026-09-03 PM 指示）。

    旧形式は「テーマ名・当日順位・上昇銘柄数・+3%以上の売買代金合計・局面・点灯銘柄・
    材料」の7列表で、テーマ名・点灯銘柄・材料の3列が長文のため table-layout:fixed の
    均等7分割では全セルが折り返して縦長になり「読めない」と却下された（PM 2026-09-03）。

    新形式はテーマごとのブロック（本日のテーマ・直近2週間と同じ体裁に統一）:
        **{順位（表示順）}位 {テーマ名} ｜ 上昇{n_up}銘柄 ｜ +3%以上の売買代金 {turn3_oku}億円 ｜ {局面}**
        材料: {Claude が1文で書く。無ければ「材料なし（値動きのみ）」}

        | コード | 銘柄名 | 何の会社 | 時価総額 | 騰落率 |
        |---|---|---|---|---|
        （+3%以上の点灯銘柄のみ・売買代金順・最大 max_codes 行）

    見出し行・銘柄表は機械が出した確定値であり、Claude は**そのまま転記**する
    （削除・並べ替え・銘柄の追加除外を禁止）。**材料の1文だけ**を Claude が書く。
    表に「何の会社」列を新設したため、呼び出し側は codes の各レコードへ desc を
    含めていない場合、誌面を書く Claude が raw 内の該当銘柄ブロックの記述で埋める
    （§38 の「何の会社」空欄禁止の連鎖を流用）。

    固定文（`_EARLY_FOOTER`）は欄末尾に1回だけ置く（テーマブロックごとには置かない）。

    Args:
        rows: select_early_candidates の戻り値。
        today_labels: 当日テーマ欄にも載る見込みのテーマ名の集合（見出しの注記に使う）。
        max_codes: 銘柄表に載せる最大行数（+3%以上の点灯銘柄を売買代金順に採る）。
    """
    lines = ["## 初動候補テーマ（機械抽出）", ""]
    if not rows:
        lines += [
            "本日は基準（上位10位以内・上昇4銘柄以上・うち+3%以上が2銘柄以上・その売買代金合計100億円以上）を"
            "満たすテーマがありません",
            "",
        ]
        return lines

    lines += [
        "> **この欄は機械が出した確定値です。見出し行と銘柄表はそのまま誌面へ転記してください**"
        f"（最大{EARLY_MAX_ROWS}テーマ）。"
        "テーマ・銘柄行の削除・並べ替え・追加や除外を禁止します"
        f"（判定は当日 score 上位{EARLY_TOP_POOL}位以内・上昇{EARLY_MIN_NUP}銘柄以上・"
        f"うち+{EARLY_MOVE_PCT:.0f}%以上が{EARLY_MIN_NUP3}銘柄以上・その売買代金合計"
        f"{EARLY_MIN_TURN3_OKU:.0f}億円以上の機械条件のみ）。",
        "",
        "> **`材料:` の1文だけをあなたが書きます**。raw の材料一覧にその行の点灯銘柄の"
        "材料があれば「銘柄名（コード）」を主語にした**1文**を書き、無ければ"
        "`材料なし（値動きのみ）` と書いてください。",
        "",
        "> **この欄は状況把握であり売買推奨ではありません**。「チャンス」「注目」"
        "「狙い目」「初動」「先回り」等の推奨語と、「可能性が高い」「とみられる」等の"
        "推測語を禁止します。固定文は欄の末尾に1回だけ置きます（テーマブロックごとに"
        "繰り返さない）。",
        "",
    ]

    labels = today_labels or set()
    for r in rows:
        note = ""
        if r.get("theme") in labels or r.get("label") in labels:
            note = "　（本日のテーマ欄にも掲載）"
        head = (
            f"**{int(r.get('rank') or 0)}位 {r.get('label') or r.get('theme') or ''} ｜ "
            f"上昇{int(r.get('n_up') or 0)}銘柄 ｜ "
            f"+3%以上の売買代金 {float(r.get('turn3_oku') or 0.0):.0f}億円 ｜ "
            f"{r.get('phase') or ''}**{note}"
        )
        lines.append(head)
        lines.append("")
        lines.append("材料: （ここに1文）")
        lines.append("")
        lines.append("| コード | 銘柄名 | 何の会社 | 時価総額 | 騰落率 |")
        lines.append("|---|---|---|---|---|")
        moved = []
        for c in (r.get("codes") or []):
            try:
                if float(c.get(pct_key) or 0) >= EARLY_MOVE_PCT:
                    moved.append(c)
            except (TypeError, ValueError):
                continue
        for c in moved[: max(int(max_codes or 0), 0)]:
            code = str(c.get("code") or "").strip()
            name = str(c.get("name") or "").strip()
            lines.append(
                f"| {code} | {name} | （要記入） | {_mcap_str(c) or '―'} | "
                f"{_pct_str(c, pct_key)} |"
            )
        lines.append("")

    lines.append(_EARLY_FOOTER)
    lines.append("")
    return lines


# --------------------------------------------------------------------------
# 夜間 PTS（当日部を PTS へ差し替え）
# --------------------------------------------------------------------------
def detect_night(pts_risers, theme_master_path: Path | str | None = None):
    """PTS 上昇銘柄から「本日のテーマ」を算出する（当夜版）。

    PTS には売買代金がほぼ無い銘柄が多いため、資金量スコアは
    夜間売買代金（turnover）が取れる場合のみ使い、取れない場合は
    log10(1+0)=0 とならないよう最小値 1 億円相当を下限とする。

    Args:
        pts_risers: [{"code","name","pts_pct","turnover","market"}, ...]
    """
    code_to_themes, theme_size, stale_note, excluded = load_theme_map(theme_master_path)
    if not code_to_themes:
        return {"rows": [], "stale_note": stale_note, "excluded_count": excluded}

    recs = []
    for r in pts_risers:
        try:
            pct = float(r.get("pts_pct") or 0)
        except (TypeError, ValueError):
            continue
        if pct <= 0:
            continue
        # 夜間売買代金は薄いため 1億円を下限に置く（log 圧縮の分母を安定させる）
        tv = r.get("turnover")
        try:
            tv = float(tv or 0)
        except (TypeError, ValueError):
            tv = 0.0
        recs.append({**r, "return_pct": pct, "turnover": max(tv, 1e8)})

    per_theme = score_one_day(recs, code_to_themes)
    entries = [
        {"theme": t, "score": v["score"], "codes": v["codes"]}
        for t, v in per_theme.items()
        if len({str(c.get("code")) for c in v["codes"]}) >= MIN_CODES_FOR_ALERT
    ]
    rows = merge_overlapping_themes(entries, theme_size)
    # 表示ベースの二次統合（2026-09-04 PM 指示・当日欄と同じ扱い）。
    rows = merge_by_display_overlap(
        rows,
        lambda r: {str(c.get("code")) for c in (r.get("codes") or [])},
        theme_size=theme_size,
    )
    for r in rows:
        r["codes"] = sorted(r["codes"], key=lambda c: -float(c.get("return_pct") or 0))
    rows.sort(key=lambda r: (-r["score"], r["theme"]))
    return {"rows": rows, "stale_note": stale_note, "excluded_count": excluded}


# --------------------------------------------------------------------------
# md セクション生成
# --------------------------------------------------------------------------
def _lead_codes_cell(codes: list[dict], pct_key: str = "return_pct") -> str:
    """主導銘柄セル: 「コード 社名（+X.X%）」を LEAD_CODES 件まで（旧形式・互換のため残す）。"""
    parts = []
    for c in codes[:LEAD_CODES]:
        code = str(c.get("code", ""))
        name = str(c.get("name") or "").strip()
        try:
            pct = f"（{float(c.get(pct_key)):+.1f}%）"
        except (TypeError, ValueError):
            pct = ""
        parts.append(f"{code} {name}{pct}".strip())
    return "<br>".join(parts)


# 事業説明を1行に収めるための整形（2026-08-31 PM 指示・スカスカ表の解消）。
# 出典は EDINET DB の事業概要（呼び出し側が desc_lookup で供給）であり、本モジュールは
# 記憶ベースの事業説明を一切生成しない。取れない銘柄は空文字を返し、誌面側の指示で扱う。
_DESC_STRIP_RE = re.compile(
    r"^(当社(グループ)?(は|では|の)|同社(グループ)?(は|では|の)|株式会社|"
    r"当グループ(は|では|の)|わたくしども(は|の))"
)
# 説明の切り出しに使う区切り（読点・接続の切れ目）
_DESC_CUT_CHARS = "、。，．・）」"


def _pct_str(rec: dict, pct_key: str = "return_pct") -> str:
    """騰落率を「+12.3%」形式で返す（取れなければ空文字）。"""
    try:
        return f"{float(rec.get(pct_key)):+.1f}%"
    except (TypeError, ValueError):
        return ""


# テーマ系の表で時価総額を補完するためのグローバル lookup（code -> 億円）。
# 2週間欄・初動候補の主導銘柄は history parquet 由来のレコードであり、当日の動意母集団に
# 入っていない銘柄には mcap_oku が付かない（9/3 実測で2週間欄の12銘柄が `―` になった）。
# make_mover_report が全上場銘柄の full_df（当日終値×発行済株数）から作った辞書を
# set_mcap_lookup() で注入し、レコードに値が無い銘柄だけここから補う。
_MCAP_LOOKUP: dict[str, float] = {}


def set_mcap_lookup(mapping: dict | None) -> None:
    """全上場銘柄の時価総額辞書（code -> 億円）を注入する（2026-09-04 PM 指示）。

    make_mover_report が full_df の MarketCapOku から組んで渡す。当日の動意母集団に
    入らない銘柄（2週間欄の主導銘柄など）の時価総額を誌面で `―` にしないための補完。
    推計はせず、full_df に実在する値だけを使う（§0 一次情報）。
    """
    global _MCAP_LOOKUP
    _MCAP_LOOKUP = {str(k).strip(): v for k, v in (mapping or {}).items() if str(k).strip()}


def _mcap_str(rec: dict) -> str:
    """時価総額を「1,234億円」「1.9兆円」形式で返す（取れなければ空文字）。

    2026-09-03 PM 指示: テーマ系の全表に時価総額列を追加する。値の出所は
    make_mover_report 側が組む記録の `mcap_oku`（当日終値×発行済株数の screening_master
    MarketCapOku 由来・億円単位）。取れない銘柄は空欄にする（推計で埋めない・§0）。
    他レポート（夜間PTS の cap_str 等）と同じ書式（10,000億円以上は兆円表記）に揃える。
    """
    oku = None
    try:
        oku = float(rec.get("mcap_oku"))
        if oku != oku:  # NaN
            oku = None
    except (TypeError, ValueError):
        oku = None
    if oku is None:
        # レコードに値が無い銘柄は全上場銘柄の辞書から補う（2026-09-04 PM 指示）。
        try:
            oku = float(_MCAP_LOOKUP.get(str(rec.get("code") or "").strip()))
            if oku != oku:
                return ""
        except (TypeError, ValueError):
            return ""
    if oku >= 10000:
        return f"{oku / 10000:.1f}兆円"
    return f"{oku:,.0f}億円"


def order_lead_codes(
    codes: list[dict],
    material_lookup=None,
    limit: int = LEAD_CANDIDATES,
    min_turnover_head: int = LEAD_CODES,
) -> list[dict]:
    """raw へ出す主導銘柄の候補を「材料保有を優先し、その中で売買代金順」で返す。

    2026-08-31 PM 指示。従来は売買代金上位 LEAD_CODES(3) 件で固定していたため、
    材料（なぜ動いた）の裏が取れている銘柄がテーマ内4位以下に居ると raw へ出ず、
    _cr §38 の積極支持判定（材料がテーマの共通材料を支持する銘柄2社以上でテーマ維持）
    に使えないまま行が落ちていた。8/28 実測で 336A（自動運転車の6位）・3987（フィジカル
    AI の9位）が該当し、直近2週間の熱いテーマが5テーマ→1テーマまで痩せた。

    並び:
      1. 材料テキストを持つ銘柄（当日 raw の材料・stock_context_daily の日付付き遡り材料の
         どちらでも可）を売買代金降順で並べる。
      2. 材料を持たない銘柄を売買代金降順で続ける。

    材料保有銘柄が min_turnover_head 件未満のときも 2. で必ず補完されるため、
    候補が痩せることはない（§25 の銘柄除外禁止）。機械は**取捨選択をしない**。
    どれを誌面へ載せるかは GHA 側 Claude の積極支持判定に委ねる。

    material_lookup が None の場合は従来どおりの売買代金順（先頭 limit 件）を返す。

    Args:
        codes: detect_today / compute_theme_heat が返す売買代金降順の主導銘柄リスト。
        material_lookup: code -> list[str] を返す callable。
        limit: raw へ出す最大件数。

    Returns:
        並べ替え済みの主導銘柄リスト（最大 limit 件）。入力の dict はそのまま使う。
    """
    src = list(codes or [])
    if not src:
        return []
    if material_lookup is None:
        return src[:limit]

    def _turnover(c: dict) -> float:
        try:
            return float(c.get("turnover") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _has_material(c: dict) -> bool:
        code = str(c.get("code") or "").strip()
        if not code:
            return False
        try:
            items = list(material_lookup(code) or [])
        except Exception:
            return False
        return any(str(it).strip() for it in items)

    with_mat = [c for c in src if _has_material(c)]
    without = [c for c in src if c not in with_mat]
    with_mat.sort(key=lambda c: -_turnover(c))
    without.sort(key=lambda c: -_turnover(c))
    return (with_mat + without)[:limit]


def clean_business_desc(text: str, limit: int = BIZ_DESC_SOURCE_CHARS) -> str:
    """事業概要の原文を raw 掲載用に整える（意味の圧縮はしない）。

    機械が行うのは、改行・空白の潰しと定型の主語（「当社は」等）の除去、および raw が
    肥大しないための長さ上限だけ。**15字前後への要約は誌面を書く Claude が行う**
    （機械が limit 字で切ると「クルマを人の運転なしで走らせるための…」のように文節の
    途中で切れて読めないため。2026-08-31 実測で確認）。

    原文が無ければ空文字を返す（推測で埋めない・§0 記憶ベース禁止）。
    """
    t = re.sub(r"\s+", "", str(text or "")).strip()
    if not t:
        return ""
    t = _DESC_STRIP_RE.sub("", t).lstrip("、。 ")
    if not t:
        return ""
    if len(t) <= limit:
        return t
    head = t[:limit]
    cut = max(head.rfind(ch) for ch in _DESC_CUT_CHARS)
    if cut >= limit // 2:
        return head[:cut + 1]
    return head + "…"


# 旧名の互換エイリアス（呼び出し側が残っている場合のため）
shorten_business_desc = clean_business_desc


def _lead_stock_table(
    codes: list[dict],
    desc_lookup=None,
    pct_key: str = "return_pct",
    limit: int = LEAD_CANDIDATES,
    material_lookup=None,
) -> list[str]:
    """主導銘柄の密な表（1銘柄=1行・全セル1行で収まる4列）を返す。

    列: コード / 銘柄名 / 何の会社 / 騰落率

    「何の会社」列には desc_lookup（呼び出し側が EDINET 事業概要／法人プロフィールの
    business_summary から供給）の**原文**を入れる。誌面を書く Claude が、この原文と
    raw 内の `**何の会社**` 記述を素材に BIZ_DESC_TARGET_CHARS 字前後へ言い換える
    （機械が字数で切ると文節の途中で切れて読めないため・_cr §38）。

    desc_lookup は build_desc_lookup で組んだフォールバック連鎖（当日 EDINET 事業概要 →
    直近10営業日の蓄積 → screening_master の業種名）を渡すため、表示対象銘柄で素材が
    空になることは構造的に起きない。それでも空になった場合は行を必ず残し
    （§25 の銘柄除外禁止）、セルを `（要記入）` にして Claude が raw 内の該当銘柄
    ブロックと材料テキストから書く（誌面に `―` を出すことは _cr §38 で禁止）。

    列: コード / 銘柄名 / 何の会社 / 時価総額 / 騰落率（2026-09-03 PM 指示で時価総額を追加）。
    """
    out = [
        "| コード | 銘柄名 | 何の会社 | 時価総額 | 騰落率 |",
        "|---|---|---|---|---|",
    ]
    for c in order_lead_codes(codes, material_lookup, limit):
        code = str(c.get("code", ""))
        name = str(c.get("name") or "").strip()
        desc = ""
        if desc_lookup is not None:
            try:
                desc = clean_business_desc(desc_lookup(code) or "")
            except Exception:
                desc = ""
        out.append(
            f"| {code} | {name} | {desc or '（要記入）'} | {_mcap_str(c) or '―'} | "
            f"{_pct_str(c, pct_key)} |"
        )
    out.append("")
    return out


def _lead_code_ids(rows: list[dict], material_lookup=None) -> list[str]:
    """理由素材を集めるべき銘柄コード（raw の主導銘柄候補のみ）。

    2026-08-31: 候補を LEAD_CANDIDATES 件へ広げたため、材料保有優先の並びで拾う。
    """
    out: list[str] = []
    for r in rows:
        for c in order_lead_codes(r.get("codes", []), material_lookup, LEAD_CANDIDATES):
            code = str(c.get("code", ""))
            if code and code not in out:
                out.append(code)
    return out


def build_duplicate_map(
    rows: list[dict],
    limit: int = LEAD_CANDIDATES,
    max_rows: int | None = None,
    material_lookup=None,
) -> dict[str, list[str]]:
    """`code -> その銘柄が主導銘柄として載っている候補テーマ名の一覧` を返す。

    2026-08-31 PM 指示（帰属精度の改善）。同一銘柄が複数候補テーマの主導銘柄として
    出てくると、誌面で「本来の材料と別のテーマに引っ張られた行」「同じ銘柄が複数の
    テーマに重複掲載された行」が生まれる。機械はどちらが正しい帰属かを判定しない
    （材料テキストの読解が要るため）が、**どの銘柄がどの候補に重複して載っているか**は
    機械的に算出できる。ここで出した一覧を raw の注記に出し、誌面を書く Claude が
    重複と誤帰属を見落とさずに検出できるようにする。

    Returns:
        重複（2テーマ以上に載る）銘柄のみを含む dict。単独掲載の銘柄は入れない。
    """
    target = rows if max_rows is None else rows[:max_rows]
    seen: dict[str, list[str]] = {}
    for r in target:
        label = format_theme_label(r)
        for c in order_lead_codes(r.get("codes") or [], material_lookup, limit):
            code = str(c.get("code", ""))
            if not code:
                continue
            lst = seen.setdefault(code, [])
            if label not in lst:
                lst.append(label)
    return {code: names for code, names in seen.items() if len(names) >= 2}


def _dup_note(code: str, self_label: str, dup_map: dict[str, list[str]] | None) -> str | None:
    """その銘柄が他候補にも載っている場合の注記行の本文を返す（無ければ None）。"""
    if not dup_map:
        return None
    others = [n for n in dup_map.get(str(code), []) if n != self_label]
    if not others:
        return None
    return "重複掲載: " + " / ".join(others)


def _tag_names_for(code, code_to_themes: dict, limit: int = 4) -> str:
    """その銘柄が持つみんかぶタグ名を `/` 区切りで返す（資金テーマでない括りは除外済み）。

    材料起点の当日テーマ判定では、タグは**同タグ複数点灯のヒント**としてのみ使う
    （2026-09-02 PM 承認の改修2）。タグが付いていること自体は掲載根拠にしない。
    """
    c = str(code or "").strip()
    names = list(code_to_themes.get(c) or code_to_themes.get(c[:4]) or [])
    if not names:
        return ""
    return " / ".join(str(n) for n in names[:limit])


def render_today_roster(
    universe_records: list[dict],
    material_lookup=None,
    desc_lookup=None,
    pct_key: str = "return_pct",
    theme_master_path: Path | str | None = None,
    max_material_items: int = 2,
    heading: str = "## 本日の動意母集団（材料一覧・Claude がここからテーマを括る）",
) -> list[str]:
    """母集団全銘柄の1行表を返す（2026-09-02 PM 承認の改修1・2＝材料起点への転換）。

    旧 `render_today_candidates` は「みんかぶ辞書タグ単位の候補15件」を出しており、
    (a) タグに無いテーマは構造的に出せない (b) 材料がタグ名を名指ししないと落ちる
    (c) 材料未取得の銘柄は候補にすら載らない、という3つの取りこぼしがあった
    （9/2 の当日テーマは1件）。

    本関数は辞書を**起点から外し**、母集団（extract_radar_universe の約100銘柄）を
    そのまま1銘柄1行で並べる。Claude は材料テキストを読み、同じ出来事で動いた銘柄を
    束ねてテーマ名を自由に付ける。タグ列は「同タグが複数点灯している」ヒントとしてのみ
    参照する。

    材料が取れない銘柄も**行ごと落とさず**「開示・報道なし（値動きのみ）」と明示する
    （空欄禁止・§25 の銘柄除外禁止）。

    Args:
        universe_records: [{"code","name","return_pct"/pct_key,"turnover","market"}] のリスト。
            呼び出し側（make_mover_report / make_pts_mover_report）が母集団から組む。
        material_lookup: code -> list[str]（build_material_lookup 推奨・遡り材料つき）。
        desc_lookup: code -> str（build_desc_lookup 推奨・業種名まで落ちるフォールバック連鎖）。
    """
    lines = [heading, ""]
    recs = [r for r in (universe_records or []) if str(r.get("code") or "").strip()]
    if not recs:
        lines += ["本日の母集団なし", ""]
        return lines

    def _turn(r) -> float:
        try:
            return float(r.get("turnover") or 0)
        except (TypeError, ValueError):
            return 0.0

    recs = sorted(recs, key=lambda r: -_turn(r))

    code_to_themes, _size, _stale, _exc = load_theme_map(theme_master_path)

    lines += [
        "> **これは機械が出した母集団であり、テーマではありません**。"
        "下の全銘柄の `材料` 欄を読み、**同じ出来事・同じ材料で動いた銘柄を2社以上束ねて"
        "テーマ名を付けて**ください（テーマ名は材料に即して自由に命名して構いません）。"
        f"誌面の `## 本日のテーマ` は**{MAX_ROWS_TODAY}〜{MAX_ROWS_TODAY_MAX}テーマ**を"
        "目標とし、束ねられなければ少ない件数で確定します（水増し禁止）。判定手順は prompts 側。",
        "",
        "> **`所属タグ` 列はヒントに過ぎません**。同じタグが複数行に出ていれば"
        "「同じ括りの銘柄に資金が入った可能性を材料で確かめる」きっかけとして使い、"
        "**タグが付いていることだけを掲載根拠にしないでください**"
        "（タグは事業の一部が触れているだけで、その日の値動きの理由ではありません）。"
        "タグに無いテーマ名を材料から作って構いません。",
        "",
        "> **材料欄の読み方**。`{M/D}時点の材料:` は直近"
        f"{CONTEXT_LOOKBACK_DAYS}営業日以内にその銘柄が動意 raw に載った最新日の記述を"
        "機械が遡って添えた一次情報です。当日テーマの根拠に使って構いませんが、"
        "誌面の理由文で触れるときは `8/25に` のように日付を必ず明示してください。"
        "`開示・報道なし（値動きのみ）` の銘柄は**テーマにも単独材料にも載せません**。",
        "",
        "> **1銘柄は1テーマにだけ帰属させます**。材料が実際に指している出来事の"
        "テーマ1つへ入れ、同じ銘柄を複数テーマの主導銘柄として重複掲載しないでください。",
        "",
        "> **所属タグ列に `（母集団外・材料あり）` と付いた銘柄**は、時価総額・売買代金の"
        "機械フィルタでは主母集団に入らなかったものの、動意誌面本体には掲載されており"
        "材料テキストが確認できる銘柄です。テーマの起点・構成銘柄として通常どおり掲載して"
        "構いません（初動候補欄・2週間の継続性集計はこの拡張の対象外で従来どおりです）。",
        "",
        "| コード | 銘柄名 | 何の会社 | 時価総額 | 騰落率 | 売買代金 | 所属タグ | 材料 |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for r in recs:
        code = str(r.get("code") or "").strip()
        name = str(r.get("name") or "").strip()
        try:
            pct = f"{float(r.get(pct_key)):+.1f}%"
        except (TypeError, ValueError):
            pct = ""
        t = _turn(r)
        turn_s = f"{t / 1e8:.1f}億円" if t > 0 else ""
        mcap_s = _mcap_str(r)

        desc = ""
        if desc_lookup is not None:
            try:
                desc = clean_business_desc(desc_lookup(code) or "")
            except Exception:
                desc = ""

        items: list = []
        if material_lookup is not None:
            try:
                items = [str(m).strip() for m in (material_lookup(code) or []) if str(m).strip()]
            except Exception:
                items = []
        if items:
            mat = " ／ ".join(items[:max_material_items])
        else:
            # 空欄禁止（2026-09-02 PM 承認の改修1）。取れなかった事実を明示する。
            mat = "開示・報道なし（値動きのみ）"

        tags = _tag_names_for(code, code_to_themes)
        if r.get("_out_of_radar"):
            tags = (tags + " " if tags else "") + "（母集団外・材料あり）"

        cells = [code, name, desc, mcap_s, pct, turn_s, tags, mat]
        cells = [str(c).replace("|", "／").replace("\n", " ").strip() for c in cells]
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines += [
        f"> **単独材料**: 上の一覧で材料が明確（大型受注・提携・上場承認・大型開示等）"
        "でありながら同じ出来事で動いた銘柄が2社に満たない銘柄は、"
        "誌面の `## 単独材料（テーマ未満・観察）` へ最大5行で載せてください"
        "（材料が `開示・報道なし（値動きのみ）` の銘柄は載せません）。",
        "",
    ]
    return lines


# --------------------------------------------------------------------------
# 自前括りテーマの蓄積（2026-09-02 PM 承認の改修4）
# --------------------------------------------------------------------------
# 誌面 md の末尾に GHA 側 Claude が出力する機械可読ブロックの書式（HTML コメント内 JSON）。
#   <!-- OWN_THEMES_JSON
#   [{"theme": "自動運転", "codes": ["593A", "336A"], "material": "トヨタ28年市販車搭載報道"}]
#   OWN_THEMES_JSON -->
# 誌面には一切表示されない（PDF レンダラは HTML コメントを出力しない）ため §29 に反しない。
OWN_THEMES_BLOCK_RE = re.compile(
    r"<!--\s*OWN_THEMES_JSON\s*(.*?)\s*OWN_THEMES_JSON\s*-->",
    re.DOTALL,
)


def parse_own_themes_block(md_text: str) -> list[dict]:
    """誌面 md から自前括りテーマの機械可読ブロックを読み取る（失敗時は空リスト）。

    Returns:
        [{"theme": str, "codes": [str, ...], "material": str}] のリスト。
    """
    m = OWN_THEMES_BLOCK_RE.search(str(md_text or ""))
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        theme = str(d.get("theme") or "").strip()
        codes = [str(c).strip() for c in (d.get("codes") or []) if str(c).strip()]
        if not theme or len(codes) < MIN_CODES_FOR_ALERT:
            continue  # テーマ名なし・2社未満は貯めない
        out.append({
            "theme": theme,
            "codes": sorted(set(codes)),
            "material": str(d.get("material") or "").strip()[:60],
        })
    return out


def append_own_themes(
    entries: list[dict],
    trade_date,
    lane: str = "mover",
    path: Path | str | None = None,
) -> Path | None:
    """自前括りテーマを日次 parquet へ追記する（同一 date+lane は上書き）。

    列: date / lane / theme / codes（`,` 区切り文字列）/ n_codes / material

    Returns:
        書き出した parquet のパス（entries が空なら None）。
    """
    rows = []
    date_str = str(trade_date)
    for e in entries or []:
        codes = [str(c).strip() for c in (e.get("codes") or []) if str(c).strip()]
        theme = str(e.get("theme") or "").strip()
        if not theme or len(codes) < MIN_CODES_FOR_ALERT:
            continue
        rows.append({
            "date": date_str,
            "lane": str(lane),
            "theme": theme,
            "codes": ",".join(sorted(set(codes))),
            "n_codes": len(set(codes)),
            "material": str(e.get("material") or "").strip()[:60],
        })
    if not rows:
        return None

    p = Path(path) if path else OWN_THEMES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows, columns=["date", "lane", "theme", "codes", "n_codes", "material"])
    if p.exists():
        try:
            old = pd.read_parquet(p)
            old = old[~(
                (old["date"].astype(str) == date_str) & (old["lane"].astype(str) == str(lane))
            )]
            new = pd.concat([old, new], ignore_index=True)
        except Exception:
            pass
    new.to_parquet(p, index=False)
    return p


def build_own_theme_lookup(
    trade_date=None,
    lookback_days: int = CONTEXT_LOOKBACK_DAYS,
    min_overlap: float = 0.5,
    path: Path | str | None = None,
):
    """辞書テーマの銘柄集合に一致する自前テーマ名を返す callable を組み立てる。

    段階導入（2026-09-02 PM 承認の改修4）。継続性（熱量）の計算そのものは辞書ベースの
    ままとし、**表示するテーマ名だけ**を自前括りへ寄せる。辞書テーマの主導銘柄集合と
    自前テーマの銘柄集合の重なりが min_overlap 以上（Jaccard ではなく自前側の被覆率）
    なら、直近で最も新しい自前テーマ名を返す。

    Returns:
        set[str] -> str|None の callable（該当なしは None）。
    """
    p = Path(path) if path else OWN_THEMES_PATH
    if not Path(p).exists():
        return lambda codes: None
    try:
        df = pd.read_parquet(p)
    except Exception:
        return lambda codes: None
    if df.empty or "codes" not in df.columns:
        return lambda codes: None

    df = df.copy()
    df["date"] = df["date"].astype(str)
    if trade_date is not None:
        dates = sorted({d for d in df["date"].unique() if d <= str(trade_date)})
        df = df[df["date"].isin(set(dates[-lookback_days:]))]
    df = df.sort_values("date")

    entries = [
        (str(t), {c.strip() for c in str(cs).split(",") if c.strip()})
        for t, cs in zip(df["theme"], df["codes"])
    ]

    def _lookup(codes) -> str | None:
        target = {str(c).strip() for c in (codes or []) if str(c).strip()}
        if not target:
            return None
        best = None
        for theme, own in entries:  # 後勝ち＝より新しい日付を優先
            if not own:
                continue
            overlap = len(own & target) / len(own)
            if overlap >= min_overlap:
                best = theme
        return best

    return _lookup


def render_today_candidates(
    result: dict,
    material_lookup=None,
    max_rows: int = TODAY_CANDIDATES,
    pct_key: str = "return_pct",
    desc_lookup=None,
) -> list[str]:
    """`## 本日のテーマ候補` を返す（2026-08-31 PM 確定の新形式）。

    機械はテーマを確定させない。スコア上位 max_rows 件を候補として出し、各候補の
    主導銘柄ごとに raw 内に既にある材料テキスト（TDNet 開示タイトル・カブラボ／立花
    QUICK 個別解説・Yahoo ニュース見出し）をぶら下げる。

    テーマ名の付け直し・共通材料の判定・理由文の生成はレポート作成 Claude が行う
    （機械側で確定的なテーマ名変更・理由文生成をしない）。

    2026-09-01 PM 承認の改修1: material_lookup に build_material_lookup（遡り材料つき）
    を渡すことで、当日セクションでも「当日動意上位入り＋直近10営業日以内の実在材料」で
    テーマを成立させられる。当日材料の保有率は実測で低く（8/28 は動意上位100銘柄中4銘柄）、
    当日材料のみを要求すると誌面が1〜2テーマへ張り付いていた。
    候補に並ぶのは detect_today の出力＝**当日の動意上位で上昇した銘柄**のみであり、
    当日株価が動いていない銘柄が混ざることは構造的に起きない（水増し防止）。

    Args:
        material_lookup: code -> list[str] を返す callable（呼び出し側が raw から供給）。
            build_material_lookup を渡すと当日分→遡り分のフォールバックが効く。
    """
    lines = ["## 本日のテーマ候補", ""]
    # 2026-09-04 PM 指示: 絞り込み後の表示銘柄で「統合 → 新規性判定」を再適用し、
    # 変化がなくなるまで繰り返す（初動候補・2週間欄と同じ扱い）。
    rows = refine_rows_by_display(
        result.get("rows") or [],
        lambda r: {
            str(c.get("code")) for c in (r.get("codes") or [])[:MAX_LEAD_ROWS]
        },
        max_rows=max_rows,
    )
    if not rows:
        lines += ["本日の点灯なし", ""]
        return lines

    lines += [
        "> 機械が算出した候補です。**このまま誌面へ転記しません**。"
        "主導銘柄2社以上に共通する材料があるものだけを残し、材料でテーマ名を付け直して"
        f"{MAX_ROWS_TODAY}テーマの `## 本日のテーマ` を**テーマごとのブロック形式**で"
        "作ってください（見出し行＋主導銘柄の4列表。判定手順と誌面形式は prompts 側）。",
        "",
        "> **主導銘柄は候補です**。各テーマにぶら下がる主導銘柄は最大"
        f"{LEAD_CANDIDATES}件の**候補**であり（材料テキストを持つ銘柄を優先し、"
        "その中で売買代金順）、全件を誌面へ載せません。積極支持と判定した銘柄は"
        f"**全部**表に載せ、**表は2〜{MAX_LEAD_ROWS}行**に収めてください。",
        "",
        # 2026-09-01 PM 承認の改修1。当日セクションが1〜2テーマに張り付く原因は
        # 「当日の動意上位に**当日材料**を持つ銘柄が同一テーマ2社以上」を暗黙に
        # 要求していたこと（8/28 実測で当日材料保有は100銘柄中4銘柄のみ）。
        # 直近2週間側で既に稼働している遡り材料を当日側の候補生成にも開放する。
        "> **遡り材料の扱い（当日のテーマも可）**。材料行が"
        f"`{{M/D}}時点の材料: ...` の形のものは、機械が直近{CONTEXT_LOOKBACK_DAYS}"
        "営業日以内にその銘柄が動意 raw に載った最新日の記述を遡って添えた一次情報です。"
        "**当日のテーマでもこの遡り材料で共通材料を組み立てて構いません**"
        "（当日材料が無いことだけを理由に外さないでください）。ここに並ぶ銘柄は"
        "**すべて当日の動意上位に入った銘柄**であり、当日株価が動いた事実は確認済みです。"
        "誌面の理由文で遡り材料に触れるときは `8/25に` のように日付を必ず明示してください。",
        "",
        f"> **目標件数**: `## 本日のテーマ` は **{MAX_ROWS_TODAY}テーマ**を目標とし、"
        "支持2銘柄未満で落とした分は次の候補へ繰り上げて補充してください"
        "（候補を検証し尽くしても届かない場合のみ、届いた件数で確定）。",
        "",
        "> **帰属判定**: 各主導銘柄の材料を読み、その銘柄が実際に動いた材料が属する"
        "テーマ1つにだけ帰属させてください。`重複掲載:` の注記が付いた銘柄は複数候補へ"
        "同時に載っています。材料と合わないテーマからは外し、同一銘柄を複数テーマの"
        "主導銘柄として重複掲載しないでください（判定手順は prompts 側）。",
        "",
    ]
    dup_map = build_duplicate_map(rows, material_lookup=material_lookup)
    for i, r in enumerate(rows, 1):
        _label = format_theme_label(r)
        lines.append(
            f"{i}. **{i}位 {format_theme_label(r)}**（スコア {r['score']:.0f}"
            f"・点灯{len({str(c.get('code')) for c in r['codes']})}銘柄）"
        )
        for c in order_lead_codes(r["codes"], material_lookup, LEAD_CANDIDATES):
            code = str(c.get("code", ""))
            name = str(c.get("name") or "").strip()
            try:
                pct = f"（{float(c.get(pct_key)):+.1f}%）"
            except (TypeError, ValueError):
                pct = ""
            lines.append(f"   - {code} {name}{pct}".rstrip())
            # 「何の会社」欄の素材（誌面の主導銘柄表にそのまま入る）。取れない銘柄は
            # 行を出さず、誌面側では raw 内の `**何の会社**` 記述から書かせる（_cr §38）。
            if desc_lookup is not None:
                try:
                    _d = clean_business_desc(desc_lookup(code) or "")
                except Exception:
                    _d = ""
                if _d:
                    lines.append(f"     - 何の会社: {_d}")
            # 他候補にも主導銘柄として載っている場合の注記（誤帰属・重複掲載の検出用）。
            _dup = _dup_note(code, _label, dup_map)
            if _dup:
                lines.append(f"     - {_dup}")
            items: list = []
            if material_lookup is not None:
                try:
                    items = list(material_lookup(code) or [])
                except Exception:
                    items = []
            if items:
                for it in items[:3]:
                    lines.append(f"     - 材料: {str(it).strip()}")
            else:
                lines.append(
                    f"     - 材料: raw 内の見出し `### {code} {name}` を参照".rstrip()
                )
    lines.append("")
    return lines


def render_today_section(
    result: dict,
    max_rows: int = MAX_ROWS_TODAY,
    desc_lookup=None,
    pct_key: str = "return_pct",
) -> list[str]:
    """`## 本日のテーマ` のブロック骨組み（旧形式・互換のため残す）。

    現行の動意／PTS raw は render_today_candidates を使い、誌面は Claude が組み立てる。
    """
    lines = ["## 本日のテーマ", ""]
    rows = refine_rows_by_display(
        result.get("rows") or [],
        lambda r: {
            str(c.get("code")) for c in (r.get("codes") or [])[:MAX_LEAD_ROWS]
        },
        max_rows=max_rows,
    )
    if not rows:
        lines += ["本日の点灯なし", ""]
        return lines
    for _rank, r in enumerate(rows, 1):
        lines.append(f"**{_rank}位 {format_theme_label(r)}**")
        lines.append("")
        lines += _lead_stock_table(r["codes"], desc_lookup, pct_key)
    return lines


def _theme_group_names(row: dict) -> set[str]:
    """その行が代表しているテーマ名の集合（代表名＋統合された併記名）。"""
    return {row.get("theme")} | set(row.get("merged_names") or [])


def select_heat_rows(
    heat_result: dict,
    today_result: dict | None = None,
    max_rows: int = MAX_ROWS_HEAT,
) -> list[dict]:
    """熱量降順で max_rows 件を採り、当日1位テーマを必ず含める（2026-08-31 PM 確定）。

    当日スコア1位のテーマが熱量順で max_rows から漏れる場合、最下段の行を落として
    その行を最下段へ割り込ませる。当日1位が熱量表そのものに存在しない（減衰で除外された
    等）場合は何もしない。
    """
    rows = list(heat_result.get("rows") or [])
    picked = rows[:max_rows]
    if not today_result:
        return picked

    top_today = (today_result.get("rows") or [])
    if not top_today:
        return picked
    want = _theme_group_names(top_today[0])

    def _hit(r: dict) -> bool:
        return bool(_theme_group_names(r) & want)

    if any(_hit(r) for r in picked):
        return picked
    forced = next((r for r in rows if _hit(r)), None)
    if forced is None:
        return picked
    if len(picked) < max_rows:
        return picked + [forced]
    return picked[: max_rows - 1] + [forced]


def today_shown_names(today_result: dict | None, max_rows: int = MAX_ROWS_TODAY) -> set[str]:
    """当日セクションへ掲載される見込みのテーマ名の集合（統合併記名を含む）。

    raw では「当日候補の上位 max_rows 件」を掲載見込みとみなす。誌面で Claude が
    行を落として繰り上げた場合の最終判定は _cr §38 の手順で行う。
    """
    names: set[str] = set()
    for r in (today_result or {}).get("rows", [])[:max_rows]:
        names |= _theme_group_names(r)
    return {n for n in names if n}


def select_heat_rows_v2(
    heat_result: dict,
    today_result: dict | None = None,
    max_rows: int = MAX_ROWS_HEAT,
) -> list[dict]:
    """v12（2026-09-01 PM 承認）: **純粋な継続性順**で採る（降格ペナルティ撤廃）。

    (a) 並びの主軸は sustain（**当日を除く**直近10営業日の点灯日数 × 平均点灯日スコア）。
        点灯日数を掛ける構造により、単日だけ急騰したテーマ（点灯日数 1 前後）は
        上位化しない。compute_theme_heat（旧指標）の結果を渡された場合は sustain が
        無いので heat へフォールバックし、従来どおり動く。
    (b) 当日セクションへ載る見込みのテーマにも順位ペナルティを**掛けない**（v11 撤廃）。
        当日に点灯した継続テーマが2週間側から消える副作用を断つ。`today_shown` フラグは
        誌面の【当日掲載済み】注記のために立てるだけで、並び順へ影響させない。
    """
    rows = list(heat_result.get("rows") or [])
    if not rows:
        return []
    shown = today_shown_names(today_result)
    for r in rows:
        r["today_shown"] = bool(_theme_group_names(r) & shown)
        r["_rank_key"] = float(r.get("sustain", r.get("heat", 0.0)) or 0.0)
    rows.sort(key=lambda r: (-r["_rank_key"], -float(r.get("heat", 0.0)), r["theme"]))

    # 誌面に出る銘柄（主導銘柄表の上位 MAX_LEAD_ROWS 件）で判定する。
    def _disp(r: dict) -> set:
        return {str(c.get("code")) for c in (r.get("codes") or [])[:MAX_LEAD_ROWS]}

    # 2026-09-04 PM 指示: 銘柄の絞り込み後に「表示ベース統合 → 新規性判定」を再適用し、
    # 行数が変化しなくなるまで繰り返す（絞り込みで表示銘柄が減って初めて見える重複を畳む）。
    # 新規性フィルタ: 上位行に未掲載の銘柄が MIN_NEW_CODES 件未満の行は既出銘柄の
    # 寄せ集めで新しい情報を足さないため落とし、次点を繰り上げる。
    out = filter_new_codes(rows, _disp, max_rows=max_rows)
    for _ in range(MERGE_REFINE_PASSES):
        before = len(out)
        out = merge_by_display_overlap(out, _disp, score_key="heat")
        for r in out:
            r["codes"] = sorted(
                r.get("codes") or [], key=lambda c: -float(c.get("turnover") or 0)
            )
        out.sort(
            key=lambda r: (
                -float(r.get("_rank_key", 0.0) or 0.0),
                -float(r.get("heat", 0.0) or 0.0),
                str(r.get("theme") or ""),
            )
        )
        out = filter_new_codes(out, _disp, max_rows=max_rows)
        if len(out) == before:
            break
    return out


def lit_days_str(row: dict) -> str:
    """「10日中7日点灯」表記。当日を除く直近 lit_window 営業日が母数。"""
    w = int(row.get("lit_window") or 0)
    if not w:
        return ""
    return f"{w}日中{int(row.get('lit_days') or 0)}日点灯"


def heat_delta_str(row: dict) -> str:
    """前2週比を「+146 / ±0 / -12」形式で返す。"""
    d = row.get("delta") or 0.0
    return "±0" if abs(d) < 0.05 else f"{d:+.0f}"


def render_heat_section(
    result: dict,
    max_rows: int = MAX_ROWS_HEAT,
    today_result: dict | None = None,
    desc_lookup=None,
    pct_key: str = "return_pct",
    candidates: int = HEAT_CANDIDATES,
    material_lookup=None,
    own_theme_lookup=None,
) -> list[str]:
    """`## 直近2週間の熱いテーマ` をテーマごとのブロック形式で返す（2026-08-31 PM 確定）。

    旧形式（テーマ/局面/熱量/前2週比/主導銘柄/動いた理由 の6列表）は、主導銘柄セルへ
    複数銘柄を縦積みするため長い銘柄名が折り返し、理由セルが1文しか無い分の余白が
    大きく空いていた（PM 指摘「スカスカの項目がある表が嫌い」）。

    新形式は1テーマ=1ブロック:
        **{テーマ名}**｜局面 {局面}・熱量 {熱量}・前2週比 {Δ}
        {共通理由1文（Claude が書く）}
        | コード | 銘柄名 | 何の会社 | 騰落率 |   ← 全セルが1行で収まる密な表

    並びは熱量降順。当日1位テーマは today_result を渡すと必ず含める。

    2026-08-31 PM 指示（帰属精度の改善）により、raw には誌面掲載数（max_rows）より
    多い candidates 件を出す。誌面を書く Claude は熱量上位から順に「主導銘柄の材料が
    そのテーマに合っているか」を検証し、合わない行を落として次の熱量候補へ**繰り上げる**
    （表がスカスカ・短くなりすぎないようにするため）。掲載は熱量降順・当日1位テーマの
    保証を維持する。機械側は取捨選択をしない。
    """
    lines = ["## 直近2週間の熱いテーマ", ""]
    pool = max(int(candidates or 0), max_rows)
    # v11（2026-09-01 PM 承認）: 継続性順＋当日掲載テーマの降格で候補を採る。
    # sustain 列が無い（旧 compute_theme_heat の結果）場合は heat へフォールバックする。
    rows = select_heat_rows_v2(result, today_result, pool)
    if not rows:
        lines += ["本日の点灯なし", ""]
        return lines
    lines += [
        f"> 熱量降順の候補 {len(rows)} 件です（誌面は上位 {max_rows} テーマ**目標**）。"
        "各テーマの主導銘柄の材料がそのテーマに合っているかを上から順に検証し、"
        "支持銘柄が2社未満になった行は落として次の候補へ繰り上げてください"
        f"（{max_rows} テーマに届くまで繰り上げます）。"
        "`重複掲載:` の注記が付いた銘柄は他テーマにも載っています（判定手順は prompts 側）。",
        "",
        "> **【当日掲載済み】の注記が付いたテーマは `## 本日のテーマ` にも載る見込みです**。"
        "v12（2026-09-01 PM 承認）で降格ペナルティは撤廃済みであり、当日掲載を理由に"
        "**順位を下げたり最下段へ回したりしないでください**。並び順の主軸は**純粋な継続性**"
        "（当日を除く直近10営業日の点灯日数×平均スコア）であり、候補順を当日掲載の有無で"
        "入れ替えません。重複は許容し、重複掲載したテーマの理由文へ「本日のテーマにも掲載"
        "している」旨を明記してください。ただし**掲載3件のすべてが当日と同一テーマで"
        "埋まることだけは禁止**し、その場合のみ最下位1件を注記の無い次候補へ置き換えます。"
        "見出しの `{N}日中{M}日点灯` はそのまま誌面へ転記してください。",
        "",
        f"> **主導銘柄は候補です**。各テーマの4列表は最大 {LEAD_CANDIDATES} 件の**候補**"
        "であり（テーマ構成銘柄のうち直近10営業日の動意に登場した銘柄を、材料テキストを"
        "持つものから優先し、その中で累計売買代金順に並べたもの）、全件を誌面へ載せません。"
        f"積極支持と判定した銘柄は**全部**表に載せ、**表は2〜{MAX_LEAD_ROWS} 行**に"
        "収めてください（支持が2社未満なら行ごと落として繰り上げ）。",
        "",
    ]
    dup_map = build_duplicate_map(rows, material_lookup=material_lookup)
    for _rank, r in enumerate(rows, 1):
        label = format_theme_label(r)
        # 2026-09-02 PM 承認の改修4（段階導入）: 継続性の計算は辞書ベースのままだが、
        # 過去に Claude が材料から作った自前テーマと主導銘柄集合が一致する場合は
        # 自前テーマ名を優先表示する（辞書タグ名は括弧で併記して出所を残す）。
        if own_theme_lookup is not None:
            try:
                _own = own_theme_lookup([str(c.get("code")) for c in (r.get("codes") or [])])
            except Exception:
                _own = None
            if _own and _own != label:
                label = f"{_own}（辞書名: {label}）"
        # 見出し先頭に掲載順位（熱量降順・当日1位保証で割り込んだ行も掲載位置の順位）を付す。
        # 2026-08-31 PM 指示。誌面で行を落として繰り上げた場合は誌面の掲載位置で振り直す
        # （振り直しの指示は _cr §38）。
        parts = [f"局面 {r['phase']}"]
        lit = lit_days_str(r)
        if lit:
            parts.append(lit)
        parts.append(f"熱量 {r['heat']:.0f}")
        parts.append(f"前2週比 {heat_delta_str(r)}")
        head = f"**{_rank}位 {label}**｜" + "・".join(parts)
        if r.get("today_shown"):
            head += "　【当日掲載済み】"
        lines.append(head)
        lines.append("")
        # 共通理由の1文は Claude が書く（raw では空行のプレースホルダを置かない）。
        if r["codes"]:
            lines += _lead_stock_table(
                r["codes"], desc_lookup, pct_key, material_lookup=material_lookup
            )
            dup_lines = []
            for c in order_lead_codes(r["codes"] or [], material_lookup, LEAD_CANDIDATES):
                note = _dup_note(str(c.get("code", "")), label, dup_map)
                if note:
                    dup_lines.append(f"- {c.get('code')} {c.get('name') or ''} — {note}".rstrip())
            if dup_lines:
                lines += dup_lines + [""]
        else:
            lines += ["（主導銘柄なし）", ""]
    return lines


def render_reason_material(
    today_result: dict,
    heat_result: dict | None,
    material_lookup,
    max_rows_today: int = TODAY_CANDIDATES,
    max_rows_heat: int = HEAT_CANDIDATES,
) -> list[str]:
    """`### 理由素材（Claude 転記用・誌面には出さない）` を返す。

    当日候補は render_today_candidates が銘柄ごとに材料を持つため、本ブロックは主に
    熱量表の主導銘柄を補う。重複した銘柄は1回だけ出す。

    Args:
        material_lookup: code -> list[str] を返す callable。呼び出し側（動意 / PTS）が
            raw 内に既にある動意理由テキスト（TDNet 開示タイトル・カブラボ解説・
            Yahoo ニュース見出し等）を渡す。
    """
    rows = (today_result.get("rows") or [])[:max_rows_today]
    codes = _lead_code_ids(rows, material_lookup)
    if heat_result:
        heat_rows = select_heat_rows(heat_result, today_result, max_rows_heat)
        codes += [c for c in _lead_code_ids(heat_rows, material_lookup) if c not in codes]
    if not codes:
        return []

    lines = ["### 理由素材（Claude 転記用・誌面には出さない）", ""]
    name_by_code: dict[str, str] = {}
    for r in rows + ((heat_result or {}).get("rows") or []):
        for c in r.get("codes", []):
            name_by_code.setdefault(str(c.get("code")), str(c.get("name") or ""))

    for code in codes:
        items = []
        try:
            items = list(material_lookup(code) or [])
        except Exception:
            items = []
        head = f"- **{code} {name_by_code.get(code, '')}**".rstrip()
        if items:
            lines.append(head)
            for it in items[:5]:
                lines.append(f"  - {str(it).strip()}")
        else:
            # 機械抽出できない場合は raw 内の該当ブロックへのポインタを書く
            lines.append(f"{head}: raw 内の見出し `### {code} {name_by_code.get(code, '')}` を参照")
    lines.append("")
    return lines


def render_internal_flags(result: dict) -> list[str]:
    """内部フラグ行（レポート本文には出さない・*_internal_flags.txt 用）。"""
    note = result.get("stale_note")
    return [f"[theme_radar] {note}"] if note else []


# --------------------------------------------------------------------------
# テーマレポート用の自前順位 API（2026-09-02 PM 承認）
# みんかぶ急上昇/人気ランキングの転記をやめ、動意母集団から自前で順位を作る。
#   急上昇 Top10 相当 = 当日スコア順（score_one_day / detect_today）
#   人気   Top10 相当 = 継続性順（compute_theme_heat_v2 の sustain 降順）
# 誌面には各テーマの点灯日数・前2週比・局面を必ず添える。
# --------------------------------------------------------------------------
def theme_stats_lookup(
    codes_today=None,
    history_parquet: Path | str | None = None,
    trade_date=None,
    theme_master_path: Path | str | None = None,
    window: int = HEAT_WINDOW_DAYS,
):
    """テーマ名 -> {lit_days, lit_window, sustain, heat, prev_heat, delta, phase} を返す。

    compute_theme_heat / compute_theme_heat_v2 は上位 HEAT_CANDIDATE_POOL 件へ
    切り詰めるため、当日スコア上位のテーマでも統計が欠ける（誌面の「点灯日数」が
    n/a になる）。本関数は切り詰めずに全テーマ分を素で計算して返す。
    """
    code_to_themes, _ts, _sn, _ex = load_theme_map(theme_master_path)
    hist = _load_history(history_parquet)
    end = str(trade_date) if trade_date else datetime.now(JST).date().isoformat()

    if codes_today:
        rows_today = []
        for r in codes_today:
            if not r.get("code"):
                continue
            try:
                if float(r.get("return_pct") or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            rows_today.append({
                "code": str(r.get("code")), "name": r.get("name") or "",
                "return_pct": r.get("return_pct"), "turnover": r.get("turnover"),
                "market": r.get("market"), "date": end,
            })
        if rows_today:
            if not hist.empty:
                hist = hist[hist["date"] != end]
            hist = pd.concat([hist, pd.DataFrame(rows_today)], ignore_index=True)
    if hist.empty:
        return {}

    dates = sorted(d for d in hist["date"].unique() if d <= end)
    cur_dates = dates[-window:]
    prev_dates = dates[-(window * 2):-window]
    past_dates = [d for d in dates if d != end][-window:]

    def _sum_scores(day_list):
        acc: dict[str, float] = defaultdict(float)
        for d in day_list:
            day = hist[hist["date"] == d].drop_duplicates(subset=["code"]).to_dict("records")
            for t, v in score_one_day(day, code_to_themes).items():
                acc[t] += float(v.get("score") or 0.0)
        return acc

    cur = _sum_scores(cur_dates)
    prev = _sum_scores(prev_dates)

    lit_days: dict[str, int] = defaultdict(int)
    lit_score: dict[str, float] = defaultdict(float)
    for d in past_dates:
        day = hist[hist["date"] == d].drop_duplicates(subset=["code"]).to_dict("records")
        for t, v in score_one_day(day, code_to_themes).items():
            if len(v.get("codes") or []) >= SUSTAIN_MIN_CODES:
                lit_days[t] += 1
                lit_score[t] += float(v.get("score") or 0.0)

    today_day = hist[hist["date"] == end].drop_duplicates(subset=["code"]).to_dict("records")
    today_scores = score_one_day(today_day, code_to_themes)

    out: dict[str, dict] = {}
    for t in set(cur) | set(prev) | set(lit_days):
        days = int(lit_days.get(t, 0))
        avg = (lit_score.get(t, 0.0) / days) if days else 0.0
        heat = float(cur.get(t, 0.0))
        prev_h = float(prev.get(t, 0.0))
        lit_today = len((today_scores.get(t) or {}).get("codes") or []) >= MIN_CODES_FOR_ALERT
        out[t] = {
            "lit_days": days,
            "lit_window": len(past_dates),
            "sustain": float(days) * avg,
            "heat": heat,
            "prev_heat": prev_h,
            "delta": heat - prev_h,
            "phase": _phase(heat, prev_h, lit_today),
        }
    return out


def _attach_stats(rows: list[dict], stats: dict) -> list[dict]:
    """行（統合テーマ含む）へ lit_days / delta / phase を代表値で付ける。"""
    for r in rows:
        group = _theme_group_names(r)
        cands = [stats[t] for t in group if t in stats]
        if not cands:
            r.setdefault("lit_days", 0)
            r.setdefault("lit_window", 0)
            r.setdefault("sustain", 0.0)
            r.setdefault("delta", 0.0)
            r.setdefault("phase", "")
            continue
        best = max(cands, key=lambda s: (s["lit_days"], s["sustain"]))
        r["lit_days"] = best["lit_days"]
        r["lit_window"] = best["lit_window"]
        r["sustain"] = best["sustain"]
        r["heat"] = max(s["heat"] for s in cands)
        r["prev_heat"] = max(s["prev_heat"] for s in cands)
        r["delta"] = r["heat"] - r["prev_heat"]
        r["phase"] = best["phase"]
    return rows


def rank_themes_own(
    codes_today,
    history_parquet: Path | str | None = None,
    trade_date=None,
    theme_master_path: Path | str | None = None,
    top_n: int = 10,
):
    """テーマレポート用の自前ランキングを返す（みんかぶ順位の置き換え）。

    Returns:
        {"trade_date": str,
         "rise":    [ {rank, theme, merged_names, score, codes, lit_days, lit_window,
                       delta, phase, sustain}, ... ]   # 当日スコア順（急上昇 Top10 相当）
         "popular": [ 同じ形。sustain 降順（人気 Top10 相当） ],
         "stale_note": str|None}
    """
    end = str(trade_date) if trade_date else datetime.now(JST).date().isoformat()
    stats = theme_stats_lookup(
        codes_today, history_parquet=history_parquet, trade_date=end,
        theme_master_path=theme_master_path,
    )

    today = detect_today(codes_today, theme_master_path=theme_master_path)
    rise = _attach_stats([dict(r) for r in today["rows"][:top_n]], stats)
    for i, r in enumerate(rise, 1):
        r["rank"] = i

    heat = compute_theme_heat_v2(
        codes_today, history_parquet=history_parquet, trade_date=end,
        theme_master_path=theme_master_path,
    )
    popular = _attach_stats([dict(r) for r in heat.get("rows", [])[:top_n]], stats)
    for i, r in enumerate(popular, 1):
        r["rank"] = i

    return {
        "trade_date": end,
        "rise": rise,
        "popular": popular,
        "stale_note": today.get("stale_note"),
    }
