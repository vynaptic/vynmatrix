"""Deterministic, recorded-data research without runtime execution authority."""

from .linear_model import predict_return_model, train_return_model, validate_model

__all__ = ["predict_return_model", "train_return_model", "validate_model"]
