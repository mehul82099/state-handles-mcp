import json
import os
import re
import subprocess
import sys
import time
import unittest

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "state_handles_mcp.py")


class Server:
    def __init__(self, env_extra=None):
        env = dict(os.environ)
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, SERVER], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        rid = self.next_id
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method,
             "params": params or {}}).encode() + b"\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline().decode()
        msg = json.loads(line)
        self.assert_id(msg, rid)
        return msg

    @staticmethod
    def assert_id(msg, rid):
        assert msg.get("id") == rid, f"bad id: {msg}"

    def notify(self, method, params=None):
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method,
             "params": params or {}}).encode() + b"\n")
        self.proc.stdin.flush()

    def result(self, method, params=None):
        msg = self.request(method, params)
        assert "result" in msg, f"error from {method}: {msg}"
        return msg["result"]

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=5)


def make_server(env_extra=None):
    s = Server(env_extra)
    s.result("server/discover")
    return s


def make_basket(s, label=None):
    r = s.result("tools/call", {"name": "create_basket",
                                "arguments": {"label": label} if label else {}})
    return r["structuredContent"]["basket_id"]


class DiscoverTests(unittest.TestCase):
    def test_discover_stateless(self):
        s = make_server()
        try:
            d = s.result("server/discover")
            self.assertEqual(d["protocolVersion"], "2026-07-28")
            self.assertEqual(d["serverInfo"]["name"], "state-handles-mcp")
            self.assertIn("tools", d["capabilities"])
            self.assertIn("resources", d["capabilities"])
        finally:
            s.close()

    def test_legacy_initialize(self):
        s = Server()
        try:
            r = s.result("initialize", {"protocolVersion": "2025-06-18",
                                        "clientInfo": {"name": "t"},
                                        "capabilities": {}})
            self.assertEqual(r["protocolVersion"], "2025-06-18")
            s.notify("notifications/initialized")
            self.assertEqual(len(s.result("tools/list")["tools"]), 7)
        finally:
            s.close()

    def test_unknown_method(self):
        s = make_server()
        try:
            msg = s.request("bogus/method")
            self.assertEqual(msg["error"]["code"], -32601)
        finally:
            s.close()

    def test_parse_error(self):
        s = Server()
        try:
            s.proc.stdin.write(b"{not json\n")
            s.proc.stdin.flush()
            msg = json.loads(s.proc.stdout.readline().decode())
            self.assertEqual(msg["error"]["code"], -32700)
        finally:
            s.close()

    def test_notification_gets_no_response(self):
        s = make_server()
        try:
            s.notify("notifications/initialized")
            # Next response must belong to the following request.
            r = s.result("ping")
            self.assertEqual(r, {})
        finally:
            s.close()


class ListCacheHintTests(unittest.TestCase):
    def setUp(self):
        self.s = make_server()

    def tearDown(self):
        self.s.close()

    def test_tools_list_ttl_and_scope(self):
        r = self.s.result("tools/list")
        self.assertEqual(r["ttlMs"], 60000)
        self.assertEqual(r["cacheScope"], "public")
        self.assertEqual(len(r["tools"]), 7)

    def test_prompts_and_resources_lists_carry_hints(self):
        for method, key in (("prompts/list", "prompts"),
                            ("resources/list", "resources")):
            r = self.s.result(method)
            self.assertEqual(r["ttlMs"], 60000)
            self.assertEqual(r["cacheScope"], "public")
            self.assertEqual(r[key], [])

    def test_resource_templates(self):
        r = self.s.result("resources/templates/list")
        self.assertEqual(r["cacheScope"], "public")
        self.assertEqual(r["resourceTemplates"][0]["uriTemplate"],
                         "basket://{basket_id}")

    def test_lists_stable_across_calls(self):
        # SEP-2567: list results must not vary per connection/session.
        first = self.s.result("tools/list")
        make_basket(self.s)
        second = self.s.result("tools/list")
        self.assertEqual(first, second)

    def test_tool_names_sep986(self):
        pattern = re.compile(r"^[A-Za-z0-9_./-]{1,64}$")
        for t in self.s.result("tools/list")["tools"]:
            self.assertRegex(t["name"], pattern)

    def test_create_description_documents_durability(self):
        tools = {t["name"]: t for t in self.s.result("tools/list")["tools"]}
        self.assertIn("expire", tools["create_basket"]["description"])
        self.assertIn("in-memory", tools["create_basket"]["description"])


