# Codex Limits Overlay

Compact always-on-top Windows overlay for OpenAI Codex usage limits.

## Features

- Always-on-top frameless overlay.
- Shows 5-hour and weekly Codex usage limits.
- Uses local codex app-server.
- No manual token copying.
- Dark / light / auto theme.
- Size presets.
- Refresh interval menu.
- Tray menu and right-click menu.

## Requirements

- Windows.
- Python 3.10+.
- Node.js + npm.
- Codex CLI installed with:

```powershell
npm install -g @openai/codex
```

- Codex must be logged in and have valid auth in `CODEX_HOME`.

## Development Setup

```powershell
py -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python codex_limits_overlay.py
```

## Important Notes

- The app does not store OpenAI tokens.
- It reads limits through local Codex app-server.
- Codex CLI is required and is not bundled with the app.
- If multiple accounts are used by swapping `auth.json`, use Refresh now to re-read the active account.
