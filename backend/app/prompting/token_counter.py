class Utf8TokenEstimator:
    def count(self, text: str) -> int:
        byte_count = len(text.encode("utf-8"))
        return (byte_count + 3) // 4
