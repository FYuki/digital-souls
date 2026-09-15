# #341 残受入条件の検証（2026-09-15）

本書は子Issueのチェック項目と証跡を対応づける。mainへのマージ・dogfood配備は対象外。
semantic-v9の正式評価と、会話・意味抽出を12bにした実接続受入が完了した。
以下の最終結果を現状とし、後半には失敗を含む先行試行を残す。
GitHubへの統合・CI・レビューの状態はPR #409 / #405を正本とする。


## 最終結果

- 意味抽出の実行commit: `6c55e6d73ff3b3d12397369b4db2567603b1c212`。
  追加4件を含む104件×3回・cacheなし。全3回が分類別90%以上、禁止保存/別character混入0件。
  元100件の入力・期待値は変更していない。事前のv6両モデル比較600件とは別の、選定12bの回帰評価。
- 各回の出来事除外・明示確認・固定属性矛盾は90%、他8分類は100%。
  `event_exclusion-06`、`confirmation-10`、`fixed_conflict-10`の誤りが各回に残る。
  **全312件正解という意味ではない。** 判定基準や禁止保存の扱いを緩めていない。
- 1ケースの中央値は3.29〜3.30秒、p95は3.58〜3.60秒。
  Ollama定期観測の合計VRAM最大は約11.80GB。GPU全体の使用量は他processを含み、専有量と区別する。
  input/output tokens、model digest、設定、ファイルhash、資源観測は[機械可読結果](semantic-memory-341-remaining-results.json)に保存。
- 実接続commit: `f887b3e9d83cc41225647862dff92118a153e9eb`。
  root: `/tmp/ds-memory-341-gwcons0n`。会話・意味抽出は12b、privacy・Episode抽出はe4b、Embeddingは実nomic。
  共通privacyのprompt・判定・QUERY_GATEの2秒上限は不変。

| 最終の実接続シナリオ | 結果 / 根拠 |
|---|---|
| 通常会話の単一自己申告 | DIRECT_EXTRACTION、実SQLite、実Chromaへの保存を確認 |
| 元発言の変更と古い索引 | 実version増加、古い実ベクトルを残して正本で失効・検索除外 |
| #100の保存入口 | 実会話から抽出した2件のEpisodeを根拠に、一般化候補を契約入力。保存・冪等性・別character/古い版の拒否を確認 |
| 自己申告と一般化の共存 | 正本では両方ACTIVE。別会話の応答は自己申告の否定的な好みを回答 |
| 派生記憶の実UI削除 | 204、全版本文NULL、実索引除外、元Episode保持、同じ根拠からの再生成拒否 |
| 削除直後の別会話 | 「コーヒーがお好きではない」と回答。自己申告の参照IDあり、削除IDなし |
| 根拠Episode失効 | 古い実Chroma候補をSQLiteで排除、再評価待ち、outbox後の索引削除 |
| character越境 | 別namespaceへ故障注入した実ベクトルでも所有検査で拒否。UI/APIも別characterのGET/DELETEは404、一覧は空 |
| 保存拒否 | 通常発言は処理済みとなり、対象の意味記憶を形成しない |
| 削除後の新しいEpisode根拠 | 新しい通常の出来事は自己申告を変更せず、意味記憶の追加/版増加もなし。新Episodeを含む一般化候補は受理 |
| 新しい一般化との共存後 | 同じ質問に現在の自己申告を回答。応答参照は自己申告を含み、一般化IDを除外 |
| 実UIの表示 | 自己申告だけに訂正ボタン、一般化に削除ボタン、削除後の本文消去・根拠保持を日本語表示のスクリーンショットでも確認 |

3応答の本文と参照IDは`manual-review.json`、各phaseの正本/索引結果は`boundaries-*.json`、
HTTP会話は`lifecycle-*.json`、実ブラウザは`ui-derived-*.json`に保存した。
最初のheadless画像は日本語フォントが不足したため、既存Windowsフォントを専用fontconfigから参照し、
`ui-japanese-*.png`で再確認した。アプリのDOMや返却値を置換していない。

### 現構成での会話優先

