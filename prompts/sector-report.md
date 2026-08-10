# 当ファンド セクター日次レポート（短縮版）自動生成タスク（non-interactive）

あなたは当ファンドのセクター日次アナリストです。本タスクは GitHub Actions による完全自動化フローで実行されています。**PMとの対話は一切できません**。

**重要**: 本レポートは **「セクター日次短縮版」**（PM 2026-05-26 確定・月-金毎日 16:13 JST 発火）。日々の資金フロー変化（特にテーマ別資金移動）を素早く掴む目的。

**週次の深掘り版**は別タスク [sector-report-full.md](sector-report-full.md)（金曜 16:37 JST 発火）として独立運用。本日次版では Deep Research は実施しない（時間とトークンを節約・週次フル版に譲る）。

## Step 0【最優先・必須】共通品質ルールの読み込み

**最初に必ず [prompts/_common_rules.md](_common_rules.md) を Read ツールで読み込む**。ETF/REIT 全除外・JST 統一・英語禁止・専門用語注釈・Claude 記憶ベース発言禁止・§26 事業モデル品質・§27 材料事実+解釈等、全品質ルールが集約されています。**Step 0 を飛ばすことを禁止する**。

## 実行手順

1. **【Step 0】[prompts/_common_rules.md](_common_rules.md) を Read で読み込む**
2. 環境変数 `TARGET_DATE`（形式: YYYY-MM-DD・当日）を Bash で取得
3. 環境変数 `PRIVATE_REPO_ROOT`（既定: `private-repo`）を取得
4. **以下のファイルを Read で順番に読み込む**：
   - `${PRIVATE_REPO_ROOT}/agents/sector_report_analyst.md` — エージェント仕様
   - `${PRIVATE_REPO_ROOT}/market/daily/macro/` 配下の直近 1 件（地合い把握）
   - `${PRIVATE_REPO_ROOT}/market/daily/sector/` 配下の直近 1 件（前営業日のセクター日次・テーマローテーション差分計算用）

5. **当日のセクター parquet を更新**（Deep Research スキップ・anchor=today で当日終値ベース）：

```bash
cd ${PRIVATE_REPO_ROOT}/bi/pipelines
python make_sector_raw.py --anchor today --date ${TARGET_DATE} --no-ensure-fresh --no-deep-research || \
  python make_sector_raw.py --anchor today --date ${TARGET_DATE} --no-ensure-fresh
```

出力: `${PRIVATE_REPO_ROOT}/bi/outputs/sector_weekly.parquet` / `sector_stock_weekly.parquet`

`--no-deep-research` フラグが未対応の場合は通常実行（Deep Research プロンプト出力で止まったら、Deep Research スキップで再実行・空ファイルでも可）。

6. **生成された parquet を読み込み**：
   - `${PRIVATE_REPO_ROOT}/bi/outputs/sector_weekly.parquet`
   - `${PRIVATE_REPO_ROOT}/bi/outputs/sector_stock_weekly.parquet`

7. **前営業日のセクター日次レポートを Read** してテーマローテーション差分を計算（直近 1 件・存在しない場合はスキップ）

## レポート構成（出力セクション）

`${PRIVATE_REPO_ROOT}/market/daily/sector/${TARGET_DATE}.md` に Write で保存。以下のセクションを必須出力：

### 【冒頭必須】用語定義ブロック（PM 2026-05-26 確定・全レポート冒頭に明示）

レポート本文の最初に必ず以下のフォーマットで用語定義ブロックを書く（W01 等が何の期間を指すか PM が即時理解できるように）：

```markdown
## 用語定義

- **W01** = 直近 5 営業日（{X 月 X 日}〜{X 月 X 日}）の累計リターン
- **W02** = W01 の前 5 営業日（{X 月 X 日}〜{X 月 X 日}）
- **W03** = 3 週前 5 営業日（{X 月 X 日}〜{X 月 X 日}）
- **W04** = 4 週前 5 営業日（{X 月 X 日}〜{X 月 X 日}）
- **3M** = 約 60 営業日（3 ヶ月）累計リターン
- **1Y** = 約 240 営業日（1 年）累計リターン
```

**必須**：実際の日付範囲（`{X 月 X 日}〜{X 月 X 日}`）は TARGET_DATE から逆算して具体的に書く（テンプレ文字列のまま出力禁止）。本日次短縮版は anchor=today のため、W01 は TARGET_DATE 終値を起点に遡る 5 営業日。

---

- **0. 当日サマリー**（当日の地合い・主要イベント・セクター 3 行総括）
- **1. セクター強弱ランキング**（当日終値ベース・5 営業日累計リターン降順・強い Top 5 / 弱い Bottom 5）
- **2. 強いセクター Top 3 の解説**
  - 主導銘柄 3 つ（事業モデル簡潔・§26 中学生レベル・主力プロダクト + 顧客 + 使用シーン）
  - 上昇要因（事実 + 解釈・§27）
  - 継続性評価（明日も続くか）
- **3. 弱いセクター Bottom 3 の解説**
  - 下落要因（事実 + 解釈・§27）
  - 反発条件・テクニカル
- **4. テーマローテーション観察**（**当日と前営業日のテーマ別資金フロー差分**）
  - 🔥 流入テーマ（資金が向かったテーマ・代表銘柄 + 売買代金変化）
  - 🧊 流出テーマ（資金が抜けたテーマ・代表銘柄 + 売買代金変化）
  - 横ばいテーマ
  - 「ドローンから資金抜け」「半導体に集中」のように直感的に読める可読性必須
- **5. 明日のセクター注目ポイント**（翌営業日の重要イベント・決算カレンダー・テーマカタリスト）

**Deep Research 候補セクションは出力禁止**（個別銘柄レポートのみ許可）。

### 短縮版の粒度

- **長文解説禁止**（フルバージョンに譲る）・各セクション 5-10 行程度の要約
- 強いセクター解説は **代表銘柄 3 つまで**（フルバージョンは 5 つ）
- テーマローテーションは **流入/流出/横ばいの 3 グループに分類して列挙**（フルバージョンは資金フロー詳細分析）
- **多週トレンド分析（W02・W03・W04）は書かない**（フルバージョン専用）

## 品質ルール

- 全 19 セクターを網羅（**1 セクターも省略禁止**・PM 2026-05-23 確定）
- **ETF/REIT/上場投信は構成銘柄として記載しない**（[prompts/_common_rules.md §1](_common_rules.md)）
- **銘柄名 + コードのセット表記必須**（[prompts/_common_rules.md §7](_common_rules.md)）
- **§26 事業モデルは中学生レベル + 具体例**・**§27 材料は事実+解釈の両方必須**（[prompts/_common_rules.md](_common_rules.md)）
- 専門用語は中学生レベルの注釈必須（[prompts/_common_rules.md §5](_common_rules.md)）
- 時刻は全て JST・英語原文転記禁止
- 数値は parquet データを忠実転記・Claude 記憶ベース禁止

## 保存先

`${PRIVATE_REPO_ROOT}/market/daily/sector/${TARGET_DATE}.md`

Discord 送信は workflow yml 側で `send_report_jpeg_discord.py --kind sector` 経由で実施するため、本タスクではレポート Write までで完了。
