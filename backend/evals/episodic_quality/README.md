# Episode / FactのLLM判断品質評価

現行のThreadEpisodeExtractorとSemanticPrivacyClassifierをpromptfooから実LLMへ接続する。
旧memory_extraction suiteはMemoryCandidateExtractorが対象であり、#340のEpisode / Fact操作の品質証明には使わない。
privacyには同一モデル・promptでの既存3回受入記録があるが、本suiteでは保存前のADMISSIONを再測定する。

## 評価対象と合格条件

| 分類 | 件数 | 固定正解で確認する判断 |
|---|---:|---|
| selection | 10 | 日常体験からFactを抽出し、挨拶・相槌からFactを創作しない |
| episode_boundary | 10 | 一続きの経験のCONTINUEと、後日の語り直しのNEW |
| fact_operation | 10 | 明確な訂正・補足のUPDATE、再参照のREFERENCE、曖昧な対象でのNEW |
| what_where_why | 10 | 話題の対象・明言された場所と理由、未申告値の非補完 |
| temporal | 10 | 相対日/月/年、絶対日時の精度、不明日時 |
| context | 10 | 仮定・創作・否定・予定 |
| links | 10 | 二つの独立したFactの抽出とEpisodeへの関連付け |
| evidence | 10 | 本文に存在する根拠引用、出典ID・版・role・位置・primary範囲 |
| catalog | 10 | 同一threadの既存記録の対象・操作・確実/曖昧判定 |
| same_event | 10 | 同じ出来事の再言及と、別回・仮定・創作・予定の照合見送り |
| privacy | 60 | 既存日英合成corpusの保存前意味分類 |

各分類の正解率90%以上を要求する。100件側は分類ごとに9/10以上、privacyは54/60以上。
全体平均で低い分類を相殺しない。privacyには既存の棄権率10%未満・偽陰性率5%未満・
偽陽性率20%未満も追加で要求する。ABSTAINや推論エラーは正解にせず分母に残す。
case欠落・重複・未知IDやモデル/provenance混在は実行不成立。キャッシュを使った結果は不正解。

正解は推論入力へ渡さない。LLM judgeは使わず、列挙値・対象ID/版・件数・変更項目、
事前固定した語句・null・引用一致を照合する。自由記述の言い換えを網羅する試験ではない。
全ケースは合成であり実ユーザーの会話を使わない。元の行為者100件suiteは別途維持する。

## 本番との接続と限界

- extract: 本番SYSTEM_PROMPT、ExtractionBatch、schema修復とcanonical anchor修復を使う。
  操作選定後の5W再推論も本番と同様に呼び、確定した抽出結果を採点する。
- ground: 対象行為だけを固定し、本番_ground_contentで5Wと文脈を読み直す。
- catalog: 本番CATALOG_SCAN_PROMPTとCatalogMatchesを呼ぶ。同一性の意味判断もここで測る。
  大量catalogの全ページ走査やmerges提案の最終採用・DB統合実行はこの正解率に含めない。
- privacy: 既存production providerとADMISSIONのtimeout・再試行をそのまま使う。
  QUERY_GATEや複数出典/生成slotをまとめた保存処理全体はこの再測定の対象外。

実推論を使う判断段階ごとのコンポーネント評価である。キュー、DB、privacyの総合保存判定、
検索・応答、Speech/LiveKitまでを通す#344の総合受入を代替しない。
各分類10件は初期の回帰corpusであり、実運用分布への汎化や統計的な90%保証ではない。
失敗例を見てpromptを調整した場合、同じ集合の再評価であることを明示する。

## 実行

backend依存とrootのpromptfooを導入し、Ollamaのgemma4:e4bを利用可能にする。
backendのvenvをPATHへ通して npm run eval:episodic-quality を実行する。

推奨設定: MEMORY_EXTRACTION入力32768 / 出力4096 / timeout180秒、
MEMORY_FORMATION_LLM_TIMEOUT_SECONDS=180、MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS=400。
INFERENCE_TARGET_MEMORY_EXTRACTIONとINFERENCE_TARGET_PRIVACYはollama/gemma4:e4b、
両TargetのOPTIONS_JSONは {"temperature":0,"think":false} とする。
privacyは本番ADMISSION profileのtimeout・再試行も適用される。

既存devとの推論競合を避ける。共有Ollama・dogfood・devのprocessやデータは操作しない。
-- --output-dir <空ディレクトリ> を指定できる。結果・case別進捗・manifestとCLIログは保持する。
manifestにはcommit、corpusと評価コードのhash、model digest、設定を記録する。
結果全文はローカルに保持し、privacyの既存方針に従って外部へ共有しない。
共有記録は件数・分類別成績・設定・失敗種別などの集計に限定する。

## 分類だけの再測定

npm run eval:episodic-quality -- --categories catalog same_event のように分類を指定できる。
選んだ分類の全件を測定し、その分類ごとに90%を要求する。全項目の合格とは扱わない。
manifestには実行したcase IDを記録し、corpus全体のhashも維持する。
引用位置の一意な原文一致による補正は本番resolve_quoteと同じ条件で採点する。
