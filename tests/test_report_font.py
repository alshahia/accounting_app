# pyright: basic
"""The report renderer must work on a Windows host and in a Linux container.

Real CI caught the failure this module now guards against: on Linux the first
candidate font path does not exist, reportlab raises `TTFError` (which is not an
`OSError`), and every PDF download endpoint returned 500. These tests keep the
fallback behaviour honest without depending on which fonts the machine has.
"""

from __future__ import annotations

import importlib

import pytest

from app.modules.reports import render


@pytest.fixture(autouse=True)
def _reset_registered_font() -> None:
    render._registered_font = None
    yield
    render._registered_font = None


def test_missing_font_path_falls_back_to_helvetica(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        render,
        "PDF_FONT_CANDIDATES",
        (r"C:\no\such\font.ttf", "/no/such/font.ttf"),
    )
    assert render._font() == "Helvetica"


def test_unreadable_font_file_falls_back_to_helvetica(monkeypatch: pytest.MonkeyPatch) -> None:
    # This module's own source file exists but is not a TrueType font, so
    # reportlab raises TTFError (not OSError) when it tries to parse it.
    assert render.__file__ is not None
    monkeypatch.setattr(render, "PDF_FONT_CANDIDATES", (str(render.__file__),))
    assert render._font() == "Helvetica"


def test_explicit_font_override_is_tried_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAHMOUD_PDF_FONT", r"C:\custom\font.ttf")
    reloaded = importlib.reload(render)
    assert reloaded.PDF_FONT_CANDIDATES[0] == r"C:\custom\font.ttf"
    monkeypatch.delenv("MAHMOUD_PDF_FONT")
    importlib.reload(render)
