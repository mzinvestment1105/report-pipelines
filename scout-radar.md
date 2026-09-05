学習型 Scout Radar で銘柄発掘レポートを生成して Discord に送信する。

<!-- AUTO-GENERATED MEMORY LIST: START - 編集禁止・optimize_memory.py で再生成 -->

## 必読 memory（自動生成・編集禁止）

> 本セクションは [bi/pipelines/optimize_memory.py](../../bi/pipelines/optimize_memory.py) `--update-skills` で自動生成されます。手動編集すると次回再生成時に上書きされます。memory 追加時は `/memory-check rewrite` を実行してください。

スキル起動時、以下を **Read ツールで実際に読み込む** ことから開始する。前回の記憶でスキップ厳禁。

- [feedback_3tier_summary_isolation.md](../../claude_memory/feedback_3tier_summary_isolation.md) — Market Speed II 3層構造（ページ1 Weekly/ページ2 Monthly/ページ3 Archive）のサマリーには他ページ（アメリカ・テー…
- [feedback_candle_judgment.md](../../claude_memory/feedback_candle_judgment.md) — 前日比%だけで「大陰線」と断定しない。実体長・上髭・下髭を分けて確認しないと下髭付き小陰線を大陰線と誤判定する
- [feedback_check_skills_first.md](../../claude_memory/feedback_check_skills_first.md) — 銘柄分析・レポート生成系タスクは必ず最初にスキル一覧を確認し、対応スキルがあれば呼び出してから着手する
- [feedback_content_decisions.md](../../claude_memory/feedback_content_decisions.md) — トークン削減・圧縮などの理由でコンテンツの内容・範囲を変える判断をClaudeが勝手にしてはいけない
- [feedback_coverage_definition.md](../../claude_memory/feedback_coverage_definition.md) — PMが「カバレッジ銘柄」と言ったときの参照対象を固定する用語定義
- [feedback_credit_data_misread.md](../../claude_memory/feedback_credit_data_misread.md) — Nomura/Barclays/JPMorgan/モルガン・スタンレー/Goldman等の証券会社名表示は5%超空売り報告制度の機関空売り。個人信用残データ…
- [feedback_deep_research_save.md](../../claude_memory/feedback_deep_research_save.md) — PMはDeep Research結果をチャットに貼るだけ・Claudeが指定ファイルに保存。全Deep Researchフローに適用
- [feedback_discord_send_rules.md](../../claude_memory/feedback_discord_send_rules.md) — 各レポートのDiscord webhook割り当てとフォーマット規則・フォールバック禁止
- [feedback_doui_homophone.md](../../claude_memory/feedback_doui_homophone.md) — PM の Q&A 直後の一語応答は同意系（同意・賛成・OK・了解）を最優先で解釈し、銘柄関連用語（動意・買い・売り等）として解釈しない
- [feedback_earnings_report.md](../../claude_memory/feedback_earnings_report.md) — 決算関連成果物のフォーマット・送信先・粒度の固定ルール
- [feedback_edinetdb_search_limit_bug.md](../../claude_memory/feedback_edinetdb_search_limit_bug.md) — MCP の search_companies は limit 引数バグで死ぬが、REST API 直叩き（/v1/search）は正常動作する。バグは MC…
- [feedback_edinetdb_token_retry.md](../../claude_memory/feedback_edinetdb_token_retry.md) — EDINET DB は4キーをラウンドロビン使用・429や検索失敗で即PM相談せず自分でデバッグ
- [feedback_etf_reit_not_individual.md](../../claude_memory/feedback_etf_reit_not_individual.md) — raw データの市場区分タグだけで判断せず、銘柄名 vs コード比較・セクター nan チェック等で ETF/REIT を目視除外する
- [feedback_external_factor_analysis.md](../../claude_memory/feedback_external_factor_analysis.md) — 原料・為替・関税・規制等の外部要因が業績に与える影響を分析する際の必須手順。年次データだけで断言せず、四半期粒度・一次データで検証する。
- [feedback_file_links.md](../../claude_memory/feedback_file_links.md) — 会話中のファイル参照は必ずMarkdownリンク形式で記載すること。CLAUDE.mdに明記済みだが守れなかった実績があるため二重担保。
- [feedback_fullpath_for_pm_files.md](../../claude_memory/feedback_fullpath_for_pm_files.md) — PM 渡し用ファイル（画像・CSV・生成成果物）は markdown リンクだけでなく Windows フルパス（C:\... 形式）を必ず併記する
- [feedback_gratitude_to_pm.md](../../claude_memory/feedback_gratitude_to_pm.md) — PMがClaudeに指導・修正・ルール追加を指示した時、まず「ありがとうございます」と述べてから本題に入る
- [feedback_jargon_annotation_all_reports.md](../../claude_memory/feedback_jargon_annotation_all_reports.md) — 全レポート種別で専門用語に中学生レベルの注釈を付ける・「初めて見た人が読める」基準・事業モデル・テーマ用語含む
- [feedback_jargon_in_chat.md](../../claude_memory/feedback_jargon_in_chat.md) — レポートだけでなく会話・分析・解説・雑談を含む全発話で専門用語使用時に括弧注釈を必須化。GPU・SaaS・営業CF・希薄化・PER 等の見慣れた略語も全て対象。
- [feedback_jst_timezone_unified.md](../../claude_memory/feedback_jst_timezone_unified.md) — マクロ・動意・セクター・銘柄含む全レポートで時刻表記をJST（日本時間）に統一。米国時間で言及する場合は必ず換算したJSTを併記
- [feedback_language.md](../../claude_memory/feedback_language.md) — PMへの応答は必ず敬語。タメ口・ため口は厳禁。出力前に終止形・表見出しまで確認する。
- [feedback_log_commit_workflow.md](../../claude_memory/feedback_log_commit_workflow.md) — ログは完了ごとに自動追記・コミットはPM明示時のみ。並列で確認しない
- [feedback_macro_no_english_raw.md](../../claude_memory/feedback_macro_no_english_raw.md) — 全レポートで英語見出し・本文・引用句を1文字も貼らない。Write前に必ず英文grep自己検証する手順を必須化
- [feedback_macro_report_workflow.md](../../claude_memory/feedback_macro_report_workflow.md) — ANTHROPIC_API_KEYは不要。マクロレポートは他レポートと同様にClaude Codeが直接分析する
- [feedback_market_speed_import.md](../../claude_memory/feedback_market_speed_import.md) — 楽天証券 Market Speed II の監視銘柄インポートで弾かれる銘柄パターン・件数上限・置換動作。watchlist_manager 運用時に必参照
- [feedback_model_opus_priority.md](../../claude_memory/feedback_model_opus_priority.md) — Claude モデル選定では PM の output 品質最優先原則に従い、Opus を第一選択肢にする。Sonnet を「過剰品質・コスト効率悪い」と勝手…
- [feedback_movers_raw_full_read.md](../../claude_memory/feedback_movers_raw_full_read.md) — movers_raw.md は 500-700KB / 3,000+行ある。引数なしの Read で末尾が欠落し「詳細未取得」誤記事故が起きるため Grep…
- [feedback_neutral_recording.md](../../claude_memory/feedback_neutral_recording.md) — memory・session・log・journal・research・skill 定義・CLAUDE.md・コミットメッセージ等あらゆるファイルに PM …
- [feedback_no_external_api.md](../../claude_memory/feedback_no_external_api.md) — Anthropic API・OpenAI API等の課金型外部APIを伴う案を二度と提案しない。Claude Codeセッション内で完結する案のみ提案する
- [feedback_no_local_path_dump.md](../../claude_memory/feedback_no_local_path_dump.md) — Discord 等に送信した後の応答で、PM が開けないローカル画像/ファイルのパス・サイズの表を冗長に出さない
- [feedback_no_memory_based_numbers.md](../../claude_memory/feedback_no_memory_based_numbers.md) — すべての分析でClaudeの記憶（訓練データ・スナップショット・「だったはず」等）による数値・事実・固有名詞の発言を全面禁止。一次情報ツールで実値取得してか…
- [feedback_no_screening_master_edit.md](../../claude_memory/feedback_no_screening_master_edit.md) — 新規分析指標は本番screening_master.parquetに直接列追加せず独立parquetとして作成する
- [feedback_no_scroll_no_abbrev.md](../../claude_memory/feedback_no_scroll_no_abbrev.md) — PMに選択を求める時は選択肢の中身を毎回フルで書く。略称・参照リンクのみで「過去の説明を見て」と求めるのを禁止
- [feedback_no_session_hook.md](../../claude_memory/feedback_no_session_hook.md) — SessionStart/Stop hook・自動pull/push・バックグラウンド処理の新規提案を禁止。コンテキスト消費試算なしの自動化を二度としない
- [feedback_no_term_fabrication.md](../../claude_memory/feedback_no_term_fabrication.md) — PMが実際に発言・記述していない用語を「PMが使った」「PMの認識」として記録・再利用することを禁止
- [feedback_notion_full_transfer.md](../../claude_memory/feedback_notion_full_transfer.md) — ローカルMarkdown本体をNotionに反映する作業では、行単位で1:1の完全転記を行う。任意のセクション・段落・テーブル・行を独自判断で削除・要約・ス…
- [feedback_pdf_unavailable_no_speculation.md](../../claude_memory/feedback_pdf_unavailable_no_speculation.md) — PDF・一次情報の取得が失敗・文字化けした場合、推測ベースで補完しない。「未取得・未確認」を明示し、別経路を試行する。
- [feedback_positions_freshness.md](../../claude_memory/feedback_positions_freshness.md) — 保有銘柄の死守ライン・売却計画・指値・期待値・跨ぎ判断を語る前に positions.md と thesis_master.md の最終更新日を必ず確認する
- [feedback_post_failure_language.md](../../claude_memory/feedback_post_failure_language.md) — PMから叱責・失敗指摘を受けた直後の応答は、敬語（desumasu）だけでなく謙譲語（いたします・お送りいたします等）を用いて謝罪と再発防止を明示する。「取…
- [feedback_remove_item_zero_use.md](../../claude_memory/feedback_remove_item_zero_use.md) — 空フォルダ・不要ファイル・テスト残骸であっても Remove-Item / del / rm / unlink を一切使わない。すべて Move-Item …
- [feedback_rule_rewrite_after_drop.md](../../claude_memory/feedback_rule_rewrite_after_drop.md) — 既存売却ルールに従わなかった後、株価下落により含み益縮小→売らなくてよい新ルールに書き換える典型的な失敗パターン。事前コミット型ルール設計と書き換え条件の明…
- [feedback_scraping_aggressive.md](../../claude_memory/feedback_scraping_aggressive.md) — X / Yahoo / みんかぶ / 株探 等のスクレイピングを規約違反だからと躊躇しない・保守的姿勢を禁止
- [feedback_sector_map_vs_report.md](../../claude_memory/feedback_sector_map_vs_report.md) — 「マップ」と「レポート」は完全に別物のタスク。即区別する
- [feedback_session_carryover.md](../../claude_memory/feedback_session_carryover.md) — セッションログの「次回持ち越し」に何を書くべきか・書いてはいけないか
- [feedback_simple_tasks.md](../../claude_memory/feedback_simple_tasks.md) — 単純な操作（parquet→Excel変換等）に不要な確認・調査・git操作を挟むな
- [feedback_skill_websearch_auto.md](../../claude_memory/feedback_skill_websearch_auto.md) — スキル定義に WebSearch 使用が組み込まれている場合、PM がスキルを起動した時点で WebSearch 使用は黙示的承認済み・追加確認しない
- [feedback_stock_code_pair.md](../../claude_memory/feedback_stock_code_pair.md) — 銘柄名を出力する際は必ず銘柄コードとセットで表記する。レポート・チャット応答・テーブル・コード内文字列の全てに適用。
- [feedback_stock_report_rules.md](../../claude_memory/feedback_stock_report_rules.md) — stock-reportスキルでのDeep Research省略禁止・ETL失敗検知の徹底ルール
- [feedback_theme_prototype_no_call.md](../../claude_memory/feedback_theme_prototype_no_call.md) — dev/prototype/themes/ 配下は凍結プロトタイプ・テーマレポート作成時に参照・呼び出ししない
- [feedback_token_efficiency.md](../../claude_memory/feedback_token_efficiency.md) — 7256 Deep Dive作業でトークン消費が過大だった。軽量化の方向性をメモ
- [feedback_tradingview_banned.md](../../claude_memory/feedback_tradingview_banned.md) — mcp__tradingview-chart__* は接続不可のため一切使用禁止
- [feedback_us_market_close_jst.md](../../claude_memory/feedback_us_market_close_jst.md) — 米国市場引け後の出来事を「日本市場引け後」と表記することを禁止。JST 換算は翌朝 05:00 頃が正解
- [feedback_verification_facts.md](../../claude_memory/feedback_verification_facts.md) — 過去テーゼの検証時、予測値を実績扱いしない・確定事実を「不明」と書かない
- [feedback_vix_market_specification.md](../../claude_memory/feedback_vix_market_specification.md) — VIX・恐怖指数に触れる時は必ず「米国S&P500のVIX」「日経VI」等を明示。曖昧表現禁止
- [project_edinet_deepdive.md](../../claude_memory/project_edinet_deepdive.md) — EDINETから有報・四半期報告書を取得してDeep Diveレポートを生成するツールの実装・動作確認済み状況
- [user_profile.md](../../claude_memory/user_profile.md) — MizukiのPMプロフィール - Amazon PM、日本株スイングトレーダー、感情管理が課題、AI活用投資を推進

