# Architecture Decision Records

`docs/decisions/`直下には、現行設計の根拠として参照できるADRだけを置く。
完全に置換・失効したADRは`archive/`へ移動し、判断当時の経緯を残す。

## 状態

| 状態 | 意味 | 配置 |
|---|---|---|
| `ACTIVE` | 全体または一部が現行仕様として有効 | `docs/decisions/` |
| `ARCHIVED` | 全面失効、または有効な判断を現行文書へ統合済み | `docs/decisions/archive/` |

一部だけが有効なADRでは、状態タグを増やさず、状態欄に現行範囲と後続ADRを文章で記載する。
archive内の文書を現行仕様や実装のSource of Truthとして使用しない。

## Archive

archiveの一覧と置換先は[`archive/README.md`](archive/README.md)を参照する。
