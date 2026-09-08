# Character Life Runtime vertical slice (#249)

Character Life Runtimeの採否比較用に、同じ最小フローをLangGraphとDBOSで実装する。

## 比較するフロー

```text
複数Episode ID
  ↓
Reflection候補生成（stub）
  ↓
Goal Intention生成（stub）
  ↓
NO_CHANGE / APPLIED等の共通result
```

本spikeは認知品質を比較するものではない。確認対象は次に限定する。

- 既存Python domain contractをそのまま引数・結果に使えるか
- SQLiteで実行状態を保持できるか
- Backend再起動後の再開モデル
- queue / schedulerを別実装せず任せられる範囲
- FastAPIへ組み込む際の追加境界
- 既存SQLite/Chroma/Inference/Execution Gateを正本のまま維持できるか

## DBOS

```bash
cd backend
python -m spikes.character_life_runtime.dbos_slice
```

`DBOS_SYSTEM_DATABASE_URL`を省略するとspike専用SQLiteを使う。

DBOSはworkflow/stepの完了状態をsystem DBへ記録し、queueとscheduleも同じruntimeで管理できる。
本spikeではdomain処理をstep境界へ置き、Character Lifeの正本データはDBOS system DBへ保存しない。

## LangGraph

```bash
cd backend
python -m spikes.character_life_runtime.langgraph_slice
```

SQLite checkpointerを使い、同じフローをStateGraphとして実行する。
Graph stateは実行状態だけに限定し、Memory/Reflection/Personalityの正本にはしない。

## Letta

2026-09時点の現行Agent SDKはPython版がなく、Python統合はApp Server WebSocket protocolまたはAPI client経由となる。
さらにMemory/Dreamingを採用すると、digital-soulsのSQLite/Chroma、privacy admission、lineage、人格形成正本と責務が重複する。
このため同一プロセスのvertical slice対象から外し、アーキテクチャ比較のみ行う。

## 判定

比較結果と採用判断は `docs/decisions/character-life-runtime-2026-09.md` を正本とする。
