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
    MONGODB_URI: str = Field(default_factory=lambda: os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    MONGODB_DB: str = Field(default_factory=lambda: os.getenv("MONGODB_DB", "thinkmate_db"))
    CHAT_BUFFER_MAX_CHARS: int = Field(default_factory=lambda: int(os.getenv("CHAT_BUFFER_MAX_CHARS", "10000")))
    CHAT_BUFFER_TRIM: int = Field(default_factory=lambda: int(os.getenv("CHAT_BUFFER_TRIM", "10")))
    USER_MEMORY_BUDGET_CHARS: int = Field(default_factory=lambda: int(os.getenv("USER_MEMORY_BUDGET_CHARS", "4000")))
    CHARS_PER_TOKEN: int = Field(default_factory=lambda: int(os.getenv("CHARS_PER_TOKEN", "4")))
    MESSAGE_BATCH_DELAY_SECS: float = Field(default_factory=lambda: float(os.getenv("MESSAGE_BATCH_DELAY_SECS", "1.5")))
    MAX_BATCH_DELAY_SECS: float = Field(default_factory=lambda: float(os.getenv("MAX_BATCH_DELAY_SECS", "5.0")))
    RATE_LIMIT_MAX_REQUESTS: int = Field(default_factory=lambda: int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "5")))
    RATE_LIMIT_WINDOW_SECS: float = Field(default_factory=lambda: float(os.getenv("RATE_LIMIT_WINDOW_SECS", "10.0")))
    MAX_QUEUED_MESSAGES: int = Field(default_factory=lambda: int(os.getenv("MAX_QUEUED_MESSAGES", "10")))
    MAX_INPUT_CHARS: int = Field(default_factory=lambda: int(os.getenv("MAX_INPUT_CHARS", "1000")))
    MAX_RESPONSE_CHARS: int = Field(default_factory=lambda: int(os.getenv("MAX_RESPONSE_CHARS", "1000")))
    PERSONA_FILE: str = Field(default_factory=lambda: os.getenv("PERSONA_FILE", "persona.md"))

config = Config()