同commitの別root `/tmp/ds-memory-341-7tkfc709`で、12bの意味抽出呼出し開始から1.016秒後に通常会話を送った。
抽出終了は会話送信後70.5ms、応答は1.332秒。再試行後は自己申告1件、意味抽出の未処理0件、index待ち0件。
元の2会話を保持した。単一標本であり、常時1秒やcold起動の性能を保証しない。
先行のe4b会話・12b抽出の並走実測も[先行記録](semantic-memory-341-2026-09-15.md)に保持する。

### 残る制限

- e4b会話は、正しい自己申告と優先指示が入力に含まれていても、人格と複数の経験を含む今回の文脈では再申告を求めた。
  会話側を12bにした構成で受入した。e4bの同等品質、他の人格や自由文全般は未保証。
- 同一モデルで異なる総contextを切り替えると繰返しQUERY_GATE timeoutが起きた。
  最終構成は各用途の入力+出力を36,864に統一。coldロードや他処理との競合時のtimeoutは引き続き利用を見送る。
- 一般化だけを想起した先行応答には、本人が述べたような出典表現の過剰な断定があった。
  一般化の表出品質を受入完了とはしない。自己申告優先・保存/検索/失効の契約と区別する。
- #100の一般化生成、内省の質問生成、外部活動学習、mainマージ、dogfood配備は対象外。

## 検証範囲の区別

- 通常会話からの直接取得は、実Gemma 4→実SQLite→実Chroma/nomic→別会話応答で確認する。
- #100の一般化生成は対象外。通常会話から実抽出した2件のEpisodeを根拠に、
  `SemanticStore.save`へEXPERIENCE_DERIVED候補を契約入力する。
  その後のprivacy分類、保存、検索、UI操作、失効は実装・実サービスを通す。
- 出典編集APIと#100のEpisode失効発行元は未提供のため、停止中の専用test rootへ
  変更イベントを故障注入する。正本やEmbeddingをmockに置き換えない。
- query gateが拒否・timeoutになった空結果は、古い索引の排除成功として数えない。
  gateのNOT_SENSITIVEと、生の実Chroma候補に対象IDが存在することを別々に検査する。

## 受入条件と証跡の対応

| 子Issue | 対応範囲 | 証跡 |
|---|---|---|
| #345 | 命題・形成型・根拠版・状態・関係・適用日時、Candidateと正本の境界、#100入口、非破壊migration、outbox、冪等性 | `test_semantic_store.py`、`test_persona_memory_schema.py`、`test_response_provenance.py`、本書の実境界試験 |
| #346 | 増分処理、明示発言・確認、会話優先、編集失効、予約回復、用途別Target、promptfoo本番経路 | `test_semantic_runtime.py`、`test_semantic_idle_recovery.py`、`test_semantic_catalog.py`、固定評価、先行の実並走・再起動証跡 |
| #347 | 訂正・時間変化・矛盾・自己申告優先、出典/Episode失効、古い索引排除、削除再生成境界、競合・越境 | `test_semantic_retrieval.py`、`test_semantic_management.py`、先行実UI/居住地検証、本書の境界試験 |
| #348 | 監査、出典遷移、自己申告限定訂正、全形成型削除、履歴消去、character分離 | 先行の実UI訂正/直接取得削除、本書の実UI派生削除、`SemanticMemoryManagement.unit.test.ts` |
| #390 | 固定100件×両モデル各3回、設定・誤り・時間・tokens・資源・会話負荷を計測 | [v6比較](semantic-memory-341-v6-comparison.json)、[並走実測](semantic-memory-341-preemption.json)。変更後の12bは104件×3回すべて基準達成 |
| #349 | 通常会話→保存→別会話→UI→後続応答、source/privacy/character/失敗回復、#100契約 | [先行実接続](semantic-memory-341-2026-09-15.md)と本書を合わせて確認 |

## 先行の追加境界試験の結果

実行アプリcommitは`cd4e37e92ce13c9277ebc193178b00e93cf2a57c`。
専用rootは`/tmp/ds-memory-341-6rlfzxkn`。通常会話・privacy・Episodeはe4b、意味抽出は12b、
Embeddingはnomic-embed-text、専用Ollamaは11438。モデルdigest・各設定はruntime manifestに保存する。

