"""Service: PII redaction using Microsoft Presidio.

Runs on every string field of an inbound event that might contain user
content: ``input_preview``, ``output_preview``, ``args_preview``,
``result_preview``, ``message``, and the values inside ``metadata`` /
``attributes`` (recursively).

If Presidio is disabled in config or fails to initialize (e.g. missing spaCy
model), redaction silently becomes a no-op — we never block ingestion on PII.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Fields per-log_type that should have their text scanned for PII.
_TEXT_FIELDS_BY_LOG_TYPE = {
    "inference": ("input_preview", "output_preview"),
    "tool_execution": ("args_preview", "result_preview"),
    "application": ("message",),
}

# Free-form dict fields that should have their string values walked.
_DICT_FIELDS = ("metadata", "attributes")


class PIIService:
    """Wraps Presidio analyzer + anonymizer. Lazy-initialized; safe to disable."""

    def __init__(self, *, enabled: bool, entities: list[str], template: str = "<{}>") -> None:
        self.enabled = enabled
        self.entities = entities
        self.template = template
        self._analyzer = None
        self._anonymizer = None
        if enabled:
            self._try_init()

    def _try_init(self) -> None:
        try:
            from presidio_analyzer import AnalyzerEngine  # type: ignore[import-untyped]
            from presidio_anonymizer import AnonymizerEngine  # type: ignore[import-untyped]

            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Presidio init failed; PII redaction disabled: %s", exc)
            self.enabled = False

    def redact_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Return an event dict with detected PII anonymized in known text
        fields. Operates on a shallow copy — never mutates the caller's dict."""
        if not self.enabled or self._analyzer is None or self._anonymizer is None:
            return event

        log_type = event.get("log_type")
        text_fields = _TEXT_FIELDS_BY_LOG_TYPE.get(log_type, ())
        if not text_fields and not any(k in event for k in _DICT_FIELDS):
            return event

        out = dict(event)
        for field in text_fields:
            if isinstance(out.get(field), str) and out[field]:
                out[field] = self._redact_text(out[field])

        for dict_field in _DICT_FIELDS:
            if isinstance(out.get(dict_field), dict):
                out[dict_field] = self._redact_dict(out[dict_field])

        return out

    def _redact_text(self, text: str) -> str:
        from presidio_anonymizer.entities import OperatorConfig  # type: ignore[import-untyped]

        try:
            results = self._analyzer.analyze(text=text, entities=self.entities or None, language="en")  # type: ignore[union-attr]
            if not results:
                return text
            operators = {
                entity: OperatorConfig("replace", {"new_value": self.template.format(entity)})
                for entity in {r.entity_type for r in results}
            }
            anon = self._anonymizer.anonymize(text=text, analyzer_results=results, operators=operators)  # type: ignore[union-attr]
            return anon.text
        except Exception as exc:  # noqa: BLE001
            logger.debug("PII redaction failed; passing through: %s", exc)
            return text

    def _redact_dict(self, d: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, str):
                out[k] = self._redact_text(v) if len(v) > 0 else v
            elif isinstance(v, dict):
                out[k] = self._redact_dict(v)
            else:
                out[k] = v
        return out
