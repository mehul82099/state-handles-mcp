#!/usr/bin/env python3
"""state-handles-mcp - a zero-dependency MCP server demonstrating the
post-session stateless pattern on the 2026-07-28 protocol:

- SEP-2575 stateless core: server/discover, protocol version per request,
  legacy initialize handshake still answered for older clients.
- SEP-2567 sessionless state via explicit handles: baskets are created by
  create_basket(), which returns an opaque, unguessable basket_id; every
  other tool takes that id as an ordinary argument. No session state.
- SEP-2549 list caching hints: tools/list, prompts/list, resources/list,
  resources/templates/list and resources/read carry ttlMs + cacheScope.
- SEP-2164: unknown resource URIs fail with -32602 (Invalid params).
- SEP-1303: bad arguments are tool execution errors (isError), not
  protocol errors.

Run: python3 state_handles_mcp.py   (stdio JSON-RPC, one message per line)
Env: STATE_HANDLES_BASKET_TTL overrides the 30-minute basket idle TTL
(seconds, float) - used by the test suite to exercise expiry.
"""
import json
import os
import secrets
import sys
import time

PROTOCOL_VERSION = "2026-07-28"
LEGACY_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
SERVER_INFO = {"name": "state-handles-mcp", "version": "1.0.0"}
LIST_TTL_MS = 60_000          # tool/prompt/resource lists are static
READ_TTL_MS = 5_000           # basket reads change often
BASKET_TTL_SECONDS = float(os.environ.get("STATE_HANDLES_BASKET_TTL", 30 * 60))
HANDLE_BYTES = 16             # 128 bits of entropy (SEP-2567, unauthenticated)
MAX_SKU_LEN = 200
MAX_QTY = 100_000

ERR_INVALID_PARAMS = -32602   # also the SEP-2164 resource-not-found code
ERR_METHOD_NOT_FOUND = -32601
ERR_PARSE = -32700


class UnknownHandle(Exception):
    pass


class ExpiredHandle(Exception):
    pass


class CheckedOut(Exception):
    pass


class BasketStore:
    """In-memory basket state keyed by explicit handles. The server owns the
    state; the client holds a name for it (SEP-2567)."""

    def __init__(self, ttl_seconds=BASKET_TTL_SECONDS):
        self.ttl = ttl_seconds
        self._baskets = {}

    def create(self, label=None):
        handle = "bsk_" + secrets.token_hex(HANDLE_BYTES)
        now = time.monotonic()
        self._baskets[handle] = {
            "label": label, "items": {}, "created": now, "touched": now,
            "checked_out": False,
        }
        return handle

    def _live(self, handle):
        b = self._baskets.get(handle)
        if b is None:
            raise UnknownHandle(handle)
        if time.monotonic() - b["touched"] > self.ttl:
            del self._baskets[handle]
            raise ExpiredHandle(handle)
        b["touched"] = time.monotonic()
        return b

    def view(self, handle):
        b = self._live(handle)
        return {"basket_id": handle, "label": b["label"],
                "items": [{"sku": s, "quantity": q}
                          for s, q in sorted(b["items"].items())],
                "item_count": sum(b["items"].values()),
                "checked_out": b["checked_out"]}

    def add(self, handle, sku, quantity):
        b = self._live(handle)
        if b["checked_out"]:
            raise CheckedOut(handle)
        b["items"][sku] = b["items"].get(sku, 0) + quantity
        return self.view(handle)

    def remove(self, handle, sku):
        b = self._live(handle)
        if b["checked_out"]:
            raise CheckedOut(handle)
        b["items"].pop(sku, None)
        return self.view(handle)

    def checkout(self, handle):
        b = self._live(handle)
        b["checked_out"] = True
        return self.view(handle)

    def destroy(self, handle):
        self._live(handle)
        del self._baskets[handle]
        return {"basket_id": handle, "destroyed": True}

    def list_live(self):
        out = []
        for h in list(self._baskets):
            try:
                b = self._live(h)
            except ExpiredHandle:
                continue
            out.append({"basket_id": h, "label": b["label"],
                        "item_count": sum(b["items"].values()),
                        "checked_out": b["checked_out"]})
        return out


