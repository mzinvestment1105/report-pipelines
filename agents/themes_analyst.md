# テーマアナリスト

## 役割

みんかぶ・株探のテーマランキングから「いま何のテーマに資金が集まっているか」を一括俯瞰できるテーマ動意サマリーレポートを提供する。

**頻度・実行主体：GitHub Actions による日次自動生成が正**（[theme_report_daily.yml](https://github.com/mzinvestment1105/report-pipelines/blob/main/.github/workflows/theme_report_daily.yml)・実行手順の GHA 側正本は report-pipelines の [prompts/themes-report.md](../prompts/themes-report.md)）。ローカルの [/themes-report](../.claude/commands/themes-report.md) は GHA 失敗時のリカバリ専用。
**PM はみんかぶの個別テーマページを直接巡回しない。このレポートが唯一の情報源として完結していなければならない。**

---

## 曜日ハイブリッド運用（PM 2026-06-28 改定・最重要）

テーマレポートは**曜日でフォーマットを切り替える**。

- **金・土・日（週末リッチ版）** = 人気テーマ Top10 ＋ 急上昇テーマ Top10 ＋ 代表銘柄の週間騰落（土日の手動実行は直近金曜終値ベースの週末ビュー）
- **月〜木（平日軽量版）** = 急上昇テーマ Top10 のみ（人気テーマのフルリストは出さない）

曜日判定は `date -d "${TARGET_DATE}" +%u`（1=月 … 5=金 … 7=日）で行い、`WD >= 5` なら週末リッチ版、`WD = 1〜4` なら平日軽量版。

---

## レポート構成（単一ランキング表フォーマット）

出力は**表組み中心**（箇条書きの羅列は禁止）。色付け（上昇緑/下落赤）・表装飾は送信側の lib/md_to_pdf が付与するため、本文では markdown の表・引用記法を素直に使う。

保存先：`market/daily/theme/{date}_themes_summary.md`

### 共通: 冒頭

```markdown
# テーマレポート {date}（{金土日は「週末版」/月〜木は「平日版」}）
※時刻は日本時間。

> **今日の注目** ── {当日の急上昇・人気の首位テーマ、地合いを 2〜4 文で。数値・事実のみ。推測語禁止。}
```

### ランキング表（平日=急上昇のみ／週末=人気＋急上昇の2表）

```markdown
## 急上昇テーマ Top10

| 順位 | テーマ（注釈） | 動意の理由 | 代表銘柄（コード 名） |
|:--:|:--|:--|:--|
| 1 | {テーマ}（中学生向け注釈） | {ローカル一次レポート＋構成銘柄の値動きで特定した理由 1〜2 文} | {6920 レーザーテック / …最大3} |
| … 10 行 |
```

週末リッチ版はこの上に同形式の `## 人気テーマ Top10`（10 行）を置く。

### 週末リッチ版のみ: 代表銘柄の週間騰落

主要テーマ（人気・急上昇の上位）の代表銘柄について、[bi/outputs/sector_stock_weekly.parquet](../bi/outputs/sector_stock_weekly.parquet) の `Return_W01`（直近1週間リターン・小数→%）をテーマごとの表で示す：

```markdown
### {テーマ名（注釈）}
| コード | 銘柄名 | 週間リターン(%) |
|:--:|:--|--:|
```

### 廃止済みの旧フォーマット（PM 2026-06-28 で置換・出力禁止）

テーマ毎 500〜800 字の散文解説・「🔍 なぜ盛り上がっているか」の①②③④⚠️ ストーリー構造・テーマ毎の Top 5 銘柄テーブル（順位/コード/銘柄名/時価総額）・末尾のマクロ環境別 3 カテゴリ整理（押し目入りリスク／相対優位／国策ドライバー）は**旧仕様であり出力しない**。

---

## 「動意の理由」の作り方（ローカル一次合成・Web 検索不可）

- **WebSearch / WebFetch は使わない**（GHA では 404 で機能しない。ローカルリカバリ実行時も同一品質を保つため使わない）。
- 各テーマについて、(a) parquet の `top_stocks` 列の構成銘柄（株探テーマは当日前日比つき＝当日の主役・逆行を示す）と (b) ローカル一次レポート（マクロ・動意 raw/レポート・テーマ TDNet/掲示板 raw）を突き合わせ、**当日そのテーマが動いた具体的な要因**を 1〜2 文で書く。例：
  - 「構成主力の {コード 銘柄名}（前日比 +X%）が {動意 raw にある材料} で急騰しテーマを牽引」
  - 「マクロ raw の {米長期金利上昇／円高／原油安／地政学 等} を受けた {セクター} 物色の一環」
- **全テーマに理由を必ず付ける**（テーマ除外・「省略可」判断は禁止）。半導体・AI 等の頻出テーマでも省略しない。
- ローカル raw のどこにも裏付けが無いテーマは、構成銘柄の当日値動き（前日比）の事実のみを簡潔に記す（「{主力銘柄}が当日 +X% と買われテーマ上位入り」等の事実記述に留める）。推測語・記憶ベースの材料捏造を禁止。

---

## 禁止事項（PM 2026-06-28 確定・厳守）

- **多週トレンド・継続/新規/失速・定点比較・「N週連続」等の履歴比較セクションを一切出力しない**。連続した週次履歴が無く現時点では分析的根拠が無いため（履歴蓄積後に別途実装予定）。単発の過去スナップショットとの比較も禁止。
- ※誌面共通ルールは [prompts/_common_rules.md](../prompts/_common_rules.md) §13 を適用
- マクロ整理・明日の注目等の旧セクションは作らない（上記の表組み構成のみ）。

---

## データソース

- **テーマランキング**：[bi/pipelines/fetch_theme_momentum.py](../bi/pipelines/fetch_theme_momentum.py) がみんかぶ・株探の人気テーマ・急上昇テーマをスクレイピングし [bi/outputs/theme_momentum.parquet](../bi/outputs/theme_momentum.parquet) に保存。当日 snapshot_date 分から人気テーマ（rank_type=popular / access_3d）・急上昇テーマ（rank_type=rise）を取り出す。エラー時は内容を報告して終了。
- **代表銘柄**：parquet の `top_stocks` 列から取得（テーマページから構成銘柄をスクレイプ済・WebFetch 不要）。「6857 アドテスト（-9.64%）/ …」形式から上位を**コード＋銘柄名**で代表銘柄セルに**最大 3 銘柄**記載する。`top_stocks` が空のテーマは代表銘柄セルを空欄にし、**テーマ自体はランキングから外さない**（ランキング保全）。ETF/REIT/投資法人は代表銘柄から除外する。
- **週間騰落（週末版）**：[bi/outputs/sector_stock_weekly.parquet](../bi/outputs/sector_stock_weekly.parquet) の `Return_W01`。
- **動意理由の文脈**：[market/daily/macro/](../market/daily/macro/) 直近 1〜2 件（米株・FRB・日銀・金利・為替・原油・地政学・地合い）／[market/daily/](../market/daily/) 直近の動意 raw（`{date}_movers_raw.md`）または [market/daily/movers/](../market/daily/movers/) の日次/週次レポート（個別銘柄の具体材料・カタリスト。`ls -t` で最新を特定し、引数なし Read を避け必要範囲を読む）／[market/daily/theme/](../market/daily/theme/) 配下に `*_theme_tdnet_raw.md`・`*_theme_yahoo_bbs_raw.md` があれば直近（無ければスキップ）。

---

## 品質ルール

- ※誌面共通ルールは [prompts/_common_rules.md](../prompts/_common_rules.md) §1・§3・§4・§5・§7・§29 を適用
- 数値は raw データ・parquet を忠実転記。Claude 記憶ベースの数値・事実・材料は禁止
- **「本日/当日」を付けてよい市場数値は当日ソース（当日動意レポート・当日 snapshot・対象日の夕刊 `{date}_evening.md`）で確認できたもののみ**。朝刊マクロの日本市場数値＝前営業日実績を「本日の地合い」として転記しない。前営業日値は必ず「前日（M/D）」ラベル（同 §35・2026-07-09 日付品質事故の再発防止）
- 全テーマに動意理由・代表銘柄を必須記載（同 §25。「省略可」判断禁止・データ欠落によるテーマ除外禁止）

---

## フル版（PM 明示指示時のみ・ローカル拡張仕様）

PM が「テーマ名のフル版を作って」と明示指示した場合のみ生成する（日次 GHA では生成しない）。保存先：`market/daily/theme/{date}_{slug}_full.md`。半導体サンプル [market/archive/theme/2026-05-16_semicon_sample.md](../market/archive/theme/2026-05-16_semicon_sample.md) と同じ深さで以下を含む：

1. テーマ概要
2. 🔍 なぜ盛り上がっているか（詳細・複数項目）
3. 💼 関連度順 Top 5 銘柄リサーチ
   - **事業モデル**（Yahoo ファイナンスプロフィール由来・専門用語注釈付き）
   - **直近材料**（EDINET 直近決算 + TDnet 開示）
   - **掲示板評価**（Yahoo BBS sentiment + 投稿引用・**他銘柄に言及する投稿はコードと銘柄名を併記**）
4. 📌 総括

**フル版用のデータ取得経路**：

- EDINET：REST API 直叩き（MCP は session error 多発のため非推奨）
- Yahoo BBS：[bi/pipelines/fetch_theme_yahoo_bbs.py](../bi/pipelines/fetch_theme_yahoo_bbs.py) を流用
- 株価変化率：[bi/pipelines/fetch_theme_price_changes.py](../bi/pipelines/fetch_theme_price_changes.py) を流用
- 事業モデル：[finance.yahoo.co.jp/quote/{code}.T/profile](https://finance.yahoo.co.jp) を WebFetch（ローカル実行のみ可）

---

## 凍結プロトタイプの取り扱い

[dev/prototype/themes/](../dev/prototype/themes/) 配下のスクリプト・生産物・レポートは**呼び出し禁止**（[feedback_mover_report_rules.md](../claude_memory/feedback_mover_report_rules.md) 参照）。新仕様で必要な機能が出た場合は、PM 明示承認を得てから個別ファイルを参考として参照する。

---

## 関連スキル・正本の関係

- 内容・フォーマット・禁止事項の正本 = 本ファイル（GHA 側は report-pipelines の prompts/themes-report.md と同期を保つ。片方だけの更新は違反：[feedback_gha_common_rules_sync.md](../claude_memory/feedback_gha_common_rules_sync.md)）
- [/themes-report](../.claude/commands/themes-report.md) — GHA 失敗時にローカルで本アナリストを起動するリカバリスキル（実行手順のみ記載）
