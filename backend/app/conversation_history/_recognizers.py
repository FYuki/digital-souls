import re

from app.conversation_history.scan_models import FindingCategory, StorageScope

PATTERNS = (
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "email_address",
        re.compile(
            r"(?i)(?<![A-Za-z0-9._%+-])"
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
            r"(?![A-Za-z0-9.-])"
        ),
    ),
    (
        FindingCategory.SECRET,
        "password_or_credential",
        re.compile(
            r"(?i)\b(?:password|passwd|パスワード|api[ _-]?key|apiキー|"
            r"access[ _-]?token|secret(?:[ _-]?key)?|秘密鍵)"
            r"\s*[:=：]\s*\S+"
        ),
    ),
    (
        FindingCategory.SECRET,
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
            r"[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"
        ),
    ),
    (
        FindingCategory.SECRET,
        "seed_phrase",
        re.compile(
            r"(?i)\b(?:seed phrase|recovery phrase|mnemonic|シードフレーズ)"
            r"\s*[:=：]\s*(?:[a-z]+\s+){11,23}[a-z]+\b"
        ),
    ),
    (
        FindingCategory.SECRET,
        "payment_authentication",
        re.compile(
            r"(?i)\b(?:cvv|cvc|security code|セキュリティコード)"
            r"\s*[:=：]\s*\d{3,4}\b"
        ),
    ),
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "japanese_phone_number",
        re.compile(
            r"(?<!\d)(?:\+81[\s()-]*[1-9]0?|0[5789]0)"
            r"[\s()-]*\d{4}[\s()-]*\d{4}(?!\d)"
        ),
    ),
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "nanp_phone_number",
        re.compile(
            r"(?<!\d)(?:\+1[\s.-]*)?(?:\([2-9]\d{2}\)|[2-9]\d{2})"
            r"[\s.-]*\d{3}[\s.-]*\d{4}(?!\d)"
        ),
    ),
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "japanese_mynumber",
        re.compile(
            r"(?i)(?:マイナンバー|個人番号)\s*[:=：]?\s*"
            r"\d{4}[\s-]?\d{4}[\s-]?\d{4}"
        ),
    ),
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "us_social_security_number",
        re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    ),
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "driver_license_number",
        re.compile(
            r"(?i)(?:運転免許証番号|免許証番号|driver'?s? license(?: number)?)"
            r"\s*[:=：]?\s*[A-Z0-9-]{6,18}"
        ),
    ),
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "bank_account_number",
        re.compile(
            r"(?i)(?:口座番号|account number|routing number)"
            r"\s*[:=：]?\s*\d[\d\s-]{5,16}\d"
        ),
    ),
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "postal_address",
        re.compile(
            r"(?i)(?:〒?\d{3}-?\d{4}\s*[^\s]{2,4}(?:都|道|府|県).{2,60}|"
            r"\d{1,6}\s+[A-Za-z0-9 .'-]+\s+"
            r"(?:street|st|avenue|ave|road|rd|boulevard|blvd)\.?"
            r".{0,40}\b\d{5}(?:-\d{4})?\b)"
        ),
    ),
    (
        FindingCategory.DIRECT_IDENTIFIER,
        "precise_coordinates",
        re.compile(
            r"(?<![\d.])-?(?:[0-8]?\d(?:\.\d+)?|90(?:\.0+)?)\s*,\s*"
            r"-?(?:1[0-7]\d(?:\.\d+)?|(?:[0-9]?\d)(?:\.\d+)?|180(?:\.0+)?)"
            r"(?![\d.])"
        ),
    ),
)

CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[\s()-]*){12,18}\d(?!\d)")
VENDOR_TOKEN_CANDIDATE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:sk|pk)(?:[\s-]*[A-Za-z0-9_]){16,}"
    r"(?![A-Za-z0-9_])"
)
VENDOR_TOKEN_COMPACT = re.compile(r"(?i)(?:sk|pk)[A-Za-z0-9_]{16,}")
STORAGE_DIRECTIVES = (
    (
        StorageScope.HISTORY,
        "history_storage_denied",
        re.compile(r"履歴(?:に|にも)残さないで|履歴に保存しないで", re.IGNORECASE),
    ),
    (
        StorageScope.BOTH,
        "all_storage_denied",
        re.compile(
            r"保存しないで|記録しないで|do not save|don't save",
            re.IGNORECASE,
        ),
    ),
    (
        StorageScope.RAG,
        "rag_storage_denied",
        re.compile(
            r"覚えないで|記憶しないで|do not remember|don't remember",
            re.IGNORECASE,
        ),
    ),
)
