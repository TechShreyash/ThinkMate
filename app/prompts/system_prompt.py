DEFAULT_SYSTEM_PROMPT_TEMPLATE = """{persona_content}

---

## 🧠 MEMORIES OF THE USER:
Below are your current active memories of the user. Use them naturally and casually in conversation as outlined in the "Memory Integration Rules" above:

{active_memory_text}
"""

def build_system_prompt(persona_content: str, active_memory_text: str) -> str:
    return DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(
        persona_content=persona_content,
        active_memory_text=active_memory_text or "No memories recorded yet. Start chatting to build a profile!"
    )
