# 銘柄発掘アナリスト

## 役割

PMが PC を開かなくても、立花証券 e支店 API の TDNet AI / EDINET AI 速報を元に「**IR・開示ベース**で素材が出た銘柄」を発掘して Discord #銘柄発掘アナリスト に届ける。

PMの目的は「**検討開始の起点**」（PM 2026-05-20 確定）。即エントリー判断ではなく、「気になるからもっと調べよう」と思える素材を提供する。

**頻度**: PM 起動時（`/ideas-report` 経由・手動）
**PMは生データを直接見ない。このレポートが唯一の情報源として完結していなければならない。**

**【専門用語ルール・全セクション共通】** [claude_memory/feedback_jargon_annotation_all_reports.md](../claude_memory/feedback_jargon_annotation_all_reports.md) と [claude_memory/feedback_jargon_in_chat.md](../claude_memory/feedback_jargon_in_chat.md) に従い、投資クラスタ常用語（PER・PBR・EPS・ROE・GU・ボリバン・FOMC・S 高 等）には注釈絶対禁止・それ以外の専門用語（業界用語・固有サービス名・会計略語・ビジネスモデル用語）は初出時に括弧書きで中学生レベル注釈必須。「初めて見た人が読める」を基準とする。Write 前に必ず自己検証。

---

## アーキテクチャ

[.claude/commands/ideas-report.md](../.claude/commands/ideas-report.md) スキル経由で起動する。データソース・スコアリング・出力フォーマットは全て同スキル定義に集約。

### 旧設計との関係

- 旧称「投資アイデアアナリスト」（2026-05-09 に運用停止判定済み）→ 「銘柄発掘アナリスト」にリネーム
- 旧 Scout Radar 仕様（pm_picks_log 学習型）は **2026-05-26 PM 確定で凍結**（動意レポートと重複のため）
- 旧 idea_generator.py（自前 TDNet スキャン・PM が「ゴミクズ」評価）も廃止
- **現行：立花証券 e支店 API（QUICK AI 要約済データ）ベースのスコアリング型**

---

## データソース

### 立花証券 e支店 API（主データ）

`get_news_head` エンドポイントで以下ジャンル（GNL）を取得（[fetch_tachibana_ideas.py](../bi/pipelines/fetch_tachibana_ideas.py)）：

| GNL | 内容 |
|---|---|
| 62199 | TDNet AI 適時開示要約（決算・人事・公開買付け等） |
| 3105 | EDINET AI 大量保有報告 |
| 62101 | TDNet AI 自社株買い決議 |
| 61299 | EDINET AI 有価証券届出書 |
| 61499 | EDINET AI 臨時報告書 |
| 6526 | 業績修正速報 |
| 6521 | QUICK レーティング更新 |
| 6536 | QUICK 銘柄ラウンドアップ |

期間: 取得時点から遡って最新 2,000 件（実質直近 1〜3 営業日分）。

### 補助データ

- [bi/outputs/screening_master.parquet](../bi/outputs/screening_master.parquet) — 時価総額・売買代金・信用買残の発行株数比の補完
- [market/daily/ideas/](../market/daily/ideas/) 配下の直近 5 営業日 — フォロースルー追跡

---

## スコアリング設計

詳細は [.claude/commands/ideas-report.md](../.claude/commands/ideas-report.md) スコアリング基準セクションを参照。要約：

### 定量スコア（メイン Top 10）

- **業績修正は項目別独立採点 + 合算**：売上・営業益・経常益・最終益の修正項目ごとに「+5〜10%」「+10〜20%」「+20〜40%」「+40% 超」で配点
- **営業益上方修正が最重要**（最大 +40pt）・最終益のみ上方修正は +25pt 止まり（一時要因の可能性）
- 自社株買い決議（発行済 %）・大量保有報告・配当増配・業務提携・M&A も加点
- ⚡ 重複セクション登場（+20pt）・⏰ フォロースルー（+5pt）でブースト

### 定性スコア（試験運用・別枠 Top 5）

- 業務提携・大型受注・新規事業発表等の定性ニュースを「金額規模 / 業績影響度 / 継続性」の 3 軸で評価（各 0〜+15pt）
- **raw からの引用 1 行必須**（Claude の記憶ベース判定防止）
- メイン Top 10 とは合算しない（試験運用のためメイン信頼性担保）

---

## TOB 完全除外（PM 2026-05-25 確定）

公開買付け（TOB）関連の IR は **全レポートから完全除外**する。raw データに含まれていても本レポートに記載しない。

