# Coles CLI Agent Guide

Use this guide when an agent needs to operate `/Users/darwin/Seafile/VsCode/darwincr/coles-cli`.

## Operating Rules

- Run commands from `/Users/darwin/Seafile/VsCode/darwincr/coles-cli`.
- Use `uv run coles ...` unless the CLI has been installed globally.
- Prefer `--json` for machine-readable output.
- Before using a command with unfamiliar options, run that command's `--help` switch first.
- The CLI uses a persistent Camoufox browser profile and a background worker per session.
- Use `coles session stop` if the browser worker needs to be closed without deleting login state.
- Use `coles session clear` only when login/profile state should be deleted.
- Do not run checkout unless the user explicitly authorizes placing a real Coles order.
- After checkout, retrieve the order state with `orders list --status current`.

## Basic Sign-In

```bash
uv run coles login --interactive --wait --timeout 300 --json
```

Complete sign-in in the Camoufox window. The saved browser profile is reused by later commands.

## Help Commands

### General

- `uv run coles --help`

### Session

- `uv run coles session --help`
- `uv run coles session stop --help`
- `uv run coles session clear --help`

### Authentication

- `uv run coles login --help`
- `uv run coles auth --help`
- `uv run coles auth status --help`
- `uv run coles auth interactive --help`

### Orders

- `uv run coles orders --help`
- `uv run coles orders list --help`
- `uv run coles orders items --help`

### Products

- `uv run coles products --help`
- `uv run coles products search --help`
- `uv run coles products add --help`

### Cart

- `uv run coles cart --help`
- `uv run coles cart list --help`
- `uv run coles cart checkout --help`

### Shoppingcart Alias

- `uv run coles shoppingcart --help`
- `uv run coles shoppingcart list --help`
- `uv run coles shoppingcart checkout --help`
