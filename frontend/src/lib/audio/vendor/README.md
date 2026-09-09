# 短音声補助VADの配布物

`libfvad.wasm`は[libfvad-wasm](https://github.com/OzymandiasTheGreat/libfvad-wasm/tree/2692578b7f2af573e3a6efb84e4356b41250d414)の`lib/embedded.js`から抽出した変更なしのWASMである。WebRTC由来のVADを16kHz・mode 0で利用する。JavaScript wrapperは本プロジェクトで実装し、原packageのJavaScriptは同梱しない。

- 元commit: `2692578b7f2af573e3a6efb84e4356b41250d414`
- 元`embedded.js` SHA-256: `7dd307528d53cf2fb81495099f4d3de94e6a7c9de582617976cfcefe0428ca76`
- WASM: 15,331 bytes、SHA-256 `3fadafc9c5c1c3117d0178ae631484c6dabc72e0f8742b95c370cd39f46cf171`
- ライセンス本文: [LICENSE.libfvad](LICENSE.libfvad)。元repositoryに含まれるWebRTC・Daniel PirchのBSD条項と著作権表示を保持する。

更新時は元commitとライセンス、抽出後hashを確認し、音声・非発声音・開始位置の比較とブラウザ実接続を再検証する。
