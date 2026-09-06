# 外部MCPの会話利用（#182）

## 状態と範囲

`ACTIVE`。2026-09-07に#182で合意した契約。実装進捗と受入結果はIssue／PRで管理する。

初期受入は外部stdio MCPとStreamable HTTP MCPを使うブラウザのテキスト・LiveKit音声会話。
音声はSTTからTool選択・実行・回答統合・TTS・ブラウザ再生まで含む。
自作Addon、管理画面、Prompt取得・明示適用、MCP画像／音声結果の内容理解、
キャラクター全体へのbinding永続化、長時間自律runtimeは後続とする。

## 責務

#104のnative definition、validated active snapshot、trust/effective policy、Execution Gateを正本とする。
Tool CatalogはLLM選択用のprojectionであり、独自Query/Command分類やstable operation IDを要求しない。
annotationsをTool利用層で再解釈せず、元schemaで実行引数を再検証する。

Tool判断・入力待ち・現在turnの結果はtransport非依存のCoreサービスが所有する。
HTTPとLiveKitは同じサービスを呼び、履歴保存は既存のsanitizerとMemory Formationへ委譲する。
旧WebSocket baselineへTool機能を追加しない。

## 推論と候補

`tool-routing`をoptionalなCore Targetとして追加する。要求能力は構造化生成とtoken推定。
Provider/Modelは環境で明示し、未設定時は通常会話を維持してTool利用を無効とする。
CoreのCaller認可を通し、AddonにはTarget選択権を渡さない。

ToolDecisionは構造化生成を使い、候補IDとbindingの選択肢をschemaで制限する。
引数はJSON objectとして解釈し、元MCP schemaで再検証する。
接続有効性・sharing・固定snapshot・operation status・binding可能性で候補を絞り、
名前・説明と要求の関連性で順位を付けてからschemaを提示する。

初期値は候補8件、schema合計4,096 tokens、結果合計4,096 tokens。
UTF-8 byte数による保守的な上界とAdapterの入力全体推定を併用し、モデルの入力上限を優先する。
巨大schemaは候補数を減らして対応する。schemaを縮約して実行制約を消さない。
数値は実測で調整可能とする。

## binding

binding不要な接続はunbound。必要な接続は管理側が対象のlabel、character、operation、
必要な固定引数を定義する。ユーザー明示指定、会話内の既選択、一意の候補の順に解決し、
複数候補は質問する。固定引数と矛盾するLLM引数は拒否する。

bindingは呼び出し単位でGateへ渡す。validator用IDをCoreが発行し、会話・主体に紐づける。
複数対象を使ってもloop・snapshot・予算を分割しない。入力回答後の再実行は元bindingを保持する。
実行待機中の停止・binding失効もdispatch直前に確認する。

## loop・入力待ち・停止

1 loopは固定snapshotで行う一連の判断・実行。追加呼び出しは同じloop内で行う。
既定は6 calls、同一Tool連続3回、同一引数2回、自動cycle最大3回。
refreshで新snapshotを使う場合だけ次loopへ進み、予算不足を理由にloopを新規作成しない。
非信頼の結果本文だけではrefreshを許可しない。
CoreのLLM判断回数とturn処理時間にも有限の上限を設ける。

MRTRは既存contextに明示された情報で回答可能ならそのまま再開できる。
通常の追加情報は短い質問として表示・読み上げ、テキスト／STT回答をCoreがinteractionへ対応付ける。
再開時は同じsnapshot/grant/binding/budget。requestStateはopaqueでありLLMや画面へ渡さない。
新たな許可や管理設定が必要な場合は案内して停止し、secretをチャットや音声で収集しない。

質問待ちは初期10分。明示停止、会話切替、切断、期限切れで終了し、再起動を越えて永続化しない。
質問読み上げ中の回答開始を待機取消しと混同しない。別要求への切替は旧操作を終了して新要求を処理する。
通常のbarge-inでは旧responseの新規dispatchと読み上げを止め、遅延結果を次responseへ混入させない。
開始済みの外部副作用を取り消せたとは扱わず、結果不明の操作を自動再送しない。

## 結果と受入

text／structuredContentを必要量だけsanitizeして現在turnへ追加し、省略を明示する。
画像等の非対応要素はnative正本に残し、内容を理解したとは扱わない。
生endpoint、credential、auth/tool error本文をLLM・画面・通常logへ渡さない。
Coreが付与したsource、revision、bindingを維持し、画面には人が読める出典を表示する。

実LLM＋公開Filesystem／Everything Serverで選択・実行を検証する。
ブラウザのテキストと、実STT/TTS/LiveKitを通す音声の両方で回答統合まで受け入れる。
MRTRや故障・境界条件はfixtureによる契約テストを併用し、独立Serverとの実接続証跡と区別する。
dogfoodのデータやプロセスをテスト対象にせず、共通推論サービスをテストから停止しない。