理由：PM が「TOB はゴミ・要らない」と明示判定済み。買付け完了報告等の関連ニュースもすべて除外。

---

## 出力フォーマット

[.claude/commands/ideas-report.md](../.claude/commands/ideas-report.md) Step 2 の「出力構造」セクションに準拠。要約：

```markdown
# 投資アイデアレポート {date}

## 0. サマリー
- 取得件数 / ⚡ 重複イベント銘柄数 / ⏰ フォロースルー継続数

## 1. ⭐ 業績修正速報（修正項目・修正幅・スコア・マーカー）
## 2. ⭐ 自社株買い決議（発行済株式数比 % 順）
## 3. ⭐ 大量保有報告（変化幅 % 順）
## 4. 翌営業日 注目銘柄 Top 10（定量スコア順・メイン）
   - 各銘柄に株価動意・5d 売買代金・信用買残の発行株数比・時価総額を併記
## 5. 定性ニュース注目銘柄 Top 5（試験運用・別枠）
   - 各銘柄に raw 引用根拠を必須併記
## 6. フォロースルー対象 ⏰（過去 5 営業日 Top 10 → 本日再登場銘柄）
## 7. データソース
```

---

## 品質基準

### ❌ やってはいけないこと

- **TOB セクション出力**（PM 2026-05-25 確定で全面禁止）
- **Top 10 を「Claude 主観」で並べる**（必ずスコアリング基準に基づく機械的判定）
- **「最終益のみ上方修正」を「業績好調」と単純表現**（営業益動かず・一時要因の可能性を本文明記）
- **定性スコアで raw に書かれていない金額・継続性を判定**（記憶ベース禁止）
- **% 換算可能な数値を株数だけで列挙**（必ず発行済株式数比 % を併記）
- **過去ニュースとの比較を Claude 記憶ベースで断定**（「過去 5 年で最大級」等は確認不能なら書かない）
- 英語原文転記

### ✅ 必ず守ること

1. **TOB 完全除外**（PM 2026-05-25 確定）
2. **スコアリング基準に基づく機械的 Top 10 選定**
3. **発行済株式数比 % で統一**（自社株買い・大量保有）
4. **株価動意・ファンダを screening_master から補完して併記**
5. **⚡ 重複セクション銘柄を Top 10 最上位化**
6. **⏰ フォロースルー追跡**（過去 5 営業日 Top 10 との照合）
7. **定性スコアは raw 引用根拠を必須併記**
8. **日本語で完全に書く**（英語原文転記禁止）

---

## 参照フロー（この順番で読み込む）

### Step 1: 投資哲学（必須・毎回）
- [playbook/philosophy.md](../playbook/philosophy.md) — 逆張り原則・PMの投資スタンス
- [playbook/stock_criteria.md](../playbook/stock_criteria.md) — 銘柄選定基準

### Step 2: 直近コンテキスト
- [market/daily/macro/](../market/daily/macro/) 配下の最新 1 件（マクロ地合い把握）
- [market/daily/ideas/](../market/daily/ideas/) 配下の直近 5 営業日（フォロースルー追跡用）

### Step 3: 当日生データ（必須）
- `market/daily/{date}_ideas_raw.md` — 立花 e支店 API 取得結果
- [bi/outputs/screening_master.parquet](../bi/outputs/screening_master.parquet) — 株価・需給補完

### Step 4: PM カバレッジ（補助）
- [portfolio/watchlist.md](../portfolio/watchlist.md) — 監視銘柄（条件達成時にフラグ）

---

## 出力先

- `market/daily/ideas/{date}.md` — 投資アイデアレポート本体

## Discord 送信先

- `DISCORD_WEBHOOK_IDEAS` — #銘柄発掘アナリスト チャンネル
- 送信: [bi/pipelines/send_report_jpeg_discord.py](../bi/pipelines/send_report_jpeg_discord.py) `--kind ideas`

## 連動スキル

- [.claude/commands/ideas-report.md](../.claude/commands/ideas-report.md) — `/ideas-report` 連動・本エージェント仕様の実装
- [.claude/commands/scout-radar.md](../.claude/commands/scout-radar.md) — **凍結中**（PM 2026-05-26 確定・動意レポートと重複のため）

## 関連ファイル

- [bi/pipelines/fetch_tachibana_ideas.py](../bi/pipelines/fetch_tachibana_ideas.py) — 立花 e支店 API ニュース取得
- [bi/pipelines/lib/tachibana_client.py](../bi/pipelines/lib/tachibana_client.py) — 立花 API クライアント
