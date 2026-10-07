"""Offline test doubles shared by every platform test suite (FOUNDATION-owned)."""

from .dynamodb import ExpressionError, FakeDynamoDB

__all__ = ["FakeDynamoDB", "ExpressionError"]
