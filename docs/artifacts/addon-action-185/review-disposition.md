# main向けCodeRabbitレビューへの対応

対象: [PR #311](https://github.com/FYuki/digital-souls/pull/311)。
初回レビューは `ff414e83a384fad7a0b41a187ee52ea078e22189` → `da2504282f89fa8e1b0716db73069beaaa31824d` の全差分53ファイル。
Run ID: `c6c55375-2387-4ddf-842a-10672941f01c`。21件の指摘を現行実装と#185の合意へ照合した。

| コメントID | 対応 |
|---|---|
| 3982339611 | 未解決実行のSQL絞込みとTask検索のindexを追加 |
| 3982339633 | 回復追跡のDB例外を捕捉し次巡へ継続。例外本文を記録しない。エラー後の実回復を検証 |
| 3982339641 | 不採用。#185の承認単位は接続 × 操作群 × 実行場面。キャラクター別承認への変更は合意を変更する。接続sharing、Autonomy Grant、binding、確認要求のactor照合は維持 |
| 3982339652 | 確認fingerprintへ接続identity・操作群・実行場面を追加し、別scopeの重複排除を防止 |
| 3982339662 | 再開時の結果投影も3000byte境界へ制限 |
| 3982339674 | pause時のloop検索キーを正規化したrun IDへ統一 |
| 3982339694 | 不採用。Utteranceは永続レコードではなく、ConversationCoreSession内の入力。Coreはcharacter IDを保持しない。LiveKitの予約済みsession→character/conversation照合と、Tool実行時の両ID照合を既に実施。永続会話・確認要求・実行記録のcharacter IDは保持 |
| 3982339702 | 接続失敗から再送信前に返すMCPFailureへretryableを引き継ぐ。未送信のまま再試行属性を保持するテストを追加 |
| 3982339711 | 接続無効化で当該要求だけend_wait。同一loopの別接続の承認・実行を維持する回帰を追加 |
| 3982339718 | 追加回復照会のMCPFailureで既に保存済みのCONFLICTを失わず返す |
| 3982339731 | 通常応答・JSONResponseの双方にCache-Control: no-storeを設定し、HTTP境界で検証 |
| 3982339740 | 投影後のRESULT_UNKNOWNで当該turnの追加dispatchを止める。本文の差分提案に従い、要約内の「次操作へ進む」という逆の指示は採用しない |
| 3982339760 | 承認・Tool・Character Lifeの共有補助を収集対象test moduleから独立した補助moduleへ抽出 |
| 3982339767 | サービス専用の時計差し替えに全time APIを保持。共有time.monotonicの直接変更はasyncioの時計も変えるため採用しない |
| 3982339775 | キュー作成の観測期限を承認待機期限より短くする |
| 3982339780 | characterが同じでsessionだけ異なる画面続行を拒否する回帰を追加 |
| 3982339790 | 引数が違う不明操作のreplayedがfalse（未指定）であることを明示検証 |
| 3982339793 | schema適合・binding IDなしの分類を独立に検証 |
| 3982339796 | 相対書込pathをunresolved_write_pathとして拒否する回帰を追加 |
| 3982339803 | 音声sessionの入力受付終了を待機中／idleの双方で検証 |
| 3982339808 | 不採用。ユーザーのtimezoneはAsia/Tokyo。2026-09-10 UTC夕刻は2026-09-11 JSTであり、Issue・ADRの日付は合意日に一致 |

M5で追加した実接続由来の修正・証跡はこの初回レビューの後にmain向け差分へ入る。最新差分・CIの確認が完了するまでPRをReadyにしない。

## M5統合後の全差分レビュー

対象は `ff414e83a384fad7a0b41a187ee52ea078e22189` → `ec483a1d32c0f9cd68b89cd0afd8b3a1c29f0b30` の98ファイル。
Run ID: `27208663-90e6-40d4-98ab-f628a422e9ed`、review `5173066912`。初回21件は統合後に解決済みとなった。

| コメントID | 対応 |
|---|---|
| 3984239926 | 待機中の単回承認を要求IDと固定fingerprintへ予約し、別要求による消費を防止。遅い承認の将来枠は分離。競合・重複回答・旧DB移行・期限切れ・stop・再起動を検証 |
| 3984239935 | 音声制御入力はCoreのstate lock内で確認状態と期限を再検証。lock待機中の期限切れでは入力・応答を登録しない回帰を追加 |
| 3984239942 | 回復結果のprojectionでも、list・dict・string・深さによる内部省略をLLMへ明示。初回の外部結果整形から保存・回復・再返却まで省略通知を保持する。省略なし・outcome・replayed保持も検証 |

関連unitとmypyの結果、最終統合CI・実接続再受入・修正差分のCodeRabbit確認はPR #311に記録する。
