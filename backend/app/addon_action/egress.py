"""承認とは別に、外部送信直前の意味privacyを評価する。"""

from app.async_worker import run_sync
from app.external_mcp.models import Json, encode
from app.privacy.semantic.classifier import SemanticPrivacyClassifier
from app.privacy.semantic.contracts import (
    SemanticClassification,
    SemanticClassifierCallProfile,
)


class ActionEgress:
    def __init__(self, classifier: SemanticPrivacyClassifier | None) -> None:
        self.classifier = classifier

    async def allowed(self, arguments: Json) -> bool:
        if not arguments:
            return True
        if self.classifier is None:
            return False
        result = await run_sync(
            self.classifier.classify,
            encode(arguments),
            SemanticClassifierCallProfile("ADDON_ACTION_EGRESS", 15, 0, 0, 15),
        )
        return result.classification == SemanticClassification.NOT_SENSITIVE
