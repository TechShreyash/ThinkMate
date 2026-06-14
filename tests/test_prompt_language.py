"""Prompt-content assertions for Part B (memory English-normalization + reply language/script).

Part B is LLM-driven, so it is verified by asserting the *prompt content* rather than model
output: the extraction prompt must instruct English normalization with proper-noun preservation,
the group extraction note must reinforce English-per-participant, and the system prompt must carry
the language/script matching rule (Hinglish vs Devanagari, judged from recent context, reply-only).
"""

from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT
from app.prompts.system_prompt import DEFAULT_SYSTEM_PROMPT_TEMPLATE
from app.services.memory_extractor import _GROUP_EXTRACTION_NOTE


def _norm(text: str) -> str:
    """Lowercase + whitespace-collapse so assertions are robust to formatting."""
    return " ".join(text.lower().split())


# --- Requirement 7: store all user memory in English -------------------------------------------

def test_extraction_prompt_has_language_normalization_header():
    """The extraction prompt declares a dedicated LANGUAGE NORMALIZATION rule (Req 7.1, 7.2)."""
    assert "language normalization" in _norm(SYSTEM_EXTRACTION_PROMPT)


def test_extraction_prompt_requires_english_regardless_of_language():
    """Every fact/belief/event stored in English regardless of conversation language (Req 7.1, 7.2)."""
    body = _norm(SYSTEM_EXTRACTION_PROMPT)
    assert "in english" in body
    assert "regardless of the language" in body


def test_extraction_prompt_translates_not_transliterates():
    """Non-English content is translated to natural English, not transliterated (Req 7.1)."""
    body = _norm(SYSTEM_EXTRACTION_PROMPT)
    assert "translate" in body
    assert "transliterate" in body


def test_extraction_prompt_preserves_proper_nouns():
    """Proper nouns, names, and quoted identifiers kept in original form (Req 7.3)."""
    body = _norm(SYSTEM_EXTRACTION_PROMPT)
    assert "proper noun" in body
    assert "original form" in body
    # names / place names / brand names / quoted identifiers are all called out
    assert "name" in body
    assert "quoted identifier" in body


def test_extraction_prompt_includes_pune_example():
    """The Hindi -> English example preserves the proper noun "Pune" (Req 7.3)."""
    assert "मुझे पुणे में नौकरी मिली" in SYSTEM_EXTRACTION_PROMPT
    assert "Got a job in Pune" in SYSTEM_EXTRACTION_PROMPT


def test_group_note_reinforces_english_per_participant():
    """The multi-party note reinforces English storage while names stay original (Req 7.4)."""
    body = _norm(_GROUP_EXTRACTION_NOTE)
    assert "english" in body
    # per-participant framing + names preserved
    assert "name" in body
    assert ("original form" in body) or ("their original" in body)


# --- Requirement 8: match the user's language and script in replies ----------------------------

def test_system_prompt_replies_in_user_current_language():
    """Reply in the language the user is currently using (Req 8.1)."""
    body = _norm(DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    assert "language" in body
    assert "currently using" in body or "currently writing" in body


def test_system_prompt_distinguishes_hinglish_and_devanagari():
    """Hinglish -> Hinglish and Devanagari Hindi -> Devanagari distinction is explicit (Req 8.2, 8.3)."""
    body = _norm(DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    assert "hinglish" in body
    assert "devanagari" in body


def test_system_prompt_judges_from_recent_context_not_isolated_message():
    """Current language/script judged from recent context, not one isolated message (Req 8.4)."""
    body = _norm(DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    assert "recent" in body
    assert "isolated message" in body
    assert "switch" in body


def test_system_prompt_language_matching_independent_of_memory_storage():
    """Language/script matching affects the reply only, not how memories are stored (Req 8.5)."""
    body = _norm(DEFAULT_SYSTEM_PROMPT_TEMPLATE)
    assert "reply only" in body
    # explicit independence from memory storage (memories stay English)
    assert "memories" in body
    assert "english" in body
