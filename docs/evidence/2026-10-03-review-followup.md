# CodeRabbitレビュー指摘の評価と対応

対象は[PR #5のfull review](https://github.com/FYuki/digital-souls-core/pull/5#issuecomment-5958467692)です。
初回reviewのcoverageは`d356df29f52d2b720da569edc85492b5dae42add`。
後続の修正にこのreviewの合格を流用せず、最終headのCIとreview coverageを再確認します。

## Security Architectureの残存Medium

localhost bindingだけではcaller identityを確立できず、Host/Origin検証がない状態で送信を
有効にするとDNS rebinding等を通じてprovider利用枠を消費されうる、という指摘を採用しました。
アプリ内で曖昧・非loopback Host、別origin・opaque origin・重複headerをbody解析前に拒否し、
送信有効なfake profileでもproviderに到達しない回帰テストを追加しています。

これは認証の代替ではありません。信頼された単独利用者のloopback専用という制約、
サンプルの`external_send_allowed=false`、実資格/model未確定のためIT2/ST未実施という状態は維持します。
複数利用者や公開サービスではcaller認証・scope認可が必要です。認証情報や契約を新設していません。

## Docstring Coverage warning

既定80%に対して2.44%というwarningを確認しました。公開API、applicationの文脈/認可境界、
stream所有権、エラー分類、切断処理に、呼出側が必要とする制約・副作用のdocstringを補っています。
テスト関数名を繰り返すだけの説明を大量追加して比率を上げることはしません。
80%はこのrepoで合意した必須CI条件ではなく、warningを無効化したり閾値を下げたりしていません。
残るカバレッジwarningは境界説明を優先した選択として評価し、最終review結果にも明記します。

## Linked Issueのlock確認がinconclusive

原因はCodeRabbit既定の`!**/*.lock`除外でした。公式path-filter仕様に従って
`.coderabbit.yaml`で正確な既定patternから`!`を外した`**/*.lock`を指定し、lockをreview対象に戻します。
他のファイルをreview対象から外す設定ではありません。適用されたことは次のbot報告でも確認します。
出典: [CodeRabbit Path Filters](https://docs.coderabbit.ai/reference/glossary#path-filters)。

実体はコミット済み[uv.lock](../../core/uv.lock)で、runtime/dev依存の正確なversionと配布artifact hashを保持します。
[pyproject.toml](../../core/pyproject.toml)でも直接依存を正確なversionに固定しています。
[API CI](../../.github/workflows/api.yml)で次を実行します。

1. `uv lock --check`でmanifestとlockの一致を確認。
2. `uv sync --frozen`でlockを書き換えず導入。
3. lint/format/type、socket禁止UT/IT1を実行。
4. sdist/wheelをbuildし、`uv export --frozen --no-dev --no-emit-project`でruntimeを取り出す。
5. 独立venvへ`uv pip install --require-hashes`でruntimeを導入し、wheelを追加してAPI importを確認。

初期実装と親review修正のexact head CIにもbuild/install成功があり、最新PR本文から実行URLを参照できます。
lockを包括的な脆弱性監査の代わりとして扱ってはいません。

## ローカル検証

Ubuntu / Python 3.12.3 / uv 0.8.22 / Node 24.19.0、2026-10-02 UTCに実行。
対象revisionは`5dc71119b935f9234f988f1fa95ae62faf285ca3`です。
同じSHAの[API CI](https://github.com/FYuki/digital-souls-core/actions/runs/37050863193)と
[文書CI](https://github.com/FYuki/digital-souls-core/actions/runs/37050863218)でも成功を確認しました。

- `uv lock --check`、`uv sync --frozen`: PASS、lock変更なし。
- `uv run --no-sync ruff check src tests`、`ruff format --check src tests`、`mypy`: PASS。
- `uv run --no-sync pytest -m ut -q`: 18 PASS。
- `uv run --no-sync pytest -m it1 -q`: 81 PASS。両群のskip/xfail/xpassは0。
- 既存のSDK非推奨・合成応答serializer warningは残存。テスト失敗を隠す設定は追加していません。
- `uv build --no-build-isolation`、hash必須runtime install、独立venvへのwheel install/import: PASS。
- Node文書ツールの必須23テスト、`node tools/check-docs.mjs`、`git diff --check`: PASS。
- `.coderabbit.yaml`を公式schemaで検証: PASS。botでの新しいcoverageは別途確認が必要。
- IT2/ST（実model）: NOT RUN。資格・modelの確認が未完了で、サンプルは外部送信拒否を維持。
