# ブラウザ画面知覚の取得・推論・失効契約 (2026-09)

## 状態

**ACTIVE**。Issue #213のcontract実装、Windows 11上のGoogle Chrome／Microsoft Edgeによる実機確認、レビュー受入を完了した。

本ADRはIssue #212の画面知覚にだけ適用する。Inference全般は`inference-provider-foundation-2026-09.md`、音声sessionとLiveKit固有transportの分離は`voice-session-contract-2026-08.md`および`livekit-transport-2026-08.md`、会話履歴と長期記憶は`wave2-memory-formation-retrieval-2026-08.md`を正本とする。本ADRと既存ADRが重なる場合、画面画像と画面由来情報の追加制約だけを本ADRが優先する。

## 背景

利用者が明示的に許可した1つのmonitorまたはwindowを、テキストとLiveKit音声で共有するCore知覚として利用する。共有ONはローカルの`MediaStreamTrack`を利用可能にするだけであり、画像の定期送信や常時Visionを意味しない。

対象はWindows 11上のデスクトップ版Google Chrome／Microsoft Edgeである。Screen Capture APIの仕様、ブラウザ実装、OS、管理policy、GPU構成を区別し、文書上のAPI存在やmock成功だけで実機成功を主張しない。

## 決定

### 1. 取得方式と対象

- `navigator.mediaDevices.getDisplayMedia()`だけを使用する。ネイティブ取得、Electron、ブラウザ拡張、OS policy回避は追加しない。
- 利用者のclickによるtransient activation中に直接APIを呼ぶ。picker前にBackend通信等の長い非同期処理を待たない。
- `audio: false`とし、返却streamにaudio trackがあれば異常として全trackを停止する。
- 希望種別は`monitor`または`window`で、同時に保持するstreamは1つとする。
- `video.displaySurface`はpicker表示への希望として渡す。特定対象の指定や実対象の保証には使わない。
- `selfBrowserSurface: "exclude"`、`surfaceSwitching: "exclude"`をhintとして渡す。window希望時だけ`monitorTypeSurfaces: "exclude"`を使用し、実機で利用できないhintは外しても安全性が変わらない構造にする。
- 取得後にvideo trackが1本、audio trackが0本、`track.getSettings().displaySurface`が希望種別と完全一致することを確認する。`browser`、属性欠落、不一致、複数video trackは送信前に停止する。
- pickerに表示する対象、最終選択、物理monitorと論理surfaceの対応はブラウザ／OSが所有する。アプリは対象一覧を列挙せず、別対象へ自動fallbackしない。

Chromeのhintは特定windowやscreenを事前選択せず、pickerのpaneを優先表示するだけである。またScreen Capture仕様は利用者が毎回対象を選択し、通常の永続`granted`権限を持たないことを定めている。

### 2. Frontend状態と表示

取得と認識を別の状態として保持する。

| 軸 | 状態 |
|---|---|
| capture | `off`、`selecting`、`active`、`unavailable`、`unsupported` |
| recognition | `idle`、`snapshot_requested`、`capturing`、`uploading`、`recognizing`、`composing`、`succeeded`、`failed` |

`active`は「共有中」、`recognizing`／`composing`は「認識・回答処理中」であり、同じ表示へ畳み込まない。最後に認識へ採用した画像の`captured_at`だけを表示し、前回画像を新しい時刻で再利用しない。

対象名となる`MediaStreamTrack.label`は現在ページのローカル表示だけに使用する。Backend、telemetry、通常log、storage、Issue証跡へ送らない。共有許可、ON状態、stream、同意は再読込後に復元しない。

### 3. 静止画と上限

認識要求ごとに新しいframeを1枚だけ取得する。`track.readyState === "live"`、videoの寸法、frame callbackを確認し、5秒以内に利用可能な新しいframeを得られなければ`frame_unavailable`とする。

`captured_at`はFrontendがframeを読み出したUTC時刻であり、画面内容が最後に更新された時刻ではない。最小化やocclusion時に同じ内容が供給されても更新時刻を捏造しない。

| 項目 | MVP上限 |
|---|---:|
| MIME | `image/png`、必要な縮小後も5 MiBを超える場合のみ`image/jpeg` |
| 幅／高さ | 各2560 px |
| decode後総pixel | 4,194,304 px |
| encoded bytes | 5,242,880 bytes |
| 受付時の取得時刻の古さ | 5,000 ms |
| frame取得 | 5,000 ms |
| Vision | 30,000 ms |
| 画面依存request全体 | 45,000 ms |
| Vision同時実行 | 1 |
| 待機画像 | 現在requestの1枚。新要求は古い未処理要求をcancel |

