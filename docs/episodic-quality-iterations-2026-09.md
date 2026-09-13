# Episode抽出の指示文・出力設計の評価実験

## 条件と反復上限

2026-09-13のユーザー指示により、改善候補と実測の組を最大20回とする。
既存v12を反復0とし、同一候補の確認再実行は反復内のrunとして明示する。
候補間で正解データ・各カテゴリ90%の基準を変えない。
診断用の固定部分集合と全件の受入結果を区別する。
モデル能力不足は、同条件のモデル比較なしには断定しない。
実ユーザー会話・dogfoodデータは使わない。

## 基準値

v12の既存評価は170件中104件正解、12カテゴリ中8カテゴリが90%未満。
抽出・推論エラー48件を含む。行為者評価は84/100。
170件の基準値は同一モデル条件による80+90件の採用結果であり、単一runではない。
証跡: /tmp/ds340-quality-report-6zph0zdq。

## 反復0: 診断

固定ケース fact_operation-01 ではFactの訂正内容そのものは正しく抽出したが、
存在しないEpisode key "null" へのlinkを2試行とも作り、全体が検証で拒否された。
出典・保存契約は緩めず、出力の組み立てを改善する。
ローカル合成診断証跡: /tmp/ds340-iteration-00。

## 反復1: 出力順序と空配列の明示

v13: recordsを完成してからlinksを作る、実在するkeyだけを参照する、
無候補は空配列、統合時の既存FactをREFERENCEとして列挙することを明示する。

## 診断中の候補

| 反復 | 変更 | 固定6件の正解数 |
|---|---|---:|
| 1 | v13、出力順序・空配列を追記 | 1/6 |
| 2 | 番号と引用による簡素な計画出力 | 1/6 |
| 3 | NEW/UPDATE/REFERENCE別の型と形式例 | 0/6 |
| 4 | 型の項目順をoperation先頭へ統一 | 2/6 |
| 5 | 反復4の生成文法制約だけ解除、事後検証は維持 | 0/6 |

部分集合は selection-01 / episode_boundary-01 / fact_operation-01 /
links-01 / evidence-01 / merge_proposal-01 を初めに固定した。
正解数は最終の5W確認・出典検証を通した結果で、下書きだけの成績ではない。
反復3では型ごとの項目順が異なりREFERENCEへ偏った。反復4でNEWも返るようになった。
反復5はMarkdown囲みなどの形式違反が増え、採用しない。
合成診断のリクエスト・応答は /tmp/ds340-iteration-NN に限定して保存する。
これは実ユーザー本文を保存する機能ではなく、productionには組み込まない。

| 6 | 種類別の既存記憶一覧・原文を保った引用文の提示 | 3/6 |
| 7 | 反復6のままモデルだけqwen3-coder:30bへ変更 | 4/6 |
| 8 | 出来事単位の重複抑制、5W指示を短縮し下書きのUNKNOWNを除去 | 3/6 |
| 9 | 同一出来事判定の分離、同一IDの参照統合、同値の変更項目を除外 | 4/6 |
| 10 | Fact操作とEpisode境界を別推論へ分割 | 0/6 |
| 11 | 種類別の配列位置、未処理入力フラグへ変更 | 4/6 |

反復6と7はモデル以外の指示・出力設計を固定した比較。gemma4:e4bは3/6、
qwen3-coder:30bは4/6であり、後者もEpisode継続と統合に失敗した。
小さい部分集合であり、モデル一般の能力順位や上位モデルへの変更だけでの解決は示さない。
反復9でgemma4:e4bの統合正例が成功したため、少なくともこの失敗には出力設計による改善余地がある。

反復10はEpisodeのcomplete=falseや一覧外targetの生成が残った。
反復11は完了を「未処理の入力が残るか」にし、FactとEpisodeの番号をそれぞれの一覧位置へ合わせた。
失敗した候補をproductionへ昇格させず、現行抽出器はv12を維持する。

## 反復12: 提示範囲に生成を限定

候補番号をschemaのenumにも反映し、NEWしか選べない入力では既存操作の分岐を提示しない。
引用番号、Episodeの話題番号にも同じ範囲制限を適用し、説明内schemaと生成schemaを一致させる。
挨拶・前置き・本題・締めくくりを含む一般例をJSONで明示した。
固定6件は6/6正解。これは受入合格ではないため、変更された経路の全件評価へ進む。
証跡: /tmp/ds340-iteration-12。

