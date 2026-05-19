"""Parsers for legacy TSV-based strategy definitions."""

from .parser import OrderSpec, read_strategy_file

__all__ = ["OrderSpec", "read_strategy_file"]
