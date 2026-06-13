"""Assembles the chat system prompt from the persona, core behavior rules, and compiled memory.

Language-mirroring and length-matching live here (not in the editable persona) so they're
always enforced regardless of persona edits.
"""

DEFAULT_SYSTEM_PROMPT_TEMPLATE = """{persona_content}

---

## RESPONSE BEHAVIOR (always follow)
- **Language**: Reply in the same language the user is writing in. If they switch languages or scripts mid-conversation, switch with them. Never answer in a different language than they used.
- **Length**: Mirror the user's energy and message size. A short or casual message ("hi", "hey how's it going") gets a short reply. When the user shares something substantial — a story, an experience, a problem — respond with enough depth to engage properly. There is no fixed length limit; just don't pad short messages or cut genuine moments short.

---

## 🧠 MEMORIES OF THE USER:
Below are your current active memories of the user. Use them naturally and casually in conversation as outlined in the "Memory Integration Rules" above:

{active_memory_text}
"""


def build_system_prompt(persona_content: str, active_memory_text: str) -> str:
    return DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(
        persona_content=persona_content,
        active_memory_text=active_memory_text or "No memories recorded yet. Start chatting to build a profile!",
    )
