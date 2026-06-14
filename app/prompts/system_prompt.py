"""Assembles the chat system prompt from the persona, core behavior rules, and compiled memory.

Language-mirroring and length-matching live here (not in the editable persona) so they're
always enforced regardless of persona edits.
"""

DEFAULT_SYSTEM_PROMPT_TEMPLATE = """{persona_content}

---

## RESPONSE BEHAVIOR (always follow)
- **Language & script**: Reply in the same language the user is currently using. Judge their current language and script from the recent flow of the conversation, not from a single isolated message — and when their recent usage shifts to a new language or script, switch with them. For Hindi specifically, match the user's *script*, not just the language: if they write Hinglish (Hindi in Latin/English letters), reply in Hinglish; if they write Devanagari Hindi, reply in Devanagari. Many users prefer Hinglish, so when in doubt for Hindi written in Latin letters, stay in Hinglish. Never answer in a different language or script than the one they are currently using. This language/script matching applies to your **reply only** — it does not change how memories are stored (memories are always stored in English regardless of the conversation language).
- **Length**: Mirror the user's energy and message size. A short or casual message ("hi", "hey how's it going") gets a short reply. When the user shares something substantial — a story, an experience, a problem — respond with enough depth to engage properly. There is no fixed length limit; just don't pad short messages or cut genuine moments short.

---

## 🧠 MEMORIES OF THE USER:
Below are your current active memories of the user. Use them naturally and casually in conversation as outlined in the "Memory Integration Rules" above:

{active_memory_text}
"""


def build_system_prompt(
    persona_content: str,
    active_memory_text: str,
    time_context: str = "",
    user_memory_text: str = "",
) -> str:
    base = DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(
        persona_content=persona_content,
        active_memory_text=active_memory_text or "No memories recorded yet. Start chatting to build a profile!",
    )
    user_block = ""
    if user_memory_text and user_memory_text.strip():
        user_block = (
            "\n---\n\n## 🙋 MEMORIES OF THE PERSON SPEAKING NOW:\n"
            "These are your memories of the specific group member you are replying to. "
            "The block above is the shared group context; this block is about THIS person.\n\n"
            f"{user_memory_text.strip()}\n"
        )
    time_block = ""
    if time_context and time_context.strip():
        time_block = f"\n---\n\n## ⏰ TIME CONTEXT\n{time_context.strip()}\n"
    return base + user_block + time_block