| シナリオ | 観測結果 | 一次証跡（専用root内） |
|---|---|---|
| 単一の花の好みを通常会話で伝える | 12bがDIRECT_EXTRACTIONを保存、実索引・検索から参照 | `lifecycle-source-edit.json` |
| 合成発言の編集 | 実version triggerで版が増加。古い実索引IDを残したままSQLiteでINACTIVEとなり検索から排除 | `boundaries-prepare.json` |
| #100形式の共通保存と検索 | 実Episode 2件→実privacy採用→Semantic保存→実nomic/Chroma検索。同一receiptで二重保存なし。別character/古いEpisode版は拒否 | `boundaries-prepare.json` |
| 派生記憶のUI監査 | 根拠Episodeへのリンクが実画面内で表示位置へ移動。本文訂正ボタンなし、直接PATCHは422 | `ui-derived-inspect.json` / `.png` |
| 別characterからの管理 | 一覧は空、対象GET/DELETEは404 | `ui-derived-inspect.json` |
| 派生記憶のUI削除 | DELETE 204、全版の本文NULL、実Chromaから除外、同じEpisode根拠からの再生成拒否。元Episodeは保持 | `ui-derived-delete.json` / `.png`、`boundaries-deleted.json` |
| 根拠Episode失効 | 古い実索引候補を残しても検索で利用せず、EXPERIENCE_DERIVEDの再評価待ちとなる。outbox後に実索引から消える | `boundaries-invalid.json` |
| 誤ったcharacter索引 | 別namespaceに実ベクトルとmioriのIDを注入しても正本の所有検査で排除 | `boundaries-foreign.json` |
| 保存拒否の通常会話 | 発言は処理済みとなり、拒否対象の意味記憶は形成しない | `lifecycle-privacy-optout.json` |

失効・越境試験の初回はモデル読み込み後のprivacy QUERY_GATEがTIMEOUTになった。
空結果を成功とせず、読み込み後の再試行でgate通過と対象排除を確認した。cold起動の制限は残る。

自己申告を追加した時点では一般化をACTIVEのまま保持し、応答の参照では現在の自己申告を優先した。
最初の応答は具体的な好みを覚えていないと曖昧に答えた。その後の同じ質問では現在の自己申告を回答した。
この先行応答の不十分さを記録し、後続の新しい根拠を使う再検証は以下の誤採用を修正してから行う。

## 実接続で発見した抽出の不具合

既存の否定的なコーヒーの好みがある状態で、コーヒーを選んで飲んだ出来事を伝えると、
semantic-v6が肯定的な好みへCHANGEした。一般化候補の直接入力による結果ではなく、
通常会話の実12b抽出で起きた。失敗したrootと旧レコードは保持する。

- `6d1310d`で`boundary-cases.jsonl`の4件を先に固定した。元100件の入力・期待値は不変。
- v6の追加4件診断は2件の禁止保存で不合格。`/tmp/ds341-boundary-v6-12b`。
- v7は追加4件×3回が成功したが、全体1回目で明示確認20%・訂正80%となり不合格。
  2回目の途中で評価専用process treeを停止し、途中結果を合格として数えない。
- v8は文脈確認の許可を明示し、訂正の引用を原文どおりにする指示を追加。
  追加4件×3回、明示確認10件、訂正10件の診断がそれぞれ100%・90%・100%。
  全体1回目は矛盾保留80%で不合格。以降の繰返しは途中停止した。
- v9はFIXEDの例示と既存属性の優先関係を明確にし、値の断定だけを過去の訂正とみなさないようにした。
  矛盾保留10件は100%、明示確認10件は90%。後続の全体評価と実会話受入は上記の最終結果を参照。

## 自動テスト

関連Backend 61件、schema移行・provenance 54件、Frontend意味記憶UI 5件、character切替の遅延応答module 1件が成功。
加えてchat prompt / RAG / prompt builder / semantic retrievalの関連70件が成功（前記と重複あり）。
Svelte/TypeScript検査はエラー0・警告0。
実行ログは`/tmp/ds341-admin/remaining-v7-regression.log`、`remaining-migrations.log`、`remaining-ui-unit.log`。
これらのmoduleテストにはmock境界があり、上記の実サービス結果とは分ける。
既存レコードを保持するschema更新を確認しており、旧preference本文を新Semanticへ自動変換した証明ではない。