STORE = BasketStore()


def tool(text, structured):
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": structured}


def tool_error(text):
    # SEP-1303: argument/domain failures are tool execution errors.
    return {"content": [{"type": "text", "text": text}], "isError": True}


TOOLS = [
    {"name": "create_basket",
     "description": "Create a new shopping basket and return its opaque "
                    "basket_id handle. Thread the handle through every "
                    "follow-up call. Baskets expire after "
                    f"{int(BASKET_TTL_SECONDS)}s idle (create a new one to "
                    "recover); they are in-memory and lost on server restart.",
     "inputSchema": {"type": "object", "properties": {
         "label": {"type": "string", "description": "Optional label.",
                   "maxLength": MAX_SKU_LEN}},
         "additionalProperties": False}},
    {"name": "add_item",
     "description": "Add quantity units of an sku to basket_id.",
     "inputSchema": {"type": "object", "required": ["basket_id", "sku"],
         "properties": {
             "basket_id": {"type": "string"},
             "sku": {"type": "string", "maxLength": MAX_SKU_LEN},
             "quantity": {"type": "integer", "minimum": 1,
                          "maximum": MAX_QTY, "default": 1}},
         "additionalProperties": False}},
    {"name": "remove_item",
     "description": "Remove an sku entirely from basket_id (no-op if absent).",
     "inputSchema": {"type": "object", "required": ["basket_id", "sku"],
         "properties": {"basket_id": {"type": "string"},
                        "sku": {"type": "string"}},
         "additionalProperties": False}},
    {"name": "view_basket",
     "description": "Read the current contents of basket_id.",
     "inputSchema": {"type": "object", "required": ["basket_id"],
         "properties": {"basket_id": {"type": "string"}},
         "additionalProperties": False}},
    {"name": "checkout_basket",
     "description": "Check out basket_id: returns the final contents and "
                    "freezes the basket (further mutations fail).",
     "inputSchema": {"type": "object", "required": ["basket_id"],
         "properties": {"basket_id": {"type": "string"}},
         "additionalProperties": False}},
    {"name": "destroy_basket",
     "description": "Destroy basket_id and release its state.",
     "inputSchema": {"type": "object", "required": ["basket_id"],
         "properties": {"basket_id": {"type": "string"}},
         "additionalProperties": False}},
    {"name": "list_baskets",
     "description": "List live baskets (id, label, item count, checked_out) "
                    "so a client can recover handles it lost track of.",
     "inputSchema": {"type": "object", "properties": {},
                     "additionalProperties": False}},
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}

RESOURCE_TEMPLATE = {
    "uriTemplate": "basket://{basket_id}",
    "name": "basket",
    "description": "Live contents of one basket, addressed by its handle.",
    "mimeType": "application/json",
}


def _handle_error(callable_, handle):
    try:
        return callable_()
    except UnknownHandle:
        return tool_error(f"Unknown basket_id '{handle}'. Create one with "
                          "create_basket or recover ids with list_baskets.")
    except ExpiredHandle:
        return tool_error(f"Basket '{handle}' has expired after "
                          f"{int(BASKET_TTL_SECONDS)}s idle. Create a new "
                          "one with create_basket.")
    except CheckedOut:
        return tool_error(f"Basket '{handle}' is already checked out; "
                          "create a new basket to keep shopping.")


