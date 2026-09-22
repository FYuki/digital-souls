# 通知基盤のCodeRabbit指摘対応（2026-09-22）

対象は[PR #490](https://github.com/FYuki/digital-souls/pull/490)、レビュー対象headは`13ffa06d79b0ed05509468f35995e11fac1145b3`。
CodeRabbitの2026-09-22 14:13 UTCのレビュー4件を実装・契約と照合した。

| 指摘 | 判断・修正 |
|---|---|
| [世代変更後に通知が永久抑止される](https://github.com/FYuki/digital-souls/pull/490#discussion_r4072624741) | 不具合を再現して採用。ただし提案の「streamが違えば常に失効」は採用しない。遅れたconsumerが旧世代の履歴を読む場合に、OFF中の分を遡及通知してしまうため。共有sourceの受信位置を維持し、EventRuntimeが世代変更を確認したbaselineで旧世代の抑止位置を失効させる。 |
| [結果revisionの上限到達でconsumerが停止する](https://github.com/FYuki/digital-souls/pull/490#discussion_r4072624768) | 採用。識別行の1,024件上限と既存の重複判定記録を保持し、超過分は通知を増やさず、履歴不足・取得不能を記録する。処理位置とACKを進め、batch中の受理済み通知を巻き戻さない。 |
| [読み取りごとの全件整理・書き込みロック](https://github.com/FYuki/digital-souls/pull/490#discussion_r4072624783) | 採用。get／listingの期限切れ判定を検索条件へ移し、未読件数・保持不足も同じ現在時刻で除外する。状態変更は同じtransaction内で更新行を返す。物理削除は保存時・毎分の保守処理で行う。 |
| [ADRの実装状態表記が古い](https://github.com/FYuki/digital-souls/pull/490#discussion_r4072624791) | 採用。ACTIVEを維持し、通知runtime・API・UIの実装済み範囲と、#364／#365／#366の後続会話接続を分けた。 |

事前の再現確認で、すでにONの通知へ同じON設定を再送すると、受信済み未処理Eventを破棄する問題も確認した。OFFからONへ変わる登録だけに受信位置の境界を設定し、再起動・別端末からの再送でも新着を失わないよう修正した。

## 検証と残る境界

- 通知・通知専用起動・共有Eventの関連試験は54件成功。その後、初回consumer未処理とbatch途中の上限到達を追加し、通知単体・モジュール26件成功を確認した。
- Backend全体の型検査は375ファイル成功。
- 世代変更後の新着再開、旧世代の遡及防止、同じONの再送、上限到達後のACK・再起動・重複防止、期限切れの未読件数・状態変更、読み取りで書き込まないことを検証した。
- 実ブラウザ適合検証の今回の記録は[レビュー修正後の証跡](../artifacts/notification-183/review-2026-09-22/browser-conformance.json)に保存する。
- 実外部サービス・dogfood受入は未実施。過去の検証記録は変更しない。
- 初回CodeRabbitレビューは完了したが、修正後のCodeRabbit再レビュー承認を示す記録ではない。mainへの最終マージはユーザーが実施する。

用語・公開API・schemaは変更していない。保持・再開・上限の現行挙動は[通知runtime](../notification-runtime.md)へ反映した。README・AGENTS・用語集・アーキテクチャの責務境界は変更不要。
