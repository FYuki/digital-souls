"""外部MCP基盤の公開境界。"""
from .client import ExternalMCPClient
from .models import Connection, Discovery, MCPFailure, Snapshot
from .registry import Registry

__all__ = ["Connection", "Discovery", "ExternalMCPClient", "MCPFailure", "Snapshot", "Registry"]