def call_tool(name, arguments):
    arguments = arguments or {}
    if name not in TOOL_BY_NAME:
        return None  # caller maps to -32602
    schema = TOOL_BY_NAME[name]["inputSchema"]
    for req in schema.get("required", []):
        if req not in arguments:
            return tool_error(f"Missing required argument '{req}'.")
    if name == "create_basket":
        label = arguments.get("label")
        if label is not None and (not isinstance(label, str)
                                  or len(label) > MAX_SKU_LEN):
            return tool_error("label must be a string of at most "
                              f"{MAX_SKU_LEN} characters.")
        handle = STORE.create(label)
        return tool(f"Created basket {handle}.",
                    {"basket_id": handle, "label": label})
    handle = arguments.get("basket_id", "")
    if name == "add_item":
        sku = arguments.get("sku", "")
        qty = arguments.get("quantity", 1)
        if not isinstance(sku, str) or not sku or len(sku) > MAX_SKU_LEN:
            return tool_error(f"sku must be 1-{MAX_SKU_LEN} characters.")
        if not isinstance(qty, int) or isinstance(qty, bool) \
                or not 1 <= qty <= MAX_QTY:
            return tool_error(f"quantity must be an integer 1-{MAX_QTY}.")
        return _handle_error(lambda: tool(
            f"Added {qty} x {sku} to {handle}.",
            STORE.add(handle, sku, qty)), handle)
    if name == "remove_item":
        sku = arguments.get("sku", "")
        if not isinstance(sku, str) or not sku:
            return tool_error("sku must be a non-empty string.")
        return _handle_error(lambda: tool(f"Removed {sku} from {handle}.",
                                          STORE.remove(handle, sku)), handle)
    if name == "view_basket":
        def _v():
            v = STORE.view(handle)
            return tool(f"{handle}: {v['item_count']} item(s)"
                        + (" (checked out)" if v["checked_out"] else "")
                        + ".", v)
        return _handle_error(_v, handle)
    if name == "checkout_basket":
        return _handle_error(lambda: tool(
            f"Checked out {handle}.", STORE.checkout(handle)), handle)
    if name == "destroy_basket":
        return _handle_error(lambda: tool(
            f"Destroyed {handle}.", STORE.destroy(handle)), handle)
    if name == "list_baskets":
        live = STORE.list_live()
        return tool(f"{len(live)} live basket(s).", {"baskets": live})
    return None


def read_resource(uri):
    if uri.startswith("basket://"):
        handle = uri[len("basket://"):]
        try:
            view = STORE.view(handle)
        except (UnknownHandle, ExpiredHandle):
            return None  # caller maps to -32602 (SEP-2164)
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(view, indent=2)}],
                "ttlMs": READ_TTL_MS, "cacheScope": "private"}
    return None


def capabilities():
    return {"tools": {"listChanged": False},
            "resources": {"listChanged": False}}


def handle_message(msg):
    if not isinstance(msg, dict) or "id" not in msg:
        return None  # notification or malformed: nothing to answer
    rid = msg["id"]
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "server/discover":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO, "capabilities": capabilities()}}
    if method == "initialize":
        # Legacy pre-2026-07-28 handshake: answer and serve the same tools.
        requested = params.get("protocolVersion", "")
        version = requested if requested in LEGACY_VERSIONS \
            or requested == PROTOCOL_VERSION else PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "serverInfo": SERVER_INFO, "capabilities": capabilities()}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "tools": TOOLS, "ttlMs": LIST_TTL_MS, "cacheScope": "public"}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "prompts": [], "ttlMs": LIST_TTL_MS, "cacheScope": "public"}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "resources": [], "ttlMs": LIST_TTL_MS, "cacheScope": "public"}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "resourceTemplates": [RESOURCE_TEMPLATE],
            "ttlMs": LIST_TTL_MS, "cacheScope": "public"}}
    if method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments"))
        if result is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": ERR_INVALID_PARAMS,
                "message": f"Unknown tool '{params.get('name')}'."}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    if method == "resources/read":
        result = read_resource(params.get("uri", ""))
        if result is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": ERR_INVALID_PARAMS,  # SEP-2164
                "message": f"Resource not found: {params.get('uri')}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    return {"jsonrpc": "2.0", "id": rid, "error": {
        "code": ERR_METHOD_NOT_FOUND, "message": f"Unknown method '{method}'."}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": ERR_PARSE, "message": "Parse error"}})
                + "\n")
            sys.stdout.flush()
            continue
        response = handle_message(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
