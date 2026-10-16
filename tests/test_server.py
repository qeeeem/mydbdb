import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import server


class FakeNeoDB:
    def __init__(self):
        self.calls = []
        self.marks = {}
        self.fail_post = False
        self.timeout_after_post = False
        self.items = {cat: {"uuid": cat + "123", "category": cat, "title": "查理 & <原文>",
                            "api_url": f"/api/{'album' if cat == 'music' else cat}/{cat}123",
                            "year": 2024, "cover_image_url": "/cover.jpg"} for cat in server.CATEGORIES}

    def request(self, method, path, token=None, payload=None):
        self.calls.append((method, path, token, copy.deepcopy(payload)))
        if path == "/api/me":
            if token != "test-token":
                raise server.AppError("授权失效", 401)
            return {"username": "test-user"}
        if path.startswith("/api/catalog/search?"):
            params = parse_qs(urlsplit(path).query)
            return {"data": [self.items[params["category"][0]]], "count": 1, "pages": 1}
        uuid = path.rsplit("/", 1)[-1]
        if method == "GET" and not path.startswith("/api/me/"):
            return copy.deepcopy(next(item for item in self.items.values() if item["uuid"] == uuid))
        if method == "GET":
            if uuid not in self.marks:
                raise server.AppError("not marked", 404)
            return copy.deepcopy(self.marks[uuid])
        if self.fail_post:
            raise server.AppError("NeoDB unavailable", 502)
        item = next(item for item in self.items.values() if item["uuid"] == uuid)
        self.marks[uuid] = {**copy.deepcopy(payload), "item": item,
                            "created_time": payload.get("created_time", "2026-10-16T12:00:00+00:00")}
        if self.timeout_after_post:
            self.timeout_after_post = False
            raise server.AppError("请求结果未确认", 502)
        return {"message": "OK"}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "site"
        self.root.mkdir()
        for name in ("index.html", "add.js"):
            (self.root / name).write_bytes((server.ROOT / name).read_bytes())
        self.output = self.root / "media_data.json"
        self.output.write_text("[]", encoding="utf-8")
        self.fake = FakeNeoDB()
        self.httpd = server.create_server(0, self.root, self.fake)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

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

    def connect(self):
        status, body = self.request("/api/connect", {"token": "test-token"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def search(self, category="movie"):
        status, body = self.request("/api/search?q=Charlie&cat=" + category)
        self.assertEqual(status, 200)
        return body[0]

    def payload(self, item=None):
        return {"uuid": (item or self.search())["uuid"], "status": "complete", "rating": 8,
                "comment": "短评\n第二行", "visibility": 2}

    def test_page_and_add_script_are_served_from_same_origin(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b'<script src="add.js"></script>', body)
        self.assertNotIn(b"ngrok-free.app", body)
        status, body = self.request("/add.js")
        self.assertEqual(status, 200)
        self.assertIn(b"'/api/add'", body)

    def test_public_search_all_seven_categories_without_token(self):
        for category in sorted(server.CATEGORIES):
            with self.subTest(category=category):
                item = self.search(category)
                self.assertEqual(item["category"], category)
                self.assertEqual(item["year"], "2024")
                self.assertEqual(item["poster"], "https://neodb.social/cover.jpg")
        self.assertTrue(all(call[2] is None for call in self.fake.calls))

    def test_selection_reads_detail_year_missing_from_search(self):
        self.connect()
        item = self.search()
        self.httpd.app.items[item["uuid"]] = {k: v for k, v in self.httpd.app.items[item["uuid"]].items() if k != "year"}
        status, body = self.request("/api/mark?uuid=" + item["uuid"])
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["year"], "2024")
        self.assertIsNone(body["mark"])

    def test_port_in_use_is_rejected_instead_of_sharing_a_static_server_port(self):
        with self.assertRaises(OSError):
            server.create_server(self.httpd.server_port, self.root, self.fake)

    def test_connect_validates_and_persists_token_without_returning_it(self):
        self.connect()
        status, session = self.request("/api/session")
        self.assertTrue(session["connected"])
        self.assertEqual(session["username"], "test-user")
        self.assertNotIn("test-token", json.dumps(session))
        loaded = server.Application(self.root, self.fake)
        self.assertTrue(loaded.session()["connected"])
        status, _ = self.request("/api/connect", {"token": "invalid"})
        self.assertEqual(status, 401)
        self.assertEqual(loaded.token, self.httpd.app.token)

    def test_add_requires_connection_without_writing_local_data(self):
        before = self.output.read_bytes()
        status, _ = self.request("/api/add", self.payload())
        self.assertEqual(status, 401)
        self.assertEqual(self.output.read_bytes(), before)

    def test_add_all_categories_syncs_fields_then_refreshes_local_data(self):
        self.connect()
        for category in sorted(server.CATEGORIES):
            item = self.search(category)
            status, result = self.request("/api/add", self.payload(item))
            self.assertEqual(status, 200)
            self.assertTrue(result["synced"])
            remote = self.fake.marks[item["uuid"]]
            self.assertEqual(remote["shelf_type"], "complete")
            self.assertEqual(remote["rating_grade"], 8)
            self.assertEqual(remote["comment_text"], "短评\n第二行")
            self.assertEqual(remote["visibility"], 2)
            self.assertFalse(remote["post_to_fediverse"])
            self.assertEqual(result["item"]["timestamp"], "2026-10-16")
        self.assertEqual(len(json.loads(self.output.read_text(encoding="utf-8"))), 7)

    def test_updating_existing_mark_keeps_tags_date_poster_and_no_duplicates(self):
        self.connect()
        payload = self.payload()
        self.request("/api/add", payload)
        uuid = payload["uuid"]
        self.fake.marks[uuid]["tags"] = ["original tag"]
        self.fake.marks[uuid]["created_time"] = "2020-01-02T00:00:00Z"
        rows = json.loads(self.output.read_text(encoding="utf-8"))
        rows[0]["poster"] = "https://existing.test/poster.jpg"
        self.output.write_text(json.dumps(rows), encoding="utf-8")
        payload.update(status="dropped", rating=None, comment="", visibility=0)
        status, result = self.request("/api/add", payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.marks[uuid]["tags"], ["original tag"])
        self.assertEqual(result["item"]["timestamp"], "2020-01-02")
        self.assertEqual(result["item"]["poster"], "https://existing.test/poster.jpg")
        self.assertEqual(len(json.loads(self.output.read_text(encoding="utf-8"))), 1)
        status, result = self.request("/api/mark?uuid=" + uuid)
        self.assertEqual(result["mark"]["visibility"], 0)

    def test_remote_failure_does_not_write_local_or_claim_success(self):
        self.connect()
        self.fake.fail_post = True
        before = self.output.read_bytes()
        status, result = self.request("/api/add", self.payload())
        self.assertEqual(status, 502)
        self.assertFalse(result["ok"])
        self.assertEqual(self.output.read_bytes(), before)

    def test_local_save_failure_reports_remote_success_and_retry_does_not_repost(self):
        self.connect()
        payload = self.payload()
        with patch.object(server, "write_media", side_effect=OSError("locked")):
            status, result = self.request("/api/add", payload)
        self.assertEqual(status, 500)
        self.assertFalse(result["ok"])
        self.assertTrue(result["synced"])
        status, result = self.request("/api/add", payload)
        self.assertEqual(status, 200)
        posts = [c for c in self.fake.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(json.loads(self.output.read_text(encoding="utf-8"))), 1)

    def test_post_timeout_reconciles_remote_state_on_retry(self):
        self.connect()
        self.fake.timeout_after_post = True
        payload = self.payload()
        self.assertEqual(self.request("/api/add", payload)[0], 502)
        self.assertEqual(self.request("/api/add", payload)[0], 200)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "POST"]), 1)

    def test_invalid_payloads_never_post(self):
        self.connect()
        for change in ({"rating": 11}, {"rating": 7.5}, {"rating": True}, {"visibility": 9},
                       {"status": []}, {"uuid": "notSelected"}, {"comment": []}):
            with self.subTest(change=change):
                status, _ = self.request("/api/add", {**self.payload(), **change})
                self.assertIn(status, (400, 409))
        self.assertFalse(any(c[0] == "POST" for c in self.fake.calls))

    def test_private_files_directory_listing_and_traversal_not_served(self):
        self.connect()
        for path in ("/.local/neodb.json", "/.git/config", "/data/", "/data/export.zip", "/%2e%2e/.local/neodb.json", "/server.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_foreign_origin_bad_host_and_missing_csrf_rejected(self):
        self.assertEqual(self.request("/api/session", headers={"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.request("/api/session", headers={"Host": "evil.test"})[0], 403)
        status, _ = self.request("/api/connect", {"token": "test-token"}, {"X-Mydbdb-CSRF": ""})
        self.assertEqual(status, 403)
        self.assertFalse(self.fake.calls)

    def test_https_proxy_origin_is_explicit_and_still_requires_csrf(self):
        origin = "https://8.148.157.132:18443"
        self.assertEqual(self.request("/api/session", headers={"Origin": origin})[0], 403)
        self.httpd.public_origin = origin
        self.assertEqual(self.request("/api/session", headers={"Origin": origin})[0], 200)
        self.assertEqual(self.request("/api/connect", {"token": "test-token"},
                                      {"Origin": origin, "X-Mydbdb-CSRF": ""})[0], 403)
        self.assertEqual(self.request("/api/connect", {"token": "test-token"}, {"Origin": origin})[0], 200)
        self.assertEqual(self.request("/api/session", headers={"Origin": "http://8.148.157.132:18443"})[0], 403)
        self.assertEqual(self.request("/api/session", headers={"Host": "8.148.157.132:18443"})[0], 403)
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_release_uses_persistent_data_and_credentials_not_release_files(self):
        state = Path(self.temp.name) / "state"
        state.mkdir()
        (state / "media_data.json").write_text("[]", encoding="utf-8")
        self.httpd.app = server.Application(self.root, self.fake, state_dir=state)
        self.connect()
        self.assertEqual(self.request("/api/add", self.payload())[0], 200)
        self.assertEqual(json.loads(self.output.read_text()), [])
        self.assertEqual(len(self.request("/media_data.json")[1]), 1)
        self.assertEqual(self.httpd.app.config_path, state / "neodb.json")
        release2 = Path(self.temp.name) / "release2"
        release2.mkdir()
        (release2 / "RELEASE").write_text("new-release")
        self.httpd.app = server.Application(release2, self.fake, state_dir=state)
        self.assertTrue(self.request("/api/session")[1]["connected"])
        status, health = self.request("/_health")
        self.assertEqual(status, 200)
        self.assertEqual(health, {"ok": True, "count": 1, "release": "new-release"})
        self.assertEqual(self.request("/neodb.json")[0], 404)

    def test_public_origin_must_be_https_origin(self):
        for origin in ("http://example.test", "https://user:pass@example.test", "https://example.test/path",
                       "https://example.test?x=1", "https://example.test#fragment"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                server.create_server(0, self.root, self.fake, public_origin=origin)


class ClientTests(unittest.TestCase):
    def test_redirects_not_followed_and_errors_do_not_expose_token(self):
        client = server.NeoDBClient()
        error = HTTPError("https://neodb.social/api/me", 307, "redirect", {}, None)
        with patch.object(client.opener, "open", side_effect=error) as send:
            with self.assertRaises(server.AppError) as caught:
                client.request("POST", "/api/me/shelf/item/abc", "secret-token", {})
        self.assertEqual(send.call_count, 1)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