<!-- AUTO-GENERATED MEMORY LIST: END -->


> ⚠️ **プロトタイプ（2026-05-25 立花証券 e支店 API 統合）**
>
> 旧設計（[dev/scout_radar_design.md](../../dev/scout_radar_design.md)）の Phase 2 build_scout_signals.py が PM 初期ルール待ちのため未実装。**つなぎとして立花証券の AI 市況データを使った Scout プロトタイプ**を提供。PMの利用フィードバックで継続改善する。

## プロトタイプの位置づけ

立花証券 QUICK AI 市況は「**動意候補の素材データ**」として極めて優秀：

- **寄り前注文予想（GNL=60010）**: 朝の寄り付き前に動意候補をリストアップ
- **材料発生（GNL=60030）**: リアルタイム値動きで「今動いている」銘柄
- **ストップ高 / 新高値 / 新安値 / 売買代金上位**
- **QUICK 個別銘柄解説（動意理由）**

これらを**ジャンル横断で「銘柄言及頻度」で集計**し、**頻度上位＝今動いている**として Scout 候補を抽出する。

## 手順

### Step 1: raw データ取得（立花証券 e支店 API）

```
cd "bi/pipelines" && python fetch_tachibana_scout.py --date {date} --limit 1000
```

出力: [market/daily/{date}_scout_raw.md](../../market/daily/) — AI 市況 + 寄り前注文 + 材料発生 + ストップ高 等を 90 件規模で取得、銘柄言及頻度ランキング Top 30 を計算

