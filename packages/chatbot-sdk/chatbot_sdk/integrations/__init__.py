"""Optional, vendor-specific integration modules.

Each submodule is gated on an extra (e.g. ``pip install chatbot-sdk[openai]``)
and lazily imports its provider library at the top via
:func:`chatbot_sdk._require._require_extra`. We deliberately don't import any
of them here — importing this package must not pull in vendor SDKs."""

from __future__ import annotations
