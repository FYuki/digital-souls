# 共通Irodori推論サービス

#329の共通GPU推論サービス。Ubuntu-dogfood側のサービス管理者が所有し、
dev/testはloopback HTTP `http://127.0.0.1:50024`で利用する。
この実装資材の追加だけでは、共有サービスの配備・実会話受入が済んだことにはならない。

## 実行境界

作者のIrodori-TTS-Server（revision `841fb7c`）の通常API関数を、
単一のGPU worker processから再利用する。重みとcodecは#330のmetadataと同じrevisionを取得し、
CUDA/BF16・非量子化で使用する。モデル・codecのキャッシュは会話data rootから独立させる。

外向きのserviceは単一workerと上限付き待機キューを所有する。
待機中はdogfoodを優先し、実行中の合成を優先順位だけで止めない。
切断した待機要求は削除し、実行済み要求は結果を破棄しつつworker終了まで枠を保持する。
推論timeout・worker異常時は自分が所有するprocessを停止し、準備合成から再生成する。
準備・推論の期限にはworkerからの返信全体の受信を含め、返信途中でprocessが停止しても枠を保持し続けない。
再準備失敗時はnot readyとし、管理者がサービスを再起動する。別TTSへ自動切り替えしない。

## 準備と音声登録

`env.example`を参考に所有者が永続保存先を用意する。
採用WAVとmetadataは[光織の引き渡し](../../characters/miori/voice.md)を使用する。
リポジトリルートで次を実行する。音声登録はGPUを使わない。

```bash
PYTHONPATH=. backend/.venv/bin/python -m irodori_service.voices \
  --audio characters/miori/assets/voice/miori-b3-4221.wav \
  --metadata characters/miori/assets/voice/miori-b3-4221.json \
  --directory /var/lib/digital-souls/irodori/voices
```

WAVのhashと形式を確認し、同一の登録は再利用する。
異なる音声や設定による同じIDの上書きは拒否する。
serviceは音声volumeをread-onlyでmountし、HTTPによる上書き・削除・任意pathの参照を提供しない。
本登録用JSONはservice内部のmanifestであり、CCV schemaではない。

## イメージとdogfoodの管理単位

通常配布は `.github/workflows/container-images.yml` のIrodoriイメージを使用し、
`DOGFOOD_IRODORI_IMAGE` に検証対象のimmutable digestを指定する。
ローカルの初回buildはリポジトリルートから次を実行する。

```bash
docker build -f irodori_service/Dockerfile -t digital-souls/irodori:dev .
docker image inspect --format '{{.Id}}' digital-souls/irodori:dev
```

同じserver/code revisionを検証済みのローカルimageを再利用する場合のみ、
`--build-arg IRODORI_UPSTREAM_IMAGE=<検証済みimage>` を指定できる。
固定model/codec revisionのcacheは既存のものをコピーして再利用できる。元cacheは削除しない。
containerはUID/GID `10001:10001`で動作する。所有者が専用cache directoryをこのUID/GIDの
所有にし、コピー済みのcacheも書込み可能な所有権へ揃える。元cacheや会話data rootは対象にしない。
参照音声は全利用者が読めるdirectory・read-onlyファイルとして配置し、書込み権限を付けない。

`DOGFOOD_IRODORI_IMAGE`は必須で、`repository@sha256:<64桁>`を使う。
ローカル検証でloadしたものは内容固定の`sha256:<64桁>` image IDも許可する。
`:dev`等の可変tagは起動前検証で拒否する。`env.example`は誤配備を防ぐため空欄で、
コメントの形式例は対象commitに置き換える。

