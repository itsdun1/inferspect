"""Chatbot SDK — captures inference, tool execution, and application logs."""

from __future__ import annotations

from chatbot_sdk.client import InferenceLogger
from chatbot_sdk.schema import (
    ApplicationLog,
    InferenceLog,
    LogEnvelope,
    LogType,
    ToolExecutionLog,
)
from chatbot_sdk.structlog_processor import LogShippingProcessor
from chatbot_sdk.sync import SyncInferenceLogger

__all__ = [
    "ApplicationLog",
    "InferenceLog",
    "InferenceLogger",
    "LogEnvelope",
    "LogShippingProcessor",
    "LogType",
    "SyncInferenceLogger",
    "ToolExecutionLog",
]

__version__ = "0.2.1"
