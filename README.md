# state-handles-mcp

Sessions are gone from MCP. This repo is a zero-dependency reference for
how stateful servers work now: a single-file Python MCP server (stdio)
implementing the 2026-07-28 stateless core with the
[SEP-2567](https://modelcontextprotocol.io/seps/2567-sessionless-mcp-via-explicit-state-handles)
explicit state-handle pattern (a working shopping-basket demo) and
[SEP-2549](https://modelcontextprotocol.io/seps/2549-ttl-for-list-results)
caching hints (`ttlMs` + `cacheScope`) on every list and read result.

The 2026 protocol line removed the session: SEP-2575 removed the
`initialize` handshake, SEP-2567 removed the `Mcp-Session-Id` header and
session-scoped state, and SEP-2549 added TTL hints so clients can cache
list results safely now that lists no longer vary per session. What
replaces session state is a tool-design pattern, not new wire format:
`create_basket()` returns an opaque `basket_id`, and the model threads
that string through every later call. This repo is that pattern, working
end to end, with a full stdio test harness.

## Tools

| Tool              | Purpose                                          |
| ----------------- | ------------------------------------------------ |
| `create_basket`   | Mint an opaque `basket_id` (optional label)      |
| `add_item`        | Add `quantity` of an `sku` to a basket           |
| `remove_item`     | Remove an `sku` (no-op if absent)                |
| `view_basket`     | Read contents                                    |
| `checkout_basket` | Freeze and return final contents                 |
| `destroy_basket`  | Release the state                                |
| `list_baskets`    | Recover live handles                             |

Plus a `basket://{basket_id}` resource template for reading one basket
as a resource.

## Run it

```sh
python3 state_handles_mcp.py
```

Client configuration (stdio):

```json
{
  "mcpServers": {
    "basket": {
      "command": "python3",
      "args": ["/path/to/state_handles_mcp.py"]
    }
  }
}
```

## What it demonstrates, mapped to the specs

- **Explicit handles (SEP-2567).** `create_basket` returns
  `bsk_` + 128 bits of CSPRNG hex, opaque and unguessable, per the SEP's
  guidance for unauthenticated servers where the handle is a bearer
  token. Two baskets are fully isolated: the model, not the session,
  decides what is shared and what is not - the cardinality fix the SEP
  argues for.
- **Durability is documented where the model sees it.** The
  `create_basket` tool description states the 30-minute idle expiry and
  in-memory lifetime, following the SEP's "put the policy in the
  `create_*` tool's description" guidance.
- **Useful expiry errors.** An expired handle returns
  `basket 'bsk_…' has expired … Create a new one with create_basket` -
  a tool execution error the model can recover from, not an opaque
  failure.
- **Caching hints (SEP-2549).** `tools/list`, `prompts/list`,
  `resources/list` and `resources/templates/list` return
  `ttlMs: 60000, cacheScope: "public"` (they are identical for every
  caller); `resources/read` on a basket returns
  `ttlMs: 5000, cacheScope: "private"` (user-specific state, mirrors
  HTTP `Cache-Control: private`).
- **Session-independent lists (SEP-2567 §session-independent list
  endpoints).** `tools/list` returns the same result before and after
  state changes - verified by test.
- **Modern + legacy handshakes (SEP-2575).** Answers stateless
  `server/discover` and still serves pre-2026-07-28 clients through a
  legacy `initialize`.
- **Error conventions.** Unknown resource URIs return `-32602`
  ([SEP-2164](https://modelcontextprotocol.io/seps/2164-standardize-resource-not-found-error-code)
  - note this SEP changed the recommended code from `-32002` to
  `-32602`); bad arguments are tool-level `isError` results
  ([SEP-1303](https://modelcontextprotocol.io/seps/1303-input-validation-errors-as-tool-execution-errors));
  tool names follow
  [SEP-986](https://modelcontextprotocol.io/seps/986-specify-format-for-tool-names)
  (1-64 chars, `[A-Za-z0-9_./-]`).

## Security notes

- Handles carry 128 bits of entropy and are bound to lifetimes
  (30-minute idle expiry); the SEP warns handles end up in chat logs and
  prompts, so they are treated as bearer tokens and expire.
- No network access, no filesystem access, no environment reads beyond
  the TTL override used by the tests. State is in-memory only.
- Inputs are bounded: sku/label lengths and quantity ranges validated
  before any state change; all failures are tool-level errors with
  actionable messages.

## Tests

29 stdlib `unittest` tests drive the real server over stdio as an MCP
client: both handshakes, TTL/cacheScope on every cacheable method, list
stability across state changes, SEP-986 name conformance, handle
opacity/uniqueness/entropy, the full basket lifecycle (add, view,
remove, checkout freeze, destroy, list recovery), isolation between
baskets, expiry (short-TTL server via `STATE_HANDLES_BASKET_TTL`),
expiry refresh on activity, SEP-2164 error codes, and argument
validation as tool errors. CI runs them on Ubuntu 24.04 and Windows,
Python 3.9 and 3.13.

```sh
python3 -m unittest -v
```

Honest limits: baskets are in-memory per process (a restart loses them -
the `create_basket` description says so); this is a reference for the
pattern, not a production cart; no authentication layer, so the
`(handle, auth_context)` authorization check from SEP-2567 is out of
scope by design.

## Sources

- [SEP-2567: Sessionless MCP via Explicit State Handles](https://modelcontextprotocol.io/seps/2567-sessionless-mcp-via-explicit-state-handles) (basket example and handle guidance quoted from it)
- [SEP-2549: TTL for List Results](https://modelcontextprotocol.io/seps/2549-ttl-for-list-results) (`CacheableResult`, `ttlMs`, `cacheScope`)
- [SEP-2575: Stateless MCP](https://modelcontextprotocol.io/seps/2575-stateless-mcp), [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [SEP-2164](https://modelcontextprotocol.io/seps/2164-standardize-resource-not-found-error-code), [SEP-1303](https://modelcontextprotocol.io/seps/1303-input-validation-errors-as-tool-execution-errors), [SEP-986](https://modelcontextprotocol.io/seps/986-specify-format-for-tool-names)
- All fetched 2026-11-05.

## License

MIT
