# System Setup & Deployment Guide

This guide provides step-by-step instructions to configure, run, and host the ThinkMate self-learning Telegram bot. It is written for newcomers: each step explains not just *what* to type but *why* it matters, so you can adapt the process to your own environment with confidence.

You will move through five stages — checking prerequisites, cloning and preparing the project, registering a Telegram bot, pointing ThinkMate at your LLM and database, and finally launching the application. The defaults shown here mirror the values in [`.env.example`](../.env.example), so if you ever want a quick reference for a setting, that file is the source of truth. For a deeper explanation of every variable and how to tune it, see the [configuration guide](development/configuration.md); for the bird's-eye view of how the pieces fit together, see the [architecture overview](architecture.md). To return to the project entry point at any time, head back to the [README](../README.md).

---

## 📋 Prerequisites

Before starting, ensure your system meets the following requirements:
*   **Python**: Version `3.12` or higher installed (matches `pyproject.toml`'s `requires-python`).
*   **Git**: Installed (for cloning the repository and managing version control).
*   **MongoDB**: An active MongoDB server or connection string (Atlas or local instance).
*   **LLM Provider**: Either an active API key (OpenAI, OpenRouter, Groq) or a running local inference server (LM Studio, Ollama).
*   **Telegram Client**: An active Telegram account to register the bot and obtain tokens.

These are the moving parts ThinkMate depends on at runtime: Python runs the bot, MongoDB stores its long-term memory, and the LLM provider supplies the language model that powers conversation and memory extraction. Telegram is the front door users talk through. Confirming all five up front avoids stalling partway through setup.

---

## 🛠️ Step 1: Clone the Repository & Configure Environment

ThinkMate uses the high-performance Python package manager **`uv`** for managing virtual environments, installing dependencies, and executing scripts. `uv` is chosen because it resolves and installs dependencies far faster than the traditional `pip`/`venv` combination while using the same familiar commands, which keeps onboarding quick.

1.  **Clone the code** to your local machine:
    ```bash
    git clone https://github.com/yourusername/ThinkMate.git
    cd ThinkMate
    ```

2.  **Create a virtual environment**:
    A virtual environment isolates ThinkMate's dependencies from the rest of your system, so project versions never clash with other Python tools. Using `uv`, create the virtual environment:
    ```bash
    uv venv
    ```

3.  **Activate the virtual environment**:
    Activation points your shell at the isolated environment you just created. Pick the command that matches your operating system:
    *   **Windows (Powershell / Command Prompt)**:
        ```powershell
        .venv\Scripts\activate
        ```
    *   **Linux / macOS (Bash / Zsh)**:
        ```bash
        source .venv/bin/activate
        ```

4.  **Install dependencies**:
    This pulls in every package ThinkMate needs, pinned in `requirements.txt`:
    ```bash
    uv pip install -r requirements.txt
    ```

4.  **Copy the configuration file**:
    ThinkMate reads its settings from a `.env` file that is never committed to version control, so your secrets stay local. Create a custom `.env` file by copying the template:
    ```bash
    cp .env.example .env
    ```

---

## 🤖 Step 2: Create a Telegram Bot

Every Telegram bot needs an identity and an API token, and those are issued by Telegram's own bot-management bot. To interact with the bot, you must register a token via Telegram's official **BotFather**:

1.  Open Telegram and search for `@BotFather`.
2.  Send the `/newbot` command to initiate the creation process.
3.  Choose a display name for your bot (e.g., `ThinkMate Companion`).
4.  Choose a unique username ending in `bot` (e.g., `ThinkMateAI_bot`).
5.  **Copy the API Token** generated (it looks like `1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
6.  Edit your `.env` file and paste the token:
    ```env
    TELEGRAM_BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ
    ```

This token is the credential ThinkMate uses to send and receive messages on behalf of your bot, so treat it like a password and keep it out of shared or public places.

---

## 🧠 Step 3: Configure Your LLM & Database Endpoints

ThinkMate is compatible with any inference service that supports the standard OpenAI Chat Completion protocol. That compatibility is intentional: it lets you start on a free local model and later switch to a hosted API without changing any code — only the values in your `.env` file change. This section wires up two things: where ThinkMate stores memory (MongoDB) and which model it talks to (the LLM provider).

### MongoDB Settings
Configure the connection to your MongoDB instance:
```env
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=thinkmate_db
```

### LLM Provider Options

Choose **one** of the options below based on whether you want to run a model locally or call a cloud API. Local options keep everything on your machine and cost nothing to run; the cloud option trades that for higher-quality models and zero local hardware requirements.

#### Option A: Local LLM (LM Studio)
1.  Download and open **LM Studio**.
2.  Download an instruction-tuned model (e.g., `Llama-3-8B-Instruct`).
3.  Navigate to the **Local Server** tab.
4.  Select your model, adjust configuration details (such as context length), and click **Start Server**.
5.  Set your `.env` file parameters:
    ```env
    LLM_BASE_URL=http://localhost:1234/v1
    LLM_API_KEY=lm-studio
    LLM_MODEL=lmstudio-community/Meta-Llama-3-8B-Instruct
    LLM_EXTRACTION_MODEL=lmstudio-community/Meta-Llama-3-8B-Instruct
    ```

#### Option B: Local LLM (Ollama)
1.  Download and install **Ollama**.
2.  Pull a suitable model:
    ```bash
    ollama pull llama3:8b
    ```
3.  Set your `.env` file parameters:
    ```env
    LLM_BASE_URL=http://localhost:11434/v1
    LLM_API_KEY=ollama
    LLM_MODEL=llama3:8b
    LLM_EXTRACTION_MODEL=llama3:8b
    ```

#### Option C: Cloud API (OpenAI)
1.  Obtain an API key from the [OpenAI Developer Platform](https://platform.openai.com/).
2.  Set your `.env` file parameters:
    ```env
    LLM_BASE_URL=https://api.openai.com/v1
    LLM_API_KEY=sk-proj-... # Your real OpenAI API Key
    LLM_MODEL=gpt-4o
    LLM_EXTRACTION_MODEL=gpt-4o-mini
    ```

---

## ⚙️ Step 4: Environment Variable Reference

The table below lists the core settings you are most likely to touch, along with their defaults. ThinkMate ships with sensible defaults for all of them, so you only need to change what your setup requires; the rest can be left alone. Open your `.env` file and review the following configurations:

| Parameter | Type | Default Value | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | String | *Required* | API Token received from `@BotFather`. |
| `LLM_BASE_URL` | URL | `http://localhost:1234/v1` | Base URL of the OpenAI-compatible HTTP server. |
| `LLM_API_KEY` | String | `none` | API authentication header. |
| `LLM_MODEL` | String | `gpt-4o` | Identifier of the LLM for main conversational interactions. |
| `LLM_EXTRACTION_MODEL` | String | `gpt-4o-mini` | Identifier of the LLM for sliding-window memory extraction. |
| `MONGODB_URI` | String | `mongodb://localhost:27017` | Connection URI for the MongoDB database instance. |
| `MONGODB_DB` | String | `thinkmate_db` | Target MongoDB database name. |
| `CHAT_BUFFER_MAX_CHARS` | Integer | `10000` | Max character count allowed in the buffer before extraction triggers. |
| `NEW_USER_EXTRACTION_CHARS` | Integer | `1000` | Lower buffer-char trigger applied to new/sparse users so their memory profile builds quickly. Capped at `CHAT_BUFFER_MAX_CHARS`. |
| `NEW_USER_MEMORY_THRESHOLD` | Integer | `5` | A user with fewer than this many stored memory items (facts + beliefs + events) is treated as "new" and uses `NEW_USER_EXTRACTION_CHARS`. |
| `CHAT_BUFFER_TRIM` | Integer | `10` | The count of oldest messages kept/trimmed from active buffer and summarized. |
| `USER_MEMORY_BUDGET_CHARS` | Integer | `4000` | Character-budget limit for the compiled memory profile. Exceeding this triggers compression. |
| `CHARS_PER_TOKEN` | Integer | `4` | Legacy char-to-token ratio; no longer drives output limits (the `max_tokens` cap was removed). |
| `MESSAGE_BATCH_DELAY_SECS` | Float | `1.5` | Delay in seconds the bot waits after receiving a message to batch rapid messages. |
| `MAX_BATCH_DELAY_SECS` | Float | `5.0` | Max seconds from first message in a batch before processing is forced. |
| `MAX_INPUT_CHARS` | Integer | `2500` | Inbound messages longer than this are ignored (anti-abuse — blocks pasted logs/essays), not a normal chat cap. |
| `MAX_RESPONSE_CHARS` | Integer | `2000` | Legacy soft reference; no longer drives a `max_tokens` cap. Reply length is governed by the system-prompt "Length" rule. |
| `PERSONA_FILE` | Path | `persona.md` | Location of the Markdown configuration defining the bot's tone. |

This reference covers the everyday settings; the full list, including group-chat, observability, and consolidation tuning, lives in [`.env.example`](../.env.example) and is explained in the [configuration guide](development/configuration.md).

---

## 🚀 Step 5: Start the Application

Once variables are configured, run the following verification checks:

1.  **Start Polling**:
    "Polling" means the bot repeatedly asks Telegram for new messages, which needs no public URL or webhook setup — ideal for local development. Using `uv run` to execute the application:
    ```bash
    uv run main.py
    ```
2.  Look for logging outputs in the terminal. These lines confirm each subsystem came up cleanly. The logs should report:
    *   Successful connection to the MongoDB client and database.
    *   Initialization of database indexes.
    *   Polling loop initialization for the bot's username.

3.  Open Telegram, search for your bot's username, and press **Start** (or send `/start`).
4.  Confirm the bot replies with the startup welcome message.

A successful welcome reply means the full loop — Telegram, the bot, the LLM, and MongoDB — is working end to end. From here, see the [architecture overview](architecture.md) to understand what happens on each message.
