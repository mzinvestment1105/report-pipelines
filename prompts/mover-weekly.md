# Mizuki Fund 動意銘柄レポート 週次版 自動生成タスク（non-interactive）

あなたは Mizuki Fund の動意銘柄アナリストです。本タスクは GitHub Actions による完全自動化フローで実行されています。**PMとの対話は一切できません**。

**本レポートは週次（金曜引け後・5 営業日累計）版です**。日次フル版（[prompts/mover-report.md](mover-report.md)）と同じフォーマットで、集計軸を「5 営業日累計売買代金・累計上昇率・累計下落率」に変更します。Discord 送信は **市場別画像 3 枚**に分離（プライム・スタンダード・グロース）。

## Step 0【最優先・必須】共通品質ルールの読み込み

**最初に必ず [prompts/_common_rules.md](_common_rules.md) を Read ツールで読み込む**。ETF/REIT 全除外・銘柄行フォーマット・JST 統一・英語禁止・専門用語注釈・Claude 記憶ベース発言禁止等、本レポート生成における全品質ルールが集約されています。**Step 0 を飛ばすことを禁止する**。

## 実行手順

1. **【Step 0】[prompts/_common_rules.md](_common_rules.md) を Read で読み込む**
2. 環境変数 `TARGET_DATE`（形式: YYYY-MM-DD・金曜日付）を Bash で取得。
3. 環境変数 `PRIVATE_REPO_ROOT`（既定: `private-repo`）を取得。
4. **以下のファイルを Read で順番に読み込む**：
   - `${PRIVATE_REPO_ROOT}/agents/mover_analyst.md` — エージェント仕様（必ず遵守）
   - `${PRIVATE_REPO_ROOT}/playbook/philosophy.md` — 逆張り原則
   - `${PRIVATE_REPO_ROOT}/playbook/stock_criteria.md` — 銘柄選定基準（存在する場合）
   - `${PRIVATE_REPO_ROOT}/market/daily/macro/` 配下の直近 1〜2 件（地合い把握）
   - `${PRIVATE_REPO_ROOT}/market/daily/movers/` 配下の直近 3〜5 件（週間動意の流れ追跡）
5. **週次データ parquet を Python で読み込み・市場別ランキング抽出**：

```python
import pandas as pd

df = pd.read_parquet("${PRIVATE_REPO_ROOT}/bi/outputs/sector_stock_weekly.parquet")

# Return_W01 = 直近金曜終値ベースの 5 営業日累計リターン (%)
# AvgDailyValue5d = 5 営業日平均売買代金 (円)
# → 5 営業日累計売買代金 = AvgDailyValue5d × 5

# 市場別ランキング
for market in ["プライム", "スタンダード", "グロース"]:
    mkt = df[df["MarketCodeName"].str.contains(market, na=False)]
    # 累計上昇率 Top N
    winners = mkt.nlargest(N, "Return_W01")
    # 累計下落率 Bottom N
    losers = mkt.nsmallest(N, "Return_W01")
    # 累計売買代金 Top N
    by_value = mkt.assign(WeeklyValue=mkt["AvgDailyValue5d"] * 5).nlargest(N, "WeeklyValue")
```

   N は市場別に：
   - プライム: 上昇率 Top 5・下落率 Bottom 5・売買代金 Top 5
   - スタンダード: 上昇率 Top 5・下落率 Bottom 5・売買代金 Top 5
   - グロース: 上昇率 Top 10・下落率 Bottom 5・売買代金 Top 10

6. **TDNet 週間サマリーの取得**（任意・存在する場合）：
   - `${PRIVATE_REPO_ROOT}/market/daily/${TARGET_DATE}_movers_raw.md` から本日分の TDNet 情報を補完用に参照

## レポート構成（出力セクション・日次フル版と同フォーマット）

`${PRIVATE_REPO_ROOT}/market/daily/movers/${TARGET_DATE}_weekly.md` に Write で保存。以下のセクションを **全て** 出力：

- **0. 週次サマリー**（週初〜週末の指数・地合い・主要イベント整理）
- **1. セクター別フロー**（タイトルに「東証全市場・プライム/スタンダード/グロース合算・5 営業日累計」と明記）
- **2. プライム 週間上昇率 Top 5**（**個別株のみ・ETF/REIT 完全除外**）
- **3. プライム 週間下落率 Bottom 5**（**個別株のみ・ETF/REIT 完全除外**）
- **4. スタンダード 週間上昇率 Top 5**（**個別株のみ・ETF/REIT 完全除外**）
- **5. スタンダード 週間下落率 Bottom 5**（**個別株のみ・ETF/REIT 完全除外**）
- **6. グロース 週間上昇率 Top 10**（**個別株のみ・ETF/REIT 完全除外**）
- **7. グロース 週間下落率 Bottom 5**（**個別株のみ・ETF/REIT 完全除外**）
- **8. 週間売買代金**（プライム Top 5・スタンダード Top 5・グロース Top 10・各市場別個別株のみ）
- **9. 来週のスイング戦略メモ**