class BasketFlowTests(unittest.TestCase):
    def setUp(self):
        self.s = make_server()

    def tearDown(self):
        self.s.close()

    def test_create_returns_opaque_handle(self):
        handle = make_basket(self.s, label="groceries")
        self.assertRegex(handle, r"^bsk_[0-9a-f]{32}$")  # 128 bits, opaque

    def test_handles_unique(self):
        handles = {make_basket(self.s) for _ in range(20)}
        self.assertEqual(len(handles), 20)

    def test_add_view_flow(self):
        h = make_basket(self.s)
        r = self.s.result("tools/call", {"name": "add_item", "arguments": {
            "basket_id": h, "sku": "shoes", "quantity": 2}})
        self.assertEqual(r["structuredContent"]["item_count"], 2)
        self.s.result("tools/call", {"name": "add_item", "arguments": {
            "basket_id": h, "sku": "shoes", "quantity": 1}})
        v = self.s.result("tools/call", {"name": "view_basket",
                                         "arguments": {"basket_id": h}})
        self.assertEqual(v["structuredContent"]["items"],
                         [{"sku": "shoes", "quantity": 3}])
        self.assertTrue(v["content"][0]["text"].startswith(h))

    def test_remove_item(self):
        h = make_basket(self.s)
        self.s.result("tools/call", {"name": "add_item", "arguments": {
            "basket_id": h, "sku": "hat"}})
        r = self.s.result("tools/call", {"name": "remove_item",
            "arguments": {"basket_id": h, "sku": "hat"}})
        self.assertEqual(r["structuredContent"]["items"], [])
        # Removing an absent sku is a no-op, not an error.
        r = self.s.result("tools/call", {"name": "remove_item",
            "arguments": {"basket_id": h, "sku": "ghost"}})
        self.assertNotIn("isError", r)

    def test_checkout_freezes(self):
        h = make_basket(self.s)
        self.s.result("tools/call", {"name": "add_item", "arguments": {
            "basket_id": h, "sku": "shoes"}})
        r = self.s.result("tools/call", {"name": "checkout_basket",
                                         "arguments": {"basket_id": h}})
        self.assertTrue(r["structuredContent"]["checked_out"])
        r = self.s.result("tools/call", {"name": "add_item", "arguments": {
            "basket_id": h, "sku": "hat"}})
        self.assertTrue(r["isError"])
        self.assertIn("checked out", r["content"][0]["text"])

    def test_destroy(self):
        h = make_basket(self.s)
        r = self.s.result("tools/call", {"name": "destroy_basket",
                                         "arguments": {"basket_id": h}})
        self.assertTrue(r["structuredContent"]["destroyed"])
        r = self.s.result("tools/call", {"name": "view_basket",
                                         "arguments": {"basket_id": h}})
        self.assertTrue(r["isError"])
        self.assertIn("Unknown", r["content"][0]["text"])

    def test_list_baskets(self):
        h1 = make_basket(self.s, label="a")
        h2 = make_basket(self.s)
        self.s.result("tools/call", {"name": "add_item", "arguments": {
            "basket_id": h2, "sku": "x", "quantity": 4}})
        r = self.s.result("tools/call", {"name": "list_baskets"})
        by_id = {b["basket_id"]: b
                 for b in r["structuredContent"]["baskets"]}
        self.assertEqual(by_id[h1]["label"], "a")
        self.assertEqual(by_id[h2]["item_count"], 4)

    def test_unknown_handle_error_is_useful(self):
        r = self.s.result("tools/call", {"name": "view_basket",
            "arguments": {"basket_id": "bsk_" + "0" * 32}})
        self.assertTrue(r["isError"])
        self.assertIn("create_basket", r["content"][0]["text"])

    def test_missing_required_argument(self):
        r = self.s.result("tools/call", {"name": "add_item",
                                         "arguments": {"sku": "x"}})
        self.assertTrue(r["isError"])
        self.assertIn("basket_id", r["content"][0]["text"])

    def test_bad_quantity(self):
        h = make_basket(self.s)
        for bad in (0, -1, 2**20, "3", 1.5, True):
            r = self.s.result("tools/call", {"name": "add_item",
                "arguments": {"basket_id": h, "sku": "x", "quantity": bad}})
            self.assertTrue(r.get("isError"), f"accepted {bad!r}")

    def test_unknown_tool_protocol_error(self):
        msg = self.s.request("tools/call", {"name": "nope"})
        self.assertEqual(msg["error"]["code"], -32602)

    def test_two_baskets_isolated(self):
        # The cardinality fix: two independent handles, isolated state.
        a, b = make_basket(self.s), make_basket(self.s)
        self.s.result("tools/call", {"name": "add_item", "arguments": {
            "basket_id": a, "sku": "shoes"}})
        v = self.s.result("tools/call", {"name": "view_basket",
                                         "arguments": {"basket_id": b}})
        self.assertEqual(v["structuredContent"]["items"], [])


