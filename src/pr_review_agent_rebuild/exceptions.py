#exceptions.py
#Build Exception Hierarchy:

# Base AppError, then InvalidSignature, MalformedPayload, StorageError
# One @app.exception_handler(AppError) that maps each to a status: 401, 422, 500

# Every error response has the same JSON shape. Same keys, every time, whatever went wrong.
# A client shouldn't have to parse three different error formats.

# Put the request ID in the error body.
# That's what makes "my request failed" actionable — they quote the ID, you grep for it.

# Your handler raises domain errors. It never mentions HTTP status codes.
# The handler is the only place that knows about HTTP.


class AppError(Exception): #Base for everything in app
    status = 500
    def __init__(self, reason: str) -> None:
        super().__init__(f"Request failed: {reason}")

class InvalidSignature(AppError):
    status = "error"

class UnknownSender(AppError):
    status = 999

class MalformedPayload(AppError):
    status = 422

class StorageError(AppError):
    status = 500

class QueueError(AppError):
    status = 500

class GithubRetryError(AppError):
    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after
class GithubPermError(AppError):
    pass
