# 光織の採用音声

[Issue #330](https://github.com/FYuki/digital-souls/issues/330)で、ユーザーがB-3のCaption、
seed **4221**、話速 **1.00**を選定した。声の方向は「中性的な雰囲気を持つ女性」で、
透明感、薄い息感、繊細さ、控えめな抑揚を基準とする。

本書は音声資産の引き渡し資料であり、runtimeの設定定義ではない。
CCV schema、TTSクライアント、共有推論サービスへの配備は
[Issue #329](https://github.com/FYuki/digital-souls/issues/329)で扱う。
現在の[miori.card.json](miori.card.json)のVOICEVOX設定は変更していない。

## 採用資産

| 項目 | 値 |
|---|---|
| 参照音声 | [miori-b3-4221.wav](assets/voice/miori-b3-4221.wav) |
| 生成・受け渡し記録 | [miori-b3-4221.json](assets/voice/miori-b3-4221.json) |
| 安定したvoice ID | `miori-b3-4221` |
| モデル | `Aratako/Irodori-TTS-v4.1-Small`、非量子化、CUDA/BF16 |
| 元音声の生成条件 | B-3のCaption、seed `4221`、speed `1.00`、40 steps、参照音声なし |
| WAV形式 | 14.12秒、48,000 Hz、mono、16-bit PCM、1,355,564 bytes |
| SHA256 | `f001f7df5415e74b955a8e15f9c2955cefcbc19b3e2282150fc67f0a9dcc2b79` |

採用WAVはユーザーが試聴した元音声の完全コピーで、再生成、速度加工、音量補正はしていない。
全文を1回で生成した原音であり、分割音声の連結ではない。
Irodori-TTS-Serverは複数参照（`irodori.ref_wavs`や`voices.json`のalias）にも対応するため、
参照素材を1ファイルへ連結する必要はない。今回は選定済みの単独WAVをそのまま引き渡す。
Captionの正確な文字列、読み上げ文章、モデル／codec／コードrevisionはJSONの記録を参照する。
このJSONをCCVへそのまま転記・読み込みすることは想定しない。

声の高さ・息感・抑揚はCaptionで指示し、話速はAPIの`speed`で指定する。
seed番号の近さは声質の近さを意味しない。別文章ではseedだけで同じ声を保証できないため、
今後の合成には採用WAVを参照する`voice: "miori-b3-4221"`を指定する。
音声資産の更新が必要になった場合は別ファイル・別voice IDで比較し、このWAVを上書きしない。

## 登録手順

推論サービス管理者が、Irodori-TTS-Serverから見える`voices`ディレクトリへWAVを配置する。
Dockerではコンテナ内の一時領域ではなく、`IRODORI_VOICES_DIR`に対応する永続volume／bind mountの
ホスト側ディレクトリを使う。ファイルstemがvoice IDになる。

以下はリポジトリルートから実行する。devで確認した保存先を例にしている。
#329の共有サービスへ渡す際は、同サービスの管理者が保存先を指定して実行する。

```bash
export IRODORI_VOICES_DIR=/home/asa/dev/irodori-tts-server/voices
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

assets = Path("characters/miori/assets/voice")
record = json.loads((assets / "miori-b3-4221.json").read_text())
source = assets / record["file"]
expected = record["audio"]["sha256"]
assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, "採用WAVのhash不一致"
directory = Path(os.environ["IRODORI_VOICES_DIR"]).expanduser().resolve()
assert directory.is_dir(), "サービス管理者が参照音声の保存先を準備してください"
target = directory / record["file"]
if target.exists():
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected, "同名の別音声があります"
else:
    with target.open("xb") as output:
        output.write(source.read_bytes())
print(f"参照音声: {target} / voice ID: {record['voice_id']}")
PY
```

同じWAVの再登録はそのまま再利用し、同名の別音声は上書きしない。
ファイルの配置だけでは推論成功の確認にならないため、登録一覧と実合成も確認する。

```bash
# devのAPI。共有サービスではその接続先と必要な認証を管理者が指定する。
export IRODORI_BASE_URL=http://127.0.0.1:8088
curl --fail --silent --show-error "$IRODORI_BASE_URL/v1/audio/voices"
```

返却一覧に`id: "miori-b3-4221"`、`no_ref: false`、該当WAVの`ref_wav`があることを確認する。
次の例は、記録された参照合成リクエストをそのまま使い、別文章を読み上げる。

```bash
python3 - <<'PY'
import json
import os
import urllib.request
import tempfile
from pathlib import Path

record = json.loads(Path("characters/miori/assets/voice/miori-b3-4221.json").read_text())
payload = record["reference_synthesis_request"]
# 別の文章を試すときはpayload["input"]だけ変更する。
request = urllib.request.Request(
    os.environ["IRODORI_BASE_URL"].rstrip("/") + "/v1/audio/speech",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=1800) as response:
    audio = response.read()
with tempfile.NamedTemporaryFile(prefix="miori-reference-check-", suffix=".wav", delete=False) as output:
    output.write(audio)
    print(output.name)
PY
```

`seed=4221`、`speed=1.00`、Caption、40 stepsを参照合成でも初期値とする。
区間ごとに呼ぶ場合はIrodori側の自動分割を無効にする方針を#329の接続実装で適用する。
サーバーURLや認証は環境側で管理し、音声資産やCCVに埋め込まない。

## 引き渡し・検証状況

[選定と実API検証の記録](../../docs/miori-voice-selection-2026-09-13.md)を参照する。

- 採用判断、元音声、生成条件、voice IDは保存済み。
- devの単独Irodoriへ登録し、別文章と2区間の実合成を確認済み。
- 再合成の試聴では分割境界の間が不自然との指摘があり、固定の0.2秒・0.3秒追加でも長短のばらつきが指摘された。固定の無音追加は採用しない。
- 連結版は逐次再生を見越した追加検証素材であり、採用参照音声には使わない。分割再生の間の調整は#329へ引き継ぐ。全文の参照再合成に対する個別の受入判断は未確認。
- #329の共有サービスへの登録、再起動後の利用確認、CCV接続、実会話・遅延評価は未実施。

声の選定と、実会話での遅延測定後に行うモデルの最終採用判断を分ける。
`/health`の成功やWAVの非無音確認を、音質・実会話の受入に読み替えない。