dogfood所有者はComposeを `/opt/digital-souls-irodori/compose.yaml`、
envを `/etc/digital-souls/irodori.env` へ配置する。
既存envは上書きせず、音声・cache directoryはリポジトリと会話data rootの外に置く。
ローカルbuildを使う場合は、上のinspectが出力した内容固定のimage ID
（sha256:に続く64桁）を、このenvファイルのDOGFOOD_IRODORI_IMAGEへ設定する。
可変tagのdigital-souls/irodori:devを設定値にしない。
この設定を終えてから、下記の起動前検証とsystemd／手動起動を実行する。
[起動前検証](validate-deployment.py)も `/opt/digital-souls-irodori/` へ配置する。
[systemd unit](digital-souls-irodori.service)を `/etc/systemd/system/` へ配置し、
`systemctl daemon-reload` 後に `systemctl enable --now digital-souls-irodori.service` を実行する。
これはdogfood側だけの導入操作であり、devのEnvironment CLIやテストfixtureから実行しない。
GPU空き不足などで起動できない場合は未配備・未準備として記録し、実合成成功までreadyと扱わない。

## 起動・停止とreadiness

所有者が設定したenvファイルを指定して実行する。

```bash
python3 /opt/digital-souls-irodori/validate-deployment.py \
  --env-file /etc/digital-souls/irodori.env --compose-file /opt/digital-souls-irodori/compose.yaml
docker compose --env-file /etc/digital-souls/irodori.env \
  -f /opt/digital-souls-irodori/compose.yaml up -d --wait --wait-timeout 600
curl --fail http://127.0.0.1:50024/health/ready
curl --fail http://127.0.0.1:50024/version
```

`/health/live`は生存確認、`/health/ready`はモデル読み込み・固定音声による実合成成功を要求する。
`/version`にはrevision、初回準備時間、件数、上限だけを返し、会話本文・音声・例外詳細を含めない。
起動時の準備時間を通常TTFAに混在させない。health成功は音質・実会話受入の証拠ではない。

停止は同じ所有者が同じcompose/envで `stop` を実行する。
dev/testのcleanupへ登録しない。volume、元WAV、既存モデルcacheを削除しない。
`docker compose down -v`や別Irodori実験の資産削除は不要。

## API

`GET /v1/audio/voices`は登録済みIDを返す。
`POST /v1/audio/speech`へ#330の `reference_synthesis_request` を渡す。
区間合成では `irodori.chunking_enabled=false` を指定し、
`X-DS-Environment: dev` / `test` / `dogfood` をBackendの実行環境から設定する。
このheaderは公開クライアント用の認証方式ではなく、loopback上の信頼するBackend間の区分である。
同一ホストの悪意ある利用者間を隔離する境界は提供せず、ブラウザの指定headerを転送しない。
LAN公開や非信頼clientの受付を行う場合は、この配備をそのまま流用しない。

成功は48 kHz・mono・PCM16 WAV。
不正設定は422、参照音声欠落は404、既存音声の変更検出は409、
待機上限超過は429、準備未完了は503、待機／推論timeoutは504で、
`error.code`から原因を識別する。入力本文をエラーレスポンスへ含めない。

## BackendとCCVの接続

正式なLiveKit経路で `data.extensions.digital_souls.tts_config` を次の形式にする。
`caption`は[選定metadata](../../characters/miori/assets/voice/miori-b3-4221.json)の
`reference_synthesis_request.irodori.caption`を省略せず使用する。
`generation_request`はVoice Design時の記録であり、会話中の参照合成には使用しない。

```json
{
  "engine": "irodori",
  "voice_id": "miori-b3-4221",
  "caption": "選定metadataのreference_synthesis_request.irodori.captionを設定",
  "seed": 4221,
  "speed": 1.0,
  "num_steps": 40
}
```

