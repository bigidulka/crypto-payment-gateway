"""
Кастомные исключения приложения.
"""

from typing import Any


class AppException(Exception):
    """Базовое исключение приложения."""

    def __init__(
        self,
        message: str = "An error occurred",
        code: str = "APP_ERROR",
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


class ValidationError(AppException):
    """Ошибка валидации входных данных."""

    def __init__(
        self,
        message: str = "Validation error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="VALIDATION_ERROR",
            status_code=422,
            details=details,
        )


class AuthenticationError(AppException):
    """Ошибка аутентификации."""

    def __init__(
        self,
        message: str = "Authentication failed",
    ) -> None:
        super().__init__(
            message=message,
            code="AUTHENTICATION_ERROR",
            status_code=401,
        )


class AuthorizationError(AppException):
    """Ошибка авторизации (нет доступа)."""

    def __init__(
        self,
        message: str = "Access denied",
    ) -> None:
        super().__init__(
            message=message,
            code="AUTHORIZATION_ERROR",
            status_code=403,
        )


class NotFoundError(AppException):
    """Ресурс не найден."""

    def __init__(
        self,
        resource: str = "Resource",
        resource_id: str | None = None,
    ) -> None:
        message = f"{resource} not found"
        if resource_id:
            message = f"{resource} with id '{resource_id}' not found"
        super().__init__(
            message=message,
            code="NOT_FOUND",
            status_code=404,
        )


class InvoiceNotFoundError(NotFoundError):
    """Инвойс не найден."""

    def __init__(self, invoice_id: str) -> None:
        super().__init__(resource="Invoice", resource_id=invoice_id)
        self.code = "INVOICE_NOT_FOUND"


class InvoiceExpiredError(AppException):
    """Инвойс истёк."""

    def __init__(self, invoice_id: str) -> None:
        super().__init__(
            message=f"Invoice '{invoice_id}' has expired",
            code="INVOICE_EXPIRED",
            status_code=410,  # Gone
        )


class InvoiceAlreadyPaidError(AppException):
    """Инвойс уже оплачен."""

    def __init__(self, invoice_id: str) -> None:
        super().__init__(
            message=f"Invoice '{invoice_id}' is already paid",
            code="INVOICE_ALREADY_PAID",
            status_code=409,  # Conflict
        )


class PaymentError(AppException):
    """Ошибка при обработке платежа."""

    def __init__(
        self,
        message: str = "Payment processing error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="PAYMENT_ERROR",
            status_code=400,
            details=details,
        )


class BlockchainError(AppException):
    """Ошибка при взаимодействии с блокчейном."""

    def __init__(
        self,
        message: str = "Blockchain error",
        chain: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        details = details or {}
        if chain:
            details["chain"] = chain
        super().__init__(
            message=message,
            code="BLOCKCHAIN_ERROR",
            status_code=502,  # Bad Gateway
            details=details,
        )


class DuplicateError(AppException):
    """Дубликат (idempotency key уже использован и т.д.)."""

    def __init__(
        self,
        message: str = "Duplicate request",
        existing_id: str | None = None,
    ) -> None:
        details = {}
        if existing_id:
            details["existing_id"] = existing_id
        super().__init__(
            message=message,
            code="DUPLICATE",
            status_code=409,  # Conflict
            details=details,
        )


class RateLimitError(AppException):
    """Превышен лимит запросов."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: int | None = None,
    ) -> None:
        details = {}
        if retry_after:
            details["retry_after"] = retry_after
        super().__init__(
            message=message,
            code="RATE_LIMIT_EXCEEDED",
            status_code=429,
            details=details,
        )
