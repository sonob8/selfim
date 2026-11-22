"""固定ランタイムパッケージ(Proposer編集外)。

このパッケージはイメージ内で HTTP 契約(AgentCore 互換 /invocations, /ping)と
プロバイダ選択を担う。scaffold/entrypoint.py(編集可能)が壊れても、
サーバ自体はこのランタイムが保護する。
"""
