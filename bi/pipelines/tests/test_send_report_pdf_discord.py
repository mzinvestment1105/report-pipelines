# -*- coding: utf-8 -*-
"""send_report_pdf_discord.py の最小テスト（外部送信はモック・本番ファイルは触らない）。

【なぜこのファイルだけテストするか】
send_report_pdf_discord.py は GHA 12 workflow から 21 回参照される唯一の配信経路で、
ここが壊れると全レポート種別の配信が同時に止まる一点集中リスクを持つ。
bi/pipelines/ 配下 132 本の .py のうちテストがあるのは data_guards.py だけで、
この最頻出スクリプトは無検査だった（2026-09-07 実測）。

【何を検証するか（関数化されている範囲＝引数処理・webhook 選択・PDF 不在・ペイロード組み立て）】
  1. --kind が KIND_CONFIG のキーに限定される（未知の kind は argparse が弾く）
  2. md が存在しない日付では PDF を作らず戻り値 1 で終わる（配信しない）
  3. 種別ごとに正しい webhook 環境変数から URL を読む（誤送信の防止）
  4. webhook 未設定なら送信せず戻り値 1（Discord へ空 URL を叩きに行かない）
  5. --skip-send は PDF を作るが送信しない
  6. _post_pdf が Discord へ渡す multipart ペイロードの形（content・filename・
     application/pdf・payload_json）が崩れていない
  7. _post_pdf が 4xx/5xx を False として返す（送信失敗を成功と誤認しない）
  8. 動意の md にだけ文書タイトル H1 を足す _ensure_movers_doc_title の分岐

【外部依存の扱い】
実際の Discord 送信（requests.post）と PDF 描画（render_markdown_to_pdf）はモックする。
ネットワークにも本番 webhook にも触れず、bi/outputs/ の本番ファイルも書き換えない
（PDF 出力先だけは monkeypatch で tmp_path へ逃がす）。

【実行方法】
    python -m pytest bi/pipelines/tests/test_send_report_pdf_discord.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# bi/pipelines を import パスに載せる（対象スクリプトが同階層 import を使うため）
PIPELINES_DIR = Path(__file__).resolve().parents[1]
if str(PIPELINES_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINES_DIR))

import send_report_pdf_discord as mod  # noqa: E402
from send_report_jpeg_discord import KIND_CONFIG  # noqa: E402


# ---------------------------------------------------------------- helpers
class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """REPO_ROOT と PDF 出力先を tmp へ逃がし、本番ファイルを触らせない。"""
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    rendered: list[dict] = []

    def _fake_render(md_text, out_path, kind=None, target_date=None):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"%PDF-1.4 fake")
        rendered.append({"kind": kind, "target_date": target_date, "md": md_text})

    monkeypatch.setattr(mod, "render_markdown_to_pdf", _fake_render)
    return tmp_path, rendered


def _write_md(root: Path, kind: str, date: str, body: str = "# タイトル\n\n本文です。\n") -> Path:
    md_path = root / KIND_CONFIG[kind]["md_path"].format(date=date)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(body, encoding="utf-8")
    return md_path


def _run(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["send_report_pdf_discord.py", *argv])
    return mod.main()


# ---------------------------------------------------------------- 1. 引数処理
def test_unknown_kind_is_rejected(monkeypatch):
    """KIND_CONFIG に無い kind は argparse が SystemExit で弾く（誤種別の配信を防ぐ）。"""
    with pytest.raises(SystemExit):
        _run(monkeypatch, ["--kind", "no_such_kind", "--date", "2026-09-07"])


def test_stock_kind_requires_code(monkeypatch, sandbox):
    """stock は --code 必須。無ければ PDF を作らず 1 を返す。"""
    assert _run(monkeypatch, ["--kind", "stock", "--date", "2026-09-07"]) == 1


# ---------------------------------------------------------------- 2. PDF 不在
def test_missing_markdown_returns_1_and_renders_nothing(monkeypatch, sandbox):
    """md が無い日付は戻り値 1 で終わり、描画も送信もしない。"""
    _root, rendered = sandbox
    monkeypatch.setenv("DISCORD_WEBHOOK_MACRO", "https://discord.test/hook")
    assert _run(monkeypatch, ["--kind", "macro", "--date", "2026-01-01"]) == 1
    assert rendered == []


# ---------------------------------------------------------------- 3. webhook 選択
@pytest.mark.parametrize(
    "kind,env_name",
    [
        ("macro", "DISCORD_WEBHOOK_MACRO"),
        ("pts_movers", "DISCORD_WEBHOOK_MOVERS"),
        ("sector", "DISCORD_WEBHOOK_SECTOR"),
    ],
)
def test_webhook_is_read_from_kind_specific_env(monkeypatch, sandbox, kind, env_name):
    """種別ごとに定義された webhook 環境変数だけを読む（別チャンネルへの誤送信を防ぐ）。"""
    root, _rendered = sandbox
    date = "2026-09-07"
    _write_md(root, kind, date)

    # 対象種別以外の webhook を全て消し、対象だけ設定する
    for cfg in KIND_CONFIG.values():
        monkeypatch.delenv(cfg["webhook_env"], raising=False)
    monkeypatch.setenv(env_name, f"https://discord.test/{kind}")

    sent: list[str] = []
    monkeypatch.setattr(mod, "_post_pdf", lambda webhook, pdf, content: sent.append(webhook) or True)

    assert _run(monkeypatch, ["--kind", kind, "--date", date]) == 0
    assert sent == [f"https://discord.test/{kind}"]


def test_missing_webhook_returns_1_without_sending(monkeypatch, sandbox):
    """webhook 未設定なら送信せず 1 を返す（空 URL を叩かない）。"""
    root, rendered = sandbox
    _write_md(root, "macro", "2026-09-07")
    for cfg in KIND_CONFIG.values():
        monkeypatch.delenv(cfg["webhook_env"], raising=False)

    called = []
    monkeypatch.setattr(mod, "_post_pdf", lambda *a, **k: called.append(1) or True)

    assert _run(monkeypatch, ["--kind", "macro", "--date", "2026-09-07"]) == 1
    assert called == []
    assert rendered == []


def test_skip_send_renders_but_does_not_post(monkeypatch, sandbox):
    """--skip-send は PDF を作るが送信しない（検証用の空打ちで PM へ届かせない）。"""
    root, rendered = sandbox
    _write_md(root, "macro", "2026-09-07")
    monkeypatch.setenv("DISCORD_WEBHOOK_MACRO", "https://discord.test/hook")

    called = []
    monkeypatch.setattr(mod, "_post_pdf", lambda *a, **k: called.append(1) or True)

    assert _run(monkeypatch, ["--kind", "macro", "--date", "2026-09-07", "--skip-send"]) == 0
    assert len(rendered) == 1
    assert called == []


# ---------------------------------------------------------------- 4. ペイロード
def test_post_pdf_payload_shape(monkeypatch, tmp_path):
    """Discord へ渡す multipart の形（content・filename・MIME・payload_json）を固定する。"""
    pdf = tmp_path / "macro_2026-09-07.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    captured: dict = {}

    def _fake_post(url, files=None, **kwargs):
        captured["url"] = url
        captured["files"] = files
        payload = json.loads(files["payload_json"][1])
        captured["payload"] = payload
        return _FakeResponse(200)

    monkeypatch.setattr(mod.requests, "post", _fake_post)

    assert mod._post_pdf("https://discord.test/hook", pdf, "**マクロ経済レポート** 2026-09-07") is True
    assert captured["url"] == "https://discord.test/hook"
    assert captured["payload"]["content"] == "**マクロ経済レポート** 2026-09-07"
    assert captured["payload"]["attachments"] == [{"id": 0, "filename": "macro_2026-09-07.pdf"}]
    name, _fh, mime = captured["files"]["files[0]"]
    assert name == "macro_2026-09-07.pdf"
    assert mime == "application/pdf"


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500])
def test_post_pdf_returns_false_on_error_status(monkeypatch, tmp_path, status):
    """4xx/5xx は False（送信失敗を成功と誤認せず、呼び出し側が 1 を返せる）。"""
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: _FakeResponse(status, "err"))
    assert mod._post_pdf("https://discord.test/hook", pdf, "c") is False


def test_send_failure_propagates_to_exit_code(monkeypatch, sandbox):
    """送信が False なら main は 1 を返す（GHA が失敗として拾えること）。"""
    root, _rendered = sandbox
    _write_md(root, "macro", "2026-09-07")
    monkeypatch.setenv("DISCORD_WEBHOOK_MACRO", "https://discord.test/hook")
    monkeypatch.setattr(mod, "_post_pdf", lambda *a, **k: False)
    assert _run(monkeypatch, ["--kind", "macro", "--date", "2026-09-07"]) == 1


# ---------------------------------------------------------------- 5. 動意の H1 付与
def test_movers_title_added_when_first_h1_is_market_heading():
    """先頭 H1 が市場区切りなら文書タイトルを足す（マストヘッドが市場名を名乗らないように）。"""
    md = "## 本日のテーマ\n\n# グロース市場\n\n本文\n"
    out = mod._ensure_movers_doc_title(md, "2026-09-07", "動意銘柄レポート")
    assert out.startswith("# 動意銘柄レポート 2026-09-07")


def test_movers_title_not_duplicated_when_doc_title_exists():
    """既に文書タイトルがある md には二重付与しない。"""
    md = "# 動意銘柄レポート 2026-09-07\n\n# グロース市場\n\n本文\n"
    out = mod._ensure_movers_doc_title(md, "2026-09-07", "動意銘柄レポート")
    assert out == md
    assert out.count("# 動意銘柄レポート") == 1