### Step 2: Claude による分析・レポート生成

raw を読み、以下の構成で [market/daily/scout/{date}.md](../../market/daily/scout/) を生成：

```markdown
# Scout Radar レポート（プロトタイプ）{date}

> ⚠️ プロトタイプ実装：立花証券 e支店 API の QUICK AI 市況を入力としています。PMが言語化していない発掘軸を機械集計で代替。継続調整中。

## 0. サマリー
- 集計件数 / ユニーク銘柄数 / 上位言及銘柄

## 1. 🏆 言及頻度 Top 10 候補銘柄
立花 AI 市況で複数セクションに登場した銘柄＝「今動いている」銘柄として、各候補について：
- 銘柄コード + 立花での言及件数
- どのセクションに登場したか（寄り前注文/ストップ高/材料発生/売買代金 等）
- 動意理由（QUICK 個別銘柄解説があれば引用）

## 2. 🔮 寄り前注文予想（翌営業日の朝の候補）
寄り前注文予想で「値上がり/売買急増」に挙がった銘柄を列挙

## 3. 📡 当日材料発生銘柄
リアルタイムで動意した銘柄の解説

## 4. 🏷 QUICK レーティング更新
アナリスト評価変更で動く可能性のある銘柄

## 5. データソース
立花証券 e支店 API（CGL=110 AI 市況・CGL=100 GNL=3001 QUICK 個別解説・GNL=6521 レーティング更新）
```

