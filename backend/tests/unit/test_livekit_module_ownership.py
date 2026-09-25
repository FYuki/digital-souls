"""`production.py`の責務別module分割の所有境界を固定する。

#484の完了条件は、各classが指定されたmoduleへ移り、`production.py`が
環境設定解決とcomposition rootだけを持つことである。振る舞いの契約は
既存のruntime/bridge/delivery試験が担い、ここではclassの所在と
`production.py`の残存責務だけを観測する。
"""

from __future__ import annotations

import importlib
import inspect

import pytest


@pytest.mark.parametrize(
    ("module_name", "expected"),
    [
        (
            "app.livekit_transport.production_sdk",
            ["ProductionTokenSigner", "ProductionRoomManager"],
        ),
        (
            "app.livekit_transport.core_factory",
            ["ProductionConversationCoreSessionFactory"],
        ),
        (
            "app.livekit_transport.core_delivery",
            [
                "ProductionCoreEventInbox",
                "_ConversationCoreDelivery",
                "_LiveKitPcmAudioSource",
            ],
        ),
        (
            "app.livekit_transport.microphone_bridge",
            ["_ConversationCoreBridge", "_UserAudioCapture"],
        ),
        (
            "app.livekit_transport.microphone_reader",
            ["MicrophoneReaderOwner"],
        ),
        (
            "app.livekit_transport.session_runtime",
            ["ProductionRuntimeManager", "ProductionSessionOwner"],
        ),
    ],
)
def test_livekit_production_classes_have_single_owning_module(
    module_name: str, expected: list[str]
) -> None:
    module = importlib.import_module(module_name)
    for name in expected:
        owned = getattr(module, name, None)
        assert inspect.isclass(
            owned
        ), f"{module_name}.{name} must be the owning module's class"
        # 別moduleで定義されたclassの再exportは所有点の移動ではない。
        assert owned.__module__ == module_name, (
            f"{module_name}.{name} must be defined in its owning module, "
            f"not re-exported from {owned.__module__}"
        )


def test_production_module_remains_composition_root_only() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    assert inspect.isclass(production.LiveKitConfigurationError)
    assert inspect.isclass(production.ProductionResources)
    assert callable(production.resolve_livekit_settings)
    assert callable(production.configure_production_resources)
    # 互換re-exportなし:移動したclassはowning moduleからだけimportできる。
    moved = [
        "ProductionTokenSigner",
        "ProductionRoomManager",
        "ProductionConversationCoreSessionFactory",
        "ProductionCoreEventInbox",
        "_ConversationCoreDelivery",
        "_LiveKitPcmAudioSource",
        "_ConversationCoreBridge",
        "_UserAudioCapture",
        "MicrophoneReaderOwner",
        "ProductionRuntimeManager",
        "ProductionSessionOwner",
    ]
    for name in moved:
        assert not hasattr(
            production, name
        ), f"production.{name} must not remain as a compatibility re-export"
