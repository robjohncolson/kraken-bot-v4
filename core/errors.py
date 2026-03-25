from __future__ import annotations


class KrakenBotError(Exception):
    """Base exception for kraken-bot-v4."""


class ConfigError(KrakenBotError):
    """Base exception for configuration failures."""


class MissingEnvironmentVariableError(ConfigError):
    """Raised when a required environment variable is absent or blank."""

    def __init__(self, variable_name: str) -> None:
        self.variable_name = variable_name
        super().__init__(f"Required environment variable '{variable_name}' is not set.")


class InvalidEnvironmentVariableError(ConfigError):
    """Raised when an environment variable cannot be parsed as expected."""

    def __init__(self, variable_name: str, raw_value: str, expected: str) -> None:
        self.variable_name = variable_name
        self.raw_value = raw_value
        self.expected = expected
        super().__init__(
            f"Environment variable '{variable_name}' must be {expected}; got {raw_value!r}."
        )


class ExchangeError(KrakenBotError):
    """Base exception for exchange integration failures."""


class SafeModeBlockedError(ExchangeError):
    """Raised when local configuration blocks exchange mutations."""


class KrakenAPIError(ExchangeError):
    """Raised when Kraken returns an API-level error."""

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        self.error_code = error_code
        rendered = message if error_code is None else f"{message} (code={error_code})"
        super().__init__(rendered)


class RateLimitExceededError(KrakenAPIError):
    """Raised when the Kraken rate limit is exceeded."""


class InsufficientFundsError(KrakenAPIError):
    """Raised when Kraken rejects an order for insufficient funds."""


class OrderRejectedError(KrakenAPIError):
    """Raised when Kraken rejects an order for a typed reason."""


__all__ = [
    "ConfigError",
    "ExchangeError",
    "InsufficientFundsError",
    "InvalidEnvironmentVariableError",
    "KrakenAPIError",
    "KrakenBotError",
    "MissingEnvironmentVariableError",
    "OrderRejectedError",
    "RateLimitExceededError",
    "SafeModeBlockedError",
]