Backend設定は `IRODORI_BASE_URL=http://127.0.0.1:50024`、
`IRODORI_REQUEST_TIMEOUT_SECONDS=45`、
`IRODORI_READINESS_TIMEOUT_SECONDS=5`。
dogfoodの非loopback接続はHTTPSを要求し、HTTPはloopbackだけに制限する。redirectは追跡しない。
50023は既存のWhisper PCM計測Profileで使うため、共有TTSには50024を割り当てる。
`DS_ENVIRONMENT_ID`はBackendの実環境IDを要求headerへ送る。利用者入力から優先度を選ばせない。
`integration-irodori` Profileは外部Irodoriを検査するが、起動・停止・GPUモデル管理は行わない。
計測用ProfileはFrontend 18573、Backend 18500、ready gate 18574を使い、既存devと分離する。
Irodoriを必須としない既存ProfileでもCCVで選んだSessionには準備確認が適用される。

URL・timeout・pathをCCVに入れない。未知engine、不正voice ID、参照音声欠落を失敗として扱う。
会話開始APIは準備失敗をHTTP 503と原因code（`tts_config_missing`、`tts_config_invalid`、
`tts_engine_unsupported`、`tts_not_ready`、`tts_voice_missing`）で返す。
設定本文を応答へ含めず、失敗した予約を解放して再試行を許可する。
Session作成前にreadyと登録音声を確認し、既存Session／一時再接続で設定を読み直さない。
VOICEVOXへ戻す場合は従来の `{"engine":"voicevox","speaker_id":14}` を設定して新しいSessionを開始する。
旧WebSocket baselineではIrodori設定を明示エラーにし、自動fallbackしない。
出荷済み光織CCVの既定値はVOICEVOXのままとし、Irodoriの本採用判断と区別する。

## 検証状態

参照音声の検証・一覧取得は専用executorで同時2件（`DS_IRODORI_MAX_VOICE_CHECKS`）に制限し、
超過は429とする。HTTP取消後も実ファイル検証が終わるまで枠を保持し、推論・再準備を妨げない。

サービスの単体テストでは、待機dogfood優先、実行中要求の非preempt、待機上限・timeout、
取消後の枠保持、worker processの強制停止・再準備、参照IDの上書き拒否を検証する。
Backendテストでは選定metadataの送信、WAV検証、HTTP取消、準備前のSession作成拒否、
Session中／再接続時の設定固定を検証する。これらは実GPU・ブラウザの受入証跡ではない。

実GPUではモデル準備時間、区間合成、再起動・取消の影響、共有負荷を測り、
実Backend/STT/LLM/LiveKit/ブラウザによるTTFA p95 2000ms以下と試聴を別途検証する。
#329と#330はこの受入およびユーザーのモデル判断を終えて同時に閉じる。


## GPU空き待ちの間に用意する計測条件

実計測は共有サービスの準備合成成功後に行う。Image2PSD等の別作業を停止してGPUを奪わない。
計測用worktreeに、選定metadataから作成したIrodori CCVを独立したfixture commitとして保存する。
出荷CCV、dogfoodの会話data root、選定WAV原本を変更しない。
計測runnerはclean commitを要求し、実行ごとのdata rootとイメージtagを分離する。

`scripts/voice_quality/run_pilot.py --profile integration-irodori`で専用Profileを選ぶ。
`--inference-env`には実LLM/STTと`IRODORI_*`の接続設定を、
`--livekit-env`には対象LiveKitの設定を明示する。
最初は`--scheduled-fixture --trials 3 --run-id <独立したID>`で実経路を診断し、
問題解消後に同じ条件の`--controlled`（準備5回＋独立100試行）を実行する。
このProfile選択は声やengineを変更しない。Irodori設定のfixture commitとVOICEVOX設定の
fixture commitを同じProfile・入力・LLM条件で比較し、CCVと実行revisionを記録する。
PCM観測用／障害bridge用Profileとの同時指定は拒否する。

冷状態のモデル準備時間は通常TTFAから分離する。p50/p95、件数、失敗・欠測、
GPU共有条件、応答区間の音質・間合いを記録し、実ブラウザ再生開始まで測る。
GPUなしのテスト成功、image build、cache確認を実会話・音質・遅延の受入として扱わない。
