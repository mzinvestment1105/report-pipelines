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

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
JST = timezone(timedelta(hours=9))

# テーマタグ表（みんかぶ・月次更新）
THEME_MASTER_PATH = BASE_DIR / ".." / "outputs" / "theme_master_minkabu.parquet"

# 動意上位100銘柄の日次蓄積先
MOVERS_HISTORY_PATH = (
    BASE_DIR / ".." / "outputs" / "analysis" / "theme_radar" / "movers_top100_daily.parquet"
)

# 銘柄コンテキスト（「何の会社」＋「なぜ動いた」材料）の日次蓄積先。
# 2026-08-31 PM 指示。当日の動意上位100銘柄しか EDINET 事業概要・材料テキストを取得しない
# ため、熱量テーマの主導銘柄が当日 Top100 圏外だと「何の会社」が空欄（―）になり、材料も
# 取れず「材料不明」で一律除外されていた。本 parquet に日次で蓄積し、当日取れない銘柄は
# 直近 CONTEXT_LOOKBACK_DAYS 営業日以内の記述を日付付きで再利用する。
# 本番 parquet への列追加ではなく独立ファイル（file_safety_rules 準拠）。
STOCK_CONTEXT_PATH = (
    BASE_DIR / ".." / "outputs" / "analysis" / "theme_radar" / "stock_context_daily.parquet"
)

# スクリーニングマスター（業種名の最終フォールバック用・読み取りのみ）
SCREENING_MASTER_PATH = BASE_DIR / ".." / "outputs" / "screening_master.parquet"

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
# 各部の最大表示行数
# 「本日のテーマ」は機械が確定させない。機械は候補を TODAY_CANDIDATES 件まで出し、
# レポート作成 Claude が「主導銘柄2社以上に共通する材料があるテーマ」だけを残して
# 最大 MAX_ROWS_TODAY 行へ絞り込む（誌面の最終行数は Claude 判定後の結果）。
# v12（2026-09-01 PM 承認）: 当日3件・2週間3件を**目標**とする。
# 材料が積極的にテーマを支持する銘柄が2社未満のテーマは落とすため、材料が無い日は
# 3件に届かなくてよい（水増し禁止）。
MAX_ROWS_TODAY = 3
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
            }
        )
    return merged


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
    for r in rows:
        r["codes"] = sorted(
            r["codes"], key=lambda c: -float(c.get("turnover") or 0)
        )
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
    """
    out = [
        "| コード | 銘柄名 | 何の会社 | 騰落率 |",
        "|---|---|---|---|",
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
        out.append(f"| {code} | {name} | {desc or '（要記入）'} | {_pct_str(c, pct_key)} |")
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
    rows = (result.get("rows") or [])[:max_rows]
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
    rows = (result.get("rows") or [])[:max_rows]
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
    return rows[:max_rows]


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
