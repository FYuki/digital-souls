from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).parent.parent.parent.parent
README_PATH = ROOT_DIR / "infra" / "dogfood" / "README.md"


def _section(source: str, heading_pattern: str) -> str:
    match = re.search(
        rf"^(?P<marks>##+)\s+{heading_pattern}\s*$",
        source,
        flags=re.MULTILINE,
    )
    assert match is not None, f"見出しがありません: {heading_pattern}"
    next_heading = re.search(
        rf"^#{{1,{len(match.group('marks'))}}}\s+",
        source[match.end() :],
        flags=re.MULTILINE,
    )
    end = match.end() + next_heading.start() if next_heading else len(source)
    return source[match.end() : end]


def _table_rows(section: str) -> tuple[str, ...]:
    return tuple(
        line
        for line in section.splitlines()
        if line.startswith("|") and not re.fullmatch(r"[|:\- ]+", line)
    )


@pytest.mark.parametrize(
    "heading_pattern",
    (
        r"経路①.*通常のアプリケーション更新",
        r"経路②.*bootstrap管理資材のin-place更新",
        r"経路③.*partial構築／破損状態からの障害復旧",
    ),
)
def test_should_separate_each_dogfood_update_route(heading_pattern: str) -> None:
    source = README_PATH.read_text(encoding="utf-8")

    _section(source, heading_pattern)


def test_should_place_route_decision_table_before_bootstrap_and_deploy() -> None:
    source = README_PATH.read_text(encoding="utf-8")

    decision_heading = re.search(r"^##\s+更新経路の選択\s*$", source, re.MULTILINE)
    bootstrap_heading = re.search(r"^##\s+設定とbootstrap\s*$", source, re.MULTILINE)
    deploy_heading = re.search(r"^##\s+deployとrollback\s*$", source, re.MULTILINE)

    assert decision_heading is not None
    assert bootstrap_heading is not None
    assert deploy_heading is not None
    assert decision_heading.start() < bootstrap_heading.start()
    assert decision_heading.start() < deploy_heading.start()


@pytest.mark.parametrize(
    ("route", "required_terms"),
    (
        ("経路①", ("Backend", "Frontend")),
        ("経路①", ("scripts/setup-backend.sh",)),
        ("経路①", ("environments/profiles/dogfood.json",)),
        ("経路②", ("bootstrap.sh", "load-environment.sh", "render-assets.sh")),
        ("経路②", ("deployment-lib.sh",)),
        ("経路②", ("infra/dogfood/templates", "infra/dogfood/systemd")),
        ("経路②", ("infra/dogfood/env.example",)),
        ("経路②", ("service user", "標準path")),
        ("経路②", ("Docker", "Compose", "Node.js 22")),
        ("経路③", ("partial", "破損")),
    ),
)
def test_should_map_each_change_condition_to_the_required_route(
    route: str,
    required_terms: tuple[str, ...],
) -> None:
    source = README_PATH.read_text(encoding="utf-8")
    rows = _table_rows(_section(source, r"更新経路の選択"))

    assert any(route in row and all(term in row for term in required_terms) for row in rows)


def test_should_document_safe_route_selection_without_promoting_rebuild() -> None:
    section = _section(README_PATH.read_text(encoding="utf-8"), r"更新経路の選択")

    assert re.search(r"迷う.*経路②|経路②.*迷う", section)
    assert re.search(r"distribution.*最終手段|最終手段.*distribution", section)
    assert re.search(r"同列.*ない|3経路.*含めない", section)


def test_should_explain_why_application_changes_use_the_normal_route() -> None:
    section = _section(README_PATH.read_text(encoding="utf-8"), r"更新経路の選択")

    assert "dogfood_prepare_backend" in section
    assert re.search(r"Frontend.*build|build.*Frontend", section)
    assert re.search(r"dogfood.json.*実行時|実行時.*dogfood.json", section)


def test_should_order_the_five_bootstrap_update_steps() -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"経路②.*bootstrap管理資材のin-place更新",
    )
    steps = tuple(
        match.group(1)
        for match in re.finditer(r"^\d+\.\s+(.+)$", section, re.MULTILINE)
    )

    assert len(steps) == 5
    assert "backup" in steps[0] and "backup-verify" in steps[0]
    assert "stop-services.sh" in steps[1]
    assert "bootstrap.sh" in steps[2] and "新revision" in steps[2]
    assert "deploy.sh --commit" in steps[3] and "同一SHA" in steps[3]
    assert "status.sh" in steps[4] and "readiness" in steps[4]
    assert re.search(r"origin/main.*祖先", section)
    assert re.search(r"修正commit.*main.*同じ経路|修正commit.*main.*再実行", section)


