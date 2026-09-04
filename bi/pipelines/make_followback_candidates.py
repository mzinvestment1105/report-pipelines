"""X（旧 Twitter）フォローバック候補の抽出と Console フォロー実行スクリプト生成 ETL。

用途:
    運用アカウント（既定 @noctra__ai）の followers / following スナップショットを突合し、
    「相手はフォローしてくれているが、こちらが未フォロー」の相手（＝未フォローバック）を抽出する。
    そこから運用上フォローを返したくない相手を機械的な基準で除外し、
    フォロー候補 CSV・ブラウザ Console 用のフォロー実行スクリプト（.js）・PM 向けレポート（.md）を出力する。

    生成される .js は「フォロー」に加えて「相手の最新ツイート（固定ツイートを除く）へのいいね」を行う。
    いいねが取得・実行できなかった場合もフォローは実行する（--no-like でいいねを無効化できる）。

    読み取り専用。X へのアクセス・書き込みは一切行わない（取得済み JSON を読むだけ）。
    実際のフォロー操作は、生成された .js を PM がブラウザの Console に貼って手動実行する。

除外基準（既存 followback_triage_2026-08-09.csv の実運用基準を機械化したもの）:
    1. friends_count >= 3000        … フォロー数が多すぎる（誰でもフォローするタイプ）
    2. ff_ratio >= 2.0              … フォロー数がフォロワー数の 2 倍以上
    3. statuses_count <= 100        … 投稿がほとんどない
    4. bio に相互フォロー系キーワード … 相互フォロー狙いのアカウント
    5. protected（鍵アカウント）     … 投稿を読めずタイムラインの質に寄与しない
    6. bi/data/x_influencers.yaml 記載 … 別枠で扱う情報源
    7. 過去に自分がフォローしていて following から消えた相手（＝一度リム済み）
       … 再フォローするとフォロー／アンフォローの反復になるため必ず除外する

    除外分も CSV には verdict = exclude として全件残す（水増しも黙殺もしない）。

入力:
    followers … bi/outputs/x_posts/gha/profile_daily_{date}.json。
                当日分が無い / 空の場合は直近日へ遡り、その旨を標準出力とレポートに明記する。
    following … 次の順で探索し、最初に見つかったものを使う（推測での代替は行わない）
                1. bi/outputs/x_posts/gha/profile_daily_{date}.json
                2. bi/outputs/x_posts/profile_following_{date}.json（_r2 / _r3 … 連番は新しい方）
                3. bi/outputs/x_posts/profile_*_{date}.json のうち following を持つもの
    過去 following スナップショット … bi/outputs/x_posts 配下（gha 含む）の profile_*.json から
                対象アカウントの following を持つものを自動収集（リム済み判定用）。
    除外リスト … bi/data/x_influencers.yaml。

使い方（対象ハンドルは引数で外から渡す。スクリプトに直書きしない）:
    python make_followback_candidates.py --date 2026-09-04
    python make_followback_candidates.py --date 2026-09-04 --dry-run   # 件数だけ表示・ファイル出力なし
    python make_followback_candidates.py --max-candidates 100 --account noctra__ai
    python make_followback_candidates.py --no-like                     # いいねを行わない従来動作の JS を生成

出力（--dry-run 時は一切出力しない）:
    bi/outputs/x_posts/followback_candidates_{date}.csv … 未フォローバック全件の判定結果
    bi/outputs/x_posts/followback_console_{date}.js     … フォロー実行スクリプト（0 件なら生成しない）
    research/sns/{date}_followback.md                   … PM 向けレポート

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
from string import Template

JST = timezone(timedelta(hours=9))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
X_POSTS_DIR = os.path.join(REPO_ROOT, "bi", "outputs", "x_posts")
GHA_DIR = os.path.join(X_POSTS_DIR, "gha")
RESEARCH_SNS_DIR = os.path.join(REPO_ROOT, "research", "sns")
INFLUENCERS_YAML = os.path.join(REPO_ROOT, "bi", "data", "x_influencers.yaml")

CSV_FIELDS = [
    "screen_name",
    "rest_id",
    "name",
    "followers_count",
    "friends_count",
    "ff_ratio",
    "statuses_count",
    "verdict",
    "exclude_reason",
    "bio_head",
]

BIO_HEAD_LEN = 100

# --- 除外のしきい値（既存 triage CSV の実運用基準）---------------------------
MAX_FRIENDS_COUNT = 3000   # これ以上のフォロー数は除外
MAX_FF_RATIO = 2.0         # これ以上の FF 比は除外
MIN_STATUSES_COUNT = 100   # これ以下の投稿数は除外（100 ちょうども除外）

# bio に含まれていたら相互フォロー狙いとみなすキーワード（小文字化して部分一致）
MUTUAL_FOLLOW_KEYWORDS = [
    "相互フォロー",
    "相互希望",
    "相互100",
    "相互１００",
    "フォロバ100",
    "フォロバ１００",
    "フォロバ%",
    "フォロバ％",
    "#相互",
    "follow back",
    "followback",
    "f4f",
    "相互ふぉろー",
]

# --- JS 側の実行パラメータ ---------------------------------------------------
MAX_PER_RUN = 10   # 1回の実行で処理する上限件数（PM 指示・2026-09-04 に 20 から 10 へ。いいね追加で総アクション数を維持）
COOLDOWN_MIN = 30  # バッチ間に空ける最低時間（分）

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


def runs_phrase(n: int) -> str:
    """対象件数から「何回貼るか」の日本語を作る。1 回で終わる場合の言い回しを分ける。"""
    runs = (n + MAX_PER_RUN - 1) // MAX_PER_RUN
    if runs <= 1:
        return "%d 件は 1 回の実行で終わります（上限 %d 件に届かないため貼り直しは不要です）" % (
            n,
            MAX_PER_RUN,
        )
    return "%d 件を処理するには同じファイルを %d 回貼り直します" % (n, runs)


def to_int(v):
    """スナップショットの数値項目を int にする。取れなければ None（推定はしない）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
      following_snaps … [(date, {sn: user}, path)] を日付昇順（following 0 件の欠測日は除外済み）
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
    情報源として別枠で扱うアカウント群にあたる。フォロバ候補に現れた場合は候補から外す。
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


# ---------------------------------------------------------------- 判定


def build_unfollowed_history(following_snaps: list, current_following: dict) -> dict:
    """「過去に following に居たが現在は居ない」相手 → 在籍していた期間表記の辞書。

    再フォローするとフォロー／アンフォローの反復になるため、この集合はフォロバ候補から外す。
    当日の following スナップショットは呼び出し側で following_snaps に含めているため、
    現在の following に居る相手はここに現れない。
    """
    hist = {}
    for snap_date, users, _path in following_snaps:
        for sn in users:
            hist.setdefault(sn, []).append(snap_date)
    out = {}
    for sn, dates in hist.items():
        if sn in current_following:
            continue
        first, last = dates[0], dates[-1]
        out[sn] = first if first == last else "%s〜%s" % (first, last)
    return out


def has_mutual_keyword(bio: str):
    """bio に相互フォロー系キーワードが含まれていれば、そのキーワードを返す。"""
    t = (bio or "").lower()
    if not t:
        return None
    for kw in MUTUAL_FOLLOW_KEYWORDS:
        if kw.lower() in t:
            return kw
    return None


def judge(user: dict, sn: str, exclusions: set, unfollowed_history: dict):
    """1 件のフォロワーについて除外理由を判定する。除外しないなら None を返す。

    判定順は既存 triage の運用順（数値基準 → bio → 鍵 → 別枠 → 過去リム）に合わせる。
    数値が取得できていない項目はその基準では除外しない（欠損を悪材料に読み替えない）。
    """
    friends = to_int(user.get("friends_count"))
    ratio = to_float(user.get("ff_ratio"))
    statuses = to_int(user.get("statuses_count"))

    if friends is not None and friends >= MAX_FRIENDS_COUNT:
        return "フォロー数3000以上"
    if ratio is not None and ratio >= MAX_FF_RATIO:
        return "FF比2.0以上"
    if statuses is not None and statuses <= MIN_STATUSES_COUNT:
        return "投稿数100以下"
    kw = has_mutual_keyword(user.get("profile_bio"))
    if kw:
        return "bio相互フォロー系"
    if user.get("protected") is True:
        return "鍵アカウント"
    if sn in exclusions:
        return "influencers除外"
    if sn in unfollowed_history:
        return "過去にリム済み"
    return None


def sort_candidates(rows: list) -> list:
    """フォロー対象の並び順。フォロワー数の降順、同数は screen_name 昇順。"""
    return sorted(
        rows,
        key=lambda r: (
            -(to_int(r["user"].get("followers_count")) or 0),
            r["user"]["screen_name"].lower(),
        ),
    )


