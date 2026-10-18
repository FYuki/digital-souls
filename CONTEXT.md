# 用語集

Coreで共有するドメインの語彙と概念モデルです。製品の範囲は[README](README.md)、
機能の要件・実装状況・受入条件は[SPEC](SPEC.md)にあります。

## 読み方

このリポジトリでの意味を記します。正式な判断はADR、現在の挙動はコード、進捗はIssueが正本です。
「状態」は[SPEC](SPEC.md)と同じく、この文書と同じリビジョンの実装を基準に照合します。

## 境界と会話

| 用語 | 意味・区別 | 状態・参照 |
| --- | --- | --- |
| Binding | subject・client・audience・characterの組。履歴・記憶・人格の所有境界で、別Bindingの情報を混ぜない | 実装済み：[ADR 0004](docs/adr/0004-conversation-history.md) |
| Conversation / 会話、スレッド | 保存する会話のまとまり。作成・再開・アーカイブ・削除の単位。スレッドは記憶の増分抽出の枠でもあるが、知識の参照・訂正はBinding全体に及ぶ | 実装済み：[履歴API](docs/history-api.md) |
| Source / 出典、source epoch | 記憶の根拠となる元発言（会話・turn revision・message index）と、private化等で進む撤回世代 | 実装済み：[ADR 0007](docs/adr/0007-memory-provenance-and-revocation.md) |
| 指定発話の記憶除外、プライベートモード、アーカイブ | 履歴は残して記憶の対象から外す操作、スレッド単位で記憶の対象外にする操作、一覧から隠すだけの操作 | 実装済み：[ADR 0006](docs/adr/0006-conversation-memory-controls.md) |
| 会話往復 / turn | user入力と、それに対するassistantの応答・tool往復のまとまり。履歴の削除をユーザーが選べる単位の一つ | 実装済み（削除）：[ADR 0019](docs/adr/0019-memory-correction-invalidation.md) |
| 保存への同意 | 長期記憶の登録を有効にした状態で会話したこと。候補ごとの確認は行わない | 決定：ADR 0019 |
| 保存拒否の語 | 「覚えないで」「記録しないで」等。検出した発話は確認が取れるまで記憶形成の対象外とし、プライベートモードと削除機能を案内する。AIは履歴を削除しない | 実装済み：ADR 0019 |
| PrivateAgent | 記憶の形成jobや会話外の活動を実行する予定だった外部の実行基盤。使わない（開発中止） | 中止：[ADR 0027](docs/adr/0027-tool-use-and-activity-runtime.md)（旧：[ADR 0017](docs/adr/0017-memory-formation-admission.md)） |
| 会話外の活動の実行基盤 | 会話外の活動のループを回す汎用Agent。使えるツールは許可方式でCoreのMCPだけに限定し、判断と状態はCoreが持つ | 未実装：ADR 0027 |
| job の台帳 / drain API | 定期処理の永続予約と状態、有限の範囲を処理するAPI。重複の防止と再試行を持ち、中断したjobは冪等性を保って次回に最初からやり直す | 一部（形成jobは未実装）：ADR 0017、ADR 0027 |
| 流出防止ゲート / インジェクション対策 | 外部へ送る直前の内容の再判定／外部の内容と記憶を命令ではなくデータとして扱う境界。どちらもCoreが所有する | 未実装：[ADR 0020](docs/adr/0020-reflection-personality-relationship.md) |
| privacy permission | history・local・external・memoryの別許可。Binding単位でoperatorが明示する | 実装済み：[ADR 0005](docs/adr/0005-privacy-boundaries.md) |

## 記憶

