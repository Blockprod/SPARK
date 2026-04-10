"""Pytest configuration — stub unavailable symbols before collection.

diffusers 0.31+ is installed but LTXImageToVideoPipeline / LTXPipeline were
added in 0.32, so we inject MagicMock sentinels so that core/video_gen.py can
be imported without error.  The mocks are never called during unit tests.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import diffusers as _diffusers

for _sym in ("LTXImageToVideoPipeline", "LTXPipeline"):
    if not hasattr(_diffusers, _sym):
        setattr(_diffusers, _sym, MagicMock(name=f"diffusers.{_sym}"))
