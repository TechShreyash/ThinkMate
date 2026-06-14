"""Property test for defensive degradation — never raise (Task 5.2).

# Feature: implicit-bot-addressing, Property 9: Defensive degradation (never
# raise) — fuzzing malformed entities, non-string text, and None across
# is_mass_tag_spam, SpamBurstDetector.observe, and ImplicitAddressGate.decide
# never lets an exception escape; each returns its safe-default verdict.

**Validates: Requirements 1.6, 9.6, 10.14**
"""
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.group_gate import (
    ImplicitAddressGate,
    SpamBurstDetector,
    is_mass_tag_spam,
)

# Arbitrary scalar junk used for text / now / flags.
_junk = st.one_of(
    st.none(),
    st.integers(),
    st.text(max_size=20),
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False),
)

# A malformed entity-like object with arbitrary (possibly wrong-typed) attrs.
_malformed_entity = st.builds(
    SimpleNamespace,
    type=st.one_of(st.none(), st.sampled_from(["mention", "text_mention", "bold"]), st.integers()),
    offset=st.one_of(st.none(), st.integers(min_value=-5, max_value=50), st.text(max_size=4)),
    length=st.one_of(st.none(), st.integers(min_value=-5, max_value=50), st.text(max_size=4)),
)

# Entities: junk scalars, or lists mixing junk and malformed entity objects.
_entities = st.one_of(
    _junk,
    st.lists(st.one_of(_junk, _malformed_entity), max_size=6),
)


@settings(max_examples=300)
@given(
    text=_junk,
    entities=_entities,
    threshold=st.integers(min_value=-1, max_value=10),
    now=_junk,
    is_spam=_junk,
    directed=_junk,
    bot_spoke=st.booleans(),
)
def test_defensive_degradation_never_raises(
    text, entities, threshold, now, is_spam, directed, bot_spoke
):
    # is_mass_tag_spam → always a bool, never raises (Req 9.6).
    verdict = is_mass_tag_spam(text, entities, threshold=threshold)
    assert isinstance(verdict, bool)

    # SpamBurstDetector.observe → always a bool, never raises (Req 10.14).
    det = SpamBurstDetector()
    burst = det.observe(123, text, entities, now=now)
    assert isinstance(burst, bool)

    # ImplicitAddressGate.decide → always (bool, str), never raises (Req 1.6).
    gate = ImplicitAddressGate()
    if bot_spoke:
        gate.note_bot_spoke(123, 0.0)
    result = gate.decide(123, directed_at_other=directed, is_spam=is_spam, now=now)
    assert isinstance(result, tuple) and len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)