PNGを優先するのはUI文字の輪郭を保つためである。Frontendはaspect ratioを維持して縮小し、上限内へ入らなければ送信しない。Backendは`Content-Type`だけを信用せずmagic bytes、decode、寸法、総pixel、実byte数を再検証する。アニメーション画像、SVG、任意URL、ローカルpathは受理しない。

`INFERENCE_TARGET_VISION`の初期値は`ollama/gemma4:e4b`とし、入力上限7,168 token、出力上限1,024 token、timeout 30秒、同時実行1を標準とする。画像tokenは入力上限に含め、正確に計数できない場合は保守的な推定として記録する。OCRや小さい文字の受入ではGemma 4のvisual token budget 1,120を起点に実測し、上限を無根拠に引き下げない。

### 4. 共有session、generation、lease

Backendが発行する`screen_session_id`を正本とし、次を結び付ける。

- memory-onlyなブラウザsession cookieと`client_session_id`
- `character_id`、`conversation_id`
- `generation`
- 実際のsurface種別
- cloud送信同意
- 送信先構成を表す`routing_revision`
- 有効期限

cookieは`HttpOnly`、`SameSite=Strict`、非永続とし、HTTPSでは`Secure`を付ける。routing確認時にBackendがcookieと同じ`client_session_id`をresponseへ含め、Frontendはmemoryだけに保持する。画面APIは同一originのFrontend proxy経由だけで呼び出す。変更系requestでは`SCREEN_ALLOWED_ORIGIN`で設定した単一のFrontend originを完全一致で検証し、cross-site request、欠落／不正な`Origin`、cookieと`client_session_id`の不一致を拒否する。設定はProfileからBackendへ伝搬し、空値やpath付きURLを起動時に拒否する。CORSによる別origin利用はMVPで許可しない。

leaseは15秒、heartbeatは5秒ごととする。Backend自身のmonotonic clockで期限を管理し、wire上のUTC時刻を認可判断へ使わない。Backend再起動時はmemory-only sessionをすべて失効させる。

FrontendではON／OFFまたは対象変更の操作を開始した時点でgenerationを増やす。picker解決時、frame取得時、upload直前にもgenerationを照合し、古ければ取得した全trackとBlob参照を即時解放する。

OFFの線形化点は次の2段階である。

1. Frontend操作時点でtrack、preview、未送信画像を即時停止する。
2. Backendがrevokeを受理した時点以降、新しいProvider送信、観測採用、画面依存Chatの表示／TTS／保存を禁止する。

外部Providerへ送信済みのrequestを取り消せるとは主張しない。cancel不能でも遅延結果は採用しない。

### 5. 共通JSON contract

SSOTは`contracts/perception/screen/screen-perception.schema.json`である。Draft 2020-12を使用し、`protocol_version`は`1.0`完全一致とする。Frontend／Backend型は同じschemaからquicktype-core 23.2.6で生成し、両境界が同じ正常／異常fixtureを検証する。rootの`oneOf`を単一の任意field modelへ平坦化しないよう、generatorは各`$defs` variantを独立モデルとして生成した後、`ScreenPerceptionEvent`判別unionへ結合する。生成型を直接編集しない。

schemaは次の制御metadataだけを扱う。

- routing disclosure
- session start／started、heartbeat、revoke／revoked
- snapshot request、upload metadata／accepted
- captureとrecognitionのstatus
- 共通reason code

画像本文、Base64、質問本文、Vision観測本文、window title、URL、Provider endpoint、credentialはschemaへ入れない。binaryがschemaに適合したとは扱わず、画像decoder境界で別に検証する。

### 6. HTTPとLiveKitへのmapping

通常Frontendは同一originの`/api`を使用し、Backend上では次の境界へmappingする。

