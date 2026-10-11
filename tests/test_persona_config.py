"""Persona loading must stay usable after bad edits and honor path changes."""

import os
from unittest.mock import patch

import pytest
from loguru import logger

from app.config import config
from app.services import chat_manager


@pytest.fixture(autouse=True)
def isolated_persona_state(monkeypatch):
    monkeypatch.setattr(config, "PERSONA_FILE", config.PERSONA_FILE)
    monkeypatch.setattr(
        chat_manager,
        "_persona_cache",
        {"path": None, "mtime": None, "content": chat_manager._DEFAULT_PERSONA},
    )
    monkeypatch.setattr(chat_manager, "_warned_missing_persona_paths", set())


def test_missing_persona_file_warns_once(tmp_path):
    missing = tmp_path / "missing-persona.md"
    config.PERSONA_FILE = str(missing)
    sink: list[str] = []
    sink_id = logger.add(lambda message: sink.append(str(message)), level="WARNING")
    try:
        assert chat_manager.validate_persona_file() is False
        assert chat_manager.validate_persona_file() is False
        assert chat_manager._load_persona() == chat_manager._DEFAULT_PERSONA
    finally:
        logger.remove(sink_id)

    warnings = [line for line in sink if "Configured PERSONA_FILE" in line]
    assert len(warnings) == 1
    assert "using fallback default persona" in warnings[0]


@pytest.mark.parametrize("content", [b"", b"  \n\t", b"\xff\xfeinvalid"])
def test_invalid_persona_content_falls_back_without_raising(tmp_path, content):
    persona = tmp_path / "persona.md"
    persona.write_bytes(content)
    config.PERSONA_FILE = str(persona)

    assert chat_manager.validate_persona_file() is False
    assert chat_manager._load_persona() == chat_manager._DEFAULT_PERSONA


def test_persona_directory_is_not_a_valid_file(tmp_path):
    config.PERSONA_FILE = str(tmp_path)
    assert chat_manager.validate_persona_file() is False
    assert chat_manager._load_persona() == chat_manager._DEFAULT_PERSONA


def test_utf8_bom_is_removed_and_text_is_cached(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("Warm, curious companion.", encoding="utf-8-sig")
    config.PERSONA_FILE = str(persona)

    assert chat_manager.validate_persona_file() is True
    assert chat_manager._load_persona() == "Warm, curious companion."
    with patch.object(
        chat_manager, "_read_persona_file", side_effect=AssertionError("reread")
    ):
        assert chat_manager._load_persona() == "Warm, curious companion."


def test_persona_reload_detects_same_size_nanosecond_edits(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("Persona A", encoding="utf-8")
    config.PERSONA_FILE = str(persona)
    assert chat_manager._load_persona() == "Persona A"
    old_stat = persona.stat()

    persona.write_text("Persona B", encoding="utf-8")
    os.utime(
        persona,
        ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000),
    )

    assert chat_manager._load_persona() == "Persona B"


@pytest.mark.parametrize("bad_edit", [b"", b"\xff", None])
def test_failed_refresh_keeps_same_files_last_good_persona(tmp_path, bad_edit):
    persona = tmp_path / "persona.md"
    persona.write_text("Known good persona", encoding="utf-8")
    config.PERSONA_FILE = str(persona)
    assert chat_manager._load_persona() == "Known good persona"

    if bad_edit is None:
        persona.unlink()
    else:
        persona.write_bytes(bad_edit)

    assert chat_manager._load_persona() == "Known good persona"
    assert chat_manager._load_persona() == "Known good persona"


@pytest.mark.parametrize("new_path_kind", ["missing", "directory", "invalid"])
def test_switching_persona_file_never_reuses_other_paths_cache(tmp_path, new_path_kind):
    original = tmp_path / "first.md"
    original.write_text("Original persona", encoding="utf-8")
    config.PERSONA_FILE = str(original)
    assert chat_manager._load_persona() == "Original persona"

    changed = tmp_path / "second.md"
    if new_path_kind == "directory":
        changed.mkdir()
    elif new_path_kind == "invalid":
        changed.write_bytes(b"\xff")
    config.PERSONA_FILE = str(changed)

    assert chat_manager._load_persona() == chat_manager._DEFAULT_PERSONA


def test_fixed_persona_recovers_and_a_later_failure_warns_again(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_bytes(b"\xff")
    config.PERSONA_FILE = str(persona)

    assert chat_manager._load_persona() == chat_manager._DEFAULT_PERSONA
    assert str(persona) in chat_manager._warned_missing_persona_paths
    persona.write_text("Recovered persona", encoding="utf-8")
    assert chat_manager._load_persona() == "Recovered persona"
    assert str(persona) not in chat_manager._warned_missing_persona_paths

    persona.write_bytes(b"\xff")
    assert chat_manager._load_persona() == "Recovered persona"
    assert str(persona) in chat_manager._warned_missing_persona_paths
