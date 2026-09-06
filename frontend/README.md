# frontend

digital-souls の自作フロントエンド（Vite + Svelte + TypeScript）。

- テキスト／LiveKit音声チャットUI
- キャラクター別のスレッド一覧、名称変更、アーカイブ、復元、削除
- キャラクター／スレッドのピン留めとキャラクターの追加・非表示
- PCの左サイドバーと、タブレット・モバイルのドロワー
- キャラクター立ち絵の右配置／履歴背面配置と共通プレースホルダー
- Chrome／Edge標準pickerを使う単一monitor／windowの画面共有と、会話に応じたon-demand参照

キャラクター切り替えは常設セレクターではなく、対象キャラクターのスレッド選択または
キャラクターブロック内の新規スレッド作成によって行う。設定ではサーバーが検出した
キャラクターだけを、表示名とキャラクターIDを併記したプルダウンから一覧へ追加できる。

PCでは立ち絵を会話履歴の右側または背面へ配置できる。タブレット・モバイルでは背面配置に
固定し、履歴範囲を下部50%、75%、100%から選ぶ。設定はBackend SQLiteへ即時保存し、
モバイルではVisual Viewportへ追従してソフトウェアキーボードを除く表示領域に入力欄を保つ。

Epic #151の画面確認手順は
[`docs/epic-151-acceptance.md`](../docs/epic-151-acceptance.md)を参照する。

画面共有は初期OFFで、再読込後に自動復元しない。ON中も画像を定期送信せず、テキスト／LiveKitの
発言をBackendが`inspect_screen`と判断した場合、または入力欄の「現在の画面を参照」を選んだ場合に
だけ新しい静止画を1枚送る。同じ有効sessionでは質問ごとのpicker操作は不要である。OFF、対象・会話・
character変更、track終了、Backend切断で旧generationを失効させる。対象名とpreviewはローカルUIだけで
扱い、browser tabや要求種別と異なるsurfaceは送信前に拒否する。

Windows実機の確認手順とmetadata-only証跡は
[`docs/screen-perception-browser-acceptance.md`](../docs/screen-perception-browser-acceptance.md)を参照する。
