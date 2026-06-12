import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load environment variables from .env if present
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()

class Config(BaseModel):
    TELEGRAM_BOT_TOKEN: str = Field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    LLM_BASE_URL: str = Field(default_factory=lambda: os.getenv("LLM_BASE_URL", "http://localhost:1234/v1"))
    LLM_API_KEY: str = Field(default_factory=lambda: os.getenv("LLM_API_KEY", "none"))
    LLM_MODEL: str = Field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o"))
    LLM_EXTRACTION_MODEL: str = Field(default_factory=lambda: os.getenv("LLM_EXTRACTION_MODEL", ""))
    CHAT_BUFFER_MAX: int = Field(default_factory=lambda: int(os.getenv("CHAT_BUFFER_MAX", "20")))
    CHAT_BUFFER_TRIM: int = Field(default_factory=lambda: int(os.getenv("CHAT_BUFFER_TRIM", "10")))
    USER_MEMORY_BUDGET_CHARS: int = Field(default_factory=lambda: int(os.getenv("USER_MEMORY_BUDGET_CHARS", "10000")))
    CHARS_PER_TOKEN: int = Field(default_factory=lambda: int(os.getenv("CHARS_PER_TOKEN", "4")))
    MAX_INPUT_CHARS: int = Field(default_factory=lambda: int(os.getenv("MAX_INPUT_CHARS", "1000")))
    MAX_RESPONSE_CHARS: int = Field(default_factory=lambda: int(os.getenv("MAX_RESPONSE_CHARS", "1000")))
    PERSONA_FILE: str = Field(default_factory=lambda: os.getenv("PERSONA_FILE", "persona.md"))

config = Config()