class ExpiryTests(unittest.TestCase):
    def test_expired_handle_error(self):
        s = make_server({"STATE_HANDLES_BASKET_TTL": "0.3"})
        try:
            h = make_basket(s)
            time.sleep(0.5)
            r = s.result("tools/call", {"name": "view_basket",
                                        "arguments": {"basket_id": h}})
            self.assertTrue(r["isError"])
            self.assertIn("expired", r["content"][0]["text"])
            self.assertIn(h, r["content"][0]["text"])
        finally:
            s.close()

    def test_expired_basket_dropped_from_list(self):
        s = make_server({"STATE_HANDLES_BASKET_TTL": "0.3"})
        try:
            h = make_basket(s)
            time.sleep(0.5)
            r = s.result("tools/call", {"name": "list_baskets"})
            ids = [b["basket_id"] for b in r["structuredContent"]["baskets"]]
            self.assertNotIn(h, ids)
        finally:
            s.close()

    def test_activity_refreshes_expiry(self):
        s = make_server({"STATE_HANDLES_BASKET_TTL": "0.6"})
        try:
            h = make_basket(s)
            for _ in range(3):
                time.sleep(0.4)
                r = s.result("tools/call", {"name": "view_basket",
                                            "arguments": {"basket_id": h}})
                self.assertNotIn("isError", r)
        finally:
            s.close()


class ResourceReadTests(unittest.TestCase):
    def setUp(self):
        self.s = make_server()

    def tearDown(self):
        self.s.close()

    def test_read_basket_resource_private_cache(self):
        h = make_basket(self.s)
        self.s.result("tools/call", {"name": "add_item", "arguments": {
            "basket_id": h, "sku": "tea", "quantity": 2}})
        r = self.s.result("resources/read", {"uri": f"basket://{h}"})
        self.assertEqual(r["cacheScope"], "private")
        self.assertEqual(r["ttlMs"], 5000)
        body = json.loads(r["contents"][0]["text"])
        self.assertEqual(body["items"], [{"sku": "tea", "quantity": 2}])

    def test_read_unknown_resource_sep2164(self):
        msg = self.s.request("resources/read", {"uri": "basket://bsk_" + "f" * 32})
        self.assertEqual(msg["error"]["code"], -32602)
        self.assertIn("not found", msg["error"]["message"].lower())

    def test_read_other_scheme(self):
        msg = self.s.request("resources/read", {"uri": "file:///etc/passwd"})
        self.assertEqual(msg["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
