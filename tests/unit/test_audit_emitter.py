"""Emitter boundary, formatter split, and level isolation (Day 22 Task 3)."""

import json
import logging

import pytest
from pydantic import BaseModel, ValidationError
from tests.unit.audit_helpers import base_fields as _base

from azgenai_lab.core.audit import ChatTurnSuccess, emit_audit_event
from azgenai_lab.core.logging import configure_logging


def _event():
    return ChatTurnSuccess(**_base())


def test_emitter_rejects_foreign_models(capsys):
    class Sneaky(BaseModel):
        message: str = "user secret"

    with pytest.raises(ValidationError):
        emit_audit_event(Sneaky())  # type: ignore[arg-type]
    assert not any(line.startswith("{") for line in capsys.readouterr().err.splitlines())


def test_emitter_line_is_pure_json(capsys):
    configure_logging("INFO")
    emit_audit_event(_event())
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("{")]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "chat.turn"


def test_diagnostic_lines_keep_prefix(capsys):
    configure_logging("INFO")
    logging.getLogger("azgenai_lab.test").info("hello diag")
    line = next(ln for ln in capsys.readouterr().err.splitlines() if "hello diag" in ln)
    assert "correlation_id=" in line and not line.startswith("{")


def test_audit_survives_warning_level(capsys):
    configure_logging("WARNING")
    logging.getLogger("azgenai_lab.test").info("filtered diag")
    emit_audit_event(_event())
    err = capsys.readouterr().err
    assert "filtered diag" not in err
    assert any(ln.startswith("{") for ln in err.splitlines())


def test_propagation_reaches_root_capture(caplog):
    # NO configure_logging() here: basicConfig(force=True) would tear out
    # pytest's capture handler. Propagation alone is under test.
    with caplog.at_level(logging.INFO, logger="audit"):
        emit_audit_event(_event())
    assert sum(1 for r in caplog.records if r.name == "audit") == 1