| 用語 | 意味・区別 | 状態・参照 |
| --- | --- | --- |
| Working Memory / 作業記憶 | 現在の入力・目的・処理中の文脈。長期保存しない | 概念：[ADR 0016](docs/adr/0016-memory-kinds-and-records.md) |
| Conversation History / 会話履歴 | 同じ会話を継続するための履歴。別の会話から検索しない。長期記憶とは別 | 実装済み：ADR 0004 |
| Long-term Memory / 長期記憶 | 会話を越えて参照する記憶の総称。Episodic・Semantic・Reflective・Proceduralを含む | 一部：ADR 0016 |
| Episode / エピソード記憶 | 所有キャラクターが何を経験したか。経験の5W（必須はWhatの述語のみ）と経験日時を持つ。「話を聞いた」も経験 | 一部（正本の型・登録・撤回・検索は実装済み、形成は未実装）：ADR 0016、[ADR 0022](docs/adr/0022-memory-retrieval-from-records.md) |
| Fact / 関連事実 | 経験で得た、話題の対象についての情報・申告内容。独立したIDを持ち、Episodeから参照する。検証済みの真実やSemanticの正本ではない | 一部（正本・内容版・参照・撤回は実装済み、形成・照合は未実装）：ADR 0016 |
| Fact Merge / Fact Update | 別々のFactを同じ出来事として版付きのIDで結ぶ処理／対象が明確な補足・訂正でFactの内容版を更新する処理 | 一部（Factの内容版更新は実装済み、発話照合・別Fact統合は未実装）：[ADR 0017](docs/adr/0017-memory-formation-admission.md) |
| Semantic Memory / 意味記憶 | 知識として採用した事実・概念・傾向。形成方法は明示命題の直接抽出（DIRECT_EXTRACTION）と経験からの一般化（EXPERIENCE_DERIVED） | 一部（両形成種別の正本・根拠検証・撤回・検索は実装済み、形成は未実装）：ADR 0016、ADR 0022 |
| Reflective Memory / Reflection / 内省 | 本人が経験をどう意味づけたか。Semanticとは別の正本で、通常会話へ直接注入しない | 未実装：ADR 0020 |
| Procedural Memory / Skill / 手続き記憶 | どう実行するかの記憶。実行・対話の結果から学習し、実行時に参照する | 未実装：ADR 0016 |
| Candidate / 候補 | LLM等が生成し、privacy・根拠・schema・policyの検証前のデータ。検証を通ったものだけを自動で保存する | 概念：ADR 0016 |
| 保存判定（admission） | 候補をDENY_SENSITIVE・DENY_USER_REQUEST・ABSTAIN_UNKNOWN・NOT_MEMORY_WORTHY・ALLOW_STRUCTUREDに決定論的に分類する処理 | 未実装：ADR 0017 |
| stated_at / experienced_at / 対象日時 | 元発言の日時／キャラクターが経験を得た日時／話題の出来事の日時。互いに補完しない | 一部（stated_atの保存、experienced_at・部分日時の保持は実装済み、相対日時解釈は未実装）：ADR 0016 |
| last_user_mentioned_at / TOUCH | ユーザーが最後に明示的に言及した日時／再言及時にこの日時だけを更新する処理。検索の同順位の並べ替えに使う | 一部（日時の永続化・順位利用は実装済み、TOUCHは未実装）：[ADR 0018](docs/adr/0018-memory-retrieval-context.md)、ADR 0022 |
| 派生index | 正本から再構築できる検索用の索引。現行はEpisode・Semanticの保存文から作る呼出し中だけの一時vector。永続indexは未実装 | 一部：ADR 0022 |
| Consolidation / 記憶整理 | 同種の既存記憶をKEEP・MERGE・SUPERSEDE等の型付き計画で整理する処理。一般化やFact統合とは別 | 未実装：ADR 0017 |
| Invalidation / 失効 | 根拠の訂正・削除・撤回で、依存する記憶・派生結果を即時に利用停止し、残る根拠から再評価する処理 | 一部（正本・依存結果の撤回、本文消去・参照版検証は実装済み、再評価・再生成は未実装）：[ADR 0019](docs/adr/0019-memory-correction-invalidation.md) |
| Forgetting / 忘却 | 記憶内容の削除ではなく、想起しにくくなること。必要性が確認できた段階で詳細化する | 概念のみ |
| 出典情報（provenance） | 記憶の根拠の話者・場・発言時点の公開度・出典アドレス。発言時点で固定する | 提案：[ADR 0039](docs/adr/0039-memory-provenance-disclosure-separation.md) |
| 場の公開度 / broadcast | ONE_TO_ONE・CLOSED_GROUP・PUBLICの3段階／不特定の相手に届き記録が残る配信の場の属性 | 提案：ADR 0039 |
| 開示区分 | 記憶の内容が誰を特定し得るか。CHARACTER_SELF・GENERAL・ABOUT_SPEAKER・ABOUT_THIRD_PARTY・ABOUT_CONVERSATION。判定不能はABOUT_SPEAKER扱い | 提案：ADR 0039 |
| キャラクター視点への射影 | 人・場・具体的な日時を省いたキャラクター視点の文。再判定で安全な場合だけ配信等で使う | 提案：ADR 0039 |
| 参照可否の判定 | 出典情報・開示区分・参照の文脈から、参照のたびにALLOW_FULL・ALLOW_PROJECTED・DENYを決める | 提案：ADR 0039 |

## 人格と状態

| 用語 | 意味・区別 | 状態・参照 |
| --- | --- | --- |
| Temperament / 気質 | 経験で変わらない核となる性質・価値観・制約。人格より上位の制約 | 概念：ADR 0020 |
| Personality / 人格 | 経験と内省から緩やかに変わる持続的な傾向。正本はBig Five Aspectsの10因子 | 未実装：ADR 0020 |
| Life State / 生活状態 | Interest、Ongoing Activity、Goal / Implementation Intention等の中期的な状態 | 未実装：ADR 0020 |
| Current Interests / 現在の興味関心 | 今、注意や好奇心が向いている対象。Reflectionから派生し、会話の話題選択に使う | 未実装：ADR 0020 |
| Relationship State / 関係 | 相手ごとの感情の正負と接近・分離の2軸。人格とは別 | 未実装：ADR 0020 |
| Interpersonal Skill | 対人行動を上手く実行する能力。手続き記憶として学習し、関係を直接書き換えない | 未実装：ADR 0020 |