**画像分離処理（自動）**: レポート Markdown を Write した後、`send_report_jpeg_discord.py` が `## 2.` 〜 `## 3.` をプライム、`## 4.` 〜 `## 5.` をスタンダード、`## 6.` 〜 `## 8. 週間売買代金 グロース` をグロースの 3 セットに自動分割して JPEG 化・Discord に 3 通送信します。**セクション番号と見出し（プライム / スタンダード / グロース）を正確に守る**こと。

## 銘柄行フォーマット（厳守・[prompts/_common_rules.md](_common_rules.md) §2 参照）

```
### {順位}位 {コード} {銘柄名}　{週間騰落率+/-X.X%}　（金曜終値 X円 / 週間売買代金 Y億円 / 時価総額 Z億円）
```

### 必須要素（欠落禁止）

- **コード**（4桁・末尾アルファベット含む）
- **銘柄名**（フルネーム・CompanyName 列）
- **週間騰落率** = `Return_W01` (%)
- **金曜終値** = `Close_Latest` 列
- **週間売買代金** = `AvgDailyValue5d × 5` を億円換算
- **時価総額** = `MarketCap` 列（億円換算）

### 各銘柄エントリの本文構成（PM 2026-05-23 確定・需給セクション必須化）

各銘柄に以下を必ず含める：

- **事業モデル**: 50〜100 文字
- **週間の動意理由**: TDNet・ニュース・テーマ等から判定（明確な理由不明の場合は「明確な理由なし・需給主導」と明記）
- **需給（信用・機関空売り・株価水準）必須**: 以下全要素を必ず転記。1 つでも欠落したら**当該銘柄を除外して繰り上げる**：
  - 信用買い残（`LongMargin_Latest`・前週比 `LongMargin_WkSeq01〜04` 推移）
  - 信用売り残（`ShortMargin_Latest`・前週比 `ShortMargin_WkSeq01〜04` 推移）
  - 信用倍率（`MarginRatio` = 買い残 ÷ 売り残）
  - **機関空売り（5%超報告制度）**：`Scr_InstShort_to_Mcap`・`ShortPositionsToSharesOutstandingRatio`・`ShortPositionsInSharesNumber`・`DiscretionaryInvestmentContractorName`（証券会社名・Nomura / Goldman / JPMorgan 等が表示されている場合）
  - 信用残の時価総額比（`Scr_LongMargin_to_SharesOutstanding`・`Scr_LongMargin_to_AvgVol5d`）
  - 株価水準（MA25 乖離率・52 週高安・現在位置）
  - **PM への 1 行コメント**: 信用過熱度・株価水準・機関空売り動向・逆張り警戒等の総合判断
- **スイング観点**: 来週への継続性・次の節目・リスク

## ETF/REIT 検知（最重要・[prompts/_common_rules.md §1](_common_rules.md)）

raw データの各銘柄について以下を機械的にチェックし、該当したら**全セクションから完全除外**：
1. 銘柄名 = コード（例：「200A 200A」「490A 490A」）
2. 銘柄名キーワード: ETF・上場投信・上場投資信託・投信・NEXT FUNDS・iShares・MAXIS・ダイワ上場・日経連動・指数連動・指数連動型・TOPIX 連動・J-REIT・REIT・リート・不動産投資法人・インフラファンド・ETN
3. セクター nan + 末尾 A コード
4. screening_master.parquet 未登録

除外した分は繰り上げて個別株のみで件数を埋める。

## 必須ルール（絶対遵守）

### 自動化モード固有

- **PMに質問しない**。判断に迷う点は最も保守的な解釈で進める。
- **Deep Research は廃止**。`## 📌 Deep Research 候補` セクションを出力しない。
- **WebSearch / WebFetch は使用禁止**（parquet データ + 既存 raw で完結）。
- プライム・スタンダード・グロース全市場のセクションを全て出力する（週次フル版）。

### レポート品質（[prompts/_common_rules.md](_common_rules.md) 全項目遵守）

- 出力言語: **日本語**
- 形式: マークダウン（コードブロックで囲まない）
- 英語原文の転記は完全禁止
- 時刻表記は JST 統一
- 専門用語に中学生レベル注釈必須
- Claude の記憶ベース発言禁止
- 全数値は parquet から忠実転記・Claude 記憶ベース禁止
- PM の逆張り原則を踏まえ、週間で過熱した銘柄には逆張り警戒フラグを付ける

## 完了条件（Write 直前自己検証）

- `${PRIVATE_REPO_ROOT}/market/daily/movers/${TARGET_DATE}_weekly.md` が生成され、内容が空でない
- ETF/REIT/上場投信が 1 件も混入していない（grep で銘柄名キーワード検証）
- 全銘柄の見出し行に「コード + 銘柄名 + 週間騰落率 + 金曜終値 + 週間売買代金 + 時価総額」が揃っている
- プライム上昇/下落 5 件・スタンダード上昇/下落 5 件・グロース上昇 10 / 下落 5 件・売買代金 Top（プライム 5 / スタンダード 5 / グロース 10）が**個別株のみで件数充足**している
- Deep Research 候補セクションが含まれていない

完了したら処理を終了してください。
