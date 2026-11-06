class HcsError(Exception):
    """HCS API 调用失败。status=0 表示网络层错误。"""

    def __init__(self, status: int, code: str, message: str, request_id: str | None = None, *,
                 reason: str | None = None, retryable: bool | None = None):
        super().__init__(f"[{status}] {code}: {message}")
        self.status, self.code, self.message, self.request_id = status, code, message, request_id
        self.reason, self.retryable = reason, retryable
