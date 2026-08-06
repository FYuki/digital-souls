# Docker限定利用方針 (2026-06、2026-08更新)

## 状態

**ACTIVE**。

## 決定内容

Backend、Frontend、Ollamaを含む全面的なDocker化は採用しない。FastAPI、Vite、Ollama、Whisperは
WSL2上で直接実行し、VOICEVOXは既存のDocker containerを利用する。

開発とdogfoodの分離にはDockerを使わず、別WSL distribution、別port、独立clone、専用data root、
環境identityを使用する。詳細は`local-dogfood-environment-2026-08.md`を正本とする。

---

## 検討経緯

### 当初の想定

「CI/CDで検証できるものをDockerに含める」を判断軸として検討を開始した。
PostgreSQL・Qdrant・Redisを対象として想定していた。

### 判断を覆した論点

**CI/CDでのDB検証について**

統合テストにおいてDBの実プロセスは不要であり、モック（インメモリ）で代替できる。
サンプルデータ投入・クラウドDB・Dockerコンテナはいずれも過剰であると判断した。

**AIRIについて**

moeru-ai/airi のアーキテクチャ調査の結果、以下が判明した。

- WebSocket経由のサイドカー構造であり、Dockerに入れる必然性がない
- Live2D・音声・WebGPU等のデバイス依存コンポーネントを内包しており、
  Dockerに入れると設定が複雑になる
- 人格反映はCharacter Card（JSON）で完結し、ソースコード改変が不要

当時の詳細は `docs/decisions/archive/docker-airi-policy-2026-06.md` を参照。

**バージョン管理について**

バージョン固定のためだけにDockerを使う必要性は薄い。
aptのバージョン指定・Qdrantバイナリのバージョン管理で十分対応できる。

**個人開発・単一マシンについて**

Dockerが真価を発揮するのは以下の状況であり、現プロジェクトには該当しない。

- 複数人での開発（環境差異の吸収）
- 本番インフラがコンテナ前提（ECS・GKE等）
- 同一OS環境への開発・本番同居（分離のため）

2026-08に、TAKT開発中も安定版を利用するdogfood要件が生じた。検討の結果、運用相当データを
開発用filesystemやcleanupから守る境界には別WSL distributionを採用し、Docker volumeだけへ
依存しないことにした。WSL間でもnetwork namespaceと物理資源は共有されるため、portと
service ownershipは別途分離する。

### 将来の再検討条件

以下の状況が生じた場合にDockerを再検討する。

- ミニPCで複数アプリを同居させ、container単位のdeploy／resource制御が必要になった場合
- 複数台展開が必要になった場合
- OS依存を含む再現可能なimage配布が、直接実行より運用しやすくなった場合

再検討時はGPU、Whisper model cache、SQLite／Chroma volume、backup、multi-architecture image、
rollbackを含めて見積もり、アプリコード変更不要とは仮定しない。

---

## 関連

- `docs/development-environment.md` — 直接インストール構成の詳細
- `docs/infrastructure-policy.md` — インフラ全体方針
- `docs/decisions/local-dogfood-environment-2026-08.md` — 開発／dogfoodの分離境界
- `docs/decisions/archive/docker-airi-policy-2026-06.md` — 失効したAIRI個別方針の検討履歴
