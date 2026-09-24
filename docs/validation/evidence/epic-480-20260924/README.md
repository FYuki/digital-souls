# Epic #480 の固定条件・実接続比較の証拠

2026-09-24 UTCの初期main `64a9d60c4d03eff73ec1f9708852fbb111bb5628` と同期後head `37ace8107c7fff53f1c9e4a29b6344b2fcb77956` を、3組各1回で直列実行した記録である。各試行は専用Backend・Frontend・LiveKitと空のtest data rootを使用した。Ubuntu開発環境、Python 3.12.3、Node 24.15.0、LiveKit server v1.9.7、Python SDK livekit 1.1.16 / livekit-api 1.2.0。共有Ollama/VOICEVOX/Whisperへ実接続したが、サービスとdogfoodのデータ・設定は変更していない。実マイクではなく固定speech-v2入力を使用した。

`baseline/pair-1`から`pair-3`と`updated/pair-1`から`pair-3`の各ディレクトリに以下を保存した。

- `trial-manifest.json`: 既存の音声品質試行が出力したfixture、初期状態hash、server/browserの観測時刻、結果。
- `controlled-trace.jsonl`: Backendのmetadata-only trace。timestampはserver monotonic ns。会話本文・波形を含まない。
- `backend-resources.jsonl`: Backendプロセスの0.5秒間隔のCPU tickとRSS page。共有推論サービスの負荷は含まない。

各試行の生成ID（session/utterance/response/conversation/event/character）は公開用コピーから除いた。採取時の生ファイルは現在の実行ホスト上の一時領域だけにあり、`source-checksums.json`に元ファイルと保存版それぞれのSHA-256を記録した。秘密鍵、LiveKit設定、DB、音声、会話原文、Backend/SFUログ、環境変数は保存していない。証拠から外部サービスの接続先や認証値を復元することはできない。

`run-supervisor.py`は当時実行したハーネスの写しであり、各試行のLiveKit鍵を乱数生成してtest所有資源を起動・停止する。元の実行形は `python3 /tmp/ds480-perf-supervisor.py <commit済み専用worktree>` で、初期main用と同期後head用のworktreeを交互に3回指定した。再実行前にはハーネスに固定されたloopback portの占有とプロセス・containerの所有を確認し、指定worktreeの`backend/.venv/bin/python`と`frontend/node_modules`を準備して、`node`をPATHから利用可能にする。ハーネスは別worktreeの仮想環境やホスト固有のNodeパスを自動流用しない。試験失敗時は所有資源を停止して結果を記録した後に非ゼロで終了し、一時LiveKit鍵ファイルを除去する。既存の共有サービスを停止しない。

保存したファイルのみから、リポジトリルートで `python3 docs/validation/evidence/epic-480-20260924/recalculate.py` を実行できる。スクリプトは各試行を[集計JSON](../../epic-480-paired-performance-20260924.json)と照合し、3回ずつの中央値を出す。本文開始は同一server clock上の `first_text_delta - utterance_finalized/vad_speech_end`、再生開始は同一browser clock上の `startedAt - sendAt/fixture_speech_end_client_ms`。CPU秒は末尾と先頭のtick差をclock tick数で除算し、使用率は観測経過秒で除算する。peak RSSは最大page数とpage byte数からMiBへ換算する。異なるclockを直接引かない。

6試行は診断比較であり、正式な100試行・統計的性能保証・共有GPU総負荷の測定ではない。各試行の受入制約と未解決項目は[統合受入れ記録](../../epic-480-acceptance-20260924.md)を参照。
