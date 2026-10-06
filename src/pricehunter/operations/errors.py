class RecoveryError(RuntimeError):
    """Only fixed safe codes may cross the operator boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
