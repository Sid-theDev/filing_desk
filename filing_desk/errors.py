"""The one error type tools raise."""


class ToolError(Exception):
    """A failure the caller can recover from. The message and hint say what to do next."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"error": self.message, **self.details}
