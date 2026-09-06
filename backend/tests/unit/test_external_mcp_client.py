"""#154の単体検証。"""


def test_metadata_or_protocol_failure_is_not_retryable():
    from app.external_mcp.client import _failure
    from mcp.shared.exceptions import MCPError

    assert not _failure(ValueError("private-result")).retryable
    assert not _failure(MCPError(-32602, "private-protocol")).retryable
    assert _failure(TimeoutError()).retryable
    assert "private" not in str(_failure(ValueError("private-result")))
