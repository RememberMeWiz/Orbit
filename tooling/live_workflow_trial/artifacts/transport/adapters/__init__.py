"""Transport adapters. Mechanical delivery only."""
from .base import LocalAdapter
from .claude_code import ADAPTER_TYPE, ClaudeCodeAdapter

__all__ = ["ADAPTER_TYPE", "ClaudeCodeAdapter", "LocalAdapter"]
