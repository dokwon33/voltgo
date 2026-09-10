class ClientError(Exception):
    """외부 API 실패. code 는 설계서 ToolResult.error_code 표준값 (API규격_검토 §4)"""

    def __init__(self, code: str, message: str = "", retryable: bool = False):
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable
