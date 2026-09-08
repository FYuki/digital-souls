"""通常CoreのlifespanでTool Routing未設定でも管理APIを提供する。"""


def test_management_available_without_tool_routing(client):
    assert client.app.state.tool_service is None
    response = client.get("/addon-admin/connections")
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["cache-control"] == "no-store"
