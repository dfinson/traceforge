"""Concrete labeling backends.

* :mod:`copilot_sdk` — single-turn completion via ``github-copilot-sdk``.
"""

from __future__ import annotations

from .copilot_sdk import CompletionResult, CopilotSdkBackend

__all__ = ["CompletionResult", "CopilotSdkBackend"]
