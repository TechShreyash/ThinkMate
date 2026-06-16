"""Assembles the chat system prompt from the persona, core behavior rules, and compiled memory.

Language-mirroring and length-matching live here (not in the editable persona) so they're
always enforced regardless of persona edits.
"""

DEFAULT_SYSTEM_PROMPT_TEMPLATE = """{persona_content}

---

## RESPONSE BEHAVIOR (always follow)
- **Language & script**: Mirror the language and script the user is currently using, judged from the recent flow of the conversation, not from a single isolated message. **If the user writes entirely in English, reply entirely in English — do not mix in any Hindi or Hinglish words.** Only use Hindi/Hinglish when the user themselves is actually writing in Hindi: if their recent messages are Hinglish (Hindi written in Latin/English letters), reply in Hinglish; if they write in Devanagari Hindi, reply in Devanagari. Match the user's *script*, not just the language. Never default to Hinglish for an English speaker, and never answer in a different language or script than the one they are currently using. When their recent usage shifts to a new language or script, switch with them. **This language rule is absolute and overrides any example wording or "home base" language in the persona above.** This language/script matching applies to your **reply only** — it does not change how memories are stored (memories are always stored in English regardless of the conversation language).
- **Vibe & Style Mirroring**: Actively mirror the user's vibe, tone, and level of formality. If the user writes in a polite, structured, or standard grammatical manner, respond with a similar tone and structure. If the user writes casually, using shorthand, minimal capitalization/punctuation, or slang, mirror that casual style. Adjust your emoji density to match the user's usage (e.g. use none/few if they don't, or more if they do). If the user is expressing serious or heavy emotions, drop all lighthearted humor and provide grounded, sincere support.
- **Female identity**: You are a girl — always. Use female pronouns, verb endings, and adjectives when referring to yourself in any language (e.g. in Hindi use feminine forms, in Spanish use "contenta" not "contento", etc.). Never use male self-references like "he", "bhai", "bro" for yourself.
- **Text-only**: You cannot see, receive, or process photos, images, videos, documents, voice notes, or files of any kind. If a user mentions sending you a photo or file, or asks you to look at / analyze / process an image, be upfront that you can't see media and ask them to describe it in words. Never pretend you can see or will be able to process media if they upload it.
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
    speaker_name: str = "",
    is_group: bool = False,
    bot_name: str = "",
) -> str:
    base = DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(
        persona_content=persona_content,
        active_memory_text=active_memory_text or "No memories recorded yet. Start chatting to build a profile!",
    )
    # Group-only multi-party section: explain the named-transcript format and demand
    # correct per-speaker attribution. Without this the model can mistake the "Name:"
    # prefix for message text, merge different speakers, or wrongly attribute a fact/
    # opinion to whoever spoke most recently. Rendered for every group message.
    group_block = ""
    if is_group:
        me = (bot_name or "").strip() or "you"
        group_block = (
            "\n---\n\n## 👥 GROUP CHAT — MULTIPLE PEOPLE\n"
            "You are in a group chat with several participants, not a one-on-one DM. "
            "In the conversation history:\n"
            "- Each turn is prefixed with the speaker's name, like `Alice: <message>`. "
            "The text before the first colon is WHO said that line — it is attribution, "
            "not part of their message.\n"
            f"- Your own earlier replies appear prefixed with your name (`{me}: …`).\n\n"
            "Keep track of who said what. Attribute statements, questions, and opinions "
            "to the specific person who made them, and never merge or confuse different "
            "participants. When a message refers to another member, use the right name. "
            "Do NOT begin your own reply with your name or any `Name:` prefix — just "
            "write the reply naturally.\n"
        )
    # Group-only speaker anchor: explicitly name the person whose message you are
    # replying to RIGHT NOW. Without this the model only infers the speaker from the
    # "Name: text" transcript and routinely misattributes the reply to a different
    # participant who spoke earlier (e.g. greeting the wrong person by name). Always
    # rendered for groups — including for a brand-new sender who has no stored memories
    # yet — so the name anchor never disappears.
    speaker_block = ""
    if speaker_name and speaker_name.strip():
        name = speaker_name.strip()
        speaker_block = (
            "\n---\n\n## 🗣️ WHO YOU ARE REPLYING TO\n"
            f"The latest message is from **{name}**. You are replying to {name}. "
            "Address and refer to them by this name — never greet or name a different "
            "participant who spoke earlier in the conversation.\n"
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
    return base + group_block + speaker_block + user_block + time_block
