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
- `.local/service.log` and `.local/service.pid`: local runtime log and PID files for the helper commands.

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
- Set `codex_execution_mode`. Recommended: `full-auto`.
- Keep adding `[[bots]]` blocks if you need more bot + workdir pairs.
- Do not commit `config.toml`; it is already ignored by Git.

## Run

```bash
make start
```

Useful commands:

```bash
make status
make logs
make stop
make restart
```

Run in the foreground:

```bash
make run
```

If you want more verbose logs during debugging:

```bash
make debug
make debug-start
```

The underlying script is `./scripts/service.sh`, so you can also run:

```bash
./scripts/service.sh start
./scripts/service.sh stop
./scripts/service.sh status
./scripts/service.sh logs
```

## Telegram commands

- `/start` or `/help`: show usage.
- `/status`: show the current Codex thread id for this bot chat.
- `/reset`: clear the saved Codex thread id and start a fresh context next time.
- `/whoami`: show your Telegram username and numeric user id.

Any other text message is forwarded to Codex.

## Notes

- This service only handles private chats.
- The bridge keeps one long-lived `codex app-server` session per configured bot. Telegram messages become turns inside that session, so there is less per-message startup overhead than the older `codex exec` approach.
- Long-running Codex tasks stream `codex app-server` events back into Telegram. The bot keeps one English status message updated and incrementally edits the reply as agent message text arrives.
- On startup the service validates the configured `codex` binary and every Telegram bot token.
- Username-based access control depends on the Telegram username staying unchanged. If the username
  changes, update `config.toml`.
- Use `/whoami` once from Telegram if you want to copy your numeric `telegram_user_id` into
  `config.toml`.
- `skip_git_repo_check` is still accepted in the config for backward compatibility, but the current `app-server` backend does not map that field to a verified Codex setting.
- `codex_execution_mode = "full-auto"` is the recommended write-capable mode for this bridge. In this project it asks `app-server` for `approvalPolicy = "never"` plus workspace-write sandboxing when starting or resuming a thread.
- `codex_execution_mode = "danger-full-access"` asks for `approvalPolicy = "never"` plus full-access sandboxing and should only be used on a trusted machine.
- If an older saved Codex thread still behaves like a read-only session after you change this setting, send `/reset` in Telegram to start a fresh thread.
