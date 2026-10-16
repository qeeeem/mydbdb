import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import data_tools
import server


def export_zip(owner="fixture-owner", actor=None, extra=False):
    actor = actor or f"https://neodb.social/@{owner}@neodb.social/"
    meta = {"server": "neodb.social", "username": owner, "actor": actor, "created_time": "2026-10-15"}
    item = {"id": "https://neodb.social/movie/one", "uuid": "one", "category": "movie", "title": "电影", "year": 2020}
    mark = {"type": "ShelfMember", "content": {"withRegardTo": item["id"], "status": "complete",
            "published": "2026-10-15T00:00:00Z", "attributedTo": actor}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in (("catalog.ndjson", item), ("journal.ndjson", mark)):
            archive.writestr(name, json.dumps(meta) + "\n" + json.dumps(value) + "\n")
        if extra:
            archive.writestr("actor.ndjson", "must not be accepted")
    return out.getvalue()


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = server.Application(self.root, state_dir=self.root)
        self.app.data_path.write_text("[]", encoding="utf-8")
        self.data = self.app.data
        self.app.owner_path.write_text(json.dumps({"server": "neodb.social", "username": "fixture-owner", "actor": "https://neodb.social/@fixture-owner@neodb.social/"}))

    def wait(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.data.status()["job"]
            if job["state"] not in ("running", "uploading"):
                return job
            time.sleep(.01)
        self.fail("worker did not finish")

    def upload(self, raw=None, digest="a" * 64):
        identity = self.data.begin_import({"sha256": digest})["id"]
        raw = export_zip() if raw is None else raw
        for offset in range(0, len(raw), 32768):
            self.data.chunk({"id": identity, "offset": offset, "data": base64.b64encode(raw[offset:offset+32768]).decode()})
        self.data.finish_import({"id": identity})
        return self.wait()

    def test_import_previews_then_commits_and_backs_up(self):
        before = self.app.data_path.read_bytes()
        job = self.upload()
        self.assertEqual(job["state"], "ready")
        self.assertEqual(job["preview"], {"total": 1, "added": 1, "removed": 0, "updated": 0})
        self.assertEqual(self.app.data_path.read_bytes(), before)
        self.assertFalse(self.data.state_path.exists())
        self.data.commit({"id": job["id"]})
        self.assertEqual(self.data.rows()[0]["timestamp"], "2026-10-15")
        self.assertEqual(json.loads(next((self.root / "backups").glob("*.json")).read_text()), [])
        self.assertEqual(json.loads(self.data.state_path.read_text())["sha256"], "a" * 64)

    def test_same_hash_does_not_rewrite_even_after_additions_or_restart(self):
        self.data.commit({"id": self.upload()["id"]})
        self.app.data_path.write_text('[{"uuid":"added"}]')
        before = self.app.data_path.stat().st_mtime_ns
        app = server.Application(self.root, state_dir=self.root)
        self.assertTrue(app.data.begin_import({"sha256": "a" * 64})["unchanged"])
        self.assertEqual(self.app.data_path.stat().st_mtime_ns, before)
        self.assertEqual(app.data.rows(), [{"uuid": "added"}])
        job = self.upload(digest="b" * 64)
        self.assertEqual(job["preview"]["removed"], 1)

    def test_foreign_or_mixed_owner_and_account_files_are_rejected(self):
        for raw in (export_zip(owner="other"), export_zip(actor="https://evil.test/@fixture-owner/"), export_zip(extra=True), b"bad zip"):
            job = self.upload(raw)
            self.assertEqual(job["state"], "error")
            self.assertEqual(self.data.rows(), [])
            self.assertFalse(self.data.state_path.exists())
        raw = export_zip()
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(out, "w") as new:
            new.writestr("catalog.ndjson", old.read("catalog.ndjson"))
            lines = old.read("journal.ndjson").decode().splitlines()
            record = json.loads(lines[1]);record["content"]["attributedTo"] = "https://neodb.social/@other/"
            new.writestr("journal.ndjson", lines[0] + "\n" + json.dumps(record))
        self.assertEqual(self.upload(out.getvalue())["state"], "error")

    def test_chunk_order_size_and_single_job(self):
        identity = self.data.begin_import({"sha256": "a" * 64})["id"]
        for payload in ({"offset": 1, "data": "YQ=="}, {"offset": 0, "data": "!"},
                        {"offset": 0, "data": base64.b64encode(b"x" * 32769).decode()}):
            with self.assertRaises(server.AppError):
                self.data.chunk({"id": identity, **payload})
        with self.assertRaises(server.AppError):
            self.data.begin_import({"sha256": "b" * 64})
        self.data.cancel({"id": identity})
        self.assertEqual(self.data.rows(), [])

    def test_oversized_expansion_rejected_without_writing(self):
        with patch.object(data_tools, "MAX_EXPANDED", 1):
            self.assertEqual(self.upload()["state"], "error")
        self.assertEqual(self.data.rows(), [])

    def test_preview_expiry_cancel_and_stale_revision(self):
        job = self.upload()
        self.app.revision += 1
        with self.assertRaisesRegex(server.AppError, "收藏已变化"):
            self.data.commit({"id": job["id"]})
        self.data.cancel({"id": job["id"]})
        job = self.upload()
        self.app.data_path.write_text('[{"uuid":"external"}]')
        with self.assertRaisesRegex(server.AppError, "收藏已变化"):
            self.data.commit({"id": job["id"]})
        self.data.job["touched"] -= 1801
        with self.assertRaises(server.AppError):
            self.data.commit({"id": job["id"]})

    def test_backup_or_write_failure_preserves_collection_and_hash(self):
        job = self.upload()
        original = data_tools.write_media
        for fail_backup in (True, False):
            def write(path, rows):
                if fail_backup or path == self.app.data_path:
                    raise OSError("full")
                original(path, rows)
            with patch.object(data_tools, "write_media", side_effect=write), self.assertRaises(OSError):
                self.data.commit({"id": job["id"]})
            self.assertEqual(self.data.rows(), [])
            self.assertFalse(self.data.state_path.exists())

    def test_hash_failure_reports_saved_data_accurately(self):
        job = self.upload()
        original = data_tools.write_media
        def write(path, rows):
            if path == self.data.state_path:
                raise OSError("full")
            original(path, rows)
        with patch.object(data_tools, "write_media", side_effect=write):
            result = self.data.commit({"id": job["id"]})
        self.assertTrue(result["ok"])
        self.assertIn("哈希保存失败", result["message"])
        self.assertEqual(len(self.data.rows()), 1)

    def test_cancel_during_parse_does_not_restore_preview_or_write(self):
        entered, release = threading.Event(), threading.Event()
        original = self.data.parse_import
        def parse(raw, owner):
            entered.set()
            release.wait(3)
            return original(raw, owner)
        with patch.object(self.data, "parse_import", side_effect=parse):
            identity = self.data.begin_import({"sha256": "a" * 64})["id"]
            self.data.chunk({"id": identity, "offset": 0, "data": base64.b64encode(export_zip()).decode()})
            self.data.finish_import({"id": identity})
            self.assertTrue(entered.wait(2))
            self.data.cancel({"id": identity})
            release.set()
            time.sleep(.05)
        self.assertIsNone(self.data.status()["job"])
        self.assertEqual(self.data.rows(), [])

    def test_revoke_or_change_owner_after_preview_blocks_commit(self):
        job = self.upload()
        saved = self.app.owner_path.read_bytes()
        self.app.owner_path.unlink()
        with self.assertRaisesRegex(server.AppError, "^已拒绝$"):
            self.data.commit({"id": job["id"]})
        owner = json.loads(saved);owner["username"] = "replacement"
        self.app.owner_path.write_text(json.dumps(owner))
        with self.assertRaisesRegex(server.AppError, "^已拒绝$"):
            self.data.commit({"id": job["id"]})
        self.assertEqual(self.data.rows(), [])


if __name__ == "__main__":
    unittest.main()
