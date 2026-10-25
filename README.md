# telos

A small, self-modifying agent harness built around the idea of a **recursive language model (RLM)**. The model gets one tool, a persistent Python REPL, and uses it to explore, extend and rewrite its own environment (this directory), then restart itself to pick up the changes.

On the very first run, the model is welcomed into its "environment" and asked to set itself up in collaboration with you: learn your name and pronouns, shape its own prompt and tools, and so on.

## Features

- **Single `python3` tool.** The model runs arbitrary Python in a worker process. Variables persist between calls within a session, but not across a restart or a timeout.
- **Recursion with `llm_query(prompt)`.** Inside the REPL the model can spawn a sub-session of itself. Children run synchronously, have their own Python state, and receive the parent's transcript plus any extra `ctx` you pass. Recursion is capped at depth 3.
- **Self-restart with `restart()`.** The model can edit `agents.py` or `cli.py`, call `restart()`, and the process re-execs itself and resumes the same conversation (`--resume`) with the new code.
- **Persistent conversations.** Every conversation is saved as JSON in `convos/` and can be continued by ID.
- **Deadlines.** Tool calls default to a 30 s timeout (configurable per call). Model requests have a 60 s limit, with automatic retry on HTTP 429.
- **Secret hiding.** The API key is stripped from the `env` dict exposed to the model's REPL.

## Requirements

- Python 3.10 or newer (the CLI uses `match`)
- An [OpenRouter](https://openrouter.ai) API key
- `requests`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .

cp .env.example .env             # Windows: copy .env.example .env
```

Then edit `.env`:

```env
_UNDERLYING_OPENROUTER_API_KEY=sk-or-...
MODEL=stealth/ox-alpha
```

| Variable | Description |
| --- | --- |
| `_UNDERLYING_OPENROUTER_API_KEY` | Your OpenRouter API key. |
| `MODEL` | Any OpenRouter model ID that supports tool calling. |

`.env` is gitignored. Values can also come from the real environment, and `.env` takes precedence.

## Usage

```bash
python cli.py                          # start a new conversation
python cli.py -c <conversation-id>     # continue a saved conversation
python cli.py -c <id> --resume         # resume a pending model turn (used internally by restart())
```

At the `>` prompt, type a message and press Enter. Type `quit` or `exit` (or press Ctrl+D / Ctrl+C) to leave. The conversation ID is a UUID, and it's also the filename of the transcript in `convos/`.

The first time telos runs (tracked by `_first_time_running` in `memory.json`), it sends the model a setup prompt before handing control to you.

## How it works

```
cli.py      REPL loop, conversation load/save, first-run setup
agents.py   model calls, python worker, llm_query, restart
memory.json small persistent state (currently just the first-run flag)
convos/     saved conversations (gitignored)
```

1. `cli.py` appends your message to `agents.CONTEXT` and calls `agents._call_model`.
2. The model is called through OpenRouter's chat completions API with the `python3` tool and reasoning enabled.
3. Tool calls are executed in a separate `spawn`ed worker process that exposes `os`, `sys`, `json`, `time`, `env`, `ctx` (the transcript so far), `llm_query` and `restart`.
4. Results are appended to the context and the loop continues until the model replies without a tool call.
5. If `restart()` was called, the context is saved and the process is replaced via `os.execv` with `cli.py -c <id> --resume`.

## Caveats

- **The model has full code execution on your machine.** There is no sandbox beyond a separate process. It can read and write files, make network requests, and rewrite its own source. Run it somewhere you're comfortable with that.
- `system_prompt` in `agents.py` is intentionally `None`. It's left for the model (or you) to fill in.
- Sub-sessions make additional model requests, so deep recursion can get expensive.

## License

[MIT](LICENSE)