### Step 3: Discord 送信

```
cd "bi/pipelines" && python send_report_jpeg_discord.py --kind scout --date {date}
```

→ #銘柄発掘アナリスト チャンネル（`DISCORD_WEBHOOK_IDEAS`）に JPEG で送信

## プロトタイプの評価ポイント（PMが使って判断）

1. 言及頻度 Top10 の選定は的を射ているか（ノイズ・実用性）
2. 寄り前注文予想 / 材料発生のカテゴリは PMの好みに合うか
3. /ideas-report との重複・差別化（idea が IR ベース・scout が AI 市況ベース）
4. 朝・夜の発行タイミング（現状は手動・将来 GHA 化）
5. PMの「動いてる」感覚と立花の AI ロジックがどれだけ一致するか

フィードバック蓄積後に正式版（Phase 2 build_scout_signals.py + 学習型）へ昇格。

## 関連ファイル

- [bi/pipelines/fetch_tachibana_scout.py](../../bi/pipelines/fetch_tachibana_scout.py)
- [bi/pipelines/lib/tachibana_client.py](../../bi/pipelines/lib/tachibana_client.py)
- [.claude/commands/ideas-report.md](ideas-report.md) — 同チャンネル送信の姉妹スキル（IR ベース）
- [dev/scout_radar_design.md](../../dev/scout_radar_design.md) — 学習型の正式設計（Phase 2 待ち）