def test_should_keep_deployment_responsibilities_in_same_sha_deploy() -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"経路②.*bootstrap管理資材のin-place更新",
    )

    assert re.search(r"deploy.*backup.*manifest.*restart.*readiness", section, re.DOTALL)
    assert re.search(r"bootstrap.*checkout.*変更", section, re.DOTALL)
    for responsibility in ("backup", "manifest", "restart"):
        assert re.search(rf"bootstrap.*{responsibility}.*行わない", section, re.DOTALL)
    assert re.search(r"bootstrap後.*deploy|deploy.*bootstrap後", section, re.DOTALL)
    assert re.search(r"readiness失敗時.*直前commit.*自動rollback", section)
    for stop_reason in ("systemd unit", "Frontend build", "chown/chmod"):
        assert stop_reason in section


def test_should_apply_preservation_contract_before_recovery_bootstrap() -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"経路③.*partial構築／破損状態からの障害復旧",
    )

    assert "実機検証時のデータ保全" in section
    assert "7項目" in section
    assert re.search(r"稼働中SQLite.*単純コピー.*行わない", section)

    logical_backup = section.index("論理backup")
    backup_verify = section.index("backup-verify")
    stop_services = section.index("stop-services.sh")
    filesystem_copy = section.index("filesystem")
    bootstrap = section.index("bootstrap.sh")

    assert logical_backup < backup_verify < stop_services < filesystem_copy < bootstrap


def test_should_finish_recovery_through_same_sha_deploy() -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"経路③.*partial構築／破損状態からの障害復旧",
    )

    bootstrap = section.index("bootstrap.sh")
    database = section.index("conversation-history.db")
    deploy = section.index("deploy.sh --commit <同一SHA>")
    status = section.index("status.sh")
    readiness = section.index("readiness", status)

    assert bootstrap < database < deploy < status < readiness
    assert re.search(
        r"conversation-history\.db.*存在しない場合だけ.*start-services\.sh",
        section,
        re.DOTALL,
    )
    assert re.search(r"DB.*存在する場合.*省略", section)
    assert re.search(r"DB.*存在しなければ.*停止", section)
    assert re.search(r"readiness失敗時.*自動rollback", section)


def test_should_explain_normal_deploy_data_and_backup_effects() -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"経路①.*通常のアプリケーション更新",
    )

    assert re.search(r"data.*削除.*再作成.*しない", section)
    assert re.search(r"backup.*削除.*再作成.*しない", section)
    assert re.search(r"検証済み.*backup.*追加|backup.*検証済み.*追加", section)
    assert "migration" in section
    assert re.search(r"data.*更新|更新.*data", section)


@pytest.mark.parametrize(
    "required_term",
    (
        "dogfood.env",
        "backup認証鍵",
        "dogfood.revision",
        "data root",
        "backup generations",
        "deployment state",
        "Ollama model",
    ),
)
def test_should_list_each_preserved_dogfood_asset(required_term: str) -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"実機検証時のデータ保全",
    )

    assert required_term in section


def test_should_require_stopped_services_and_an_independent_private_copy() -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"実機検証時のデータ保全",
    )

    assert section.index("stop-services.sh") < section.index("filesystem")
    assert re.search(r"稼働中.*SQLite.*単純コピー.*禁止", section)
    assert "/var/tmp/digital-souls-preserve-<UTC timestamp>" in section
    assert re.search(r"/var/lib/digital-souls.*外", section)
    assert re.search(r"data root.*外", section)
    assert "root" in section and "0700" in section


def test_should_verify_logical_backup_before_machine_validation() -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"実機検証時のデータ保全",
    )

    assert re.search(
        r"実機検証.*前.*論理backup.*backup-verify|"
        r"論理backup.*backup-verify.*後.*実機検証",
        section,
        re.DOTALL,
    )


def test_should_keep_original_data_until_every_restore_check_succeeds() -> None:
    section = _section(
        README_PATH.read_text(encoding="utf-8"),
        r"実機検証時のデータ保全",
    )

    assert re.search(r"/var/lib/digital-souls.*全削除.*通常.*しない", section)
    assert re.search(r"元データ.*すべて.*成功.*削除しない", section)
    for required_check in (
        "所有者",
        "権限",
        "environment identity",
        "backup-verify",
        "status.sh",
        "readiness",
    ):
        assert required_check in section
