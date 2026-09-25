class ServiceError(Exception):
    """Ошибка операции с машиночитаемым кодом (docs/SERVICE_CONTRACT.md §2)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
