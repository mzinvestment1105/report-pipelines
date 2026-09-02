"""X（旧 Twitter）片思いフォローのリム候補分類と Console 解除スクリプト生成 ETL。

用途:
    運用アカウント（既定 @noctra__ai）の following / followers スナップショットを突合し、
    「こちらだけがフォローしている（片思い）」相手を抽出して 3 分類に振り分け、
    リム候補 CSV・ブラウザ Console 用の解除スクリプト（.js）・PM 向けレポート（.md）を出力する。

    読み取り専用。X へのアクセス・書き込みは一切行わない（取得済み JSON を読むだけ）。
    実際の解除操作は、生成された .js を PM がブラウザの Console に貼って手動実行する。

分類:
    A_block    … 過去に followers に在籍していたのに現在いない（リムられ層）。
                 解除スクリプトの対象から除外し、ブロック方針の別枠として報告する。
    B_grace    … フォロー日 + 猶予日数 がまだ対象日を過ぎていない（フォロバ待ち猶予中）。
                 フォロー日が幅でしか特定できない相手も、最遅日を採用して必ず猶予側へ倒す。
    C_unfollow … 猶予明けでフォロバがない。本日の解除対象。
    相互フォローは、いかなる場合も対象にしない（そもそも片思い集合に入らない）。

フォロー日の推定:
    following スナップショットは離散的にしか存在しないため、フォロー日は
    「初めて観測されたスナップショット日 D」と「その1つ前のスナップショット日 P」に挟まれた
    区間 (P, D] としてしか特定できない。判定不能は猶予側に倒す原則に従い、
    区間の最遅日 D をフォロー日とみなして unfollow_eligible_after = D + grace_days とする。

入力:
    following … 次の順で探索し、最初に見つかったものを使う（推測での代替は行わない）
                1. bi/outputs/x_posts/gha/profile_daily_{date}.json
                   （GHA 日次。2026-09-03 以降は --following 付きで following も含む）
                2. bi/outputs/x_posts/profile_following_{date}.json（_r2 / _r3 … 連番は新しい方）
                3. bi/outputs/x_posts/profile_*_{date}.json のうち following を持つもの
                いずれも見つからなければ明確なエラーで exit 1。
    followers … bi/outputs/x_posts/gha/profile_daily_{date}.json。
                当日分が無い / 空の場合は直近日へ遡り、その旨を標準出力とレポートに明記する。
    過去 following スナップショット … bi/outputs/x_posts 配下（gha 含む）の profile_*.json から
                対象アカウントの following を持つものを自動収集（フォロー日推定用）。
    過去 followers スナップショット  … 同じ走査で followers を持つものを収集
                （A_block の在籍履歴用。followers 0 件の欠測日は自動で除外）。
    除外リスト … bi/data/x_influencers.yaml（記載アカウントはリム候補から除外）。

使い方（対象ハンドルは引数で外から渡す。スクリプトに直書きしない）:
    python make_unfollow_candidates.py --date 2026-09-02
    python make_unfollow_candidates.py --date 2026-09-02 --dry-run   # 件数だけ表示・ファイル出力なし
    python make_unfollow_candidates.py --grace-days 3 --account noctra__ai

出力（--dry-run 時は一切出力しない）:
    bi/outputs/x_posts/oneway_follows_{date}.csv   … 片思い全件の分類結果
    bi/outputs/x_posts/unfollow_console_{date}.js  … C_unfollow の解除スクリプト（0 件なら生成しない）
    research/sns/{date}_follow_unfollow.md         … PM 向けレポート

既存ファイルは上書きしない（同名があれば中止して PM に知らせる）。
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
X_POSTS_DIR = os.path.join(REPO_ROOT, "bi", "outputs", "x_posts")
GHA_DIR = os.path.join(X_POSTS_DIR, "gha")
RESEARCH_SNS_DIR = os.path.join(REPO_ROOT, "research", "sns")
INFLUENCERS_YAML = os.path.join(REPO_ROOT, "bi", "data", "x_influencers.yaml")

CSV_FIELDS = [
    "screen_name",
    "name",
    "class",
    "class_note",
    "unfollow_eligible_after",
    "followers_count",
    "friends_count",
    "ff_ratio",
    "statuses_count",
    "bio_head",
]

BIO_HEAD_LEN = 100
MAX_PER_RUN = 10

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------- 小道具


def fail(msg: str) -> None:
    """回復不能なエラーで停止する（推測での代替データ使用はしない）。"""
    print("[中止] " + msg, file=sys.stderr)
    sys.exit(1)


def jst_today() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def parse_date(s: str) -> date_cls:
    return datetime.strptime(s, "%Y-%m-%d").date()


def add_days(s: str, n: int) -> str:
    return (parse_date(s) + timedelta(days=n)).strftime("%Y-%m-%d")


def md_slash(s: str) -> str:
    """YYYY-MM-DD → M/D 表記。"""
    d = parse_date(s)
    return "%d/%d" % (d.month, d.day)


def mmdd(s: str) -> str:
    """YYYY-MM-DD → MM-DD 表記。"""
    return s[5:]


def jp_time(iso: str) -> str:
    """ISO 8601 → 和文12時間制「YYYY-MM-DD 午前/午後H時M分」。"""
    if not iso:
        return "取得時刻不明"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "取得時刻不明"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    dt = dt.astimezone(JST)
    ampm = "午前" if dt.hour < 12 else "午後"
    h = dt.hour % 12 or 12
    return "%s %s%d時%d分" % (dt.strftime("%Y-%m-%d"), ampm, h, dt.minute)


def esc_pipe(s: str) -> str:
    """Markdown 表のセルを壊さないよう縦棒を全角へ置換し改行を潰す。"""
    return (s or "").replace("|", "｜").replace("\n", " ").replace("\r", " ").strip()


def bio_head(s: str) -> str:
    t = (s or "").replace("\n", " ").replace("\r", " ").strip()
    return t[:BIO_HEAD_LEN]


def js_str(s: str) -> str:
    return json.dumps(s or "", ensure_ascii=False)


def rel_repo(path: str) -> str:
    """レポート本文で使うリポジトリ相対パス（区切りはスラッシュ）。"""
    try:
        return os.path.relpath(path, REPO_ROOT).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


# ---------------------------------------------------------------- 入力読み込み


def load_snapshot(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pick_account(doc, account: str):
    """スナップショット JSON から対象アカウントのブロックを取り出す。"""
    if not isinstance(doc, dict):
        return None
    for a in doc.get("accounts") or []:
        if str(a.get("screen_name_requested", "")).lower() == account.lower():
            return a
    return None


def users_by_sn(users) -> dict:
    """screen_name（小文字）→ ユーザーレコードの辞書。"""
    out = {}
    for u in users or []:
        sn = u.get("screen_name")
        if sn:
            out[str(sn).lower()] = u
    return out


def snapshot_date(path: str, doc):
    """スナップショットの基準日。fetched_at を優先し、無ければファイル名の日付を使う。"""
    if doc:
        fa = doc.get("fetched_at")
        if fa:
            try:
                dt = datetime.fromisoformat(fa)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=JST)
                return dt.astimezone(JST).strftime("%Y-%m-%d")
            except ValueError:
                pass
    m = DATE_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def all_profile_files() -> list:
    """bi/outputs/x_posts 配下（gha 含む）の profile_*.json を列挙する。"""
    paths = sorted(glob.glob(os.path.join(X_POSTS_DIR, "profile_*.json")))
    paths += sorted(glob.glob(os.path.join(GHA_DIR, "profile_*.json")))
    return paths


def collect_snapshots(account: str, target_date: str):
    """対象日以前の全スナップショットを走査し、following / followers の在籍履歴を作る。

    戻り値: (following_snaps, followers_snaps)
      following_snaps … [(date, {sn: user}, path)] を日付昇順
      followers_snaps … [(date, {sn}, path)] を日付昇順（followers 0 件の欠測日は除外済み）
    """
    following_by_date = {}
    followers_by_date = {}
    for path in all_profile_files():
        doc = load_snapshot(path)
        acct = pick_account(doc, account)
        if acct is None:
            continue
        snap_date = snapshot_date(path, doc)
        if not snap_date or snap_date > target_date:
            continue
        following = acct.get("following") or []
        followers = acct.get("followers") or []
        if following:
            # 同一日に複数スナップショットがある場合は件数の多い方（全件取得側）を採る
            prev = following_by_date.get(snap_date)
            if prev is None or len(following) > len(prev[0]):
                following_by_date[snap_date] = (following, path)
        if followers:
            # followers 0 件は取得失敗（欠測）とみなし在籍履歴に入れない
            prev = followers_by_date.get(snap_date)
            if prev is None or len(followers) > len(prev[0]):
                followers_by_date[snap_date] = (followers, path)

    following_snaps = [
        (d, users_by_sn(v[0]), v[1]) for d, v in sorted(following_by_date.items())
    ]
    followers_snaps = [
        (d, set(users_by_sn(v[0]).keys()), v[1])
        for d, v in sorted(followers_by_date.items())
    ]
    return following_snaps, followers_snaps


def find_current_following(account: str, target_date: str):
    """対象日の following スナップショットを探索順に従って特定する。

    戻り値: (users, path, fetched_at, acct)。見つからなければ exit 1。
    """
    candidates = [os.path.join(GHA_DIR, "profile_daily_%s.json" % target_date)]
    candidates += sorted(
        glob.glob(os.path.join(X_POSTS_DIR, "profile_following_%s*.json" % target_date)),
        reverse=True,
    )
    candidates += sorted(
        glob.glob(os.path.join(X_POSTS_DIR, "profile_*_%s*.json" % target_date))
    )

    seen = set()
    for path in candidates:
        if path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        doc = load_snapshot(path)
        acct = pick_account(doc, account)
        if acct is None:
            continue
        following = acct.get("following") or []
        if not following:
            continue
        return following, path, doc.get("fetched_at", ""), acct

    fail(
        "%s の following スナップショットが見つかりません（対象 @%s）。\n"
        "  探索先1: %s\n"
        "  探索先2: %s\n"
        "  探索先3: %s\n"
        "  python bi/pipelines/fetch_x_profile.py --users %s --following で取得してから再実行してください。"
        % (
            target_date,
            account,
            os.path.join(GHA_DIR, "profile_daily_%s.json" % target_date),
            os.path.join(X_POSTS_DIR, "profile_following_%s.json" % target_date),
            os.path.join(X_POSTS_DIR, "profile_*_%s.json" % target_date),
            account,
        )
    )


def find_followers(account: str, target_date: str):
    """followers を GHA 日次から読む。当日が無い / 空なら直近日へ遡る。

    戻り値: (users, path, fetched_at, used_date, is_fallback)
    """
    dated = []
    for p in sorted(glob.glob(os.path.join(GHA_DIR, "profile_daily_*.json"))):
        m = DATE_RE.search(os.path.basename(p))
        if m and m.group(1) <= target_date:
            dated.append((m.group(1), p))
    dated.sort(reverse=True)

    for d, p in dated:
        doc = load_snapshot(p)
        acct = pick_account(doc, account)
        if acct is None:
            continue
        followers = acct.get("followers") or []
        if not followers:
            continue
        return followers, p, doc.get("fetched_at", ""), d, (d != target_date)

    fail(
        "%s 以前に @%s の followers を含む GHA 日次スナップショットが見つかりません（%s）。"
        % (target_date, account, os.path.join(GHA_DIR, "profile_daily_*.json"))
    )


def load_influencer_exclusions() -> set:
    """x_influencers.yaml の screen_name を除外集合として読む（PyYAML 非依存の軽量解析）。

    このファイルは fetch_x_buzz.py が「必チェックのインフルエンサー」として使う一覧で、
    運用上フォローを外したくない情報源にあたる。片思いに現れた場合はリム候補から除外する。
    """
    if not os.path.exists(INFLUENCERS_YAML):
        return set()
    out = set()
    try:
        with open(INFLUENCERS_YAML, "r", encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if not s.startswith("- "):
                    continue
                name = s[2:].strip().strip("\"'")
                if name and re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
                    out.add(name.lower())
    except OSError:
        return set()
    return out


# ---------------------------------------------------------------- 分類


def estimate_follow_window(sn: str, following_snaps: list, target_date: str):
    """フォロー日の推定区間 (下限日 or None, 最遅日) を返す。

    初めて観測されたスナップショット日 D の直前のスナップショット日 P を下限とし、
    実際のフォロー日は (P, D] のどこか。判定不能は猶予側に倒すため最遅日 D を採用する。
    どのスナップショットにも無い（= 当日分にしか居ない）場合は最遅日 = 対象日 とする。
    """
    prev_date = None
    for snap_date, users, _path in following_snaps:
        if sn in users:
            return prev_date, snap_date
        prev_date = snap_date
    return prev_date, target_date


def build_followers_history(followers_snaps: list) -> dict:
    """screen_name（小文字）→ 在籍が観測された日付リスト。"""
    hist = {}
    for snap_date, sns, _path in followers_snaps:
        for sn in sns:
            hist.setdefault(sn, []).append(snap_date)
    return hist


def classify(
    oneway_sns: list,
    current_users: dict,
    following_snaps: list,
    followers_history: dict,
    target_date: str,
    grace_days: int,
) -> list:
    """片思い各件を A_block / B_grace / C_unfollow へ振り分ける。"""
    rows = []
    for sn in oneway_sns:
        u = current_users[sn]
        past_dates = followers_history.get(sn)
        if past_dates:
            # 過去に followers に在籍していたのに現在いない = リムられ層
            first, last = past_dates[0], past_dates[-1]
            span = first if first == last else "%s〜%s" % (first, last)
            rows.append(
                {
                    "user": u,
                    "class": "A_block",
                    "class_note": "リムられ（過去フォロワー→離脱: 在籍%s）" % span,
                    "after": "",
                    "first_seen": None,
                    "lower": None,
                    "past_span": span,
                }
            )
            continue

        lower, latest = estimate_follow_window(sn, following_snaps, target_date)
        after = add_days(latest, grace_days)
        if lower:
            # 下限は「不在が確認できた直前スナップショット日」。実際のフォローはその翌日以降だが、
            # 区間表記は観測点そのものを両端に採る（下限側は開区間）。
            span = "フォロー%s〜%s" % (mmdd(lower), mmdd(latest))
            span_short = "フォロー%s〜%s" % (md_slash(lower), md_slash(latest))
        else:
            span = "フォロー%s以前" % mmdd(latest)
            span_short = "フォロー%s以前" % md_slash(latest)

        if after > target_date:
            if lower:
                note = "フォロバ待ち猶予（%s・下限不明のため最遅日 %s を採用）" % (span, mmdd(latest))
            else:
                note = "フォロバ待ち猶予（%s・最遅日 %s を採用）" % (span, mmdd(latest))
            rows.append(
                {
                    "user": u,
                    "class": "B_grace",
                    "class_note": note,
                    "after": after,
                    "first_seen": latest,
                    "lower": lower,
                    "past_span": None,
                }
            )
        else:
            rows.append(
                {
                    "user": u,
                    "class": "C_unfollow",
                    "class_note": "猶予明け・フォロバなし（%s・猶予%d日で%s到来）"
                    % (span_short, grace_days, mmdd(after)),
                    "after": after,
                    "first_seen": latest,
                    "lower": lower,
                    "past_span": None,
                }
            )
    return rows


# ---------------------------------------------------------------- 出力


def sort_rows(rows: list) -> list:
    """CSV の行順。既存の成果物に合わせ、分類名の昇順（A_block → B_grace → C_unfollow）で
    まとめたうえで screen_name の昇順（大文字小文字を無視）に並べる。"""
    return sorted(rows, key=lambda r: (r["class"], r["user"]["screen_name"].lower()))


def write_csv(path: str, rows: list) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in sort_rows(rows):
            u = r["user"]
            w.writerow(
                {
                    "screen_name": u.get("screen_name", ""),
                    "name": u.get("name", ""),
                    "class": r["class"],
                    "class_note": r["class_note"],
                    "unfollow_eligible_after": r["after"],
                    "followers_count": u.get("followers_count", ""),
                    "friends_count": u.get("friends_count", ""),
                    "ff_ratio": u.get("ff_ratio", ""),
                    "statuses_count": u.get("statuses_count", ""),
                    "bio_head": bio_head(u.get("profile_bio")),
                }
            )


def counts_desc(rows: list, key: str) -> str:
    """{日付: 件数} を「2026-08-20（17 件）・2026-08-24（13 件）」形式に整形する。"""
    c = {}
    for r in rows:
        c[r[key]] = c.get(r[key], 0) + 1
    return "・".join("%s（%d 件）" % (d, n) for d, n in sorted(c.items()))


def build_js(
    account: str,
    target_date: str,
    targets: list,
    grace_rows: list,
    oneway_total: int,
    grace_days: int,
    block_count: int,
) -> str:
    """unfollow_console_{date}.js を組み立てる（安全設計は既存版から不変）。"""
    key_suffix = target_date.replace("-", "_")
    n = len(targets)
    runs = (n + MAX_PER_RUN - 1) // MAX_PER_RUN
    after_desc = counts_desc(targets, "after")
    seen_desc = counts_desc(targets, "first_seen")

    if grace_rows:
        grace_after = sorted({r["after"] for r in grace_rows})
        grace_line = (
            " *   うち %d 件はフォロー日の下限が特定できないため猶予側に倒し（B_grace・\n"
            " *   解除可能日 %s）、本スクリプトの対象から外しています。\n"
            % (len(grace_rows), "／".join(grace_after))
        )
    else:
        grace_line = " *   猶予期間中（B_grace）の相手は今回 0 件でした。\n"

    rows_js = "\n".join(
        "    { sn: %s, after: %s },"
        % (js_str(r["user"]["screen_name"]), js_str(r["after"]))
        for r in targets
    )

    header = (
        "/* ==========================================================================\n"
        " * ノクトラ（@{account}）片思いフォロー解除スクリプト  {date} 版（対象 {n} 件）\n"
        " *\n"
        " * 【使い方 3 行】\n"
        " *   1. ブラウザで https://x.com/{account}/following を開き、ノクトラ本人でログインした状態にする\n"
        " *   2. F12 キー（または右クリック → 検証）を押して「Console」タブを開く\n"
        " *   3. このファイルの中身を全文コピーして Console に貼り付け、Enter を押す\n"
        " *\n"
        " * 【停止方法】\n"
        " *   Console に  window.__ufStop = true  と入力して Enter（次の1件に進む前に停止します）。\n"
        " *   または、その X のタブを閉じる／ページを再読み込みすれば即座に止まります。\n"
        " *\n"
        " * 【今回の対象と「猶予日」の考え方】\n"
        " *   {date} 時点の実測で、ノクトラがフォローしていて相手がフォローを返していない\n"
        " *   「片思い」は {oneway} 件でした。\n"
        "{grace_line}"
        " *   残る {n} 件が対象です。猶予は PM 指示の {grace} 日 を適用しています。\n"
        " *   この {n} 件は following の過去スナップショット（{seen_desc}）に既に載っており、\n"
        " *   最も遅く見積もってもフォロー日はその日です。そこに猶予 {grace} 日 を足すと\n"
        " *   解除可能日は {after_desc} で、いずれも本日より前です。\n"
        " *   過去にフォロワーだったのに外してきた「リムられ層」（A_block）は今回 {block} 件でした。\n"
        " *\n"
        " *   → 本日 {date} 時点で全 {n} 件が即座に対象になります。1回の実行上限は {mpr} 件\n"
        " *     （MAX_PER_RUN）のため、{n} 件を処理するには {runs} 回、同じファイルを貼り直す必要があります。\n"
        " *     処理済みの相手は localStorage に記録されるため、貼り直すたびに続きから進みます。\n"
        " *     フォローが返っていた相手は自動でスキップされるため、貼り直しは安全です。\n"
        " *\n"
        " * 【安全設計】\n"
        " *   - 1件ごとに 3〜8 秒のランダム待機（機械的な等間隔アクセスを避ける）\n"
        " *   - 1回の実行で最大 {mpr} 件まで（MAX_PER_RUN）。到達したら自動停止\n"
        " *   - 失敗が 3 件連続したら即座に全停止して警告表示\n"
        " *   - 既に解除済み（フォローしていない）相手には何も送らず「済み」として飛ばす\n"
        " *   - リスト作成後に相手からフォローが返っていた場合（フォロバ済み＝相互フォロー）は\n"
        " *     解除せず「スキップ（フォロバ済み）」として飛ばす。実行直前に X 側の最新の関係を\n"
        " *     問い合わせて判定するため、リストの鮮度落ちで相互フォローを切ってしまう事故を防ぐ\n"
        " *     （※この判定は内部 API 方式でのみ働きます。DOM 操作方式に切り替わった場合は判定できません）\n"
        " *   - 猶予日（after）前の相手は対象から自動で外れる\n"
        " *   - 処理済みの相手はブラウザの localStorage に記録するため、\n"
        " *     もう一度全文を貼り直せば「未処理の続きから」自動で再開します\n"
        " *\n"
        " * 【方式】\n"
        " *   まず screen_name から user_id を解決し（friendships/lookup.json → users/show.json の順）、\n"
        " *   X の内部 API（/i/api/1.1/friendships/destroy.json）へ POST します。\n"
        " *   認証は現在ログイン中の cookie（ct0）をブラウザ内で読むだけで、外部への送信は一切ありません。\n"
        " *   万一 API が 401/403/404 を返す場合は、DOM 操作方式（画面上の「フォロー中」ボタンを\n"
        " *   クリック → 確認ダイアログの「フォロー解除」をクリック）へ自動で切り替えます。\n"
        " *   DOM 方式は /following ページ上ならその場のカードを操作し、それ以外のページでは\n"
        " *   相手のプロフィールページを別タブで開いて操作します（同じ待機・停止・進捗表示が働きます）。\n"
        " *   ※ 別タブ方式はポップアップ許可が必要なため、/following ページで実行するのが確実です。\n"
        " * ========================================================================== */\n"
    ).format(
        account=account,
        date=target_date,
        n=n,
        oneway=oneway_total,
        grace_line=grace_line,
        grace=grace_days,
        seen_desc=seen_desc,
        after_desc=after_desc,
        block=block_count,
        mpr=MAX_PER_RUN,
        runs=runs,
    )

    return header + JS_BODY.format(rows=rows_js, mpr=MAX_PER_RUN, key=key_suffix)



# 解除エンジン本体（既存版から不変。差し替えるのは TARGET_ROWS / MAX_PER_RUN / localStorage キーのみ）
JS_BODY = r'''
(async () => {{
  "use strict";

  // ---- 対象アカウント（片思い。after = この日以降に解除対象化）--------------
  const TARGET_ROWS = [
{rows}
  ];

  // ---- 設定 ---------------------------------------------------------------
  const MAX_PER_RUN = {mpr};  // 1回の実行で処理する上限件数
  const WAIT_MIN_MS = 3000;        // 1件あたりの最小待機（3秒）
  const WAIT_MAX_MS = 8000;        // 1件あたりの最大待機（8秒）
  const MAX_CONSECUTIVE_FAIL = 3;  // 連続失敗でこの件数に達したら全停止
  const DONE_KEY = "uf_done_{key}";
  const ID_CACHE_KEY = "uf_ids_{key}";
  // X の Web アプリが実際に送信している公開 Bearer（過去スクリプトと同一）
  const BEARER =
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";

  window.__ufStop = false;

  // ---- 小道具 -------------------------------------------------------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitMs = () => Math.floor(WAIT_MIN_MS + Math.random() * (WAIT_MAX_MS - WAIT_MIN_MS));
  const getCookie = (name) => {{
    const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  }};
  const today = () => {{
    const d = new Date(Date.now() + 9 * 3600 * 1000); // JST 基準の日付
    return d.toISOString().slice(0, 10);
  }};

  const loadDone = () => {{
    try {{
      const v = JSON.parse(localStorage.getItem(DONE_KEY) || "[]");
      return new Set(Array.isArray(v) ? v.map((s) => String(s).toLowerCase()) : []);
    }} catch (e) {{
      return new Set();
    }}
  }};
  const saveDone = (set) => {{
    try {{
      localStorage.setItem(DONE_KEY, JSON.stringify([...set]));
    }} catch (e) {{
      /* localStorage が使えない環境では進捗保存だけ諦める */
    }}
  }};
  const loadIdCache = () => {{
    try {{
      const v = JSON.parse(localStorage.getItem(ID_CACHE_KEY) || "{{}}");
      return v && typeof v === "object" ? v : {{}};
    }} catch (e) {{
      return {{}};
    }}
  }};
  const saveIdCache = (obj) => {{
    try {{
      localStorage.setItem(ID_CACHE_KEY, JSON.stringify(obj));
    }} catch (e) {{
      /* 同上 */
    }}
  }};

  // ---- 事前チェック -------------------------------------------------------
  const ct0 = getCookie("ct0");
  if (!ct0) {{
    console.error(
      "[中止] cookie の ct0 を取得できませんでした。X にログインした状態の x.com のタブで実行してください。"
    );
    return;
  }}

  const TODAY = today();
  const ripe = TARGET_ROWS.filter((r) => r.after <= TODAY);
  const notYet = TARGET_ROWS.filter((r) => r.after > TODAY);

  if (ripe.length === 0) {{
    const soonest = TARGET_ROWS.map((r) => r.after).sort()[0];
    console.log(
      "[本日の対象は 0 件] 全 " + TARGET_ROWS.length +
        " 件はフォロバ待ちの猶予期間中です（本日 " + TODAY + "）。"
    );
    console.log(
      "最短で " + soonest + " から対象になります。その日以降にこのファイルをもう一度貼り付けてください。"
    );
    console.log(
      "猶予を無視して今すぐ実行したい場合は、上の TARGET_ROWS の after を全て過去日付に書き換えてから貼り付けてください。"
    );
    return;
  }}

  const TARGETS = ripe.map((r) => r.sn);
  const done = loadDone();
  const idCache = loadIdCache();
  const queue = TARGETS.filter((sn) => !done.has(sn.toLowerCase()));
  const batch = queue.slice(0, MAX_PER_RUN);

  if (batch.length === 0) {{
    console.log("[完了] 未処理の対象はありません（解除対象 " + TARGETS.length + " 件は処理済みです）。");
    console.log(
      "もう一度最初からやり直す場合は  localStorage.removeItem('" + DONE_KEY + "')  を実行してください。"
    );
    return;
  }}

  console.log(
    "[開始] 猶予明け " + TARGETS.length + " 件（猶予中 " + notYet.length + " 件は対象外） / 未処理 " +
      queue.length + " 件 → 今回 " + batch.length + " 件を処理します（上限 " + MAX_PER_RUN + " 件）。"
  );
  console.log("停止したい時は  window.__ufStop = true  と入力して Enter を押してください。");

  const API_HEADERS = {{
    authorization: BEARER,
    "x-csrf-token": ct0,
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-active-user": "yes",
  }};

  // ---- user_id 解決 -------------------------------------------------------
  // 方法A: friendships/lookup.json（複数まとめて1回。フォロー状態も同時に判定できる）
  async function prefetchIds(screenNames) {{
    const unknown = screenNames.filter((sn) => !idCache[sn.toLowerCase()]);
    if (unknown.length === 0) return {{ status: 200 }};
    let res;
    try {{
      res = await fetch(
        location.origin +
          "/i/api/1.1/friendships/lookup.json?screen_name=" +
          encodeURIComponent(unknown.join(",")),
        {{ method: "GET", credentials: "include", headers: API_HEADERS }}
      );
    }} catch (e) {{
      return {{ status: 0, reason: "通信エラー: " + e.message }};
    }}
    if (!res.ok) return {{ status: res.status, reason: "HTTP " + res.status }};
    let json = null;
    try {{
      json = await res.json();
    }} catch (e) {{
      json = null;
    }}
    if (!Array.isArray(json)) return {{ status: res.status, reason: "応答を解釈できず" }};
    for (const u of json) {{
      if (!u || !u.screen_name || !u.id_str) continue;
      const key = String(u.screen_name).toLowerCase();
      idCache[key] = String(u.id_str);
      if (Array.isArray(u.connections) && u.connections.indexOf("following") === -1) {{
        // こちらがフォローしていない＝既に解除済み
        idCache["notfollowing:" + key] = "1";
      }}
      if (Array.isArray(u.connections) && u.connections.indexOf("followed_by") !== -1) {{
        // リスト作成後に相手からフォローが返っている＝相互フォロー。解除対象から外す
        idCache["followedby:" + key] = "1";
      }}
    }}
    saveIdCache(idCache);
    return {{ status: res.status }};
  }}

  // 方法B: users/show.json（1件ずつの予備手段）
  async function resolveUserId(sn) {{
    const key = sn.toLowerCase();
    if (idCache[key]) return {{ ok: true, id: idCache[key] }};
    let res;
    try {{
      res = await fetch(
        location.origin + "/i/api/1.1/users/show.json?screen_name=" + encodeURIComponent(sn),
        {{ method: "GET", credentials: "include", headers: API_HEADERS }}
      );
    }} catch (e) {{
      return {{ ok: false, reason: "通信エラー: " + e.message, status: 0 }};
    }}
    let json = null;
    try {{
      json = await res.json();
    }} catch (e) {{
      json = null;
    }}
    if (json && json.id_str) {{
      idCache[key] = String(json.id_str);
      saveIdCache(idCache);
      return {{ ok: true, id: idCache[key] }};
    }}
    if (json && Array.isArray(json.errors) && json.errors.length > 0) {{
      const e0 = json.errors[0];
      return {{
        ok: false,
        reason: "user_id を解決できず code=" + e0.code + " " + e0.message,
        status: res.status,
      }};
    }}
    return {{ ok: false, reason: "user_id を解決できず HTTP " + res.status, status: res.status }};
  }}

  // ---- 方式1: 内部 API ----------------------------------------------------
  async function unfollowByApi(sn) {{
    if (idCache["followedby:" + sn.toLowerCase()] === "1") {{
      // データ取得後に相手がフォローを返していた相手。相互フォローを切らないよう解除しない
      return {{ ok: true, skipped: true, status: 0, note: "フォロバ済み" }};
    }}
    if (idCache["notfollowing:" + sn.toLowerCase()] === "1") {{
      return {{ ok: true, status: 0, note: "既に解除済み" }};
    }}
    const r = await resolveUserId(sn);
    if (!r.ok) return {{ ok: false, reason: r.reason, status: r.status || 0 }};

    const body = new URLSearchParams({{
      include_profile_interstitial_type: "1",
      include_blocking: "1",
      include_blocked_by: "1",
      include_followed_by: "1",
      include_want_retweets: "1",
      include_mute_edge: "1",
      include_can_dm: "1",
      include_can_media_tag: "1",
      skip_status: "1",
      user_id: r.id,
    }});
    let res;
    try {{
      res = await fetch(location.origin + "/i/api/1.1/friendships/destroy.json", {{
        method: "POST",
        credentials: "include",
        headers: Object.assign({{}}, API_HEADERS, {{
          "content-type": "application/x-www-form-urlencoded",
        }}),
        body: body.toString(),
      }});
    }} catch (e) {{
      return {{ ok: false, reason: "通信エラー: " + e.message, status: 0 }};
    }}
    let json = null;
    try {{
      json = await res.json();
    }} catch (e) {{
      json = null;
    }}
    if (json && Array.isArray(json.errors) && json.errors.length > 0) {{
      const e0 = json.errors[0];
      return {{
        ok: false,
        reason: "API エラー code=" + e0.code + " " + e0.message,
        status: res.status,
      }};
    }}
    if (!res.ok) {{
      return {{ ok: false, reason: "HTTP " + res.status, status: res.status }};
    }}
    return {{ ok: true, status: res.status }};
  }}

  // ---- 方式2: DOM 操作 ----------------------------------------------------
  // 確認ダイアログ（「フォロー解除しますか？」）の実行ボタンを押す
  async function clickConfirm(doc) {{
    for (let i = 0; i < 20; i++) {{
      const c = doc.querySelector('[data-testid="confirmationSheetConfirm"]');
      if (c) {{
        c.click();
        return true;
      }}
      await sleep(400);
    }}
    return false;
  }}

  // /following ページ上のカードから対象の「フォロー中」ボタンを探す
  async function findUnfollowButtonInPage(sn) {{
    const target = "/" + sn.toLowerCase();
    for (let attempt = 0; attempt < 40; attempt++) {{
      const cells = document.querySelectorAll('[data-testid="cellInnerDiv"]');
      for (const cell of cells) {{
        const anchors = cell.querySelectorAll('a[role="link"]');
        let hit = false;
        for (const a of anchors) {{
          const href = (a.getAttribute("href") || "").toLowerCase();
          if (href === target) {{
            hit = true;
            break;
          }}
        }}
        if (!hit) continue;
        const btn = cell.querySelector('[data-testid$="-unfollow"]');
        if (btn) return btn;
        // 既に解除済み（-follow ボタンに戻っている）
        if (cell.querySelector('[data-testid$="-follow"]')) return "already";
      }}
      window.scrollBy(0, 1400);
      await sleep(1200);
    }}
    return null;
  }}

  async function unfollowInPage(sn) {{
    window.scrollTo(0, 0);
    await sleep(1200);
    const btn = await findUnfollowButtonInPage(sn);
    if (btn === "already") return {{ ok: true, status: 0, note: "既に解除済み" }};
    if (!btn) return {{ ok: false, reason: "ページ内でフォロー中ボタンを発見できず", status: 0 }};
    btn.click();
    await sleep(800);
    const confirmed = await clickConfirm(document);
    if (!confirmed) return {{ ok: false, reason: "確認ダイアログを操作できず", status: 0 }};
    await sleep(1500);
    return {{ ok: true, status: 0 }};
  }}

  // プロフィールページを別タブで開いて「フォロー中」ボタンをクリックする
  async function unfollowByProfileTab(sn) {{
    let w = null;
    try {{
      w = window.open(location.origin + "/" + sn, "_blank");
    }} catch (e) {{
      w = null;
    }}
    if (!w) {{
      return {{
        ok: false,
        reason: "プロフィールページを開けず（ポップアップがブロックされました）",
        status: 0,
      }};
    }}
    try {{
      for (let i = 0; i < 60; i++) {{
        await sleep(1000);
        let doc = null;
        try {{
          doc = w.document;
        }} catch (e) {{
          doc = null;
        }}
        if (!doc) continue;
        const btn = doc.querySelector('[data-testid$="-unfollow"]');
        if (btn) {{
          btn.click();
          await sleep(800);
          const confirmed = await clickConfirm(doc);
          if (!confirmed) return {{ ok: false, reason: "確認ダイアログを操作できず", status: 0 }};
          await sleep(1500);
          return {{ ok: true, status: 0 }};
        }}
        if (doc.querySelector('[data-testid$="-follow"]')) {{
          return {{ ok: true, status: 0, note: "既に解除済み" }};
        }}
      }}
      return {{ ok: false, reason: "プロフィールページでフォロー中ボタンを発見できず", status: 0 }};
    }} finally {{
      try {{
        w.close();
      }} catch (e) {{
        /* 閉じられない場合は PM が手動で閉じる */
      }}
      try {{
        window.focus();
      }} catch (e) {{
        /* 無視 */
      }}
    }}
  }}

  async function unfollowByDom(sn) {{
    if (/\/following\b/.test(location.pathname)) return await unfollowInPage(sn);
    return await unfollowByProfileTab(sn);
  }}

  // ---- user_id の一括事前解決 ---------------------------------------------
  const pre = await prefetchIds(batch);
  if (pre.status === 401 || pre.status === 403 || pre.status === 404) {{
    console.warn(
      "[注意] user_id の一括解決が HTTP " + pre.status + " で失敗しました。DOM 操作方式で進みます。"
    );
  }}

  // ---- 実行ループ ---------------------------------------------------------
  let mode =
    pre.status === 401 || pre.status === 403 || pre.status === 404 ? "dom" : "api";
  let okCount = 0;
  let ngCount = 0;
  let skipCount = 0;
  let consecutiveFail = 0;
  let stoppedReason = null;
  const failed = [];
  const skipped = [];

  for (let i = 0; i < batch.length; i++) {{
    if (window.__ufStop === true) {{
      stoppedReason = "window.__ufStop による手動停止";
      break;
    }}

    const sn = batch[i];
    const label = i + 1 + "/" + batch.length + " @" + sn;

    let r = mode === "api" ? await unfollowByApi(sn) : await unfollowByDom(sn);

    // API が権限系エラーを返した場合は DOM 操作方式へ自動切替して再試行
    if (!r.ok && mode === "api" && (r.status === 401 || r.status === 403 || r.status === 404)) {{
      console.warn(
        "[方式切替] 内部 API が HTTP " + r.status + " を返したため、DOM 操作方式へ切り替えます。"
      );
      mode = "dom";
      r = await unfollowByDom(sn);
    }}

    if (r.skipped) {{
      skipCount++;
      consecutiveFail = 0;
      skipped.push(sn);
      done.add(sn.toLowerCase());
      saveDone(done);
      console.log(label + " → スキップ（フォロバ済み・相互フォローのため解除しません）");
    }} else if (r.ok) {{
      okCount++;
      consecutiveFail = 0;
      done.add(sn.toLowerCase());
      saveDone(done);
      console.log(label + " → OK" + (r.note ? "（" + r.note + "）" : ""));
    }} else {{
      ngCount++;
      consecutiveFail++;
      failed.push(sn);
      console.log(label + " → 失敗(" + r.reason + ")");
    }}

    if (consecutiveFail >= MAX_CONSECUTIVE_FAIL) {{
      console.warn(
        "[全停止] 失敗が " + MAX_CONSECUTIVE_FAIL +
          " 件連続したため、これ以上の実行を中止しました。時間を空けてから再度お試しください。"
      );
      stoppedReason = "連続失敗 " + MAX_CONSECUTIVE_FAIL + " 件";
      break;
    }}

    if (i < batch.length - 1) {{
      const w = waitMs();
      console.log("  … 次の1件まで " + Math.round(w / 1000) + " 秒待機します");
      await sleep(w);
    }}
  }}

  // ---- 結果表示 -----------------------------------------------------------
  const remaining = TARGETS.filter((sn) => !done.has(sn.toLowerCase()));

  console.log("==================== 実行結果 ====================");
  console.log("採用方式: " + (mode === "api" ? "内部 API 方式" : "DOM 操作方式"));
  if (stoppedReason) console.log("停止理由: " + stoppedReason);
  console.log(
    "解除成功 " + okCount + " 件・失敗 " + ngCount + " 件・スキップ（フォロバ済み） " + skipCount + " 件"
  );
  if (skipped.length > 0) {{
    console.log("フォロバ済みのため解除しなかった screen_name 配列:");
    console.log(JSON.stringify(skipped));
  }}
  if (failed.length > 0) {{
    console.log("失敗した screen_name 配列:");
    console.log(JSON.stringify(failed));
  }} else {{
    console.log("失敗した screen_name はありません。");
  }}
  if (remaining.length > 0) {{
    console.log(
      "未処理の screen_name 配列（次回はこのファイルを再度貼るだけで続きから実行されます）:"
    );
    console.log(JSON.stringify(remaining));
  }} else {{
    console.log("猶予明けの対象は全件処理済みです。");
  }}
  if (notYet.length > 0) {{
    console.log(
      "まだ猶予期間中で今回対象外だった相手: " + notYet.length + " 件（最短 " +
        notYet.map((r) => r.after).sort()[0] + " から対象化）"
    );
  }}
  console.log("=================================================");
}})();
'''


def build_md(
    account: str,
    target_date: str,
    grace_days: int,
    rows: list,
    following_users: dict,
    followers_sns: set,
    profile: dict,
    following_path: str,
    following_fetched: str,
    followers_path: str,
    followers_fetched: str,
    followers_used_date: str,
    followers_is_fallback: bool,
    following_snaps: list,
    followers_snaps: list,
    missing_followers_dates: list,
    excluded_influencers: list,
    js_path: str,
    csv_path: str,
) -> str:
    """research/sns/{date}_follow_unfollow.md を組み立てる。"""
    blocks = [r for r in rows if r["class"] == "A_block"]
    graces = [r for r in rows if r["class"] == "B_grace"]
    unfollows = [r for r in rows if r["class"] == "C_unfollow"]
    oneway_n = len(rows)
    mutual_n = len(set(following_users.keys()) & followers_sns)
    not_followed_back_n = len(followers_sns - set(following_users.keys()))

    L = []
    a = L.append
    a("# ノクトラ X（@%s）リム候補  %s" % (account, target_date))
    a("")
    a("## データ基準日と件数サマリー")
    a("")
    a("| 項目 | 件数 | 取得元 |")
    a("|---|---|---|")
    a(
        "| フォロー中（following） | %d | %s（%s取得） |"
        % (len(following_users), os.path.basename(following_path), jp_time(following_fetched))
    )
    a(
        "| フォロワー（followers） | %d | %s（GHA・%s取得） |"
        % (len(followers_sns), "gha/" + os.path.basename(followers_path), jp_time(followers_fetched))
    )
    a("| 相互フォロー | %d | 上記2件の突合 |" % mutual_n)
    a("| 片思いフォロー（こちらのみ） | %d | 同上 |" % oneway_n)
    a("| フォロバ未実施（相手のみ） | %d | 同上 |" % not_followed_back_n)
    a(
        "| 本日のリム対象（C_unfollow） | **%d** | 猶予%d日・判定完了 |"
        % (len(unfollows), grace_days)
    )
    a(
        "| 猶予期間中（B_grace） | %d | %s |"
        % (
            len(graces),
            ("期限 " + "／".join(sorted({r["after"] for r in graces}))) if graces else "該当なし",
        )
    )
    a(
        "| ブロック方針候補（A_block） | **%d** | %s |"
        % (len(blocks), "過去フォロワー→離脱に該当あり" if blocks else "過去フォロワー→離脱に該当なし")
    )
    a("")
    if profile:
        a(
            "アカウント実測値（following 取得時・%s）: followers_count %s / friends_count %s / "
            "ff_ratio %s / statuses_count %s"
            % (
                jp_time(following_fetched),
                profile.get("followers_count", "取得なし"),
                profile.get("friends_count", "取得なし"),
                profile.get("ff_ratio", "取得なし"),
                profile.get("statuses_count", "取得なし"),
            )
        )
        a("")
    if followers_is_fallback:
        a(
            "**注記**: %s の GHA 日次スナップショットに followers が無かったため、直近の %s 分（%s）を"
            "代用しています。この間のフォロワー増減は反映されていません。"
            % (target_date, followers_used_date, os.path.basename(followers_path))
        )
        a("")

    # 【A】リム候補
    a("## 【A】リム候補")
    a("")
    a("### 本日の解除対象（C_unfollow）: %d 件" % len(unfollows))
    a("")
    if unfollows:
        a(
            "片思い %d 件のうち、フォロー日が確定していて猶予%d日を過ぎたのが %d 件です。"
            "内訳は解除可能日 %s で、いずれも本日より前に到来済みです。"
            % (oneway_n, grace_days, len(unfollows), counts_desc(unfollows, "after"))
        )
        a("")
        a("| screen_name | name | followers | following | ff_ratio | 投稿数 | 解除可能日 |")
        a("|---|---|---|---|---|---|---|")
        for r in sorted(unfollows, key=lambda x: x["user"]["screen_name"].lower()):
            u = r["user"]
            a(
                "| %s | %s | %s | %s | %s | %s | %s |"
                % (
                    u.get("screen_name", ""),
                    esc_pipe(u.get("name", "")),
                    u.get("followers_count", ""),
                    u.get("friends_count", ""),
                    u.get("ff_ratio", ""),
                    u.get("statuses_count", ""),
                    r["after"],
                )
            )
        a("")
        a("`%s` を生成済みです（対象 %d 件）。" % (rel_repo(js_path), len(unfollows)))
    else:
        a(
            "**本日は 0 件です。水増しはしていません。** 猶予%d日を過ぎてフォロバがない相手は"
            "ありませんでした。解除スクリプト（.js）は生成していません。" % grace_days
        )
    a("")

    # B_grace
    a("### 猶予期間中（B_grace）: %d 件" % len(graces))
    a("")
    if graces:
        lowers = sorted({r["lower"] for r in graces if r["lower"]})
        latests = sorted({r["first_seen"] for r in graces})
        a(
            "残る %d 件は、直前の following スナップショット（%s）に載っておらず %s のスナップショットで"
            "初めて確認された相手です。この間に following の中間スナップショットがないため、実際の"
            "フォロー日は幅でしか特定できません。**判定不能は猶予側に倒す**方針に従い、最も遅い %s を"
            "フォロー日とみなして解除可能日を %s としています。"
            % (
                len(graces),
                "／".join(lowers) if lowers else "該当なし",
                "／".join(latests),
                "／".join(latests),
                "／".join(sorted({r["after"] for r in graces})),
            )
        )
        a("")
        a("| screen_name | name | followers | following | ff_ratio | 投稿数 | 解除可能日 |")
        a("|---|---|---|---|---|---|---|")
        for r in sorted(graces, key=lambda x: x["user"]["screen_name"].lower()):
            u = r["user"]
            a(
                "| %s | %s | %s | %s | %s | %s | %s |"
                % (
                    u.get("screen_name", ""),
                    esc_pipe(u.get("name", "")),
                    u.get("followers_count", ""),
                    u.get("friends_count", ""),
                    u.get("ff_ratio", ""),
                    u.get("statuses_count", ""),
                    r["after"],
                )
            )
    else:
        a("**本日は 0 件です。水増しはしていません。** 猶予期間中の相手はありませんでした。")
    a("")

    # A_block
    a("### 別枠: ブロック方針候補（A_block）: %d 件" % len(blocks))
    a("")
    n_snaps = len(followers_snaps)
    missing_note = (
        "（%s は followers 0 件の欠測のため除外）" % "・".join(missing_followers_dates)
        if missing_followers_dates
        else ""
    )
    if blocks:
        a(
            "片思い %d 件を followers スナップショット %d 点%sと全件突合した結果、"
            "「過去に一度でもフォロワーに在籍していて現在いない」相手が %d 件ありました。"
            "解除スクリプトの対象からは外し、ブロック方針の別枠として扱います。"
            % (oneway_n, n_snaps, missing_note, len(blocks))
        )
        a("")
        a("| screen_name | name | followers | following | 在籍していた期間 |")
        a("|---|---|---|---|---|")
        for r in sorted(blocks, key=lambda x: x["user"]["screen_name"].lower()):
            u = r["user"]
            a(
                "| %s | %s | %s | %s | %s |"
                % (
                    u.get("screen_name", ""),
                    esc_pipe(u.get("name", "")),
                    u.get("followers_count", ""),
                    u.get("friends_count", ""),
                    r["past_span"],
                )
            )
    else:
        a(
            "**本日は 0 件です。水増しはしていません。** 片思い %d 件を followers スナップショット "
            "%d 点%sと全件突合しましたが、「過去に一度でもフォロワーに在籍していて現在いない」相手は"
            "1 件もありませんでした。" % (oneway_n, n_snaps, missing_note)
        )
    a("")

    # PM の実行手順
    a("## PM の実行手順")
    a("")
    if unfollows:
        runs = (len(unfollows) + MAX_PER_RUN - 1) // MAX_PER_RUN
        a("1. ブラウザで `https://x.com/%s/following` を開き、ノクトラ本人でログインした状態にします。" % account)
        a("2. F12 キー（または右クリック → 検証）で「Console」タブを開きます。")
        a("3. `%s` の中身を全文コピーして Console に貼り付け、Enter を押します。" % rel_repo(js_path))
        a(
            "4. 1回の実行上限は %d 件（MAX_PER_RUN）です。**%d 件を処理するには同じファイルを %d 回"
            "貼り直します**。処理済みの相手はブラウザの localStorage（キー `uf_done_%s`）に記録されるため、"
            "貼り直すたびに続きから進みます。"
            % (MAX_PER_RUN, len(unfollows), runs, target_date.replace("-", "_"))
        )
        a("5. 貼り直しの間隔は空けて構いません。急ぐ必要はありません。")
        a("")
        a("安全設計:")
        a("")
        a("- 1件ごとに 3〜8 秒のランダム待機。")
        a("- 失敗が 3 件連続したら即座に全停止。")
        a(
            "- 実行直前に `friendships/lookup.json` で最新の関係を問い合わせ、`followed_by` があれば"
            "（＝リスト作成後にフォロバされていれば）解除せずスキップします。リストの鮮度落ちで"
            "相互フォローを切る事故を防ぐためです。"
        )
        a("- 途中で止めたい時は Console に `window.__ufStop = true` と入力して Enter。")
        a("- 外部への送信は一切ありません。認証はログイン中の cookie（ct0）をブラウザ内で読むだけです。")
    else:
        a(
            "**本日の解除対象は 0 件のため、実行する作業はありません。** 解除スクリプト（.js）も"
            "生成していません。次回、猶予明けの相手が出た時点で改めて生成します。"
        )
    a("")

    # 判定基準
    a("## 判定基準")
    a("")
    a(
        "- `A_block`: 過去の followers スナップショットに在籍していたのに現在いない（リムられ）"
        "→ 解除スクリプトから除外し、ブロック方針の別枠。"
    )
    a(
        "- `B_grace`: フォローしてから %d 日以内、またはフォロー日が幅でしか特定できず猶予明けを"
        "断定できない相手。`unfollow_eligible_after` を持たせ対象外とする。フォロー日は最も遅い日を"
        "採用する（判定不能は猶予側に倒す）。" % grace_days
    )
    a("- `C_unfollow`: 猶予%d日を過ぎてもフォローバックがない → 本日の解除対象。" % grace_days)
    a("- 相互フォローは、いかなる場合も解除対象にしない（片思い集合に入らない）。")
    a("")
    a("猶予は PM 指示により 7 日から %d 日へ短縮済みです（2026-08-21）。" % grace_days)
    a("")

    # データ上の制約
    a("## データ上の制約")
    a("")
    snap_line = "・".join("%s（%d 件）" % (d, len(u)) for d, u, _p in following_snaps)
    a("- **フォロー日特定の精度**: following の過去スナップショットは %s の %d 点です。" % (snap_line, len(following_snaps)))
    gaps = []
    for i in range(1, len(following_snaps)):
        prev_d = following_snaps[i - 1][0]
        cur_d = following_snaps[i][0]
        gap = (parse_date(cur_d) - parse_date(prev_d)).days
        if gap > grace_days:
            gaps.append("%s→%s（%d 日）" % (prev_d, cur_d, gap))
    if gaps:
        a(
            "  スナップショット間隔が猶予%d日より広い区間（%s）では、その間にフォローした相手の"
            "フォロー日を確定できません。該当分は全て最遅日扱いにして B_grace へ倒しています。"
            % (grace_days, "・".join(gaps))
        )
    if os.path.basename(following_path) != os.path.basename(followers_path):
        a(
            "- 取得元とその時刻が分かれています（followers は %s、following は %s）。この間の増減は"
            "反映されません。スクリプト側の実行直前再確認でこのずれは吸収されます。"
            % (jp_time(followers_fetched), jp_time(following_fetched))
        )
    if missing_followers_dates:
        a(
            "- %s の GHA スナップショットは followers 0 件の欠測のため、A_block 判定の在籍履歴からは"
            "除外しています。" % "・".join(missing_followers_dates)
        )
    if excluded_influencers:
        a(
            "- `bi/data/x_influencers.yaml` 記載のうち %d 件（%s）が片思いに含まれていたため、"
            "リム候補から除外しました。" % (len(excluded_influencers), "・".join(excluded_influencers))
        )
    else:
        a(
            "- `bi/data/x_influencers.yaml` に記載のアカウントと片思い %d 件の重なりは 0 件でした。"
            "除外処理は空振りです。" % oneway_n
        )
    a(
        "- 各アカウントの実際の投稿内容は取得していません。判定は bio・フォロワー数・投稿数・"
        "ff_ratio・スナップショット在籍履歴のみに基づきます。"
    )
    a("")
    a("生成元: `bi/pipelines/make_unfollow_candidates.py`（分類結果の全件は `%s`）" % rel_repo(csv_path))
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(
        description="X 片思いフォローのリム候補分類と Console 解除スクリプト生成（読み取り専用）"
    )
    p.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（省略時は JST の当日）")
    p.add_argument("--grace-days", type=int, default=3, help="フォロバ待ちの猶予日数（既定 3）")
    p.add_argument("--account", default="noctra__ai", help="対象アカウントの screen_name（既定 noctra__ai）")
    p.add_argument("--dry-run", action="store_true", help="ファイルを出力せず件数だけ表示する")
    args = p.parse_args()

    target_date = args.date or jst_today()
    try:
        parse_date(target_date)
    except ValueError:
        fail("--date は YYYY-MM-DD 形式で指定してください（受領値: %s）" % target_date)
    if args.grace_days < 0:
        fail("--grace-days は 0 以上で指定してください（受領値: %d）" % args.grace_days)

    account = args.account

    # --- 入力 ---------------------------------------------------------------
    following_list, following_path, following_fetched, following_acct = find_current_following(
        account, target_date
    )
    followers_list, followers_path, followers_fetched, followers_used_date, followers_is_fallback = (
        find_followers(account, target_date)
    )

    following_users = users_by_sn(following_list)
    followers_sns = set(users_by_sn(followers_list).keys())
    profile = following_acct.get("profile") or {}

    following_snaps, followers_snaps = collect_snapshots(account, target_date)
    # 当日の following は探索で確定したものを必ず使う（collect_snapshots の同日分を上書き）
    following_snaps = [s for s in following_snaps if s[0] != target_date]
    following_snaps.append((target_date, following_users, following_path))
    following_snaps.sort(key=lambda s: s[0])

    followers_history = build_followers_history(followers_snaps)

    # followers 0 件の欠測日（在籍履歴から落とした日）を洗い出して注記に使う
    have_followers_dates = {d for d, _s, _p in followers_snaps}
    missing_followers_dates = []
    for p_ in sorted(glob.glob(os.path.join(GHA_DIR, "profile_daily_*.json"))):
        m = DATE_RE.search(os.path.basename(p_))
        if not m or m.group(1) > target_date:
            continue
        if m.group(1) not in have_followers_dates:
            missing_followers_dates.append(m.group(1))

    # --- 片思い抽出 ---------------------------------------------------------
    oneway_sns = sorted(set(following_users.keys()) - followers_sns)

    exclusions = load_influencer_exclusions()
    excluded_influencers = [following_users[sn]["screen_name"] for sn in oneway_sns if sn in exclusions]
    oneway_sns = [sn for sn in oneway_sns if sn not in exclusions]

    rows = classify(
        oneway_sns,
        following_users,
        following_snaps,
        followers_history,
        target_date,
        args.grace_days,
    )

    blocks = [r for r in rows if r["class"] == "A_block"]
    graces = [r for r in rows if r["class"] == "B_grace"]
    unfollows = sorted(
        [r for r in rows if r["class"] == "C_unfollow"],
        key=lambda r: r["user"]["screen_name"].lower(),
    )

    csv_path = os.path.join(X_POSTS_DIR, "oneway_follows_%s.csv" % target_date)
    js_path = os.path.join(X_POSTS_DIR, "unfollow_console_%s.js" % target_date)
    md_path = os.path.join(RESEARCH_SNS_DIR, "%s_follow_unfollow.md" % target_date)

    # --- 集計表示 -----------------------------------------------------------
    print("対象アカウント : @%s" % account)
    print("対象日         : %s（猶予 %d 日）" % (target_date, args.grace_days))
    print("following      : %d 件  %s" % (len(following_users), rel_repo(following_path)))
    print(
        "followers      : %d 件  %s%s"
        % (
            len(followers_sns),
            rel_repo(followers_path),
            ("  ※%s の当日分が無いため代用" % target_date) if followers_is_fallback else "",
        )
    )
    print("following スナップショット: %s" % "・".join("%s(%d)" % (d, len(u)) for d, u, _ in following_snaps))
    print("followers スナップショット: %d 点%s" % (
        len(followers_snaps),
        ("（欠測除外 %s）" % "・".join(missing_followers_dates)) if missing_followers_dates else "",
    ))
    print("相互フォロー   : %d 件" % len(set(following_users.keys()) & followers_sns))
    print("片思い         : %d 件" % len(rows))
    if excluded_influencers:
        print("  ※x_influencers.yaml により除外: %d 件（%s）" % (len(excluded_influencers), "・".join(excluded_influencers)))
    print("  A_block      : %d 件" % len(blocks))
    print("  B_grace      : %d 件%s" % (len(graces), ("  期限 " + "／".join(sorted({r["after"] for r in graces}))) if graces else ""))
    print("  C_unfollow   : %d 件%s" % (len(unfollows), ("  " + counts_desc(unfollows, "after")) if unfollows else ""))

    if args.dry_run:
        print()
        print("[dry-run] ファイルは出力していません。出力予定は次の通りです。")
        print("  CSV : %s" % rel_repo(csv_path))
        print("  JS  : %s" % (rel_repo(js_path) if unfollows else "（C_unfollow 0 件のため生成しません）"))
        print("  MD  : %s" % rel_repo(md_path))
        return 0

    # --- 出力（既存ファイルは上書きしない）---------------------------------
    existing = [p_ for p_ in [csv_path, md_path] if os.path.exists(p_)]
    if unfollows and os.path.exists(js_path):
        existing.append(js_path)
    if existing:
        fail(
            "出力先に既存ファイルがあります。上書きしないため中止しました。\n  "
            + "\n  ".join(rel_repo(p_) for p_ in existing)
            + "\n  既存分を確認したい場合は --dry-run で件数だけ表示できます。"
        )

    os.makedirs(X_POSTS_DIR, exist_ok=True)
    os.makedirs(RESEARCH_SNS_DIR, exist_ok=True)

    write_csv(csv_path, rows)
    print()
    print("CSV 出力: %s（%d 行）" % (rel_repo(csv_path), len(rows)))

    if unfollows:
        js = build_js(
            account=account,
            target_date=target_date,
            targets=unfollows,
            grace_rows=graces,
            oneway_total=len(rows),
            grace_days=args.grace_days,
            block_count=len(blocks),
        )
        with open(js_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(js)
        print("JS  出力: %s（対象 %d 件）" % (rel_repo(js_path), len(unfollows)))
    else:
        print("JS  出力: なし（C_unfollow 0 件のため生成しません）")

    md_text = build_md(
        account=account,
        target_date=target_date,
        grace_days=args.grace_days,
        rows=rows,
        following_users=following_users,
        followers_sns=followers_sns,
        profile=profile,
        following_path=following_path,
        following_fetched=following_fetched,
        followers_path=followers_path,
        followers_fetched=followers_fetched,
        followers_used_date=followers_used_date,
        followers_is_fallback=followers_is_fallback,
        following_snaps=following_snaps,
        followers_snaps=followers_snaps,
        missing_followers_dates=missing_followers_dates,
        excluded_influencers=excluded_influencers,
        js_path=js_path,
        csv_path=csv_path,
    )
    with open(md_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(md_text)
    print("MD  出力: %s" % rel_repo(md_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
