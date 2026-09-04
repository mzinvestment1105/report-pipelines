#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D ラボ論点索引から、SNS 短文の素材候補を機械選定する。

用途:
    ノクトラ X の D ラボレーン（雑学・心理学）を毎日自動供給するための「素材出し」。
    本スクリプトは文章を書かない。knowledge/dlab/dlab.db（16,889 論点）から
    型フラグで候補を絞り、原文チャプターの該当箇所を切り出して JSON に出す。
    実際の短文化は後段（Claude）が原文抜粋を読んで行う。

設計（knowledge/dlab/instructions/05_write_short.md に準拠）:
    1. 型フラグで機械選別する
       - form   : D（通説否定）を最優先。次に A / B / E。form='-' は短文にしない。
       - conf   : S（研究・データ断定）を優先。W（経験則）は減点、R（一次未確認）は既定で除外。
       - fresh  : E（普遍）を優先。N（時事依存）は減点。
       - has_num: 数値ありを強く優先（05 §3-5 で数値は必須要素）。
       - has_mech / has_case: 加点。
    2. 原文へ必ず戻る
       chapters.line_no から前後の行を切り出して `source_excerpt` に入れる。
       索引の 40〜70 字だけで書かせると一般論になるため、原文抜粋を必ず同梱する。
       抜粋が取れない論点は候補から落とす（本文なしでは書かせない）。
    3. 重複を出さない
       dlab.db の usage_log（topic_id, used_at, output）に配信済みを記録し、
       既出 topic_id を除外する。同一 claim_id（同じ主張の別動画）も除外する。
       同一 safe_id（同じ動画）から 1 回の出力で 2 本以上選ばない。
    4. テーマを散らす
       親テーマ 17 個に対し、1 回の出力で同一親テーマは 1 本まで。
       さらに直近 N 日に配信した親テーマを減点し、話題の偏りを防ぐ。

出力: bi/outputs/dlab_picks/dlab_picks_{date}.json

使い方:
    python pick_dlab_topics.py --date 2026-09-04 --n 3
    python pick_dlab_topics.py --n 3 --record       # 選定結果を usage_log に記録する
    python pick_dlab_topics.py --n 3 --theme "睡眠と休息"
    python pick_dlab_topics.py --stats              # 残り素材数の確認だけ

注意:
    --record を付けた時のみ usage_log へ書き込む（DB を書き換える）。
    付けない場合は読み取り専用（mode=ro）で開くため、候補出しを何度試しても
    素材が消費されない。GHA では「配信が成功したあと」に記録する運用にし、
    生成失敗で素材を無駄に消費しないようにする。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "knowledge" / "dlab" / "dlab.db"
CHAPTERS_DIR = REPO_ROOT / "knowledge" / "dlab" / "chapters" / "videos"
OUT_DIR = REPO_ROOT / "bi" / "outputs" / "dlab_picks"

# 短文にしない型（05 §1「型 `-` の論点を無理に短文化しない」）
EXCLUDED_FORMS = {"-"}

# (除外) 親テーマ配下は SNS 素材にしない（チャンネル告知・政治・人物紹介など）
EXCLUDED_THEMES = {"ch_notice", "speaker_bio", "politics", "quote"}

# 原文抜粋の行数（05 §2「行番号から14行を読み」）
EXCERPT_LINES = 14
# 抜粋がこの文字数未満なら素材として不十分とみなして落とす
MIN_EXCERPT_CHARS = 120


def _connect(readonly: bool = True) -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"[ERROR] D ラボ DB が見つかりません: {DB_PATH}")
    if readonly:
        con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _ensure_usage_log(con: sqlite3.Connection) -> None:
    """usage_log が無い DB でも動くようにする（既存 DB には既にある）。"""
    con.execute(
        "CREATE TABLE IF NOT EXISTS usage_log("
        "topic_id INTEGER, used_at TEXT, output TEXT)"
    )


