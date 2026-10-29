# -*- coding: utf-8 -*-
"""
動意「なぜ動いた」の理由ゲート（lib/reason_check.py）と材料の説明力判定（lib/mover_materials.py）の回帰テスト。

【実行方法】
    python bi/pipelines/tests/test_reason_check.py
リポジトリに pytest は導入されていないため、test_data_guards.py と同じ「単体で走る素のスクリプト」方式。
関数名は test_* に揃えてあるので、pytest を入れた場合はそのまま収集できる。

【フィクスチャ】
    fixtures/reason_check_20261027.json
        2026-10-27 動意日次の facts（make_mover_report.py の実走・2026-10-30）から 3905・593A・2160・4493 を抜粋。
        materials には strength / timing_fit が無い（2026-10-30 より前の形）。reason_check が後付けで判定する経路も試す。

【守るもの】
    - 3905: PM 合格例が合格・循環説明（値動きの語だけ）が (e) で不合格・機械差し替え文が合格（2026-10-28）
    - 593A: 「個人投資家向け通期決算説明会のお知らせ」だけの理由文が (h)「説明力のない開示」で不合格（2026-10-30）
    - 2160: 株式譲渡報告書（weak）しか無い → 機械差し替えは固定文 NO_STRONG（確認した開示に表題）で合格
    - 4493: セミナー告知（weak）を理由に選ばない
    - 語彙の複製（PRICE_REPORT_PATTERN）が mover_materials.py と一致
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import mover_materials as mm  # noqa: E402
from lib import reason_check as rc  # noqa: E402

FIX = HERE / "fixtures" / "reason_check_20261027.json"


def _facts() -> dict:
    return json.loads(FIX.read_text(encoding="utf-8"))


def _fails(code: str, name: str, body: str, facts: dict) -> set[str]:
    md = f"### 1位 {code} {name}　+1.0%\n\n**なぜ動いた**：{body}\n"
    return {f.check for f in rc.check_reasons(md, {"stocks": {code: facts["stocks"][code]}}) if f.severity == "fail"}


def test_vocab_copy_matches():
    assert rc.PRICE_REPORT_PATTERN == mm.PRICE_REPORT_PATTERN


def test_classify_strength_weak_notices():
    for t in ("2026年9月期 個人投資家向け通期決算説明会（オンライン）のお知らせ",
              "第三者割当に係る株式譲渡報告書（新株式）",
              "【緊急開催セミナー】重大脆弱性を狙うサイバー攻撃への備え",
              "第23回新株予約権の行使状況に関するお知らせ",
              "コーポレート・ガバナンスに関する報告書 2026/10/21",
              "定款の一部変更に関するお知らせ",
              "役員の異動に関するお知らせ",
              "当社の防衛事業における取り組みについて(再掲）",
              "Notice of Revision to Earnings Forecasts",
              "（訂正）「支配株主等に関する事項について」の訂正につきまして"):
        assert mm.classify_strength(t, "", "", "tdnet")[0] == "weak", t


def test_classify_strength_strong_materials():
    for t in ("AIインフラ及びGPUコンピュートサービスの提供開始に関するお知らせ",
              "業績予想の修正に関するお知らせ",
              "2027年３月期第２四半期（中間期）決算短信〔日本基準〕(連結)",
              "業績予想及び配当予想の修正（増配）に関するお知らせ",
              "システム開発業務の受注に関するお知らせ",
              "自己株式取得に係る事項の決定に関するお知らせ",
              "株式分割及び定款の一部変更に関するお知らせ",
              "株式会社ハニー・ビーの株式取得（子会社化）に関するお知らせ",
              "代表取締役の異動に関するお知らせ",
              "（訂正・数値データ訂正）「2026年9月期 決算短信」の一部訂正について"):
        assert mm.classify_strength(t, "", "", "tdnet")[0] == "strong", t


def test_timing_fit_rules():
    from datetime import date
    # 引け後（15:30 以降）→ 翌営業日・大引け前 30 分（15:00〜15:30）→ 当日と翌営業日・日付だけ → 当日と翌営業日
    assert mm.reaction_days("2026-10-26T15:30", True) == {date(2026, 10, 27)}
    assert mm.reaction_days("2026-10-26T15:00", True) == {date(2026, 10, 26), date(2026, 10, 27)}
    assert mm.reaction_days("2026-10-23T12:15", True) == {date(2026, 10, 23)}
    assert mm.reaction_days("2026-10-23", False) == {date(2026, 10, 23), date(2026, 10, 26)}
    mats = [{"title": "システム開発業務の受注に関するお知らせ", "dt": "2026-10-26T16:00", "time_known": True,
             "source": "tdnet"},
            {"title": "システム開発業務の受注に関するお知らせ", "dt": "2026-10-22T11:00", "time_known": True,
             "source": "tdnet"}]
    mm.annotate_strength(mats, "2026-10-27", "2026-10-27")
    assert [m["timing_fit"] for m in mats] == [True, False]


def test_3905_pass_and_circular_fail():
    f = _facts()
    ok = ("10/27（火）＋20.7%：10/23 12:15 頃、会社 IR がプレスリリース『AI インフラ及び GPU コンピュートサービスの提供開始』"
          "を公表。契約額 5 年累計 1,399 億円。これを起点に 3 日連続ストップ高の 3 日目。")
    assert _fails("3905", "データセクション", ok, f) == set()
    circ = "10/27（火）＋20.7%：ストップ高買い気配で始まり、3 日連続の S 高となった。"
    assert "e" in _fails("3905", "データセクション", circ, f)
    # 説明会のお知らせ（weak）だけで理由を書くと (h)
    weak_only = "10/27（火）＋20.7%：10/23、会社 IR が『AI データセンターの稼働状況等に関する説明会』のお知らせを公表。"
    assert "h" in _fails("3905", "データセクション", weak_only, f)


def test_3905_fallback_uses_strong_origin():
    f = _facts()
    st = f["stocks"]["3905"]
    fb = rc.build_fallback_sentence(st)
    assert "AIインフラ及びGPUコンピュートサービスの提供開始" in fb, fb
    assert "これを起点に" in fb
    assert _fails("3905", "データセクション", fb, f) == set(), fb


def test_593A_briefing_notice_only_fails_h():
    f = _facts()
    body = ("10/27（火）＋2.2%：10/23 12:00、ティアフォーが『2026年9月期 個人投資家向け通期決算説明会（オンライン）のお知らせ』"
            "を公表。")
    got = _fails("593A", "ティアフォー", body, f)
    assert "h" in got, got
    fb = rc.build_fallback_sentence(f["stocks"]["593A"])
    assert "説明会" not in fb, fb
    assert _fails("593A", "ティアフォー", fb, f) == set(), fb


def test_2160_weak_only_gets_fixed_sentence():
    f = _facts()
    st = f["stocks"]["2160"]
    fb = rc.build_fallback_sentence(st)
    assert rc.NO_STRONG_HEAD in fb, fb
    assert "株式譲渡報告書" in fb and fb.count("株式譲渡報告書") == 1, fb
    assert _fails("2160", "ジーエヌアイグループ", fb, f) == set(), fb
    # 株式譲渡報告書を理由に書くと (h)
    body = "10/27（火）＋6.1%：10/23 11:30、ジーエヌアイグループが『第三者割当に係る株式譲渡報告書（新株式）』を公表。"
    assert "h" in _fails("2160", "ジーエヌアイグループ", body, f)
    # 書き手が「確認した開示: なし」の固定文を書いても facts と違えば (e)（検査側が入れ直す）
    assert "e" in _fails("2160", "ジーエヌアイグループ", "10/27（火）＋6.1%：" + rc.NO_PRIMARY + "。", f)
    # 旧定型文は (e)
    assert "e" in _fails("2160", "ジーエヌアイグループ", "10/27（火）＋6.1%：" + rc.NO_PRIMARY_OLD + "。", f)


def test_fixed_sentence_misuse_when_strong_exists():
    f = _facts()
    body = "10/27（火）＋20.7%：" + rc.NO_STRONG_TMPL.format(weak="なし") + "。"
    assert "e" in _fails("3905", "データセクション", body, f)


def test_4493_seminar_not_chosen():
    f = _facts()
    fb = rc.build_fallback_sentence(f["stocks"]["4493"])
    assert "『【緊急開催セミナー】" not in fb.split("確認した開示")[0], fb
    assert _fails("4493", "サイバーセキュリティクラウド", fb, f) == set(), fb


def test_annotated_facts_same_result():
    """make_mover_report.py が strength / timing_fit を付けた facts でも同じ結果になる。"""
    f = _facts()
    g = copy.deepcopy(f)
    for c, st in g["stocks"].items():
        mm.annotate_strength(st["materials"], st["origin_date"], st["window"][1][:10])
    for c in ("3905", "593A", "2160", "4493"):
        assert rc.build_fallback_sentence(f["stocks"][c]) == rc.build_fallback_sentence(g["stocks"][c]), c


def test_render_marks():
    f = _facts()
    st = copy.deepcopy(f["stocks"]["593A"])
    mm.annotate_strength(st["materials"], st["origin_date"], st["window"][1][:10])
    lines = mm.render_materials({"materials": st["materials"], "price_reports": st["price_reports"] or [],
                                 "origin_date": st["origin_date"], "streak_days": 1, "streak_pos": 1})
    body = "\n".join(lines)
    assert "[weak]" in body and "[strong]" in body
    assert any(ln.startswith("- [weak]") and "説明会" in ln for ln in lines)


def main() -> int:
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
        except AssertionError as e:
            bad += 1
            print(f"[FAIL] {name}: {e}")
    print(f"{len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
