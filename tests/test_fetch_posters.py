import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import fetch_posters as parser


HEADER = {"server": "example.test", "created_time": "2026-10-15T00:00:00Z"}


def item(uuid="one", **values):
    return {
        "id": f"https://example.test/movie/{uuid}", "uuid": uuid,
        "type": "Movie", "title": "测试电影", "category": "movie", "year": 2020,
        "rating": 9.8, "cover_image_url": "/covers/one.jpg", **values,
    }


def journal(kind="ShelfMember", uuid="one", **content):
    return {"type": kind, "visibility": 2, "content": {
        "withRegardTo": f"https://example.test/movie/{uuid}",
        "published": "2020-01-01T23:30:00+00:00",
        "updated": "2026-10-15T00:00:00+00:00",
        **({"status": "complete"} if kind == "ShelfMember" else {}), **content,
    }}


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "export.zip"
        self.output = self.root / "media_data.json"

    def make_archive(self, catalog=None, records=None, raw_journal=None):
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, rows in (
                ("catalog.ndjson", [item()] if catalog is None else catalog),
                ("journal.ndjson", [journal()] if records is None else records),
            ):
                data = "\ufeff" + "\n\n".join(json.dumps(row, ensure_ascii=False) for row in [HEADER, *rows]) + "\n"
                archive.writestr(name, raw_journal if name == "journal.ndjson" and raw_journal is not None else data)
            # 账号文件故意不是 JSON，确认解析不依赖或读取账号资料。
            archive.writestr("actor.ndjson", "this file must not be parsed")
            archive.writestr("attachments/unused.png", b"unused")

    def cache(self, rows):
        self.output.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return self.output

    def test_join_personal_values_and_keep_original_date(self):
        self.make_archive(records=[
            journal(), journal("Rating", value=7),
            journal("Comment", content="短评\r\n第二行 & <原文>"),
        ])
        rows, reused = parser.build_media(self.archive)
        self.assertEqual(rows, [{
            "title": "测试电影", "category": "movie", "status": "complete",
            "rating": 7, "comment": "短评\r\n第二行 & <原文>", "year": "2020",
            "timestamp": "2020-01-01", "poster": "https://example.test/covers/one.jpg", "uuid": "one",
        }])
        self.assertEqual(reused, 0)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["export.zip"])

    def test_current_shelf_only_not_history_or_unmarked_catalog(self):
        self.make_archive(catalog=[item(), item("unused")], records=[
            journal(status="wishlist"),
            {"type": "ShelfLog", "item": item()["id"], "status": "complete", "timestamp": "2026-10-15"},
            {"type": "post", "post": {}}, {"type": "Tag", "name": "tag"},
        ])
        rows, _ = parser.build_media(self.archive)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "wishlist")
        self.assertIsNone(rows[0]["rating"])
        self.assertEqual(rows[0]["comment"], "")

    def test_all_categories_statuses_and_year_fields(self):
        catalog, records = [], []
        for i, category in enumerate(sorted(parser.CATEGORIES)):
            uuid = str(i)
            catalog.append(item(uuid, category=category, year=None, pub_year=2000 + i))
            records.append(journal(uuid=uuid, status=sorted(parser.STATUSES)[i % 4]))
        self.make_archive(catalog, records)
        rows, _ = parser.build_media(self.archive)
        self.assertEqual({r["category"] for r in rows}, parser.CATEGORIES)
        self.assertEqual({r["status"] for r in rows}, parser.STATUSES)
        for value, expected in [({}, ""), ({"pub_year": 2001}, "2001"),
                                ({"release_year": 2002}, "2002"), ({"release_date": "2003-04-05"}, "2003"),
                                ({"opening_date": "2004-01-01"}, "2004"), ({"year": 2025, "pub_year": 2000}, "2025")]:
            with self.subTest(value=value):
                self.assertEqual(parser.publication_year(value), expected)

    def test_same_title_stays_separate_and_ambiguous_cache_not_reused(self):
        self.make_archive([item(), item("two")], [journal(), journal(uuid="two", status="dropped")])
        cache = self.cache([{"title": "测试电影", "category": "movie", "year": "2020", "poster": "https://cached.test/a.jpg"}])
        rows, reused = parser.build_media(self.archive, cache)
        self.assertEqual(len(rows), 2)
        self.assertEqual(reused, 0)
        self.assertEqual({r["status"] for r in rows}, {"complete", "dropped"})

    def test_legacy_cache_preserves_only_poster(self):
        self.make_archive()
        cache = self.cache([{"title": "测试电影", "category": "movie", "year": "2020", "poster": "https://cached.test/a.jpg", "rating": 1, "status": "wishlist"}])
        rows, reused = parser.build_media(self.archive, cache)
        self.assertEqual(reused, 1)
        self.assertEqual(rows[0]["poster"], "https://cached.test/a.jpg")
        self.assertIsNone(rows[0]["rating"])
        self.assertEqual(rows[0]["status"], "complete")

    def test_uuid_cache_survives_title_change(self):
        self.make_archive(catalog=[item(title="新标题")])
        cache = self.cache([{"uuid": "one", "title": "旧标题", "poster": "https://cached.test/a.jpg"}])
        rows, reused = parser.build_media(self.archive, cache)
        self.assertEqual(reused, 1)
        self.assertEqual(rows[0]["title"], "新标题")

    def test_duplicate_legacy_cache_is_not_guessed(self):
        self.make_archive()
        row = {"title": "测试电影", "category": "movie", "year": "2020", "poster": "https://cached.test/a.jpg"}
        rows, reused = parser.build_media(self.archive, self.cache([row, row]))
        self.assertEqual(reused, 0)
        self.assertEqual(rows[0]["poster"], "https://example.test/covers/one.jpg")

    def test_missing_cover_and_display_title(self):
        self.make_archive(catalog=[item(cover_image_url=None, display_title="显示标题")])
        rows, _ = parser.build_media(self.archive)
        self.assertIsNone(rows[0]["poster"])
        self.assertEqual(rows[0]["title"], "显示标题")

    def test_sort_recent_marks_first(self):
        self.make_archive([item(), item("two")], [journal(), journal(uuid="two", published="2026-10-15T08:00:00Z")])
        rows, _ = parser.build_media(self.archive)
        self.assertEqual([r["uuid"] for r in rows], ["two", "one"])

    def test_reject_missing_join_duplicate_shelf_invalid_fields_and_empty_export(self):
        cases = [
            ([], [journal()], "catalog.ndjson"),
            ([item()], [journal(), journal()], "重复"),
            ([item()], [journal(status="unknown")], "状态"),
            ([item(category="unknown")], [journal()], "分类"),
            ([item()], [journal(published="bad-date")], "时间"),
            ([item()], [journal(), journal("Rating", value=11)], "评分"),
            ([item()], [], "没有 ShelfMember"),
        ]
        for catalog, records, message in cases:
            with self.subTest(message=message):
                self.make_archive(catalog, records)
                with self.assertRaisesRegex(parser.ExportError, message):
                    parser.build_media(self.archive)

    def test_missing_member_and_malformed_line(self):
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("catalog.ndjson", json.dumps(item()))
        with self.assertRaisesRegex(parser.ExportError, "缺少 journal.ndjson"):
            parser.build_media(self.archive)
        self.make_archive(raw_journal=json.dumps(HEADER) + "\nnot json\n")
        with self.assertRaisesRegex(parser.ExportError, "journal.ndjson 第 2 行"):
            parser.build_media(self.archive)

    def test_cli_failure_keeps_previous_output(self):
        self.make_archive(raw_journal="broken")
        self.output.write_text("previous output", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = parser.main([str(self.archive), "-o", str(self.output), "--no-poster-cache"])
        self.assertEqual(code, 1)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous output")

    def test_cli_output_can_be_read_and_regenerated(self):
        self.make_archive()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(parser.main([str(self.archive), "-o", str(self.output)]), 0)
            first = self.output.read_bytes()
            self.assertEqual(parser.main([str(self.archive), "-o", str(self.output)]), 0)
        self.assertEqual(self.output.read_bytes(), first)
        self.assertEqual(len(json.loads(first)), 1)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_atomic_write_failure_keeps_previous_output(self):
        self.output.write_text("previous output", encoding="utf-8")
        with patch.object(Path, "replace", side_effect=OSError("cannot replace")):
            with self.assertRaises(OSError):
                parser.write_media(self.output, [{"title": "new"}])
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous output")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def prepare_auto(self):
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.archive = self.data_dir / "export.zip"
        self.state_path = self.data_dir / ".import-state.json"

    def run_auto(self, *options):
        with patch.object(parser, "ROOT", self.root), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return parser.main(["-o", str(self.output), *options])

    def snapshot(self, path):
        return path.read_bytes(), path.stat().st_mtime_ns

    def test_auto_no_zip_preserves_existing_output_and_state(self):
        self.prepare_auto()
        self.output.write_text("previous output", encoding="utf-8")
        self.state_path.write_text("{}", encoding="utf-8")
        before = self.snapshot(self.output), self.snapshot(self.state_path)
        self.assertEqual(self.run_auto(), 0)
        self.assertEqual((self.snapshot(self.output), self.snapshot(self.state_path)), before)

    def test_auto_missing_data_directory_does_not_create_output(self):
        self.assertEqual(self.run_auto(), 0)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / "data").exists())

    def test_auto_first_import_then_same_hash_does_not_parse_or_write(self):
        self.prepare_auto()
        self.make_archive()
        self.assertEqual(self.run_auto(), 0)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state[str(self.output.resolve())], parser.archive_hash(self.archive))
        before = self.snapshot(self.output), self.snapshot(self.state_path)
        with patch.object(parser, "build_media", side_effect=AssertionError("must not parse")):
            self.assertEqual(self.run_auto(), 0)
        self.assertEqual((self.snapshot(self.output), self.snapshot(self.state_path)), before)

    def test_auto_rename_and_touch_do_not_update(self):
        self.prepare_auto()
        self.make_archive()
        self.assertEqual(self.run_auto(), 0)
        before = self.snapshot(self.output), self.snapshot(self.state_path)
        renamed = self.archive.with_name("renamed.ZIP")
        self.archive.rename(renamed)
        os.utime(renamed, (1000000000, 1000000000))
        with patch.object(parser, "build_media", side_effect=AssertionError("must not parse")):
            self.assertEqual(self.run_auto(), 0)
        self.assertEqual((self.snapshot(self.output), self.snapshot(self.state_path)), before)

    def test_auto_changed_content_same_size_and_mtime_updates(self):
        self.prepare_auto()
        self.make_archive(records=[journal(), journal("Rating", value=7)])
        self.assertEqual(self.run_auto(), 0)
        previous_state = self.state_path.read_bytes()
        stat = self.archive.stat()
        self.make_archive(records=[journal(), journal("Rating", value=8)])
        os.utime(self.archive, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.archive.stat().st_size, stat.st_size)
        self.assertEqual(self.run_auto(), 0)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))[0]["rating"], 8)
        self.assertNotEqual(self.state_path.read_bytes(), previous_state)

    def test_auto_multiple_zips_preserves_output_and_state(self):
        self.prepare_auto()
        self.make_archive()
        self.assertEqual(self.run_auto(), 0)
        before = self.snapshot(self.output), self.snapshot(self.state_path)
        (self.data_dir / "other.zip").write_bytes(b"another zip")
        with patch.object(parser, "build_media", side_effect=AssertionError("must not parse")):
            self.assertEqual(self.run_auto(), 1)
        self.assertEqual((self.snapshot(self.output), self.snapshot(self.state_path)), before)

    def test_auto_bad_zip_preserves_successful_hash_and_retries(self):
        self.prepare_auto()
        self.make_archive()
        self.assertEqual(self.run_auto(), 0)
        before = self.snapshot(self.output), self.snapshot(self.state_path)
        self.archive.write_bytes(b"invalid zip")
        self.assertEqual(self.run_auto(), 1)
        self.assertEqual((self.snapshot(self.output), self.snapshot(self.state_path)), before)
        with patch.object(parser, "build_media", wraps=parser.build_media) as build:
            self.assertEqual(self.run_auto(), 1)
            build.assert_called_once()

    def test_auto_output_write_failure_does_not_record_hash(self):
        self.prepare_auto()
        self.make_archive()
        self.assertEqual(self.run_auto(), 0)
        before = self.snapshot(self.output), self.snapshot(self.state_path)
        self.make_archive(records=[journal(status="dropped")])
        with patch.object(parser, "write_media", side_effect=OSError("cannot write")):
            self.assertEqual(self.run_auto(), 1)
        self.assertEqual((self.snapshot(self.output), self.snapshot(self.state_path)), before)

    def test_auto_missing_output_is_regenerated(self):
        self.prepare_auto()
        self.make_archive()
        self.assertEqual(self.run_auto(), 0)
        before = self.output.read_bytes()
        self.output.unlink()
        self.assertEqual(self.run_auto(), 0)
        self.assertEqual(self.output.read_bytes(), before)

    def test_auto_output_paths_have_independent_hash_records(self):
        self.prepare_auto()
        self.make_archive()
        self.assertEqual(self.run_auto(), 0)
        self.output = self.root / "preview.json"
        self.assertEqual(self.run_auto(), 0)
        self.assertEqual(len(json.loads(self.state_path.read_text(encoding="utf-8"))), 2)


if __name__ == "__main__":
    unittest.main()