def score_topic(row: sqlite3.Row, theme_penalty: float = 0.0) -> float:
    """短文への向き不向きを 05 §1「選ぶ基準」の優先度に従って点数化する。"""
    s = 0.0
    # 型（通説否定が最も短文にしやすい）
    s += {"D": 5.0, "A": 3.0, "B": 2.5, "E": 2.0, "C": 1.0}.get(row["form"], 0.0)
    # 確度
    s += {"S": 4.0, "W": 1.0, "R": 0.0}.get(row["conf"], 0.0)
    # 鮮度（普遍を優先。時事依存は日を跨ぐと成立しなくなる）
    s += 1.5 if row["fresh"] == "E" else 0.0
    # 素材
    s += 4.0 if row["has_num"] else 0.0
    s += 2.0 if row["has_mech"] else 0.0
    s += 1.0 if row["has_case"] else 0.0
    # 本文が短すぎるものは具体が薄い
    if row["n_chars"] and row["n_chars"] >= 40:
        s += 1.0
    return s - theme_penalty


def read_excerpt(safe_id: str, line_no: int, n_lines: int = EXCERPT_LINES) -> str | None:
    """原文チャプターの該当行から n_lines 行を切り出す（05 §2）。"""
    path = CHAPTERS_DIR / f"{safe_id}.md"
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if line_no < 1 or line_no > len(lines):
        return None
    start = max(0, line_no - 1)
    chunk = lines[start : start + n_lines]
    text = "\n".join(chunk).strip()
    return text or None


