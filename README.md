# coles-cli

Drive Coles shopping from the command line through a real Camoufox browser session.

The CLI uses a persistent local browser profile and a per-session background worker. It does not handle Coles credentials; login is completed manually in the opened browser and reused from the saved profile.

## Install

```bash
uv sync
uv run python -m camoufox fetch
```

## Quickstart

```bash
uv run coles auth status --json
uv run coles login --interactive --wait --timeout 300
uv run coles orders list --status current --json
uv run coles orders list past --json
uv run coles orders items 263592298 --from-status past --json
uv run coles products search Coles Lettuce Cos Baby Hearts "|" 2 Pack --result-size 96 --json
uv run coles products add Coles Lettuce Cos Baby Hearts "|" 2 Pack --index 1 --set-quantity 2 --json
uv run coles cart list --json
uv run coles cart set-quantity --index 1 --quantity 3 --json
uv run coles cart remove --index 1 --json
uv run coles cart checkout --json
uv run coles session stop
```

`shoppingcart` is accepted as an alias for `cart`.

## Commands

`--session <name>` and `--json` work on every command.

| Command | What it does |
|---|---|
| `login --interactive --wait --timeout 300` | Open Coles and wait while you log in manually. |
| `auth status` | Report whether the current session is authenticated. |
| `orders list --status current` | List active/current orders. |
| `orders list past` | List past orders. |
| `orders items <order-id>` | Open an order and list visible items. |
| `products search <query> --result-size N` | Search Coles products and return up to `N` results across search pages. Defaults to the first page of 48 results. |
| `products add <query> --index N --set-quantity Q` | Search Coles for `<query>`, add result `N`, and set its final trolley quantity. The query can be an exact product title or normal search terms. If the current page already has the same search query, it reuses that page instead of reloading. |
| `cart list` | Open the trolley drawer and list visible cart items. |
| `cart set-quantity --index N --quantity Q` | Set visible trolley item `N` from the cart list to final quantity `Q`. |
| `cart remove --index N` | Remove visible trolley item `N` from the cart list. |
| `cart checkout` | Continue through checkout and place the order when Coles presents the final place-order action. The output tells you to retrieve the order with `orders list --status current`. |
| `session stop` | Stop the background worker without deleting the saved profile. |
| `session clear` | Delete the local browser profile, including saved Coles login state. |

## Environment

- `COLES_CLI_SESSION`: default session name.
- `COLES_CLI_HOME`: state root, default `~/.coles-cli`.
- `COLES_CLI_HEADLESS`: set to `1`, `true`, or `yes` for headless mode.
- `COLES_CLI_LOG`: Python logging level, default `INFO`.
- `COLES_CLI_LIVE`: set to `1`, `true`, `yes`, or `on` to enable future live smoke tests.

## Development

```bash
uv sync
uv run pytest -v
uv run coles --help
```