## 再実行手順

本番・dogfoodでは実行しない。`python scripts/acceptance_semantic_memory.py --model gemma4:12b --chat-model gemma4:12b`で空の専用rootを作る。
`acceptance_semantic_scenario.py ROOT LABEL MESSAGE`は通常HTTP会話を行い、処理済み出典を待つ。
以下のlabelは境界driverが読む。本文は固定した合成シナリオを使う。

1. `source-edit`: 好きな花は桜、`derived-source`: 昨日の朝に喫茶店でコーヒーを選んだ、
   `derived-source-two`: 今日も喫茶店でコーヒーを選んだ、をそれぞれ別会話へ送る。
2. Episode・意味記憶・索引の完了を確認し、専用rootの`stop`ファイルでrunnerを正常停止する。
3. `python scripts/acceptance_semantic_boundaries.py ROOT prepare`で出典編集と#100保存契約を確認する。
4. `--resume-root ROOT`でrunnerを再開し、`node frontend/scripts/acceptance-semantic-derived.cjs ROOT inspect`。
5. `self-report-priority`でコーヒーは好きではないと明示し、別会話で応答を確認する。
6. browser driverの`delete`で派生記憶を削除し、別会話の応答を確認する。
7. 正常停止後にboundary driverの`deleted`、`invalid`、`foreign`を順に実行する。
8. 再開時も`--chat-model gemma4:12b`を明示する。再開後、`derived-new-source`で新たなコーヒーの出来事を通常会話へ送る。否定的な好みが変化しないことを確認する。
   対象会話IDの新Episodeが完成したことを確認してから停止する。
   停止後の`priority`は新たなEpisodeを含む候補を受理し、一般化と自己申告の共存・検索優先を確認する。
9. 再開後の別会話で自己申告の応答利用を確認し、正常停止と所有portの閉鎖を記録する。

root・モデル・commitが異なる試行は別のmanifestで記録する。途中の失敗を既存の成功証跡へ上書きしない。

## 追加の実行設定診断

semantic-v9の104件×3回は各カテゴリ90%以上・禁止保存/越境0で合格した。
続く`/tmp/ds-memory-341-ccosx_dx`ではQUERY_GATEの2秒timeoutで通常応答の参照が空となった。
また検証driverが新Episodeの完了前に停止し、priority段階は未完了。
この試行を受入成功には数えない。専用runnerでは同一e4bの会話・privacy・Episodeの
総contextを36,864へ統一して再測定する。query gateの2秒上限、privacy判定、出力上限は変更しない。

contextを統一した`/tmp/ds-memory-341-wdgpmc36`は検索・各保存境界と最終応答が成功した。
ただし自己申告直後と派生削除後の応答は再申告を求めており、`苦手`だけの文字列検査では
誤って成功になった。手動監査を`manual-review.json`へ記録し、この試行全体は合格としない。
現在の自己申告に答えがある場合はその内容で回答する指示と、明確な否定形を確認する検査を追加した。

`dbfb51f`の指示強化もe4bの最初の応答には効果がなかった（root: `/tmp/ds-memory-341-wlsyqzbw`）。
専用診断で実際の入力を記録し、自己申告の本文・優先指示が欠落していないことを確認した。
同じ人格・RAG・質問で12bは否定的な好みを回答したため、指示強化は撤回し、
最終受入の会話Targetも12bを明示指定する。e4b会話の当該制限は未解消として残す。
意味抽出のv9正式評価と、応答モデル選定の単一シナリオ診断を同一のモデル比較として数えない。


## CodeRabbitによる受入driverの回帰修正

追加レビューで、応答直後の停止・ポーリング失敗ではsnapshotがなく、同じlabelを再利用できる問題を確認した。
`lifecycle-events.jsonl`に記録済みのlabelもHTTP開始前に拒否するよう修正した。
停止・待機失敗の2状態と、新しいlabelの許可を3つのunit検証で確認。
最終受入rootのイベントlabelはすべて一意であり、保存した受入結果への影響はない。
アプリの抽出・privacyコードは変更していない。
