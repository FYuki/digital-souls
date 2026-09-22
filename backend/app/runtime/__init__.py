"""lifespanの資源所有層。domain別ownerの構築・公開・停止を集約する。

domain層のruntimeはこの層からだけ組み立てられ、domain側はこの層を参照しない。
`app.state`は既存router向けの公開境界として維持し、owner間の依存は型付き属性で渡す。
"""
