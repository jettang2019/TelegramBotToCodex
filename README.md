# Telegram Bot To Codex

Use one or more Telegram bots as a thin bridge to `codex` CLI. Each bot is pinned to one working
directory and one allowed Telegram account from a local config file.

## What this project does

- Supports multiple Telegram bots in one process.
- Pins each bot to a single `workdir` to avoid cross-directory context problems.
- Persists Codex thread ids in a local JSON state file instead of a database.
- Restricts each bot to a configured Telegram username and optionally a numeric Telegram user id.

## What it does not do

- No database.
- No workdir switching inside the same bot.
- No attempt to recreate the full Codex terminal UI inside Telegram.

## Layout

- `config.example.toml`: checked-in example config.
- `config.toml`: local runtime config, ignored by Git.
- `.local/state.json`: local thread and polling state, ignored by Git.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

Then edit the local `config.toml` file in the repo root:

- Fill each bot `token`.
- Set an absolute `workdir`.
- Set `telegram_username` and keep the `@` or omit it, both work.
- Optionally add `telegram_user_id` for stronger access control.
- Keep adding `[[bots]]` blocks if you need more bot + workdir pairs.
- Do not commit `config.toml`; it is already ignored by Git.

## Run

```bash
python -m telegram_bot_to_codex --config config.toml
```

## Telegram commands

- `/start` or `/help`: show usage.
- `/status`: show the current Codex thread id for this bot chat.
- `/reset`: clear the saved Codex thread id and start a fresh context next time.
- `/whoami`: show your Telegram username and numeric user id.

Any other text message is forwarded to Codex.

## Notes

- This service only handles private chats.
- On startup the service validates the configured `codex` binary and every Telegram bot token.
- Username-based access control depends on the Telegram username staying unchanged. If the username
  changes, update `config.toml`.
- Use `/whoami` once from Telegram if you want to copy your numeric `telegram_user_id` into
  `config.toml`.
- If the target `workdir` is not a Git repository, leave `skip_git_repo_check = true`.
