# 画面共有ブラウザ実機確認手順

## 目的

Issue #213および#217で、Windows 11上のデスクトップ版Google Chrome／Microsoft Edgeが実際に返す画面共有surfaceとframeを確認する。mockやAPIの存在確認だけを成功証跡にしない。

個人デスクトップ、実会話、URL、window title、画像、Vision観測本文は記録しない。機微情報を含まないテスト専用の合成画面を使用する。「公開」はインターネット公開を意味せず、localhostで手動試験中だけ配信する。検証ページは製品buildやサービスへ含めない。

## 準備

1. 対象commitをcheckoutした開発用worktreeでFrontendディレクトリへ移動する。
2. 次のようにlocalhostだけへ検証ページを公開する。

   ```bash
   python3 -m http.server 4175 --bind 127.0.0.1 --directory frontend/manual
   ```

3. Windows側ブラウザで`http://localhost:4175/screen-capture-probe.html`を開く。
4. `secure context`と`API`がともに`true`であることを確認する。
5. ブラウザのversionは公式のversion画面から確認する。検証ページのUser-Agentを証跡へ転記しない。

検証ページはnetwork request、localStorage、sessionStorage、Cookie、downloadを使用しない。表示した対象名と静止画はページ内だけに保持し、共有停止またはpagehideで参照を解放する。

## Chrome／Edge共通シナリオ

各ブラウザで次を行う。

1. 「モニターを選択」から公開・合成画面を表示した単一monitorを選ぶ。
2. 実際の種別が`monitor`、video/audio track数が`1 / 0`であることを確認する。
3. 「静止画をローカル確認」を押し、previewと静止画の対象が一致することを確認する。
4. 複数monitorがある場合、選択した1台だけであることを確認する。
5. monitorごとに表示倍率が異なる場合、frame寸法と表示崩れの有無を確認する。
6. 「共有を停止」とブラウザ標準の共有停止を別々に試す。
7. 「ウィンドウを選択」から公開・合成windowを選び、同様に`window`、`1 / 0`、preview、静止画を確認する。
8. windowを背面化、最小化、サイズ変更、別monitorへ移動、終了し、frameとtrack状態を記録する。
9. pickerで拒否／取消しを行い、共有中にならないことを確認する。
10. 「ブラウザタブを選択」から合成画面のtabを選び、`browser`、`1 / 0`、preview、静止画を確認する。digital-souls自身のtabが除外されることも確認する。
11. 希望と異なる種別を選び、送信可能状態にならずtrackが停止することを確認する。
12. picker表示中にページを再読込し、後から古い共有が有効にならないことを確認する。

管理policyによる禁止を試せる環境では、設定前後の成否だけを記録する。registry値、企業名、管理情報、raw errorは記録しない。Webアプリから拒否とpolicy禁止を確実に区別できない場合、共通の`capture_not_allowed`で正しい。

## #213 記録テンプレート

次のmetadataだけをIssue #213のコメントへ記録する。

```text
実行日時: YYYY-MM-DD HH:MM JST
commit: <40桁SHA>
環境区分: dev
OS: Windows 11 <version>
Browser: Chrome または Edge <version>
対象: monitor / window / browser
secure context: true / false
displaySurface: monitor / window / browser / unavailable
video/audio tracks: <count>/<count>
preview: success / failed
local snapshot: success / failed
背面: continued / unavailable / not-tested
最小化: continued / unavailable / not-tested
共有停止: success / failed
混在DPI: success / failed / not-tested
結果: success / failed
reason code: <失敗時の固定codeだけ>
```

## #217で追加する項目

#217では完成した通常UIを使用し、上記に次を追加する。

- text／LiveKit音声の明示要求
- 共有中で会話内に別対象がない「これ何？」と位置付きの「右上の赤い表示、何？」
- 貼付本文への「これ何？」、直前回答への「それ、詳しく」、否定、引用、対象競合
- 単一対象、複数候補、対象消失、小さい文字、質問と無関係な共有画面
- Ollama／openai-apiのProvider種別とmodel
- rule／LLM／fallbackの分岐と、判定LLM呼出率
- input、参照判断、snapshot、upload、Vision、Chat初回内容あり出力、音声開始のmetadata-only遅延
- OFF、対象変更、会話変更、通信断後の遅延結果破棄
- cloud未同意、同意取消し後の画面由来履歴非送信、provenance継承、memory除外、保存禁止
- digital-souls側タブの背面／非表示と、取得対象windowの背面／最小化を分けた結果

合否集計ではADRの誤参照率、参照漏れ率、不要確認質問率、対象特定率、判定LLM呼出率、p50／p95基準を使う。45秒の障害timeout到達を会話品質の目標達成として扱わない。会話本文、画面内容、対象名は集計artifactへ残さず、合成case ID、decision、成功可否、区間時間、固定reason codeだけを記録する。

#213のprobe成功を#217の完成UI受入の代わりにしない。

## #217 完成UIの実施順

各ブラウザでmonitor、window、browserの順に共有し、各surfaceでtext、LiveKitの順に実施する。同じ共有session中は
pickerを開き直さない。

1. ON直後に画像要求が0件であることを確認する。
2. 合成画面の単一対象へ「これ何？」、位置付き要求、明示チェック付き送信を行い、各turnで新しい画像が
   1枚だけ要求され、対象を短く示した実質回答になることを確認する。
3. 貼付本文、直前回答への「それ、詳しく」、否定、引用を各1回送り、画像要求がないことを確認する。
4. 複数候補、対象消失、小さい文字、無関係な画面を各1回確認し、推測せず候補または見えない旨を
   キャラクターの言葉で返すことを確認する。
5. 取得対象windowの背面化・サイズ変更・最小化と、digital-souls側tabの背面・非表示を別々に試す。
6. OFF、対象変更picker取消し、会話変更、再読込、通信断を行い、旧画像要求・遅延回答・TTSが採用されず、
   自動再開しないことを確認する。

現在の既知実測はWindows 11 25H2、Chrome 152.0.7977.77、Edge 152.0.4191.62で、両browserとも背面化・
サイズ変更は継続、最小化は停止、対象変更pickerの取消しでは元の共有も解除された。これはMVPの
安全側挙動として受け入れ、最小化時の継続を保証しない。

## #217 証跡テンプレート

```text
実行日時: YYYY-MM-DD HH:MM JST
commit: <40桁SHA>
環境区分: dev
OS / Browser: <version>
対象: monitor / window / browser
経路: text / LiveKit / explicit UI
Provider / Model: <provider> / <model>
合成fixture ID: <ID>
分岐: rule / LLM / fallback / Vision
結果: success / failed / unsupported
reason code: <失敗時の固定codeだけ>
参照判断 ms: <数値>
実質回答開始 ms: <数値>
TTS開始 ms: <数値またはnot-applicable>
画像要求数: <数値>
背面 / 最小化 / サイズ変更: <結果>
```

集計時はcase総数、誤参照率、参照漏れ率、不要確認率、対象特定率、判定LLM呼出率、rule P95、LLM warm
P95、実質回答開始P50／P95を記録する。本文、画像、対象名、window title、URL、secret、raw errorは
記録しない。
