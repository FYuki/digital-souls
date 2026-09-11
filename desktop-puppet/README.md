# Desktop Puppet PoC

Tauri 2 + Svelte 4 で digital-souls のデスクトップ常駐キャラクターを検証するためのPoCです。

## このPoCで確認すること

- Tauri / Windows WebView2 での透過・枠なし・always-on-topウィンドウ
- Live2D Cubism Core for Web + PixiJS + pixi-live2d-display によるLive2D描画
- Live2D公式サンプル `Hiyori` の読込とIdleモーション開始
- 右クリックメニューからLive2D / 固定立ち絵の切替
- メニューからテキスト入力欄を開き、ユーザー/キャラクター合算の直近3発言を表示
- Tauri Rust IPCへのLive2D ready通知

## Live2D資産について

Cubism CoreはLive2D Proprietary Software Licenseの対象であり、このリポジトリには再配布しません。PoCではLive2D公式ホスティング版をネットワーク経由で読み込みます。

表示モデルはLive2D公式 `CubismWebSamples` の `Hiyori` を、公式GitHubのraw URLから直接読み込みます。サンプルモデルの利用条件はLive2DのFree Material Licenseおよび各サンプル利用条件に従います。

製品化時は、ユーザーが正規に取得したCubism Coreとdigital-souls用のLive2Dモデルをローカル配布物へ組み込む構成へ変更します。

## Windowsでの起動

前提:

- Node.js 20+
- Rust stable / Cargo
- Microsoft WebView2 Runtime
- Visual Studio Build Tools 2022（Desktop development with C++）

```powershell
cd desktop-puppet
npm install
npm run check
npm run tauri dev
```

ウィンドウ下部に `Live2D READY` と表示されれば、Cubism Core、モデル読込、PixiJS描画まで成功しています。

実行時には以下にも同じ結果を書き出します。

```text
%TEMP%\digital-souls-live2d-status.json
```

例:

```json
{
  "status": "ready",
  "detail": "Live2D ready: 520x760"
}
```

## 操作

- 上端の `⋮⋮` をドラッグ: ウィンドウ移動
- 右クリック: Puppetメニュー
- `テキスト入力を開く`: 小型チャットUIを表示
- `Live2D表示` / `固定立ち絵表示`: 描画方式を切替

テキスト会話は現時点ではローカル模擬応答です。BE接続時は既存Conversation Coreへ接続し、音声セッション中のテキスト入力ではVoice Session自体を終了せず、FE側で音声認識入力のみmuteする設計にします。

## CI

`.github/workflows/desktop-puppet-tauri-poc.yml` ではWindows runner上で以下を確認します。

1. Svelte/TypeScript check
2. Vite build
3. Rust `cargo check`
4. Tauri release build
5. Tauri実行ファイルを起動
6. WebView2内でLive2Dモデルを読み込み、Rust IPCへ `ready` を通知
7. status JSONが `ready` になることを確認

## PoC後の分離

MVPでは固定立ち絵を標準とし、Live2DはEnhancement扱いです。このPoCはTauri採用判断と将来Live2D追加時の技術リスクを先に潰す目的で残します。
