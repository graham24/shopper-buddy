# Shopper Buddy — project instructions

An MCP server that is purely a data layer for grocery shopping: it stores receipt
history and a freeform health profile, computes purchase cadence, and pushes items
into a real Kroger / City Market cart. Workflows (receipt imports, the weekly order)
live with the client, not here — this repo has no skills.

Personal-use project. Pragmatic and flat over abstract — see the global instructions.

## The one architectural rule

**The server holds no intelligence.** It stores purchases, does cadence arithmetic,
and calls the Kroger API. It doesn't decide what's a staple, what's due, or what goes
in the cart — it returns raw numbers and the caller judges. It makes **zero
LLM/Anthropic API calls** — receipt parsing, product matching, and health-based
judgment all happen in the conversation, and Claude sends back structured results.

If you're tempted to add an `anthropic` dependency or a prompt string to this repo,
that logic belongs in a tool docstring instead. Docstrings are the only instructions
the calling model gets, so they carry real weight — write them as guidance, not
description.

## Layout

| File | Role |
|---|---|
| `server.py` | MCP tool definitions. Bodies stay thin — delegate to the modules below. |
| `kroger.py` | Kroger API client. The only module that makes network calls. |
| `trends.py` | Cadence math over receipt history. Pure functions, no I/O beyond reads. |
| `planner.py` | The cart push log. |
| `models.py` | SQLModel tables. |
| `db.py` | Engine + `session()` contextmanager + `init_db()`. |
| `auth_cli.py` | One-time browser OAuth on `localhost:8734`. |

## Conventions

- Data access always goes through `db.session()`, never a bare `Session(engine)`.
  The DB URL comes from `SHOPPER_DB_URL` so the store can move later.
- `normalized_name` is the join key for all trend logic: lowercase, singular, no brand
  or size. Claude supplies it at ingest. Nothing else should be used to group items.
- The Kroger cart can't be read back, so `CartPush`/`CartPushItem` is the only record
  of what was ordered. `add_to_cart` logs on success; keep it that way, or callers
  can't tell what was already ordered and will double-order.
- Keep per-request state out of module globals — the only intentional caches are the
  client-credentials token in `kroger.py` and the store `location_id` on `Profile`.
  This is what keeps a future switch to `mcp.run(transport="streamable-http")` cheap.
- Secrets live in `.env` (gitignored). Never hardcode Kroger credentials.

## Kroger API gotchas

These cost real debugging time.

- Cart writes use the **Public** Cart API (`PUT /v1/cart/add`), not the Partner-tier
  `POST /v1/carts`. We aren't approved for that tier.
- **The cart is write-only.** No read, no remove, no undo. Always confirm a list with
  the user before `add_to_cart`.
- Refresh tokens **rotate on use**. A stored token can be silently dead — that's why
  `kroger_status` does a live check instead of just looking for a stored row.
- UPCs must be zero-padded to 13-digit GTINs; receipt SKUs are usually shorter.

## Receipt data

- `record_receipt` does no de-duplication. Importing the same trip twice silently
  doubles every trend.

## Testing

```bash
./venv/bin/python -m pytest
```

Tests mock HTTP at the transport layer with `respx` and never touch the real Kroger
API or your real cart. `tests/conftest.py` points the app at a temp DB and fake
credentials *before* importing `db`/`kroger`, since both read env at import time.

Anything added to `kroger.py` needs a test. Per global instructions tests aren't the
default here, but this repo has a suite because the Kroger integration is remote,
credentialed, and its failure mode is a wrong real-world cart.

## Don't

- Don't commit — leave changes uncommitted for review.
- Don't add skills or workflow docs. This repo is the data layer only.
- Don't build a frontend. Claude is the interface; that's the whole design.
- Don't call `add_to_cart` to "verify" something works. It mutates the real cart and
  cannot be undone through the API.
