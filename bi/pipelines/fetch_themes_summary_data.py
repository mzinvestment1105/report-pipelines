"""テーマレポートの raw データ（テーマ Top10 × 代表銘柄）を組み立てる。

2026-09-02 PM 承認: 順位の出所を **みんかぶランキングの転記から自前ロジックへ置換**。
  - 「急上昇テーマ Top10」 = 自前の**当日スコア順**（[theme_radar.py](theme_radar.py)
    `detect_today` / `score_one_day`。動意母集団 = 売買代金5億円以上・時価総額100億円以上・
    上昇銘柄／グロース・スタンダード全件＋プライム点数上位50）
  - 「人気テーマ Top10」（金土日のみ） = 自前の**継続性順**（`compute_theme_heat_v2` の
    sustain 降順）
  各テーマに点灯日数（10日中N日点灯）・前2週比・局面を付ける。
  みんかぶの順位は **参考列 `minkabu_rank` として raw に残す**（誌面には出さない・比較検証用）。

使い方:
  python fetch_themes_summary_data.py

前提:
  - bi/outputs/analysis/theme_radar/movers_top100_daily.parquet が最新
    （[make_mover_report.py](make_mover_report.py) が日次で append）
  - bi/outputs/theme_master_minkabu.parquet（テーマタグ表）
  - bi/outputs/screening_master.parquet（社名・市場・時価総額の付与）
  - bi/outputs/theme_momentum.parquet（参考列 minkabu_rank 用。無くても止めない）

出力:
  bi/outputs/themes_summary_top5.json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import theme_radar as T

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[2]

MOMENTUM = REPO_ROOT / "bi" / "outputs" / "theme_momentum.parquet"
SCREENING = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"
OUT = REPO_ROOT / "bi" / "outputs" / "themes_summary_top5.json"

TOP_N = 10
# 代表銘柄は点灯銘柄（当日そのテーマへ資金が入った銘柄）の売買代金降順から
TOP_STOCKS_PER_THEME = 5


def _load_history_path() -> Path:
    return Path(str(T.MOVERS_HISTORY_PATH))


def _codes_for_date(hist: pd.DataFrame, target: str) -> list[dict]:
    day = hist[hist["date"].astype(str) == target]
    return [
        {
            "code": str(r["code"]),
            "name": r.get("name") or "",
            "return_pct": r.get("return_pct"),
            "turnover": r.get("turnover"),
            "market": r.get("market"),
        }
        for _, r in day.iterrows()
    ]


def _minkabu_ranks(target: str) -> dict[str, dict[str, int]]:
    """参考列用。{rank_type: {theme_name: rank}}。取得できなければ空 dict。"""
    out: dict[str, dict[str, int]] = {"rise": {}, "popular": {}}
    try:
        mom = pd.read_parquet(MOMENTUM)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] theme_momentum 参考列を取得できません: {e}")
        return out
    mom["snapshot_date"] = mom["snapshot_date"].astype(str)
    sub = mom[(mom["snapshot_date"] == target) & (mom["source"] == "minkabu")]
    if sub.empty:
        latest = mom["snapshot_date"].max()
        print(f"  [NOTE] みんかぶ参考列: 当日 {target} 分なし（最新 {latest}）。参考列は空にする")
        return out
    for rt in ("rise", "popular"):
        g = sub[sub["rank_type"] == rt].sort_values("rank")
        out[rt] = {str(r["theme_name"]): int(r["rank"]) for _, r in g.iterrows()}
    return out


def main() -> int:
    target = os.environ.get("TARGET_DATE", "") or datetime.now(JST).date().isoformat()

    hist_path = _load_history_path()
    if not hist_path.exists():
        print(f"[BLOCKED] 動意母集団の履歴が見つかりません: {hist_path}"
              f"（make_mover_report.py の当日実行を確認）")
        return 1
    hist = pd.read_parquet(hist_path)
    hist["date"] = hist["date"].astype(str)
    latest = hist["date"].max()
    print(f"latest movers history: {latest}  (target {target})")

    # 対象日ゲート（exit 3 = 品質ゲート）。max() 素通りで前日母集団を当日扱いしない。
    if latest != target:
        print(f"[QUALITY GATE] movers history の最新 {latest} が対象日 {target} と不一致のため exit 3"
              f"（make_mover_report.py の当日実行を確認）")
        return 3

    codes_today = _codes_for_date(hist, target)
    if not codes_today:
        print(f"[QUALITY GATE] {target} の動意母集団が0件のため exit 3")
        return 3
    print(f"母集団: {len(codes_today)} 銘柄")

    ranked = T.rank_themes_own(
        codes_today, history_parquet=hist_path, trade_date=target, top_n=TOP_N
    )
    mk = _minkabu_ranks(target)

    scr = pd.read_parquet(SCREENING)
    scr["Code"] = scr["Code"].astype(str).str.replace(".0", "", regex=False).str.zfill(4)
    scr_map = scr.set_index("Code")[
        ["CompanyName", "MarketCodeName", "Close", "MarketCap"]
    ].to_dict("index")

    def _theme_entry(row: dict, rank_type: str) -> dict:
        name = row["theme"]
        # 点灯銘柄（売買代金降順・detect_today / compute_theme_heat が整列済み）
        seen: dict[str, dict] = {}
        for c in row.get("codes") or []:
            seen.setdefault(str(c.get("code")), c)
        top5 = []
        for c in list(seen.values())[:TOP_STOCKS_PER_THEME]:
            code = str(c.get("code"))
            info = scr_map.get(code, {})
            mcap = info.get("MarketCap")
            top5.append({
                "code": code,
                "stock_name_minkabu": c.get("name") or info.get("CompanyName"),
                "company_name": info.get("CompanyName"),
                "market": info.get("MarketCodeName"),
                "close": info.get("Close"),
                "mcap_okuyen": round(mcap / 1e8, 1) if mcap else None,
                "return_pct": c.get("return_pct"),
            })
        # 参考列: みんかぶ順位（統合テーマは構成名のいずれかで一致すれば拾う）
        group = T._theme_group_names(row)
        mk_rise = min((mk["rise"][t] for t in group if t in mk["rise"]), default=None)
        mk_pop = min((mk["popular"][t] for t in group if t in mk["popular"]), default=None)
        return {
            "rank_type": rank_type,
            "rank": int(row.get("rank") or 0),
            "theme_name": name,
            "merged_names": row.get("merged_names") or [],
            "theme_size": row.get("theme_size"),
            "score": round(float(row.get("score") or row.get("heat") or 0.0), 2),
            "lit_days": row.get("lit_days"),
            "lit_window": row.get("lit_window"),
            "lit_days_str": T.lit_days_str(row),
            "delta": round(float(row.get("delta") or 0.0), 1),
            "delta_str": T.heat_delta_str(row),
            "phase": row.get("phase") or "",
            "sustain": round(float(row.get("sustain") or 0.0), 1),
            "lit_count_today": len(seen),
            # --- 参考列（誌面には出さない・みんかぶとの比較検証用） ---
            "minkabu_rank_rise": mk_rise,
            "minkabu_rank_popular": mk_pop,
            "top5": top5,
        }

    results: list[dict] = []
    print("\n=== 人気テーマ Top10（自前・継続性順） ===")
    for r in ranked["popular"]:
        print(f"  [popular#{r['rank']}] {r['theme']}  {T.lit_days_str(r)}  {T.heat_delta_str(r)}")
        results.append(_theme_entry(r, "popular"))

    print("\n=== 急上昇テーマ Top10（自前・当日スコア順） ===")
    for r in ranked["rise"]:
        print(f"  [rise#{r['rank']}] {r['theme']}  {T.lit_days_str(r)}  {T.heat_delta_str(r)}")
        results.append(_theme_entry(r, "rise"))

    out_data = {
        "snapshot_date": target,
        "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
        "ranking_source": "own_theme_radar_v14",
        "ranking_definition": {
            "rise": "当日スコア順（score_one_day / detect_today）",
            "popular": "継続性順（compute_theme_heat_v2 の sustain 降順）",
            "universe": "売買代金5億円以上・時価総額100億円以上・上昇銘柄／"
                        "グロース・スタンダード全件＋プライム点数上位50",
        },
        "universe_count": len(codes_today),
        "stale_note": ranked.get("stale_note"),
        "themes": results,
    }
    OUT.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
