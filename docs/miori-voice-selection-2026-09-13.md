# 光織の音声選定・参照合成記録（2026-09-13）

## 対象と結論

[Issue #330](https://github.com/FYuki/digital-souls/issues/330)の成果物として、ユーザーが選んだ
B-3・seed 4221・speed 1.00のWAVを保存した。採用資産と登録手順の正本は
[光織のvoice.md](../characters/miori/voice.md)とする。

本変更は音声資産と引き渡し資料だけを対象とする。2026-09-13のユーザー指示により、
スキーマ定義とCCVへの反映は[Issue #329](https://github.com/FYuki/digital-souls/issues/329)側で扱う。
音声選定の完了を、Irodoriのモデル最終採用や共通推論サービスの受入と同一視しない。

## 選定経緯

1. 選定開始時のCharacter Card（commit `6f4b97dcee27c186d360ad7596655b45efd495b9`）を参照し、
   穏やかさ、丁寧さ、優しい先輩、控えめな感情表現を声の方向へ反映した。
2. ユーザーの補足を受け、性別の曖昧な声ではなく「中性的な雰囲気を持つ女性の声」へ絞った。
3. 透明感・軽やかさを持つ⑤Bから候補を作り、B-1とB-3を比較した。
4. Captionを固定してseedを比較し、B-3・seed 4221を選んだ。
5. 話速0.9、1.1、0.99、0.98を比較後、ユーザーが標準1.00を採用し、
   現設定での参照音声作成を指示した。
6. 採用WAVを波形変更なしで保存し、dev用Irodoriへ`miori-b3-4221`として登録した。

[selection.json](artifacts/miori-voice-330-2026-09-13/selection.json)に47件の異なる比較音声の
Caption、seed、speed指定、文章、生成日時、SHA256を保存した。モデルrevisionと精度も同記録に含む。
元候補WAVはdev用Irodori worktreeの`outputs/<source_run>/<source_file>`に保持する。
リポジトリには採用WAVと今回の参照合成WAVを保存し、すべての不採用WAVは複製していない。
生成用の固定セリフは比較用素材であり、現在のCharacter Cardの役割や使命を再定義しない。

## 当初のUI計画との差分

#330起票時は公式の`gradio_app_voicedesign.py`を使用する計画だった。
実際にはユーザーがdevへのServer版導入とAPIによる候補生成・比較を指示し、
公式Irodori-TTS-ServerのVoice Design APIで候補を作り、この会話上で試聴・選定した。
公式Gradio UIの構築・使用は未実施であり、その項目を完了扱いにはしていない。
digital-souls内の独自UIも追加していない。選定用に第2のGPUモデルを同時起動していない。

## 検証条件

- サーバー: dev用WSL `Ubuntu`、`http://127.0.0.1:8088`
- GPU: NVIDIA GeForce RTX 4070 Ti SUPER
- モデル: `Aratako/Irodori-TTS-v4.1-Small`、非量子化、CUDA/BF16
- 参照音声: `miori-b3-4221`、14.12秒、48 kHz、mono、16-bit PCM
- 参照合成: 採用Caption、seed 4221、speed 1.00、40 steps
- 分割比較: 同じ本文を全文1リクエストと2区間の個別リクエストで実合成。
  Irodori側の自動分割は`chunking_enabled=false`。
- 今回はIrodori単体APIの検証。Backend、CCV、LiveKitを経由していない。

モデル／codec／コードrevision、元WAVの生成条件は
[音声metadata](../characters/miori/assets/voice/miori-b3-4221.json)に保存した。
非量子化と推論精度は別の項目であり、今回は重み量子化なし・BF16推論である。

## 実接続結果

元WAVのSHA256が採用時と一致し、devの登録一覧で参照音声として解決されることを確認した。
次の3リクエストは実APIでHTTP 200、48 kHz・mono・16-bit PCMの非無音WAVを返した。
処理時間は今回の各1件の実測であり、p50/p95や発話終了から再生開始までの遅延ではない。

| 音声 | 長さ | API処理時間 |
|---|---:|---:|
| [full.wav](artifacts/miori-voice-330-2026-09-13/full.wav) | 6.88秒 | 1.881秒 |
| [segment-01.wav](artifacts/miori-voice-330-2026-09-13/segment-01.wav) | 3.20秒 | 1.538秒 |
| [segment-02.wav](artifacts/miori-voice-330-2026-09-13/segment-02.wav) | 3.56秒 | 1.540秒 |

[segmented.wav](artifacts/miori-voice-330-2026-09-13/segmented.wav)は
`segment-01.wav`と`segment-02.wav`のPCMを順に連結した試聴用音声。
無音の追加・削除・音量補正はしていない。
[verification.json](artifacts/miori-voice-330-2026-09-13/verification.json)に
リクエスト、日時、処理時間、WAV形式、SHA256、未確認事項を保存した。

## 分割境界の試聴指摘と追加比較

ユーザーの試聴で、分割連結音声は文の間がなく不自然との指摘があった。
元の`segmented.wav`は無音を挿入しない比較素材だったため、参照音声を変更せず、
同じ2区間のPCMの境界だけに無音を加えた比較版を用意した。

- [0.2秒の間](artifacts/miori-voice-330-2026-09-13/segmented-gap-200ms.wav)
- [0.3秒の間](artifacts/miori-voice-330-2026-09-13/segmented-gap-300ms.wav)

元の区間波形は変更せず、48 kHzで9,600／14,400 framesのゼロPCMを挿入したことを検証した。
これは#330の試聴素材であり、実会話の出力処理を修正したものではない。
追加比較でも、ユーザーから間に長すぎる部分と短い部分があり、一括出力でよいのではないかとの
指摘があった。固定の0.2秒・0.3秒追加は採用せず、比較版は未採用の検証記録として残す。
元音声の先頭・末尾にも間が含まれるため、挿入する無音の長さだけでは境界の間を揃えられない。

採用参照WAVは全文を1回で生成した14.12秒の原音であり、これらの連結版とは別の素材である。
参照作成のために分割・連結する必要はなく、選定済み原音をそのまま引き渡す。
公式Serverは`irodori.ref_wavs`や`voices.json`のaliasによる複数参照にも対応しており、
1ファイルへの連結は必須条件ではない。

分割比較は#329の逐次再生を見越した追加検証として扱う。#329では句点による区切りと文中の区切り、
元音声の末尾・先頭に含まれる間、連続区間の自然さ、遅延を実際の逐次再生で確認する。
本変更で実会話の合成を全文一括待ちに切り替えたり、固定の無音追加を実装したりはしない。

## 未確認事項・引き渡し

| 項目 | 状態 |
|---|---|
| ユーザーによる元音声の選定 | 完了 |
| 参照WAV・生成条件・voice ID・登録手順の保存 | 完了 |
| devの参照合成・全文／区間WAV生成 | 実APIで確認済み |
| 再合成WAVの声の一貫性・自然さ・読み | 分割境界の間の長短にユーザー指摘あり。固定無音追加は未採用、調整は#329。全文再合成の個別受入は未確認 |
| 公式Gradio UI | 未構築。実際の選定はServer APIで実施 |
| 共有推論サービスへの配備・再起動確認 | #329で対応 |
| CCV schema・設定・TTS接続 | ユーザー指示により#329へ後置 |
| VOICEVOX比較・実会話・遅延・モデル最終判断 | #329で対応 |

参照合成の試聴では、全文と分割連結を聴き比べ、選んだ声からの変化、文間の不自然さ、
日本語の読みを確認する。WAVが非無音であることだけで、これらを完了にしない。
公式モデル資料では約30秒以上の参照音声が推奨されているが、
今回はユーザーが採用した14.12秒の原本を優先して保存した。延長や別音声への差し替えは
ユーザーが試聴して判断する。

- [公式モデル資料](https://huggingface.co/Aratako/Irodori-TTS-v4.1-Small)
- [公式ServerのVoice Design・音声登録API](https://github.com/Aratako/Irodori-TTS-Server)

## リポジトリ側の確認範囲

音声のhash・形式・非無音、参照metadataとWAVの整合、文書内リンク、登録手順の同一WAV再利用、
既存の別WAVを上書きしないことを確認した。
文書に掲載した登録・参照合成コマンドもdevの実APIで実行し、WAV出力を確認した。
アプリケーションコードやruntime設定を変更しないため、ローカルのBackend／Frontend全テストは
実行対象にしていない。CI結果はPR側で別途確認する。