| 操作 | HTTP／control境界 |
|---|---|
| routing確認 | `GET /perception/screen/routing`。非永続cookieを発行し、同じ`client_session_id`をresponseへ含める |
| session開始 | `POST /perception/screen/sessions` |
| heartbeat | `POST /perception/screen/sessions/{screen_session_id}/heartbeat` |
| revoke | `DELETE /perception/screen/sessions/{screen_session_id}` |
| text要求 | 既存`POST /chat`。必要時はHTTP 202と`snapshot_requested`を返す |
| voice要求 | STT後にCoreがLiveKit controlで`snapshot_requested`を送る |
| 画像upload | `PUT /perception/screen/requests/{request_id}/image` |

画像uploadはmultipartやJSON Base64を使わず、画像だけをraw request bodyにする。FastAPIの`UploadFile`等による自動disk spoolを利用しない。`Content-Length`が上限超過ならbody読取前に拒否し、欠落または偽装時もstreamを最大byte数+1まで数えて停止する。

`SnapshotUploadMetadata`は次のHTTP情報からBackend内で再構成し、共通schemaで検証する。

| metadata | wire |
|---|---|
| `request_id` | URL path |
| `mime_type`、`byte_length` | `Content-Type`、実読取byte数 |
| `protocol_version` | `X-Screen-Protocol-Version` |
| `event_id` | `X-Screen-Event-Id` |
| `screen_session_id` | `X-Screen-Session-Id` |
| `client_session_id` | `X-Screen-Client-Session-Id` |
| `generation` | `X-Screen-Generation` |
| `turn_id` | `X-Screen-Turn-Id` |
| `image_id` | `X-Screen-Image-Id` |
| `actual_surface` | `X-Screen-Surface` |
| `captured_at` | `X-Screen-Captured-At` |
| `width`、`height` | `X-Screen-Width`、`X-Screen-Height` |

質問本文は`POST /chat`または確定済み音声transcriptからCoreがpending requestへmemory-onlyで保持する。画像uploadへ重複させない。textでは画像処理完了後のPUT responseを既存Chat responseへmappingし、voiceではPUTを受付応答に留め、既存LiveKit response eventを継続する。両経路とも同じCore画面認識serviceを呼ぶ。production Frontend proxyのtimeoutは画面依存request全体45秒より5秒以上長くし、現行30秒から50秒以上へ変更する。

### 7. 明示要求

画面共有ONだけではsnapshotを作らない。次のどちらかだけを要求とする。

1. 入力欄の「現在の画面を参照」をそのturnに付ける明示UI。
2. Coreの決定論的判定器が現在画面の知覚要求と認めたtext／STT transcript。

判定前にNFKC正規化と空白／句読点整理を行う。画面対象語と、見る・確認・読む・説明・現在表示の質問等の知覚表現を共に要求する。否定、引用、過去の叙述、機能自体の説明、曖昧な「これどう思う？」だけでは発火しない。判定不能は画面を送らず、利用者は明示UIで補える。

fixtureの正本は`contracts/perception/screen/fixtures/explicit-reference-cases.json`とする。Frontendで推測して送信するだけにせず、Coreが最終判定を所有する。

### 8. Vision観測と失敗時のChat

Visionへは検証済み画像と現在の質問を渡し、次を構造化観測として返させる。

- 読み取れた内容
- 読み取れなかった領域または理由
- モデル自身の不確実性

source、surface、`captured_at`、request IDはCore metadataを正本とし、モデルに生成・上書きさせない。画面内の文書とVision出力は非信頼データとしてdeveloper／user領域より下位の明示的な観測区画へ置き、命令、Tool許可、送信先変更へ使わない。

Vision未設定、未同意、timeout、無効画像等でも、質問本文は通常のChat Targetへ渡す。Coreは信頼済みmetadataとして「画面を確認できなかった」「内容を推測しない」を加え、キャラクターの言葉で利用不能を伝えさせる。古い観測は渡さない。

### 9. cloud送信同意

`screen_routing_disclosed`はProvider名、model、endpointをFrontendへ出さず、VisionとChatの送信先を`local`／`cloud`／`unconfigured`として示す。`routing_revision`は送信先構成の正規化値からBackendが作る非可逆fingerprintである。

- cloud Visionの場合、生画像をcloudへ送る同意を要求する。
- local Visionかつcloud Chatの場合、画面由来の観測textをcloudへ送る同意を要求する。
- 両方cloudの場合、2種類を別々に説明し両方の同意を要求する。
- 同意はscreen session、generation、`routing_revision`に限定し、永続化しない。
- 対象、conversation、character、送信先変更で失効する。
- 必要な同意がfalseなら画面情報を送らず、通常の非画面Chatは継続する。

