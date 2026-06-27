"""Tests for persona-file configuration visibility."""
from loguru import logger

from app.config import config
from app.services import chat_manager


def test_missing_persona_file_warns_once(tmp_path):
    missing = tmp_path / "missing-persona.md"
    original_persona = config.PERSONA_FILE
    sink: list[str] = []
    sink_id = logger.add(lambda message: sink.append(str(message)), level="WARNING")
    try:
        config.PERSONA_FILE = str(missing)
        chat_manager._warned_missing_persona_paths.clear()

        assert chat_manager.validate_persona_file() is False
        assert chat_manager.validate_persona_file() is False
    finally:
        logger.remove(sink_id)
        config.PERSONA_FILE = original_persona
        chat_manager._warned_missing_persona_paths.clear()

    warnings = [line for line in sink if "Configured PERSONA_FILE" in line]
    assert len(warnings) == 1
    assert "using fallback default persona" in warnings[0]