## 再実行

診断は scripts/episodic_prompt_lab.py の --iteration 1..20 / --output-dir /
--design production|compact / --format schema|plain / --model / --cases で実行する。
ケースは登録済みの synthetic=true だけを許可し、正解はproviderへ渡さない。
各runは新規ディレクトリへソースの写し、hash、model digest、結果、合成診断traceを保持する。

全件の成績判定は既存のpromptfooを使う。
npm run eval:episodic-quality -- --design compact --output-dir /tmp/unique-output
npm run eval:episodic-actor -- --design compact --output-dir /tmp/unique-actor-output

compactは評価専用の設計試作であり、アプリの実行経路を切り替える設定ではない。
出典定位・所有範囲・通常のExtractionBatch契約を通すが、
productionの大規模catalog分割・SQLite登録・検索・privacy保存までの受入は別途必要。

## 反復12の全件結果

品質170件は139/170。selection・what_where_why・context・links・evidenceは各10/10、
privacyは60/60。未達はEpisode境界5/10、Fact操作6/10、日時4/10、
catalog1/10、同一出来事照合5/10、統合提案8/10。
行為者は68/100へ後退し、特に引用内一人称2/10、引用内二人称0/10。
未知の行為者を空配列にする判断は10/10へ改善した。
この短い5W指示をそのままproductionへ適用しない。

正式証跡: /tmp/ds340-iteration-12-full-quality、
/tmp/ds340-iteration-12-full-actor。commit b90423919adb1dba97f0b5ad35d023193eb61096。
固定corpusは変更なし。promptfooのno-cache・並列1・同一モデルと予算で実測した。
CI: https://github.com/FYuki/digital-souls/actions/runs/34748549670 （全4項目成功）。

## 反復13の候補

会話本文を分割せずconversationとして提示し、引用候補はquote_optionsへ分離する。
引用候補の数を出来事数と誤認させない。引用番号と話題番号の名称を分ける。
catalogは一件のfocusの判定に分け、他候補も曖昧さを判断する文脈として提示する。
人物・日時の追加schemaは準備のみで、この候補では有効化しない。

## 反復13の結果と反復14

反復13の診断31件は15/31。catalogは7/10へ改善したが、同一出来事照合は5/10のまま。
会話全体と引用候補の再構成は一部のEpisode重複を招き、元の6件では2/6。
この再構成を一括採用せず、catalogの一件照合だけを残す候補を別途比較する。
証跡: /tmp/ds340-iteration-13。

反復14は5W確認だけを変更する。元の詳細な引用話者・先行詞・日時の説明を戻し、
人物の役割とID/nameの対応、相対/絶対/期間日時の整合性を生成schemaでも制限する。
事後のPydantic・出典・保存契約は維持する。日時30件ではなく、
what_where_why / temporal / contextの計30件と行為者100件を比較する。

## 反復14の結果

対象・場所・理由10/10、日時9/10、文脈10/10で各90%基準を満たした。
行為者は91/100となり、基準84/100、短縮案68/100から改善した。
同一のgemma4:e4bで達成している。ただし単一runの固定合成ケースであり、反復確認は別途必要。
証跡: /tmp/ds340-iteration-14-full-quality、
/tmp/ds340-iteration-14-full-actor。commit 744bec8885bbc95b84d17f65d86e90bea58cdad7。

## 反復15の候補

抽出の基本構成は反復12へ戻し、反復14の5W指示・生成制約を保持する。
catalogでは「既存記録に言及したか」ではなく「今回の出来事と同一か」を直接問う。
同一性と補足・訂正の有無を別フィールドにし、操作名はアプリ側で組み立てる。
複数対象が残る場合は一意とせずPOSSIBLEへ下げる。
完全に同じEpisode提案だけを重複排除し、空の日時オブジェクトをnullへ統一する。
日時を不明へ統一しても出典は残す。

## 反復15の結果と反復16

反復15は診断32件中29件正解。catalogと同一出来事照合は各10/10。
未達は語り直し episode_boundary-02、場所補足 fact_operation-04、
既存対象が曖昧な fact_operation-09。証跡: /tmp/ds340-iteration-15。
反復16では変更項目を先に選び、whatを変更しないUPDATEでは既存Whatを保持する。
語り直しの前置きと本文を一つのEpisodeにする一般例を追加する。