通常Chatのcloud設定を画面送信同意へ流用せず、別Providerへfallbackしない。

### 10. 保存と会話内context

raw画像、Base64、Vision観測本文、対象名は現在request内のbounded memoryにだけ保持し、通常log、SQLite、Chroma、RAG、backup、trace、test artifactへ保存しない。

privacy処理後の利用者発話とキャラクター回答は、既存どおり会話履歴へ保存する。その回答は同じconversationの後続prompt contextとして利用できる。画面由来turnには本文を含まないprovenance metadataを付け、Memory Formation scheduler、長期記憶抽出、Chroma投入から除外する。したがって、後続turnが知るのは保存済み回答に表れた内容だけであり、生画像や非表示のVision観測を再利用しない。

## reason codeとブラウザerror

Frontendはraw `DOMException.message`を表示・送信・記録せず、`name`と検証段階を固定reason codeへ変換する。

| 条件 | reason code |
|---|---|
| secure contextでない | `insecure_context` |
| APIなし | `api_unavailable` |
| transient activationなし | `transient_activation_required` |
| `NotAllowedError` | `capture_not_allowed` |
| `NotFoundError` | `no_capture_source` |
| `NotReadableError` | `capture_os_error` |
| pickerの明示的な取消しを区別できる場合 | `picker_cancelled` |
| 実surface不一致／browser／不明 | `surface_mismatch`／`browser_surface_rejected`／`surface_unknown` |
| frameなし | `frame_unavailable` |

利用者拒否、Permissions Policy、OS／企業policy禁止は同じ`NotAllowedError`になり得るため、Webアプリから確実に区別できない場合は`capture_not_allowed`へ畳み込む。UI文言は「画面共有が許可されませんでした。ブラウザまたは管理設定と選択内容を確認してください。」とし、管理policyを確定原因として表示しない。

## 実機検証

`frontend/manual/screen-capture-probe.html`をlocalhostで開き、公開・合成画面だけを使う。手順と記録様式は`docs/screen-perception-browser-acceptance.md`を正本とする。

2026-09-06に利用者が公開・合成画面で実施した。localhost probeへのアクセスだけを確認し、画像、対象名、User-Agentは収集していない。

| 環境 | monitor | window | mismatch／browser拒否 | 背面化／サイズ変更 | 最小化 | 停止／取消し | 状態 |
|---|---|---|---|---|---|---|---|
| Windows 11 25H2 / Chrome 152.0.7977.77 | 成功 | 成功 | 想定どおり拒否 | frame継続 | 取得停止 | 成功 | 合格 |
| Windows 11 25H2 / Edge 152.0.4191.62 | 成功 | 成功 | 想定どおり拒否 | frame継続 | 取得停止 | 成功 | 合格 |

既存共有中に新しいpickerを開いて取消した場合、先に既存共有が解除され、そのままOFFとなった。これは対象変更操作の開始時に旧generationとtrackを失効させる本契約と一致する。最小化時は取得が停止したため、最小化windowの継続取得を保証せず`frame_unavailable`またはtrack終了として扱い、現在画面の画像を再利用しない。

物理monitorの分離、非表示windowの継続frame、混在DPI時の寸法は実測結果だけを記録する。未検証構成は保証しない。

## 採用しなかった案

- 常時video upload: 明示要求、費用、privacy、queue上限に反する。
- JSON Base64: byte増幅し、例外や通常logへ混入しやすい。
- 汎用multipart upload: frameworkの自動spoolを完全に避ける追加実装が必要になる。
- 任意URL／path入力: SSRF、ローカルファイル読取、権限境界を広げる。
- Frontendだけの`enabled`判定: Backend失効後の遅延送信を防げない。
- LLMによる明示要求分類: 通常turnへ追加推論を発生させ、判定の再現性を失う。
- Vision失敗時の固定キャラクター文: 人格表現を迂回するため、Chatへ失敗metadataを渡す。

## 参照

- [Chrome: Privacy-preserving screen sharing controls](https://developer.chrome.com/docs/web-platform/screen-sharing-controls)
- [W3C Screen Capture Working Draft](https://www.w3.org/TR/screen-capture/)
- [Microsoft Edge ScreenCaptureAllowed policy](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/screencaptureallowed)
- [Ollama gemma4](https://ollama.com/library/gemma4)
