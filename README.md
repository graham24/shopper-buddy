# Shopper Buddy

An MCP server that turns Claude into a grocery shopper who knows your household:
it tracks what you buy from past receipts, biases choices with your health profile,
and pushes the finished list straight into your Kroger / City Market cart.

There is no frontend and no app login. Claude is the interface.

## How it works

```
Claude (chat)  ──MCP stdio──▶  shopper-buddy  ──▶  api.kroger.com
                                     │
                                     ▼
                                shopper.db
```

All the intelligence lives in the conversation. The server makes **no LLM calls** —
it stores purchases, does cadence arithmetic, and talks to Kroger. Claude parses
your receipts, picks products against your health profile, and calls the tools.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -e .
cp .env.example .env      # fill in your Kroger credentials
./venv/bin/python auth_cli.py
```

`auth_cli.py` opens a browser once, you log into Kroger, and the refresh token is
stored in `shopper.db`. Kroger requires this for cart writes — product search works
without it. Refresh tokens rotate on use, so if cart pushes start failing with
"invalid refresh_token", just run it again.

The redirect URI `http://localhost:8734/callback` must be registered in the
[Kroger developer portal](https://developer.kroger.com) for your credentials.

Register with Claude Code:

```bash
claude mcp add shopper-buddy -- /absolute/path/to/shopper-buddy/venv/bin/python \
                                 /absolute/path/to/shopper-buddy/server.py
```

## Using it

Just talk to Claude:

- *"Here's my receipt from Saturday"* (paste or drop an image) → it parses and records it
- *"What am I due to restock?"* → cadence analysis over your history
- *"I'm trying to cut sodium and hit 150g protein a day"* → saved to the health profile
- *"Build my list for this week and add it to my cart"* → cadence → product matching →
  your real Kroger cart, after showing you the list

## Tools

| Tool | Purpose |
|---|---|
| `get_shopping_context` | Health profile, preferences, store, trip cadence |
| `update_shopping_context` | Set health profile / shopping notes / zip |
| `record_receipt` | Store a parsed receipt |
| `list_receipts` / `get_receipt` | Browse history |
| `get_purchase_trends` | Per-item cadence: how often, how much, how overdue |
| `kroger_status` | Connection and resolved store |
| `search_products` | Search the store catalog |
| `resolve_known_upcs` | Skip search for items you've bought before |
| `add_to_cart` | Push to your real Kroger cart (logs what it sent) |
| `get_cart_history` | What you ordered previously |

### "Fill my cart for the week"

The server returns data; Claude makes the calls. `get_purchase_trends` gives raw
cadence per item (times bought, average gap, gap variability, days until due),
`get_cart_history` shows what was recently pushed, and `resolve_known_upcs` maps
repeat buys to the exact UPC. Claude decides what's due, picks the rest against your
health profile, shows you the list, and only pushes once you confirm.

It needs history to work — a few weeks of receipts is enough for the weekly basics.

## Development

```bash
./venv/bin/pip install -e ".[dev]"
./venv/bin/python -m pytest
```

Tests mock HTTP with `respx` and never touch the real Kroger API or your cart.

`CLAUDE.md` carries the project conventions and the Kroger API gotchas.

## Notes

- The Kroger cart API is **write-only** — items can be added, but not read back or
  removed. Confirm lists before pushing.
- `shopper.db` and `.env` are gitignored; credentials and tokens never leave your machine.
- The Kroger integration was ported from the sibling `fitness-app` repo's `src/api/kroger.ts`.