def recent_themes(con: sqlite3.Connection, days: int) -> dict[str, int]:
    """直近 days 日に配信した親テーマの出現回数（偏り防止の減点材料）。"""
    since = (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    rows = con.execute(
        "SELECT u.topic_id FROM usage_log u WHERE substr(u.used_at,1,10) >= ?",
        (since,),
    ).fetchall()
    if not rows:
        return counts
    ids = [r["topic_id"] for r in rows]
    qmarks = ",".join("?" * len(ids))
    for r in con.execute(
        f"SELECT tt.theme, COUNT(*) n FROM topic_themes tt "
        f"WHERE tt.rank=1 AND tt.topic_id IN ({qmarks}) GROUP BY tt.theme",
        ids,
    ):
        counts[r["theme"]] = r["n"]
    return counts


def parent_of(con: sqlite3.Connection) -> dict[str, str]:
    """テーマ -> 親テーマ（親自身は自分を指す）。"""
    m: dict[str, str] = {}
    for r in con.execute("SELECT theme, parent FROM themes"):
        m[r["theme"]] = r["parent"] or r["theme"]
    return m


def fetch_candidates(
    con: sqlite3.Connection,
    *,
    theme: str | None,
    allow_r: bool,
    require_num: bool,
    pool: int,
) -> list[sqlite3.Row]:
    where = [
        "t.form NOT IN ({})".format(",".join("?" * len(EXCLUDED_FORMS))),
        "t.chapter_id IS NOT NULL",
        # 既に配信した論点は出さない
        "t.topic_id NOT IN (SELECT topic_id FROM usage_log)",
        # 同じ主張が既に配信されている claim も出さない
        "(tc.claim_id IS NULL OR tc.claim_id NOT IN ("
        " SELECT tc2.claim_id FROM topic_claims tc2"
        " JOIN usage_log u2 ON u2.topic_id = tc2.topic_id))",
    ]
    params: list[object] = list(EXCLUDED_FORMS)

    excl = sorted(EXCLUDED_THEMES)
    where.append("tt.theme NOT IN ({})".format(",".join("?" * len(excl))))
    params.extend(excl)

    if not allow_r:
        where.append("t.conf <> 'R'")
    if require_num:
        where.append("t.has_num = 1")
    if theme:
        where.append("(tt.theme = ? OR th.parent = ?)")
        params.extend([theme, theme])

    sql = f"""
        SELECT t.topic_id, t.safe_id, t.ts, t.body, t.n_chars,
               t.conf, t.fresh, t.material, t.form,
               t.has_num, t.has_mech, t.has_case,
               tt.theme AS theme, th.parent AS theme_parent,
               c.line_no AS line_no, c.heading AS heading,
               v.title AS video_title,
               tc.claim_id AS claim_id
          FROM topics t
          JOIN topic_themes tt ON tt.topic_id = t.topic_id AND tt.rank = 1
          LEFT JOIN themes th ON th.theme = tt.theme
          JOIN chapters c ON c.chapter_id = t.chapter_id
          JOIN videos v ON v.safe_id = t.safe_id
          LEFT JOIN topic_claims tc ON tc.topic_id = t.topic_id
         WHERE {' AND '.join(where)}
         ORDER BY RANDOM()
         LIMIT ?
    """
    params.append(pool)
    return con.execute(sql, params).fetchall()


def pick(
    con: sqlite3.Connection,
    *,
    n: int,
    theme: str | None,
    allow_r: bool,
    require_num: bool,
    pool: int,
    recent_days: int,
) -> list[dict]:
    parents = parent_of(con)
    recent = recent_themes(con, recent_days)

    rows = fetch_candidates(
        con, theme=theme, allow_r=allow_r, require_num=require_num, pool=pool
    )
    if not rows:
        return []

    scored: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        p = parents.get(r["theme"], r["theme"])
        # 直近に出した親テーマは 1 回につき 1.5 点減点する
        penalty = 1.5 * float(recent.get(r["theme"], 0) + recent.get(p, 0))
        scored.append((score_topic(r, penalty), r))
    scored.sort(key=lambda x: -x[0])

    picked: list[dict] = []
    used_parents: set[str] = set()
    used_videos: set[str] = set()
    used_claims: set[int] = set()

    for sc, r in scored:
        if len(picked) >= n:
            break
        p = parents.get(r["theme"], r["theme"])
        if p in used_parents:
            continue  # 1 回の出力で同一親テーマは 1 本まで
        if r["safe_id"] in used_videos:
            continue  # 同じ動画から 2 本取らない
        if r["claim_id"] is not None and r["claim_id"] in used_claims:
            continue
        excerpt = read_excerpt(r["safe_id"], r["line_no"])
        if not excerpt or len(excerpt) < MIN_EXCERPT_CHARS:
            continue  # 原文が取れない論点は書かせない（05 §2）
        picked.append(
            {
                "topic_id": r["topic_id"],
                "score": round(sc, 2),
                "body": r["body"],
                "theme": r["theme"],
                "theme_parent": p,
                "flags": {
                    "conf": r["conf"],
                    "fresh": r["fresh"],
                    "material": r["material"],
                    "form": r["form"],
                    "has_num": bool(r["has_num"]),
                    "has_mech": bool(r["has_mech"]),
                    "has_case": bool(r["has_case"]),
                },
                "safe_id": r["safe_id"],
                "ts": r["ts"],
                "video_title": r["video_title"],
                "chapter_heading": r["heading"],
                "line_no": r["line_no"],
                "source_path": f"knowledge/dlab/chapters/videos/{r['safe_id']}.md",
                "source_ref": f"{r['safe_id']} {r['ts']}",
                "source_excerpt": excerpt,
            }
        )
        used_parents.add(p)
        used_videos.add(r["safe_id"])
        if r["claim_id"] is not None:
            used_claims.add(r["claim_id"])

    return picked


def record_usage(topic_ids: list[int], output_label: str) -> int:
    """配信済みとして usage_log に記録する（DB 書き込み）。"""
    if not topic_ids:
        return 0
    con = _connect(readonly=False)
    try:
        _ensure_usage_log(con)
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        con.executemany(
            "INSERT INTO usage_log(topic_id, used_at, output) VALUES (?,?,?)",
            [(tid, now, output_label) for tid in topic_ids],
        )
        con.commit()
        return len(topic_ids)
    finally:
        con.close()


def print_stats(con: sqlite3.Connection) -> None:
    total = con.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    used = con.execute("SELECT COUNT(DISTINCT topic_id) FROM usage_log").fetchone()[0]
    avail = con.execute(
        "SELECT COUNT(*) FROM topics t "
        "JOIN topic_themes tt ON tt.topic_id=t.topic_id AND tt.rank=1 "
        "WHERE t.form <> '-' AND t.conf <> 'R' AND t.chapter_id IS NOT NULL "
        "AND tt.theme NOT IN ('ch_notice','speaker_bio','politics','quote') "
        "AND t.topic_id NOT IN (SELECT topic_id FROM usage_log)"
    ).fetchone()[0]
    print(f"論点総数        : {total:,}")
    print(f"配信済み        : {used:,}")
    print(f"素材として利用可: {avail:,}")
    print(f"1日2本ペースの残: 約 {avail // 2:,} 日分")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--date", help="出力ファイル名の日付（未指定は今日・JST）")
    ap.add_argument("--n", type=int, default=2, help="選ぶ論点の本数")
    ap.add_argument("--theme", help="テーマを固定する（親テーマ名でも子テーマ名でも可）")
    ap.add_argument("--allow-r", action="store_true", help="確度 R（一次未確認）も候補に含める")
    ap.add_argument("--no-require-num", action="store_true", help="数値なしの論点も候補に含める")
    ap.add_argument("--pool", type=int, default=1200, help="採点対象にする候補プールの件数")
    ap.add_argument("--recent-days", type=int, default=14, help="テーマ偏り減点で参照する日数")
    ap.add_argument("--record", action="store_true", help="選定結果を usage_log に記録する（DB 書き込み）")
    ap.add_argument("--record-label", default="sns_dlab", help="usage_log.output に入れるラベル")
    ap.add_argument("--out-dir", help="出力ディレクトリ（未指定は bi/outputs/dlab_picks）")
    ap.add_argument("--stats", action="store_true", help="残り素材数だけ表示して終了")
    ap.add_argument("--seed", type=int, help="乱数シード（再現用）")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    con = _connect(readonly=True)
    try:
        if args.stats:
            print_stats(con)
            return 0

        picks = pick(
            con,
            n=args.n,
            theme=args.theme,
            allow_r=args.allow_r,
            require_num=not args.no_require_num,
            pool=args.pool,
            recent_days=args.recent_days,
        )

        # 数値必須で足りない場合のみ、数値なしまで広げて補充する
        if len(picks) < args.n and not args.no_require_num:
            extra = pick(
                con,
                n=args.n,
                theme=args.theme,
                allow_r=args.allow_r,
                require_num=False,
                pool=args.pool,
                recent_days=args.recent_days,
            )
            have = {p["topic_id"] for p in picks}
            have_parents = {p["theme_parent"] for p in picks}
            for e in extra:
                if len(picks) >= args.n:
                    break
                if e["topic_id"] in have or e["theme_parent"] in have_parents:
                    continue
                picks.append(e)
                have.add(e["topic_id"])
                have_parents.add(e["theme_parent"])
    finally:
        con.close()

    date_str = args.date or datetime.now(JST).strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"dlab_picks_{date_str}.json"

    payload = {
        "meta": {
            "date": date_str,
            "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "requested": args.n,
            "returned": len(picks),
            "theme_filter": args.theme,
            "require_num": not args.no_require_num,
            "allow_r": args.allow_r,
            "recorded": bool(args.record),
        },
        "picks": picks,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.record and picks:
        n = record_usage([p["topic_id"] for p in picks], args.record_label)
        print(f"usage_log に {n} 件記録しました（label={args.record_label}）")

    print(f"出力: {out_path}")
    print(f"選定: {len(picks)}/{args.n} 件")
    for p in picks:
        print(
            f"  [{p['score']:.1f}] {p['theme_parent']} / {p['theme']} "
            f"({p['flags']['form']}{p['flags']['conf']}{p['flags']['fresh']}) "
            f"{p['body'][:48]}"
        )
    if len(picks) < args.n:
        print(f"::warning::要求 {args.n} 件に対し {len(picks)} 件しか選定できませんでした")
    return 0


if __name__ == "__main__":
    sys.exit(main())