def sort_excluded(rows: list) -> list:
    """除外分の並び順。理由ごとにまとめ、その中は screen_name 昇順。"""
    return sorted(rows, key=lambda r: (r["exclude_reason"], r["user"]["screen_name"].lower()))


def reason_counts(rows: list) -> list:
    """除外理由 → 件数を、定義順（判定順）に整列して返す。"""
    order = [
        "フォロー数3000以上",
        "FF比2.0以上",
        "投稿数100以下",
        "bio相互フォロー系",
        "鍵アカウント",
        "influencers除外",
        "過去にリム済み",
    ]
    c = {}
    for r in rows:
        c[r["exclude_reason"]] = c.get(r["exclude_reason"], 0) + 1
    out = [(k, c[k]) for k in order if k in c]
    out += [(k, v) for k, v in sorted(c.items()) if k not in order]
    return out


# ---------------------------------------------------------------- 出力


def write_csv(path: str, candidates: list, excluded: list) -> None:
    """候補（優先度順）→ 除外（理由順）の順で 1 ファイルに全件書き出す。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in sort_candidates(candidates) + sort_excluded(excluded):
            u = r["user"]
            w.writerow(
                {
                    "screen_name": u.get("screen_name", ""),
                    "rest_id": u.get("rest_id", ""),
                    "name": u.get("name", ""),
                    "followers_count": u.get("followers_count", ""),
                    "friends_count": u.get("friends_count", ""),
                    "ff_ratio": u.get("ff_ratio", ""),
                    "statuses_count": u.get("statuses_count", ""),
                    "verdict": r["verdict"],
                    "exclude_reason": r["exclude_reason"],
                    "bio_head": bio_head(u.get("profile_bio")),
                }
            )


def build_js(account: str, target_date: str, targets: list, not_followed_back: int,
             excluded_n: int, do_like: bool = True) -> str:
    """followback_console_{date}.js を組み立てる。

    JS 本体は波括弧をそのまま書けるよう string.Template で差し込む
    （str.format の波括弧二重化は事故のもとなので使わない）。

    do_like=True（既定）のとき、フォローに加えて相手の最新ツイート（固定ツイートを除く）へ
    いいねを行う JS を生成する。False のときはフォローのみの従来動作。
    """
    key_suffix = target_date.replace("-", "_")
    n = len(targets)
    runs = (n + MAX_PER_RUN - 1) // MAX_PER_RUN
    total_min = max(runs - 1, 0) * COOLDOWN_MIN
    # 1人あたり: ツイート取得直後 2〜4 秒 ＋ 次の1人まで 8〜20 秒 → 平均およそ 17 秒
    per_person_sec_avg = 17 if do_like else 14
    batch_n = min(n, MAX_PER_RUN)
    run_min = max(1, int(round(batch_n * per_person_sec_avg / 60)))

    rows_js = "\n".join(
        "    { sn: %s, id: %s },"
        % (js_str(r["user"].get("screen_name")), js_str(str(r["user"].get("rest_id") or "")))
        for r in targets
    )

    if do_like:
        title_line = (
            " * ノクトラ（@%s）フォローバック＋いいね実行スクリプト  %s 版（対象 %d 件）\n"
            % (account, target_date, n)
        )
        action_block = (
            " * 【このスクリプトが行うこと】\n"
            " *   対象1人につき次の2つを行います。\n"
            " *     1. フォロー（/i/api/1.1/friendships/create.json）\n"
            " *     2. その相手の最新ツイート1件へのいいね（GraphQL FavoriteTweet）\n"
            " *   いいねの対象は「固定ツイートを除いた最新の本人ツイート」です。\n"
            " *   固定ツイート・リツイート・リプライは除外し、残った中で最も新しい1件を選びます。\n"
            " *   ツイートを取得できなかった相手・いいねが失敗した相手も、フォローは実行します\n"
            " *   （いいねは補助的な行為のため、失敗しても本体のフォローは止めません）。\n"
            " *\n"
        )
        safety_like = (
            " *   - いいねの失敗は連続失敗カウントに含めません（フォローの失敗のみ数えます）\n"
            " *   - いいねが 5 件連続で失敗した場合は「いいね機能のみ」を自動で無効化し、\n"
            " *     以降はフォローだけを続行します（Console に警告を出します）\n"
        )
        time_block = (
            " * 【所要時間の目安】\n"
            " *   1人あたり、ツイート取得後に 2〜4 秒・次の1人へ進む前に 8〜20 秒の待機を挟みます。\n"
            " *   1回（最大 %d 件）でおよそ %d 分です。全 %d 件では実行時間に加えて\n"
            " *   バッチ間の待機だけで最低およそ %d 分掛かります。\n"
            " *\n"
            % (MAX_PER_RUN, run_min, n, total_min)
        )
    else:
        title_line = (
            " * ノクトラ（@%s）フォローバック実行スクリプト  %s 版（対象 %d 件・いいねなし）\n"
            % (account, target_date, n)
        )
        action_block = (
            " * 【このスクリプトが行うこと】\n"
            " *   対象1人につきフォロー（/i/api/1.1/friendships/create.json）のみを行います。\n"
            " *   いいねは行いません（--no-like で生成された版です）。\n"
            " *\n"
        )
        safety_like = ""
        time_block = (
            " * 【所要時間の目安】\n"
            " *   1人あたり 8〜20 秒の待機を挟みます。1回（最大 %d 件）でおよそ %d 分です。\n"
            " *   全 %d 件ではバッチ間の待機だけで最低およそ %d 分掛かります。\n"
            " *\n"
            % (MAX_PER_RUN, run_min, n, total_min)
        )

    header = (
        "/* ==========================================================================\n"
        + title_line
        + " *\n"
        + action_block
        + " * 【使い方 3 行】\n"
        " *   1. ブラウザで https://x.com/%s/followers を開き、ノクトラ本人でログインした状態にする\n"
        " *   2. F12 キー（または右クリック → 検証）を押して「Console」タブを開く\n"
        " *   3. このファイルの中身を全文コピーして Console に貼り付け、Enter を押す\n"
        " *\n"
        " * 【停止方法】\n"
        " *   Console に  window.__fbStop = true  と入力して Enter（次の1件に進む前に停止します）。\n"
        " *   または、その X のタブを閉じる／ページを再読み込みすれば即座に止まります。\n"
        " *\n"
        " * 【今回の対象】\n"
        " *   %s 時点の実測で、相手がフォローしてくれていてこちらが未フォローの「未フォローバック」は\n"
        " *   %d 件でした。そのうち除外基準（フォロー数・FF比・投稿数・bio の相互フォロー狙い・\n"
        " *   鍵アカウント・別枠の情報源・過去にリム済み）に触れた %d 件を外し、残る %d 件が対象です。\n"
        " *   並び順は相手のフォロワー数の降順です。\n"
        " *\n"
        " * 【1回の上限と貼り直し】\n"
        " *   1回の実行上限は %d 件（MAX_PER_RUN）です。%s。\n"
        " *   処理済みの相手は localStorage（キー %s）に記録されるため、\n"
        " *   貼り直すたびに続きから進みます。\n"
        " *   処理済みの判定はフォローの完了で行います（いいねの成否では変わりません）。\n"
        " *\n"
        % (
            account,
            target_date,
            not_followed_back,
            excluded_n,
            n,
            MAX_PER_RUN,
            runs_phrase(n),
            "fb_done_" + key_suffix,
        )
        + time_block
        + " * 【バッチ間のクールダウン（必ず守ってください）】\n"
        " *   1回（%d 件）実行したら、次の貼り直しまで最低 %d 分空けてください。\n"
        " *   短時間に連続でフォローすると X 側の自動化検知に掛かるためです。\n"
        " *   本スクリプトは最終実行時刻を localStorage（キー %s）に記録し、\n"
        " *   前回から %d 分未満で貼り直された場合は開始前に警告して中止します。\n"
        " *   どうしても続行したい場合のみ  window.__fbForce = true  を先に実行してください。\n"
        " *\n"
        " * 【安全設計】\n"
        " *   - 1件ごとに 8〜20 秒のランダム待機（機械的な等間隔アクセスを避ける）\n"
        " *   - 1回の実行で最大 %d 件まで（MAX_PER_RUN）。到達したら自動停止\n"
        " *   - フォローの失敗が 3 件連続したら即座に全停止して警告表示\n"
        % (
            MAX_PER_RUN,
            COOLDOWN_MIN,
            "fb_lastrun_" + key_suffix,
            COOLDOWN_MIN,
            MAX_PER_RUN,
        )
        + safety_like
        + " *   - 実行直前に friendships/lookup.json で最新の関係を問い合わせ、\n"
        " *     既にフォロー済み（connections に following）の相手は送信せずスキップする\n"
        " *   - リスト作成後に相手がフォローを外していた（followed_by が消えていた）相手も\n"
        " *     フォローバックの前提が崩れているためスキップする\n"
        " *   - 処理済みの相手はブラウザの localStorage に記録するため、\n"
        " *     もう一度全文を貼り直せば「未処理の続きから」自動で再開します\n"
        " *\n"
        " * 【方式】\n"
        " *   X の内部 API（/i/api/1.1/friendships/create.json）へ POST します。user_id は\n"
        " *   スナップショットの rest_id を埋め込み済みのため、実行時の ID 解決は不要です。\n"
        " *   認証は現在ログイン中の cookie（ct0）をブラウザ内で読むだけで、外部への送信は一切ありません。\n"
        " *   万一 API が 401/403/404 を返す場合は、/followers ページ上のフォローボタンを\n"
        " *   クリックする DOM 操作方式へ自動で切り替えます（同じ待機・停止・進捗表示が働きます）。\n"
        " * ========================================================================== */\n"
    )

    body = Template(JS_BODY).substitute(
        rows=rows_js,
        mpr=str(MAX_PER_RUN),
        cooldown=str(COOLDOWN_MIN),
        key=key_suffix,
        date=target_date,
        dolike="true" if do_like else "false",
        likeengine=JS_LIKE_ENGINE if do_like else "",
    )
    return header + body


# フォロー実行エンジン本体（followback_console_2026-08-11.js を移植し、
# 実行直前の関係再確認・クールダウン・結果 JSON 出力・最新ツイートへのいいねを追加したもの）。
# Template 展開のため $rows / $mpr / $cooldown / $key / $date / $dolike / $likeengine のみが置換対象。
# $likeengine には JS_LIKE_ENGINE（いいね実装本体）が入り、--no-like のときは空文字になる。
# JS 内で $ を使う場合は $$ と書く必要がある点に注意（DOM セレクタの $= がこれに当たる）。
JS_BODY = r'''
(async () => {
  "use strict";

  // ---- 対象アカウント（未フォローバック・フォロワー数降順）------------------
  const TARGETS = [
$rows
  ];

  // ---- 設定 ---------------------------------------------------------------
  const MAX_PER_RUN = $mpr;          // 1回の実行で処理する上限件数
  const WAIT_MIN_MS = 8000;         // 1件あたりの最小待機（8秒）
  const WAIT_MAX_MS = 20000;        // 1件あたりの最大待機（20秒）
  const MAX_CONSECUTIVE_FAIL = 3;   // フォローの連続失敗がこの件数に達したら全停止
  const COOLDOWN_MIN = $cooldown;    // バッチ間に空ける最低時間（分）
  const RUN_DATE = "$date";          // 対象日（記録用 JSON に入れる）
  const DO_LIKE = $dolike;           // true なら最新ツイートへのいいねも行う
  const MAX_LIKE_FAIL = 5;          // いいねがこの件数連続で失敗したら、いいね機能のみ無効化
  const LIKE_WAIT_MIN_MS = 2000;    // ツイート取得後の待機（最小 2 秒）
  const LIKE_WAIT_MAX_MS = 4000;    // ツイート取得後の待機（最大 4 秒）
  const DONE_KEY = "fb_done_$key";
  const LASTRUN_KEY = "fb_lastrun_$key";
  const REL_CACHE_KEY = "fb_rel_$key";
  // X の Web アプリが実際に送信している公開 Bearer（過去スクリプトと同一）
  const BEARER =
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";

  window.__fbStop = false;

  // ---- 小道具 -------------------------------------------------------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitMs = () => Math.floor(WAIT_MIN_MS + Math.random() * (WAIT_MAX_MS - WAIT_MIN_MS));
  const likeWaitMs = () =>
    Math.floor(LIKE_WAIT_MIN_MS + Math.random() * (LIKE_WAIT_MAX_MS - LIKE_WAIT_MIN_MS));
  const getCookie = (name) => {
    const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  };

  const loadDone = () => {
    try {
      const v = JSON.parse(localStorage.getItem(DONE_KEY) || "[]");
      return new Set(Array.isArray(v) ? v.map((s) => String(s).toLowerCase()) : []);
    } catch (e) {
      return new Set();
    }
  };
  const saveDone = (set) => {
    try {
      localStorage.setItem(DONE_KEY, JSON.stringify([...set]));
    } catch (e) {
      /* localStorage が使えない環境では進捗保存だけ諦める */
    }
  };
  const loadLastRun = () => {
    try {
      const v = Number(localStorage.getItem(LASTRUN_KEY) || "0");
      return Number.isFinite(v) ? v : 0;
    } catch (e) {
      return 0;
    }
  };
  const saveLastRun = (ms) => {
    try {
      localStorage.setItem(LASTRUN_KEY, String(ms));
    } catch (e) {
      /* 同上 */
    }
  };
  const loadRelCache = () => {
    try {
      const v = JSON.parse(localStorage.getItem(REL_CACHE_KEY) || "{}");
      return v && typeof v === "object" ? v : {};
    } catch (e) {
      return {};
    }
  };
  const saveRelCache = (obj) => {
    try {
      localStorage.setItem(REL_CACHE_KEY, JSON.stringify(obj));
    } catch (e) {
      /* 同上 */
    }
  };

  // ---- 事前チェック -------------------------------------------------------
  const ct0 = getCookie("ct0");
  if (!ct0) {
    console.error(
      "[中止] cookie の ct0 を取得できませんでした。X にログインした状態の x.com のタブで実行してください。"
    );
    return;
  }

  // 前回実行からの経過時間チェック（連続実行による自動化検知を避けるため）
  const nowMs = Date.now();
  const lastRun = loadLastRun();
  if (lastRun > 0 && window.__fbForce !== true) {
    const elapsedMin = (nowMs - lastRun) / 60000;
    if (elapsedMin < COOLDOWN_MIN) {
      const restMin = Math.ceil(COOLDOWN_MIN - elapsedMin);
      console.warn(
        "[中止] 前回の実行から " + Math.floor(elapsedMin) + " 分しか経っていません。" +
          "バッチ間は最低 " + COOLDOWN_MIN + " 分空けてください（あと約 " + restMin + " 分）。"
      );
      console.warn(
        "どうしても今すぐ続行したい場合のみ、Console に  window.__fbForce = true  " +
          "と入力して Enter を押してから、このファイルをもう一度貼り付けてください。"
      );
      return;
    }
  }

  const done = loadDone();
  const relCache = loadRelCache();
  const queue = TARGETS.filter((t) => !done.has(t.sn.toLowerCase()));
  const batch = queue.slice(0, MAX_PER_RUN);

  if (batch.length === 0) {
    console.log("[完了] 未処理の対象はありません（全 " + TARGETS.length + " 件は処理済みです）。");
    console.log(
      "もう一度最初からやり直す場合は  localStorage.removeItem('" + DONE_KEY + "')  を実行してください。"
    );
    return;
  }

  console.log(
    "[開始] 対象 " + TARGETS.length + " 件 / 未処理 " + queue.length + " 件 → 今回 " +
      batch.length + " 件を処理します（上限 " + MAX_PER_RUN + " 件）。"
  );
  console.log("停止したい時は  window.__fbStop = true  と入力して Enter を押してください。");

  const API_HEADERS = {
    authorization: BEARER,
    "x-csrf-token": ct0,
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-active-user": "yes",
  };

  // ---- 実行直前の関係再確認 -----------------------------------------------
  // friendships/lookup.json を screen_name 100 件ずつのバッチで叩き、
  // 「既にフォロー済み」「相手がフォローを外していた」相手を実行前に洗い出す。
  // リストは前日以前のスナップショット由来で鮮度が落ちているため、この再確認が要になる。
  async function prefetchRelations(rows) {
    const names = rows.map((t) => t.sn);
    let lastStatus = 200;
    for (let i = 0; i < names.length; i += 100) {
      const chunk = names.slice(i, i + 100);
      let res;
      try {
        res = await fetch(
          location.origin +
            "/i/api/1.1/friendships/lookup.json?screen_name=" +
            encodeURIComponent(chunk.join(",")),
          { method: "GET", credentials: "include", headers: API_HEADERS }
        );
      } catch (e) {
        return { status: 0, reason: "通信エラー: " + e.message };
      }
      lastStatus = res.status;
      if (!res.ok) return { status: res.status, reason: "HTTP " + res.status };
      let json = null;
      try {
        json = await res.json();
      } catch (e) {
        json = null;
      }
      if (!Array.isArray(json)) return { status: res.status, reason: "応答を解釈できず" };
      for (const u of json) {
        if (!u || !u.screen_name) continue;
        const key = String(u.screen_name).toLowerCase();
        const conns = Array.isArray(u.connections) ? u.connections : [];
        if (conns.indexOf("following") !== -1) {
          // こちらが既にフォローしている＝フォローバック済み
          relCache["following:" + key] = "1";
        }
        if (conns.indexOf("followed_by") === -1) {
          // 相手がフォローを外している＝フォローバックの前提が崩れている
          relCache["notfollowedby:" + key] = "1";
        }
        if (u.id_str) relCache["id:" + key] = String(u.id_str);
      }
      if (i + 100 < names.length) await sleep(1500);
    }
    saveRelCache(relCache);
    return { status: lastStatus };
  }

  // ---- 方式1: 内部 API ----------------------------------------------------
  async function followByApi(t) {
    const key = t.sn.toLowerCase();
    if (relCache["following:" + key] === "1") {
      return { ok: true, skipped: true, status: 0, note: "既にフォロー済み" };
    }
    if (relCache["notfollowedby:" + key] === "1") {
      return { ok: true, skipped: true, status: 0, note: "相手がフォローを外していた" };
    }
    const userId = relCache["id:" + key] || t.id;
    if (!userId) {
      return { ok: false, reason: "user_id が不明（rest_id 未取得）", status: 0 };
    }

    const body = new URLSearchParams({
      include_profile_interstitial_type: "1",
      include_blocking: "1",
      include_blocked_by: "1",
      include_followed_by: "1",
      include_want_retweets: "1",
      include_mute_edge: "1",
      include_can_dm: "1",
      include_can_media_tag: "1",
      skip_status: "1",
      user_id: userId,
    });
    let res;
    try {
      res = await fetch(location.origin + "/i/api/1.1/friendships/create.json", {
        method: "POST",
        credentials: "include",
        headers: Object.assign({}, API_HEADERS, {
          "content-type": "application/x-www-form-urlencoded",
        }),
        body: body.toString(),
      });
    } catch (e) {
      return { ok: false, reason: "通信エラー: " + e.message, status: 0 };
    }
    let json = null;
    try {
      json = await res.json();
    } catch (e) {
      json = null;
    }
    if (json && Array.isArray(json.errors) && json.errors.length > 0) {
      const e0 = json.errors[0];
      return {
        ok: false,
        reason: "API エラー code=" + e0.code + " " + e0.message,
        status: res.status,
      };
    }
    if (!res.ok) {
      return { ok: false, reason: "HTTP " + res.status, status: res.status };
    }
    return { ok: true, status: res.status };
  }

  // ---- 方式2: DOM 操作（/followers ページ上のフォローボタンをクリック）----
  async function findFollowButtonBySn(sn) {
    const target = "/" + sn.toLowerCase();
    for (let attempt = 0; attempt < 40; attempt++) {
      const cells = document.querySelectorAll('[data-testid="cellInnerDiv"]');
      for (const cell of cells) {
        const anchors = cell.querySelectorAll('a[role="link"]');
        let hit = false;
        for (const a of anchors) {
          const href = (a.getAttribute("href") || "").toLowerCase();
          if (href === target) {
            hit = true;
            break;
          }
        }
        if (!hit) continue;
        const btn = cell.querySelector('[data-testid$$="-follow"]');
        if (btn) return btn;
        // 既にフォロー済み（-unfollow ボタン）なら発見扱いにしない
        if (cell.querySelector('[data-testid$$="-unfollow"]')) return "already";
      }
      window.scrollBy(0, 1400);
      await sleep(1200);
    }
    return null;
  }

  async function followByDom(t) {
    window.scrollTo(0, 0);
    await sleep(1200);
    const btn = await findFollowButtonBySn(t.sn);
    if (btn === "already") return { ok: true, status: 0, note: "既にフォロー済み" };
    if (!btn) {
      return { ok: false, reason: "ページ内でフォローボタンを発見できず", status: 0 };
    }
    btn.click();
    await sleep(2000);
    return { ok: true, status: 0 };
  }

$likeengine

  // ---- 関係の一括事前確認 -------------------------------------------------
  const pre = await prefetchRelations(batch);
  if (pre.status === 401 || pre.status === 403 || pre.status === 404) {
    console.warn(
      "[注意] 関係の事前確認が HTTP " + pre.status + " で失敗しました（" + (pre.reason || "") +
        "）。DOM 操作方式で進みます。"
    );
  } else {
    const alreadyN = batch.filter((t) => relCache["following:" + t.sn.toLowerCase()] === "1").length;
    const goneN = batch.filter((t) => relCache["notfollowedby:" + t.sn.toLowerCase()] === "1").length;
    console.log(
      "[事前確認] 既にフォロー済み " + alreadyN + " 件・相手がフォローを外していた " + goneN +
        " 件はスキップします。"
    );
  }

  // ---- 実行ループ ---------------------------------------------------------
  let mode =
    pre.status === 401 || pre.status === 403 || pre.status === 404 ? "dom" : "api";
  let okCount = 0;
  let ngCount = 0;
  let skipCount = 0;
  let consecutiveFail = 0;
  let stoppedReason = null;
  const okSns = [];
  const failed = [];
  const skipped = [];
  // いいねの集計
  let likeEnabled = DO_LIKE;
  let likeOk = 0;
  let likeFail = 0;
  let likeSkip = 0;
  let likeConsecutiveFail = 0;
  const likeOkSns = [];
  const likeFailSns = [];

  saveLastRun(Date.now());

  for (let i = 0; i < batch.length; i++) {
    if (window.__fbStop === true) {
      stoppedReason = "window.__fbStop による手動停止";
      break;
    }

    const t = batch[i];
    const label = i + 1 + "/" + batch.length + " @" + t.sn;

    let r = mode === "api" ? await followByApi(t) : await followByDom(t);

    // API が権限系エラーを返した場合、/followers ページ上なら DOM 方式へ自動切替して再試行
    if (
      !r.ok &&
      mode === "api" &&
      (r.status === 403 || r.status === 404 || r.status === 401) &&
      /\/followers\b/.test(location.pathname)
    ) {
      console.warn(
        "[方式切替] 内部 API が HTTP " + r.status + " を返したため、DOM 操作方式へ切り替えます。"
      );
      mode = "dom";
      r = await followByDom(t);
    }

    // ---- いいね（フォローの成否にかかわらず、フォロー対象として送信した相手に行う）----
    // r.skipped（既にフォロー済み・相手がフォローを外していた）の相手にはいいねしない。
    let likeText = "";
    if (r.skipped) {
      likeText = likeEnabled ? "・いいね未実行" : "";
    } else if (!likeEnabled) {
      likeText = DO_LIKE ? "・いいね未実行（機能停止中）" : "";
    } else if (!r.ok) {
      // フォローが失敗した相手にはいいねを行わない（関係が成立していないため）
      likeText = "・いいね未実行";
    } else {
      // doLikeFor は DO_LIKE = true で生成した版にのみ存在する
      const lr = await (typeof doLikeFor === "function"
        ? doLikeFor(t)
        : Promise.resolve({ ok: false, skip: true, note: "いいね機能なし" }));
      if (lr.ok && lr.skip) {
        likeSkip++;
        likeConsecutiveFail = 0;
        likeText = "・いいねスキップ（" + (lr.note || "") + "）";
      } else if (lr.ok) {
        likeOk++;
        likeConsecutiveFail = 0;
        likeOkSns.push(t.sn);
        likeText = "・いいねOK";
      } else if (lr.skip) {
        likeSkip++;
        likeConsecutiveFail++;
        likeText = "・いいねスキップ（" + (lr.note || "ツイートを取得できず") + "）";
      } else {
        likeFail++;
        likeConsecutiveFail++;
        likeFailSns.push(t.sn);
        likeText = "・いいね失敗（" + (lr.note || "") + "）";
      }
      if (likeConsecutiveFail >= MAX_LIKE_FAIL) {
        likeEnabled = false;
        console.warn(
          "[いいね停止] いいねが " + MAX_LIKE_FAIL +
            " 件連続で失敗したため、いいね機能のみを無効化しました。フォローはこのまま続行します。"
        );
      }
    }

    if (r.skipped) {
      skipCount++;
      consecutiveFail = 0;
      skipped.push(t.sn);
      done.add(t.sn.toLowerCase());
      saveDone(done);
      console.log(label + " → スキップ（" + (r.note || "対象外") + "）" + likeText);
    } else if (r.ok) {
      okCount++;
      consecutiveFail = 0;
      done.add(t.sn.toLowerCase());
      saveDone(done);
      okSns.push(t.sn);
      console.log(label + " → フォローOK" + (r.note ? "（" + r.note + "）" : "") + likeText);
    } else {
      ngCount++;
      consecutiveFail++;
      failed.push(t.sn);
      console.log(label + " → フォロー失敗(" + r.reason + ")" + likeText);
    }

    if (consecutiveFail >= MAX_CONSECUTIVE_FAIL) {
      console.warn(
        "[全停止] 失敗が " + MAX_CONSECUTIVE_FAIL +
          " 件連続したため、これ以上の実行を中止しました。時間を空けてから再度お試しください。"
      );
      stoppedReason = "連続失敗 " + MAX_CONSECUTIVE_FAIL + " 件";
      break;
    }

    if (i < batch.length - 1) {
      const w = waitMs();
      console.log("  … 次の1件まで " + Math.round(w / 1000) + " 秒待機します");
      await sleep(w);
    }
  }

  saveLastRun(Date.now());

  // ---- 結果表示 -----------------------------------------------------------
  const remaining = TARGETS.filter((t) => !done.has(t.sn.toLowerCase())).map((t) => t.sn);

  console.log("==================== 実行結果 ====================");
  console.log("採用方式: " + (mode === "api" ? "内部 API 方式" : "DOM 操作方式"));
  if (stoppedReason) console.log("停止理由: " + stoppedReason);
  console.log(
    "フォロー成功 " + okCount + " 件・失敗 " + ngCount + " 件・スキップ " + skipCount +
      " 件・残り " + remaining.length + " 件（次回実行分）"
  );
  if (DO_LIKE) {
    console.log(
      "いいね 成功 " + likeOk + " 件・失敗 " + likeFail + " 件・スキップ " + likeSkip + " 件" +
        (likeEnabled ? "" : "（いいね機能は途中で無効化されました）")
    );
  }
  if (skipped.length > 0) {
    console.log("スキップした screen_name 配列:");
    console.log(JSON.stringify(skipped));
  }
  if (failed.length > 0) {
    console.log("失敗した screen_name 配列:");
    console.log(JSON.stringify(failed));
  } else {
    console.log("失敗した screen_name はありません。");
  }
  if (remaining.length > 0) {
    console.log(
      "未処理の screen_name 配列（次回はこのファイルを再度貼るだけで続きから実行されます）:"
    );
    console.log(JSON.stringify(remaining));
    console.log(
      "【クールダウン】次のバッチまで最低 " + COOLDOWN_MIN +
        " 分空けてください。残り " + remaining.length + " 件を終えるには、あと " +
        Math.ceil(remaining.length / MAX_PER_RUN) + " 回の貼り直しが必要です。"
    );
    console.log(
      "  " + COOLDOWN_MIN + " 分未満で貼り直した場合、本スクリプトは開始前に警告して中止します。"
    );
  } else {
    console.log("対象は全件処理済みです。");
  }

  // ---- 実行結果の記録用 1 行 JSON -----------------------------------------
  console.log(
    "[記録用] 下記1行をコピーして bi/outputs/x_posts/followback_result_" + RUN_DATE +
      ".json へ保存してください（追記可）:"
  );
  console.log(
    JSON.stringify({
      date: RUN_DATE,
      run_at: new Date().toISOString(),
      ok: okCount,
      fail: ngCount,
      skipped: skipCount,
      remaining: remaining.length,
      ok_sns: okSns,
      fail_sns: failed,
      skipped_sns: skipped,
      like_ok: likeOk,
      like_fail: likeFail,
      like_skip: likeSkip,
      like_ok_sns: likeOkSns,
      like_fail_sns: likeFailSns,
    })
  );
  console.log("=================================================");
})();
'''

# いいね機能の実装本体。--no-like のときは JS へ一切出力しない（$likeengine を空文字に置換）。
# Template 展開の対象ではないため、この文字列内の $ はそのまま出力される（現状 $ の使用なし）。
JS_LIKE_ENGINE = r'''  // ---- 最新ツイートの取得といいね -----------------------------------------
  // GraphQL のクエリ ID は X 側のデプロイで変わるため、既知の候補を順に試す。
  // 全滅した相手は「いいねスキップ」として記録し、フォローだけを実行する。
  const USER_TWEETS_QUERY_IDS = [
    "E3opETHurmVJflFsUBVuUQ",
    "V7H0Ap3_Hh2FyS75OCDO3Q",
    "9zyyd1hebl7oNWIPdA8HRw",
    "Uuw5X2n3tuiwuL2LqWDrgQ",
    "HuTx74BxAnezK1gWvYY7zg",
  ];
  const FAVORITE_TWEET_QUERY_IDS = [
    "lI07N6Otwv1PhnEgXILM7A",
    "eXPhKk3ZWNvB2Aqi8LlHiA",
    "ZYKSe-w7KEslx3JhSIk5LA",
  ];
  // 直近で成功したクエリ ID を先頭に固定し、2人目以降の無駄な 404 を避ける
  let userTweetsQid = null;
  let favoriteQid = null;
  let userTimelineDead = false;   // v1.1 の user_timeline が使えないと判明したら二度と叩かない

  const qidOrder = (list, pinned) =>
    pinned ? [pinned].concat(list.filter((q) => q !== pinned)) : list.slice();

  // GraphQL レスポンスから instructions 配下の entries を平坦に集める
  function collectEntries(node, acc) {
    if (!node || typeof node !== "object") return acc;
    if (Array.isArray(node)) {
      for (const v of node) collectEntries(v, acc);
      return acc;
    }
    if (Array.isArray(node.entries)) {
      for (const e of node.entries) acc.push(e);
    }
    if (node.entry && typeof node.entry === "object") acc.push(node.entry);
    for (const k of Object.keys(node)) {
      if (k === "entries" || k === "entry") continue;
      const v = node[k];
      if (v && typeof v === "object") collectEntries(v, acc);
    }
    return acc;
  }

  // entry から tweet の result オブジェクトを取り出す（TweetWithVisibilityResults を剥がす）
  function tweetResultOf(entry) {
    const content = entry && entry.content;
    if (!content) return null;
    let res =
      (content.itemContent && content.itemContent.tweet_results &&
        content.itemContent.tweet_results.result) || null;
    if (!res && content.content && content.content.itemContent &&
        content.content.itemContent.tweet_results) {
      res = content.content.itemContent.tweet_results.result;
    }
    while (res && res.__typename === "TweetWithVisibilityResults" && res.tweet) {
      res = res.tweet;
    }
    return res || null;
  }

  // GraphQL UserTweets から「固定・RT・リプライを除いた最新の本人ツイート」を選ぶ
  function pickLatestFromGraphql(json, userId) {
    const entries = collectEntries(json, []);
    let best = null;
    for (const entry of entries) {
      const eid = String((entry && entry.entryId) || "");
      // 固定ツイート・プロモーション枠は対象外
      if (eid.indexOf("promoted-") === 0 || eid.indexOf("pinned-") === 0) continue;
      if (/(^|-)pinned/i.test(eid) || /promoted/i.test(eid)) continue;
      const content = entry && entry.content;
      const sc = content && (content.socialContext ||
        (content.itemContent && content.itemContent.socialContext));
      if (sc) {
        const scText = JSON.stringify(sc).toLowerCase();
        if (scText.indexOf("pinned") !== -1 || scText.indexOf("固定") !== -1) continue;
      }
      const res = tweetResultOf(entry);
      if (!res) continue;
      const legacy = res.legacy;
      if (!legacy) continue;
      // リツイート・リプライを除外
      if (legacy.retweeted_status_result || legacy.retweeted_status) continue;
      if (legacy.in_reply_to_status_id_str) continue;
      // 本人のツイートであることを確認
      const author =
        (res.core && res.core.user_results && res.core.user_results.result) || null;
      const authorId = String(
        (author && author.rest_id) || legacy.user_id_str || ""
      );
      if (userId && authorId && authorId !== String(userId)) continue;
      if (!authorId) continue;
      const tid = String(res.rest_id || legacy.id_str || "");
      if (!tid) continue;
      const ts = Date.parse(legacy.created_at || "");
      const when = Number.isFinite(ts) ? ts : 0;
      if (!best || when > best.when) best = { id: tid, when: when };
    }
    return best ? best.id : null;
  }

  async function latestTweetIdByGraphql(t) {
    const userId = relCache["id:" + t.sn.toLowerCase()] || t.id;
    if (!userId) return { id: null, reason: "user_id が不明" };
    const variables = {
      userId: String(userId),
      count: 20,
      includePromotedContent: false,
      withQuickPromoteEligibilityTweetFields: false,
      withVoice: false,
      withV2Timeline: true,
    };
    const features = {
      rweb_video_screen_enabled: false,
      profile_label_improvements_pcf_label_in_post_enabled: true,
      rweb_tipjar_consumption_enabled: true,
      verified_phone_label_enabled: false,
      creator_subscriptions_tweet_preview_api_enabled: true,
      responsive_web_graphql_timeline_navigation_enabled: true,
      responsive_web_graphql_skip_user_profile_image_extensions_enabled: false,
      premium_content_api_read_enabled: false,
      communities_web_enable_tweet_community_results_fetch: true,
      c9s_tweet_anatomy_moderator_badge_enabled: true,
      responsive_web_grok_analyze_button_fetch_trends_enabled: false,
      responsive_web_grok_analyze_post_followups_enabled: false,
      responsive_web_jetfuel_frame: false,
      responsive_web_grok_share_attachment_enabled: true,
      articles_preview_enabled: true,
      responsive_web_edit_tweet_api_enabled: true,
      graphql_is_translatable_rweb_tweet_is_translatable_enabled: true,
      view_counts_everywhere_api_enabled: true,
      longform_notetweets_consumption_enabled: true,
      responsive_web_twitter_article_tweet_consumption_enabled: true,
      tweet_awards_web_tipping_enabled: false,
      responsive_web_grok_show_grok_translated_post: false,
      responsive_web_grok_analysis_button_from_backend: false,
      creator_subscriptions_quote_tweet_preview_enabled: false,
      freedom_of_speech_not_reach_fetch_enabled: true,
      standardized_nudges_misinfo: true,
      tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled: true,
      longform_notetweets_rich_text_read_enabled: true,
      longform_notetweets_inline_media_enabled: true,
      responsive_web_grok_image_annotation_enabled: true,
      responsive_web_enhance_cards_enabled: false,
    };
    const fieldToggles = { withArticlePlainText: false };
    const qs =
      "?variables=" + encodeURIComponent(JSON.stringify(variables)) +
      "&features=" + encodeURIComponent(JSON.stringify(features)) +
      "&fieldToggles=" + encodeURIComponent(JSON.stringify(fieldToggles));

    let lastReason = "取得できず";
    for (const qid of qidOrder(USER_TWEETS_QUERY_IDS, userTweetsQid)) {
      let res;
      try {
        res = await fetch(location.origin + "/i/api/graphql/" + qid + "/UserTweets" + qs, {
          method: "GET",
          credentials: "include",
          headers: API_HEADERS,
        });
      } catch (e) {
        lastReason = "通信エラー: " + e.message;
        continue;
      }
      if (!res.ok) {
        lastReason = "HTTP " + res.status;
        continue;
      }
      let json = null;
      try {
        json = await res.json();
      } catch (e) {
        lastReason = "応答を解釈できず";
        continue;
      }
      const tid = pickLatestFromGraphql(json, userId);
      if (tid) {
        userTweetsQid = qid;
        return { id: tid };
      }
      // 200 で返ったが対象ツイートが無い（全部固定/RT/リプライ、または投稿なし）
      userTweetsQid = qid;
      lastReason = "対象ツイートなし";
      break;
    }
    return { id: null, reason: lastReason };
  }

  // 第2手: v1.1 の user_timeline（廃止されている可能性があるため静かに失敗させる）
  async function latestTweetIdByV11(t) {
    if (userTimelineDead) return { id: null, reason: "v1.1 利用不可" };
    const userId = relCache["id:" + t.sn.toLowerCase()] || t.id;
    if (!userId) return { id: null, reason: "user_id が不明" };
    let res;
    try {
      res = await fetch(
        location.origin +
          "/i/api/1.1/statuses/user_timeline.json?user_id=" +
          encodeURIComponent(String(userId)) +
          "&count=5&exclude_replies=true&include_rts=false",
        { method: "GET", credentials: "include", headers: API_HEADERS }
      );
    } catch (e) {
      userTimelineDead = true;
      return { id: null, reason: "通信エラー" };
    }
    if (!res.ok) {
      if (res.status === 404 || res.status === 403 || res.status === 410) userTimelineDead = true;
      return { id: null, reason: "HTTP " + res.status };
    }
    let json = null;
    try {
      json = await res.json();
    } catch (e) {
      return { id: null, reason: "応答を解釈できず" };
    }
    if (!Array.isArray(json)) return { id: null, reason: "応答を解釈できず" };
    let best = null;
    for (const tw of json) {
      if (!tw || !tw.id_str) continue;
      if (tw.retweeted_status) continue;
      if (tw.in_reply_to_status_id_str) continue;
      const authorId = String((tw.user && tw.user.id_str) || tw.user_id_str || "");
      if (authorId && String(userId) !== authorId) continue;
      const ts = Date.parse(tw.created_at || "");
      const when = Number.isFinite(ts) ? ts : 0;
      if (!best || when > best.when) best = { id: String(tw.id_str), when: when };
    }
    return best ? { id: best.id } : { id: null, reason: "対象ツイートなし" };
  }

  async function latestTweetId(t) {
    const g = await latestTweetIdByGraphql(t);
    if (g.id) return g;
    const v = await latestTweetIdByV11(t);
    if (v.id) return v;
    return { id: null, reason: g.reason || v.reason || "取得できず" };
  }

  async function likeTweet(tweetId) {
    let lastReason = "いいねできず";
    for (const qid of qidOrder(FAVORITE_TWEET_QUERY_IDS, favoriteQid)) {
      let res;
      try {
        res = await fetch(location.origin + "/i/api/graphql/" + qid + "/FavoriteTweet", {
          method: "POST",
          credentials: "include",
          headers: Object.assign({}, API_HEADERS, { "content-type": "application/json" }),
          body: JSON.stringify({ variables: { tweet_id: String(tweetId) }, queryId: qid }),
        });
      } catch (e) {
        lastReason = "通信エラー: " + e.message;
        continue;
      }
      let json = null;
      try {
        json = await res.json();
      } catch (e) {
        json = null;
      }
      if (json && Array.isArray(json.errors) && json.errors.length > 0) {
        const msg = String(json.errors[0].message || "");
        if (/already/i.test(msg)) {
          favoriteQid = qid;
          return { ok: true, skipped: true, note: "既にいいね済み" };
        }
        lastReason = "API エラー " + msg;
        if (res.status === 404 || res.status === 400) continue;
        favoriteQid = qid;
        return { ok: false, reason: lastReason };
      }
      if (!res.ok) {
        lastReason = "HTTP " + res.status;
        continue;
      }
      favoriteQid = qid;
      return { ok: true };
    }
    return { ok: false, reason: lastReason };
  }

  // 1人分のいいね処理（最新ツイート取得 → 待機 → いいね）
  async function doLikeFor(t) {
    const got = await latestTweetId(t);
    const w = likeWaitMs();
    await sleep(w);
    if (!got.id) {
      return { ok: false, skip: true, note: "ツイートを取得できず（" + (got.reason || "") + "）" };
    }
    const r = await likeTweet(got.id);
    if (r.ok && r.skipped) return { ok: true, skip: true, note: r.note };
    if (r.ok) return { ok: true, tweetId: got.id };
    return { ok: false, note: r.reason };
  }'''



def build_md(
    account: str,
    target_date: str,
    candidates: list,
    excluded: list,
    capped_out: int,
    max_candidates: int,
    following_users: dict,
    followers_users: dict,
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
    js_path: str,
    csv_path: str,
    do_like: bool = True,
) -> str:
    """research/sns/{date}_followback.md を組み立てる。"""
    following_sns = set(following_users.keys())
    followers_sns = set(followers_users.keys())
    mutual_n = len(following_sns & followers_sns)
    not_followed_back_n = len(followers_sns - following_sns)
    targets = sort_candidates(candidates)[:max_candidates]

    L = []
    a = L.append
    a("# ノクトラ X（@%s）フォローバック候補  %s" % (account, target_date))
    a("")
    a("## データ基準日と件数サマリー")
    a("")
    a("| 項目 | 件数 | 取得元 |")
    a("|---|---|---|")
    a(
        "| フォロワー（followers） | %d | %s（GHA・%s取得） |"
        % (len(followers_sns), "gha/" + os.path.basename(followers_path), jp_time(followers_fetched))
    )
    a(
        "| フォロー中（following） | %d | %s（%s取得） |"
        % (len(following_sns), os.path.basename(following_path), jp_time(following_fetched))
    )
    a("| 相互フォロー | %d | 上記2件の突合 |" % mutual_n)
    a("| 未フォローバック（相手のみ） | %d | 同上 |" % not_followed_back_n)
    a("| 除外 | %d | 下記の判定基準 |" % len(excluded))
    a("| **本日のフォロー対象** | **%d** | 除外後・上限%d件 |" % (len(targets), max_candidates))
    for reason, cnt in reason_counts(excluded):
        a("| 　除外内訳: %s | %d | 同上 |" % (reason, cnt))
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
    if capped_out > 0:
        a(
            "**注記**: 除外後の候補は %d 件でしたが、`--max-candidates %d` の上限により上位 %d 件のみを"
            "CSV の並び順の先頭と .js の対象にしています。残る %d 件は CSV に verdict = followback として"
            "残っており、次回以降の対象になります。"
            % (len(candidates), max_candidates, len(targets), capped_out)
        )
        a("")

    # フォロー対象候補
    a("## フォロー対象候補")
    a("")
    if targets:
        a(
            "未フォローバック %d 件のうち、除外基準に触れなかった %d 件を相手のフォロワー数の降順で"
            "並べています。本日の対象は上位 %d 件です。"
            % (not_followed_back_n, len(candidates), len(targets))
        )
        a("")
        a("| screen_name | name | followers | following | ff_ratio | 投稿数 | bio |")
        a("|---|---|---|---|---|---|---|")
        for r in targets:
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
                    esc_pipe(bio_head(u.get("profile_bio"))),
                )
            )
        a("")
        a("`%s` を生成済みです（対象 %d 件）。" % (rel_repo(js_path), len(targets)))
    else:
        a(
            "**本日は 0 件です。水増しはしていません。** 未フォローバック %d 件は全て除外基準に"
            "触れました。フォロー実行スクリプト（.js）は生成していません。" % not_followed_back_n
        )
    a("")

    # 除外
    a("## 除外した相手と理由")
    a("")
    if excluded:
        a(
            "未フォローバック %d 件のうち %d 件を除外しました。理由別の内訳は次の通りです。"
            % (not_followed_back_n, len(excluded))
        )
        a("")
        for reason, cnt in reason_counts(excluded):
            a("- %s: %d 件" % (reason, cnt))
        a("")
        a("| screen_name | 除外理由 | followers | following | ff_ratio | 投稿数 |")
        a("|---|---|---|---|---|---|")
        for r in sort_excluded(excluded):
            u = r["user"]
            a(
                "| %s | %s | %s | %s | %s | %s |"
                % (
                    u.get("screen_name", ""),
                    r["exclude_reason"],
                    u.get("followers_count", ""),
                    u.get("friends_count", ""),
                    u.get("ff_ratio", ""),
                    u.get("statuses_count", ""),
                )
            )
    else:
        a(
            "**本日は 0 件です。水増しはしていません。** 未フォローバック %d 件のうち、除外基準に"
            "触れた相手は 1 件もありませんでした。" % not_followed_back_n
        )
    a("")

    # PM の実行手順
    a("## PM の実行手順")
    a("")
    if targets:
        runs = (len(targets) + MAX_PER_RUN - 1) // MAX_PER_RUN
        batch_n = min(len(targets), MAX_PER_RUN)
        per_person_sec = 17 if do_like else 14
        run_min = max(1, int(round(batch_n * per_person_sec / 60)))
        if do_like:
            a(
                "生成した .js は、対象1人につき「フォロー」と「その相手の最新ツイート1件へのいいね」の"
                "2つを行います。いいねの対象は固定ツイートを除いた最新の本人ツイートで、リツイート・"
                "リプライも除きます。ツイートを取得できなかった相手・いいねが失敗した相手も"
                "フォローは実行します。"
            )
            a("")
        a("1. ブラウザで `https://x.com/%s/followers` を開き、ノクトラ本人でログインした状態にします。" % account)
        a("2. F12 キー（または右クリック → 検証）で「Console」タブを開きます。")
        a("3. `%s` の中身を全文コピーして Console に貼り付け、Enter を押します。" % rel_repo(js_path))
        a(
            "4. 1回の実行上限は %d 件（MAX_PER_RUN）です。**%s**。処理済みの相手はブラウザの "
            "localStorage（キー `fb_done_%s`）に記録されるため、貼り直すたびに続きから進みます。"
            "処理済みの判定はフォローの完了で行い、いいねの成否では変わりません。"
            % (MAX_PER_RUN, runs_phrase(len(targets)), target_date.replace("-", "_"))
        )
        a(
            "5. **1回（%d 件）実行したら、次の貼り直しまで最低 %d 分空けてください。** 短時間に連続で"
            "フォローすると X 側の自動化検知に掛かるためです。前回実行から %d 分未満で貼り直した場合、"
            "スクリプトは開始前に警告して中止します（続行したい場合のみ Console で "
            "`window.__fbForce = true` を実行してから貼り直します）。"
            % (MAX_PER_RUN, COOLDOWN_MIN, COOLDOWN_MIN)
        )
        if runs > 1:
            a(
                "6. 1回（%d 件）の実行時間はおよそ %d 分です。全 %d 件を終えるまでに %d 回の実行が必要で、"
                "バッチ間の待機だけで最低およそ %d 分掛かります。1 日で終える必要はありません。"
                % (batch_n, run_min, len(targets), runs, (runs - 1) * COOLDOWN_MIN)
            )
        else:
            a(
                "6. 本日の対象 %d 件は 1 回の実行（およそ %d 分）で終わります。翌日以降に新しい候補が"
                "出た場合は、その日の .js を改めて生成します。" % (len(targets), run_min)
            )
        a(
            "7. 実行が終わるたび、Console に出る `[記録用]` の1行 JSON をコピーして "
            "`bi/outputs/x_posts/followback_result_%s.json` へ保存してください（追記可）。"
            "JS の実行結果はファイルに残らないため、この1行が唯一の実行記録になります。" % target_date
        )
        a("")
        a("安全設計:")
        a("")
        if do_like:
            a("- 1人あたり、ツイート取得後に 2〜4 秒・次の1人へ進む前に 8〜20 秒のランダム待機。")
        else:
            a("- 1件ごとに 8〜20 秒のランダム待機。")
        a("- フォローの失敗が 3 件連続したら即座に全停止。")
        if do_like:
            a(
                "- いいねの失敗は連続失敗カウントに含めません。いいねが 5 件連続で失敗した場合は"
                "いいね機能のみを無効化し、フォローだけを続行します。"
            )
        a(
            "- 実行直前に `friendships/lookup.json` へ screen_name を 100 件ずつ問い合わせ、"
            "`connections` に `following` がある相手（＝既にフォロー済み）と、`followed_by` が"
            "無くなっている相手（＝相手がフォローを外していた）はフォローせずスキップします。"
            "リストの鮮度落ちによる二重フォロー・不要フォローを防ぐためです。"
        )
        a("- 途中で止めたい時は Console に `window.__fbStop = true` と入力して Enter。")
        a("- 外部への送信は一切ありません。認証はログイン中の cookie（ct0）をブラウザ内で読むだけです。")
    else:
        a(
            "**本日のフォロー対象は 0 件のため、実行する作業はありません。** フォロー実行スクリプト"
            "（.js）も生成していません。次回、除外基準に触れない相手が現れた時点で改めて生成します。"
        )
    a("")

    # 判定基準
    a("## 判定基準")
    a("")
    a(
        "- **候補の定義**: followers に居て following に居ない相手（＝相手がフォローしてくれていて、"
        "こちらが未フォロー）。相互フォローと片思いフォローは対象外です。"
    )
    a("- 上記から次に該当する相手を除外します。")
    a("")
    a("  1. `friends_count` が %d 以上 → フォロー数3000以上。" % MAX_FRIENDS_COUNT)
    a("  2. `ff_ratio` が %s 以上 → FF比2.0以上（フォロー数がフォロワー数の2倍以上）。" % MAX_FF_RATIO)
    a("  3. `statuses_count` が %d 以下 → 投稿数100以下。" % MIN_STATUSES_COUNT)
    a(
        "  4. bio に相互フォロー系のキーワード（%s ほか）を含む → bio相互フォロー系。"
        % "・".join(MUTUAL_FOLLOW_KEYWORDS[:4])
    )
    a("  5. `protected` が true → 鍵アカウント（投稿を読めません）。")
    a("  6. `bi/data/x_influencers.yaml` に記載 → influencers除外（別枠で扱う情報源）。")
    a(
        "  7. 過去の following スナップショットに在籍していたのに現在いない → 過去にリム済み。"
        "再フォローするとフォロー／アンフォローの反復になるため必ず外します。"
    )
    a("")
    a("- 除外に該当した相手も CSV には `verdict` = `exclude` として全件残しています。")
    a(
        "- 候補の並び順は相手のフォロワー数の降順（同数は screen_name 昇順）です。"
        "上限は `--max-candidates`（既定 200）です。"
    )
    a("")

    # データ上の制約
    a("## データ上の制約")
    a("")
    snap_line = "・".join("%s（%d 件）" % (d, len(u)) for d, u, _p in following_snaps)
    a(
        "- **リム済み判定の精度**: following の過去スナップショットは %s の %d 点です。"
        "スナップショットとスナップショットの間だけ following に居た相手（フォローして次の取得前に"
        "外した相手）は記録に残らないため、リム済みとして検出できません。"
        % (snap_line, len(following_snaps))
    )
    a(
        "- followers の過去スナップショットは %d 点です%s。"
        % (
            len(followers_snaps),
            (
                "（%s は followers 0 件の欠測のため除外）" % "・".join(missing_followers_dates)
                if missing_followers_dates
                else ""
            ),
        )
    )
    if os.path.basename(following_path) != os.path.basename(followers_path):
        a(
            "- 取得元とその時刻が分かれています（followers は %s、following は %s）。この間の増減は"
            "反映されません。スクリプト側の実行直前再確認でこのずれは吸収されます。"
            % (jp_time(followers_fetched), jp_time(following_fetched))
        )
    if followers_is_fallback:
        a(
            "- %s の followers が取得できていないため %s 分を代用しています。この間に増えた"
            "フォロワーは候補に入りません。" % (target_date, followers_used_date)
        )
    a(
        "- 各アカウントの実際の投稿内容は取得していません。判定は bio・フォロワー数・フォロー数・"
        "投稿数・ff_ratio・鍵アカウント判定・スナップショット在籍履歴のみに基づきます。"
    )
    a(
        "- bio は取得時点の文字列です。取得後に相互フォロー系の記述へ書き換えた相手は検出できません。"
    )
    a("")
    a("生成元: `bi/pipelines/make_followback_candidates.py`（判定結果の全件は `%s`）" % rel_repo(csv_path))
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(
        description="X のフォローバック候補抽出と Console フォロー実行スクリプト生成（読み取り専用）"
    )
    p.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（省略時は JST の当日）")
    p.add_argument("--account", default="noctra__ai", help="対象アカウントの screen_name（既定 noctra__ai）")
    p.add_argument("--dry-run", action="store_true", help="ファイルを出力せず件数だけ表示する")
    p.add_argument(
        "--no-like",
        action="store_true",
        help="生成する JS でいいねを行わない（フォローのみの従来動作。既定はいいねあり）",
    )
    p.add_argument(
        "--max-candidates",
        type=int,
        default=200,
        help="CSV / JS に載せる候補の上限（優先度上位から。既定 200）",
    )
    args = p.parse_args()

    target_date = args.date or jst_today()
    try:
        parse_date(target_date)
    except ValueError:
        fail("--date は YYYY-MM-DD 形式で指定してください（受領値: %s）" % target_date)
    if args.max_candidates < 1:
        fail("--max-candidates は 1 以上で指定してください（受領値: %d）" % args.max_candidates)

    account = args.account

    # --- 入力 ---------------------------------------------------------------
    following_list, following_path, following_fetched, following_acct = find_current_following(
        account, target_date
    )
    followers_list, followers_path, followers_fetched, followers_used_date, followers_is_fallback = (
        find_followers(account, target_date)
    )

    following_users = users_by_sn(following_list)
    followers_users = users_by_sn(followers_list)
    profile = following_acct.get("profile") or {}

    following_snaps, followers_snaps = collect_snapshots(account, target_date)
    # 当日の following は探索で確定したものを必ず使う（collect_snapshots の同日分を上書き）
    following_snaps = [s for s in following_snaps if s[0] != target_date]
    following_snaps.append((target_date, following_users, following_path))
    following_snaps.sort(key=lambda s: s[0])

    # followers 0 件の欠測日（在籍履歴から落とした日）を洗い出して注記に使う
    have_followers_dates = {d for d, _s, _p in followers_snaps}
    missing_followers_dates = []
    for p_ in sorted(glob.glob(os.path.join(GHA_DIR, "profile_daily_*.json"))):
        m = DATE_RE.search(os.path.basename(p_))
        if not m or m.group(1) > target_date:
            continue
        if m.group(1) not in have_followers_dates:
            missing_followers_dates.append(m.group(1))

    # --- 未フォローバック抽出 -----------------------------------------------
    not_followed_back_sns = sorted(set(followers_users.keys()) - set(following_users.keys()))

    exclusions = load_influencer_exclusions()
    unfollowed_history = build_unfollowed_history(following_snaps, following_users)

    candidates = []
    excluded = []
    for sn in not_followed_back_sns:
        u = followers_users[sn]
        reason = judge(u, sn, exclusions, unfollowed_history)
        if reason:
            excluded.append({"user": u, "verdict": "exclude", "exclude_reason": reason})
        else:
            candidates.append({"user": u, "verdict": "followback", "exclude_reason": ""})

    targets = sort_candidates(candidates)[: args.max_candidates]
    capped_out = len(candidates) - len(targets)

    csv_path = os.path.join(X_POSTS_DIR, "followback_candidates_%s.csv" % target_date)
    js_path = os.path.join(X_POSTS_DIR, "followback_console_%s.js" % target_date)
    md_path = os.path.join(RESEARCH_SNS_DIR, "%s_followback.md" % target_date)

    # --- 集計表示 -----------------------------------------------------------
    print("対象アカウント : @%s" % account)
    print("対象日         : %s（候補上限 %d 件）" % (target_date, args.max_candidates))
    print(
        "followers      : %d 件  %s%s"
        % (
            len(followers_users),
            rel_repo(followers_path),
            ("  ※%s の当日分が無いため代用" % target_date) if followers_is_fallback else "",
        )
    )
    print("following      : %d 件  %s" % (len(following_users), rel_repo(following_path)))
    print("following スナップショット: %s" % "・".join("%s(%d)" % (d, len(u)) for d, u, _ in following_snaps))
    print("followers スナップショット: %d 点%s" % (
        len(followers_snaps),
        ("（欠測除外 %s）" % "・".join(missing_followers_dates)) if missing_followers_dates else "",
    ))
    print("相互フォロー   : %d 件" % len(set(following_users.keys()) & set(followers_users.keys())))
    print("未フォローバック: %d 件" % len(not_followed_back_sns))
    print("  候補（followback）: %d 件" % len(candidates))
    print("  除外（exclude）   : %d 件" % len(excluded))
    for reason, cnt in reason_counts(excluded):
        print("    - %s: %d 件" % (reason, cnt))
    print("  本日のフォロー対象: %d 件%s" % (
        len(targets),
        ("（上限により %d 件を次回送り）" % capped_out) if capped_out > 0 else "",
    ))
    do_like = not args.no_like
    if targets:
        runs = (len(targets) + MAX_PER_RUN - 1) // MAX_PER_RUN
        batch_n = min(len(targets), MAX_PER_RUN)
        run_min = max(1, int(round(batch_n * (17 if do_like else 14) / 60)))
        print(
            "    1回 %d 件・貼り直し %d 回・バッチ間 %d 分以上（待機だけで最低 %d 分）"
            % (MAX_PER_RUN, runs, COOLDOWN_MIN, max(runs - 1, 0) * COOLDOWN_MIN)
        )
        print(
            "    JS の動作: %s（1回の実行時間はおよそ %d 分）"
            % ("フォロー + 最新ツイートへのいいね" if do_like else "フォローのみ（--no-like）", run_min)
        )

    if args.dry_run:
        print()
        print("[dry-run] ファイルは出力していません。出力予定は次の通りです。")
        print("  CSV : %s" % rel_repo(csv_path))
        print("  JS  : %s" % (rel_repo(js_path) if targets else "（対象 0 件のため生成しません）"))
        print("  MD  : %s" % rel_repo(md_path))
        return 0

    # --- 出力（既存ファイルは上書きしない）---------------------------------
    existing = [p_ for p_ in [csv_path, md_path] if os.path.exists(p_)]
    if targets and os.path.exists(js_path):
        existing.append(js_path)
    if existing:
        fail(
            "出力先に既存ファイルがあります。上書きしないため中止しました。\n  "
            + "\n  ".join(rel_repo(p_) for p_ in existing)
            + "\n  既存分を確認したい場合は --dry-run で件数だけ表示できます。"
        )

    os.makedirs(X_POSTS_DIR, exist_ok=True)
    os.makedirs(RESEARCH_SNS_DIR, exist_ok=True)

    write_csv(csv_path, candidates, excluded)
    print()
    print("CSV 出力: %s（%d 行）" % (rel_repo(csv_path), len(candidates) + len(excluded)))

    if targets:
        js = build_js(
            account=account,
            target_date=target_date,
            targets=targets,
            not_followed_back=len(not_followed_back_sns),
            excluded_n=len(excluded),
            do_like=do_like,
        )
        with open(js_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(js)
        print("JS  出力: %s（対象 %d 件）" % (rel_repo(js_path), len(targets)))
    else:
        print("JS  出力: なし（対象 0 件のため生成しません）")

    md_text = build_md(
        account=account,
        target_date=target_date,
        candidates=candidates,
        excluded=excluded,
        capped_out=capped_out,
        max_candidates=args.max_candidates,
        following_users=following_users,
        followers_users=followers_users,
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
        js_path=js_path,
        csv_path=csv_path,
        do_like=do_like,
    )
    with open(md_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(md_text)
    print("MD  出力: %s" % rel_repo(md_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
