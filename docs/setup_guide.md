# System Setup & Deployment Guide

This guide provides step-by-step instructions to configure, run, and host the ThinkMate self-learning Telegram bot.

---

## 📋 Prerequisites

Before starting, ensure your system meets the following requirements:
*   **Python**: Version `3.10` or higher installed.
*   **Git**: Installed (for cloning the repository and managing version control).
*   **LLM Provider**: Either an active API key (OpenAI, OpenRouter, Groq) or a running local inference server (LM Studio, Ollama).
*   **Telegram Client**: An active Telegram account to register the bot and obtain tokens.

---

## 🛠️ Step 1: Clone the Repository & Configure Environment

1.  **Clone the code** to your local machine:
    ```bash
    git clone https://github.com/yourusername/ThinkMate.git
    cd ThinkMate
    ```

2.  **Create and activate a virtual environment**:
    *   **Windows (Command Prompt / Powershell)**:
        ```powershell
        python -m venv venv
        .\venv\Scripts\activate
        ```
    *   **Linux / macOS (Bash / Zsh)**:
        ```bash
        python -m venv venv
        source venv/bin/activate
        ```

3.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

4.  **Copy the configuration file**:
    Create a custom `.env` file by copying the template:
    ```bash
    cp .env.example .env
    ```

---

## 🤖 Step 2: Create a Telegram Bot

To interact with the bot, you must register a token via Telegram's official **BotFather**:

1.  Open Telegram and search for `@BotFather`.
2.  Send the `/newbot` command to initiate the creation process.
3.  Choose a display name for your bot (e.g., `ThinkMate Companion`).
4.  Choose a unique username ending in `bot` (e.g., `ThinkMateAI_bot`).
5.  **Copy the API Token** generated (it looks like `1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
6.  Edit your `.env` file and paste the token:
    ```env
    TELEGRAM_BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ
    ```

---

## 🧠 Step 3: Configure Your LLM Endpoint

ThinkMate is compatible with any inference service that supports the standard OpenAI Chat Completion protocol. Here are configuration examples for various providers:

### Option A: Local LLM (LM Studio)
1.  Download and open **LM Studio**.
2.  Download an instruction-tuned model (e.g., `Llama-3-8B-Instruct` or `Mistral-7B-Instruct`).
3.  Navigate to the **Local Server** tab (the double-headed arrow icon on the left sidebar).
4.  Select your model, adjust configuration details (such as context length), and click **Start Server**.
5.  Set your `.env` file parameters:
    ```env
    LLM_BASE_URL=http://localhost:1234/v1
    LLM_API_KEY=lm-studio
    LLM_MODEL=lmstudio-community/Meta-Llama-3-8B-Instruct
    LLM_EXTRACTION_MODEL=lmstudio-community/Meta-Llama-3-8B-Instruct
    ```

### Option B: Local LLM (Ollama)
1.  Download and install **Ollama**.
2.  Pull a suitable model from the command line:
    ```bash
    ollama pull llama3:8b
    ```
3.  Start Ollama (runs by default on port `11434` with OpenAI compatibility on `/v1` routes).
4.  Set your `.env` file parameters:
    ```env
    LLM_BASE_URL=http://localhost:11434/v1
    LLM_API_KEY=ollama
    LLM_MODEL=llama3:8b
    LLM_EXTRACTION_MODEL=llama3:8b
    ```

### Option C: Cloud API (OpenAI)
1.  Obtain an API key from the [OpenAI Developer Platform](https://platform.openai.com/).
2.  Set your `.env` file parameters:
    ```env
    LLM_BASE_URL=https://api.openai.com/v1
    LLM_API_KEY=sk-proj-... # Your real OpenAI API Key
    LLM_MODEL=gpt-4o
    LLM_EXTRACTION_MODEL=gpt-4o-mini
    ```

### Option D: Cloud API (OpenRouter)
1.  Sign up for an API key on [OpenRouter](https://openrouter.ai/).
2.  Set your `.env` file parameters:
    ```env
    LLM_BASE_URL=https://openrouter.ai/api/v1
    LLM_API_KEY=sk-or-v1-... # Your OpenRouter API Key
    LLM_MODEL=meta-llama/llama-3-8b-instruct:free
    LLM_EXTRACTION_MODEL=meta-llama/llama-3-8b-instruct:free
    ```

---

## ⚙️ Step 4: Environment Variable Reference

Open your `.env` file and review the following configurations:

| Parameter | Type | Default Value | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | String | *Required* | API Token received from `@BotFather`. |
| `LLM_BASE_URL` | URL | `http://localhost:1234/v1` | Base URL of the OpenAI-compatible HTTP server. |
| `LLM_API_KEY` | String | `none` | API authentication header. |
| `LLM_MODEL` | String | `gpt-4o` | Identifier of the LLM for main conversational interactions. |
| `LLM_EXTRACTION_MODEL` | String | `gpt-4o-mini` | Identifier of the LLM for sliding-window memory extraction. |
| `CHAT_BUFFER_MAX` | Integer | `20` | Max number of messages allowed in the buffer before extraction triggers. |
| `CHAT_BUFFER_TRIM` | Integer | `10` | The count of oldest messages trimmed from active buffer and summarized. |
| `USER_MEMORY_BUDGET_CHARS` | Integer | `10000` | Character-budget limit for the compiled memory profile. Exceeding this triggers compression. |
| `CHARS_PER_TOKEN` | Integer | `4` | Character-to-token ratio for deriving token limits. |
| `MAX_INPUT_CHARS` | Integer | `1000` | Max message length in characters. Messages exceeding this are ignored. |
| `MAX_RESPONSE_CHARS` | Integer | `1000` | Max response length in characters. Used to cap model generations. |
| `PERSONA_FILE` | Path | `persona.md` | Location of the Markdown configuration defining the bot's tone. |

---

## 🚀 Step 5: Start the Application

Once variables are configured, run the following verification checks:

1.  **Initialize Database and Start Polling**:
    ```bash
    python main.py
    ```
2.  Look for logging outputs in the terminal. The logs should report:
    *   Successful connection to the database (`data/database.sqlite`).
    *   Creation of necessary tables if running for the first time.
    *   Polling loop initialization for the bot's username.

3.  Open Telegram, search for your bot's username, and press **Start** (or send `/start`).
4.  Confirm the bot replies with the startup welcome message.
