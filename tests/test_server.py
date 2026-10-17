import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server
from test_data_tools import export_zip


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "site"
        self.root.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()
        for name in ("index.html", "add.js", "favicon.svg"):
            (self.root / name).write_bytes((server.ROOT / name).read_bytes())
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "fflate.js").write_bytes((server.ROOT / "vendor" / "fflate.js").read_bytes())
        self.output = self.state / "media_data.json"
        self.output.write_text("[]", encoding="utf-8")
        self.httpd = server.create_server(0, self.root, state_dir=self.state)
        owner = {"server": "neodb.social", "username": "fixture-owner", "actor": "https://neodb.social/@fixture-owner@neodb.social/"}
        self.httpd.app.owner_path.write_text(json.dumps(owner))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def stop_server(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join()

    def request(self, path, payload=None, headers=None):
        hdr = {"Content-Type": "application/json", "X-Mydbdb-CSRF": self.httpd.app.csrf}
        hdr.update(headers or {})
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=hdr)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            if response.headers.get_content_type() == "application/json":
                data = json.loads(data)
            return response.status, data

    def test_public_files_and_private_paths(self):
        for path in ("/", "/index.html", "/add.js", "/media_data.json", "/favicon.svg", "/api/import/library.js"):
            self.assertEqual(self.request(path)[0], 200)
        for path in ("/import-owner.json", "/neodb.json", "/web-import.json", "/backups/", "/.git/config",
                     "/server.py", "/data/export.zip", "/vendor/fflate.js", "/../state/import-owner.json"):
            self.assertEqual(self.request(path)[0], 404)
        for path in ("/", "/add.js", "/api/data"):
            self.assertNotIn("fixture-owner", str(self.request(path)[1]))

    def test_favicon_is_linked_and_served_as_svg(self):
        self.assertIn(b'href="favicon.svg"', self.request("/")[1])
        with urlopen(self.base + "/favicon.svg", timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
            self.assertEqual(response.read(), (self.root / "favicon.svg").read_bytes())

    def test_removed_add_and_sync_interfaces_are_gone(self):
        for path in ("/api/session", "/api/search", "/api/mark", "/api/connect", "/api/add", "/api/sync/start"):
            self.assertEqual(self.request(path)[0], 404)
            self.assertEqual(self.request(path, {})[0], 404)
        page = self.request("/")[1]
        self.assertNotIn(b"openAddPanel", page)
        self.assertNotIn("从 NeoDB 同步".encode(), page)
        self.assertNotIn("限定账号".encode(), page)

    def test_api_origin_and_csrf_protection(self):
        self.assertEqual(self.request("/api/data", headers={"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.request("/api/data", headers={"Host": "evil.test"})[0], 403)
        for route in ("/api/import/start", "/api/import/chunk", "/api/import/finish", "/api/data/commit", "/api/data/cancel"):
            self.assertEqual(self.request(route, {}, {"X-Mydbdb-CSRF": ""})[0], 403)
        origin = "https://8.148.157.132:18443"
        self.assertEqual(self.request("/api/data", headers={"Origin": origin})[0], 403)
        self.httpd.public_origin = origin
        self.assertEqual(self.request("/api/data", headers={"Origin": origin})[0], 200)
        self.assertEqual(self.request("/api/import/start", {"sha256": "a" * 64},
                                     {"Origin": origin, "X-Mydbdb-CSRF": ""})[0], 403)

    def test_unconfigured_import_fails_closed_without_revealing_identity(self):
        self.httpd.app.owner_path.unlink()
        for path, payload in (("/api/data", None), ("/api/import/start", {"sha256": "a" * 64})):
            status, body = self.request(path, payload)
            self.assertEqual(status, 403)
            self.assertEqual(body, {"ok": False, "error": "已拒绝"})

    def test_chunked_import_requires_explicit_commit(self):
        status, result = self.request("/api/import/start", {"sha256": "a" * 64})
        self.assertEqual(status, 200)
        identity = result["id"]
        self.assertEqual(self.request("/api/import/chunk", {"id": identity, "offset": 0,
                         "data": base64.b64encode(export_zip()).decode()})[0], 200)
        self.assertEqual(self.request("/api/import/finish", {"id": identity})[0], 200)
        for _ in range(100):
            result = self.request("/api/data")[1]
            if result["job"]["state"] != "running":
                break
            time.sleep(.01)
        self.assertEqual(result["job"]["state"], "ready", result)
        self.assertEqual(self.request("/media_data.json")[1], [])
        self.assertEqual(self.request("/api/data/commit", {"id": identity})[0], 200)
        self.assertEqual(len(self.request("/media_data.json")[1]), 1)
        self.assertFalse((self.root / "media_data.json").exists())

    def test_exclusive_loopback_binding_and_release_health(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        with self.assertRaises(OSError):
            server.create_server(self.httpd.server_port, self.root, state_dir=self.state)
        (self.root / "RELEASE").write_text("test-release")
        self.assertEqual(self.request("/_health")[1], {"ok": True, "count": 0, "release": "test-release"})

    def test_public_origin_must_be_https_origin(self):
        for origin in ("http://example.test", "https://user:pass@example.test", "https://example.test/path",
                       "https://example.test?x=1", "https://example.test#fragment"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                server.create_server(0, self.root, public_origin=origin)


if __name__ == "__main__":
    unittest.main()
