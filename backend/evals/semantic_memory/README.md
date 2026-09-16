# #341 意味記憶の固定性能ケース

2026-09-15にprompt調整前の基準ケースを固定する。実ユーザー会話は使用しない。
100ケースは直接知識、出来事除外、明示確認、再言及、時間変化、明示訂正、
固定属性矛盾、自己申告優先、根拠除外、安全境界の10分類×10件。
case ID、自然言語入力、既存知識fixture、期待する操作・根拠を固定する。
既存知識fixtureは前提であり、その形成の実LLM成功を示さない。

本ケースをpromptに合わせて緩めない。仕様上の誤りを直す場合は根拠・変更前後・commitを記録する。
安全ケースはcore privacy契約による保存拒否を含む本番経路で評価する。
他characterのfixtureは本番のcharacter分離によりpromptへ渡されないことも検査する。

e4b/12bの共通prompt比較、cacheなし各3回、各回各分類90%以上・禁止情報保存/character混入0件は
[実行指示書](../../../docs/epic-341-semantic-memory-requirements.md)に従う。
## 実行

専用のdev/test Ollamaへe4bと12bを準備し、リポジトリrootで実行する。

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/eval_semantic_memory.py --model gemma4:e4b --endpoint http://localhost:11438 --output-dir /tmp/semantic-e4b-final
PYTHONPATH=backend backend/.venv/bin/python scripts/eval_semantic_memory.py --model gemma4:12b --endpoint http://localhost:11438 --output-dir /tmp/semantic-12b-final
```

モデルごとに100ケース×3回を独立したpromptfoo実行へ渡す。privacyは共通e4bに固定する。
未使用Targetの設定はローカル値を補うが、別Providerへのfallbackは行わない。
既存の出力directoryを上書きしない。manifestへモデルdigest・量子化・設定・commit・各ファイルhashを、
各runへ推論token・時間・privacy判定・実保存結果・Ollama読み込みメモリを残す。

`--runs 1 --limit 4`等は動作確認・調整専用であり、正式合格には数えない。
採点は実保存件数・保存した操作・値・出典・旧状態を検査し、拒否した候補だけが正しくても合格にしない。
元の会話と全slotをprivacy検査する。自己申告を三人称へ正規化した際の
話者誤認を避けるため、組立本文の意味分類では自己申告主体を「私」へ戻す。
これは出典検査・scanner・保存禁止条件の省略ではない。

この評価は記憶判断とSQLite保存経路の検証であり、Chroma・UI・実会話の受入証明ではない。
採用モデルは正式比較と負荷・実接続受入後に決める。調整結果は[進捗記録](../../../docs/semantic-memory-341-progress.md)を参照する。

正式な100ケース×3回はクリーンなコミットからのみ実行する。`--case-prefix confirmation --runs 1`は分類を絞る診断用。
各runの`*-memory.jsonl`と`*-memory-summary.json`へ、Ollamaの読み込み量とGPU使用量の定期観測を記録する。
記録値はサンプル間の瞬間最大値を保証しない。共有GPUの使用量には他プロセスが含まれる。

既存知識が入力予算に収まる場合は全件を照合する。超過時だけ会話と属性・値の文字的一致、更新日時で候補を順位づけし、
本番clientがschemaを含めて見積もったtoken上限内へ収める。正本は削除しない。意味的な同義語の網羅を保証する検索ではなく、
この候補選択も本番と性能評価で同じ経路を通す。入力だけで予算を超える設定は明示的なエラーとし、処理位置を進めない。

## 実受入で追加した出来事・既存知識の境界

`boundary-cases.jsonl`の4件は、既存の否定的な好みがあると行動から肯定的な好みへ誤更新した
実受入の合成シナリオを固定したもの。元の100件・期待値は変更しない。
追加4件はすべて出来事のため保存禁止とし、1件でも保存したら不合格とする。
固定commitは`6d1310d`。このcommitのv6診断では2件を誤保存した。

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/eval_semantic_memory.py --model gemma4:12b --include-boundary-regressions --output-dir /tmp/semantic-12b-boundary-final
```

この指定は104件×3回を実行する。選定済み12bの変更後検証であり、v6で実施したe4b/12b比較とは分ける。
`--case-prefix boundary --runs 3`は4件だけの回帰診断であり、全体の正式合格には数えない。
