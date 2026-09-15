# #341 残受入条件の検証（2026-09-15）

本書は子Issueのチェック項目と証跡を対応づける。mainへのマージ・dogfood配備は対象外。
現在は追加の実会話で見つかった出来事の誤採用を修正し、semantic-v9の再評価中。
この段階を子Issue全件完了として扱わない。

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
| #390 | 固定100件×両モデル各3回、設定・誤り・時間・tokens・資源・会話負荷を計測 | [v6比較](semantic-memory-341-v6-comparison.json)、[並走実測](semantic-memory-341-preemption.json)。変更後の12b評価を追加中 |
| #349 | 通常会話→保存→別会話→UI→後続応答、source/privacy/character/失敗回復、#100契約 | [先行実接続](semantic-memory-341-2026-09-15.md)と本書を合わせて確認 |

## 追加境界試験の結果

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
  矛盾保留10件は100%、明示確認10件は90%。全体評価と修正後の実会話受入は継続中。

## 自動テスト

関連Backend 61件、schema移行・provenance 54件、Frontend意味記憶UI 5件が成功。
実行ログは`/tmp/ds341-admin/remaining-v7-regression.log`、`remaining-migrations.log`、`remaining-ui-unit.log`。
これらのmoduleテストにはmock境界があり、上記の実サービス結果とは分ける。
既存レコードを保持するschema更新を確認しており、旧preference本文を新Semanticへ自動変換した証明ではない。

## 再実行手順

本番・dogfoodでは実行しない。既存の`acceptance_semantic_memory.py`で空の専用rootを作る。
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
8. 再開後、`derived-new-source`で新たなコーヒーの出来事を通常会話へ送る。否定的な好みが変化しないことを確認する。
   停止後の`priority`は新たなEpisodeを含む候補を受理し、一般化と自己申告の共存・検索優先を確認する。
9. 再開後の別会話で自己申告の応答利用を確認し、正常停止と所有portの閉鎖を記録する。

root・モデル・commitが異なる試行は別のmanifestで記録する。途中の失敗を既存の成功証跡へ上書きしない。

## 追加の実行設定診断

semantic-v9の104件×3回は各カテゴリ90%以上・禁止保存/越境0で合格した。
続く`/tmp/ds-memory-341-ccosx_dx`ではQUERY_GATEの2秒timeoutで通常応答の参照が空となった。
また検証driverが新Episodeの完了前に停止し、priority段階は未完了。
この試行を受入成功には数えない。専用runnerでは同一e4bの会話・privacy・Episodeの
総contextを36,864へ統一して再測定する。query gateの2秒上限、privacy判定、出力上限は変更しない。
