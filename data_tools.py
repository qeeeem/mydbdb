"""Owner-locked collection import, preview and atomic publication."""

import base64
from datetime import datetime, timezone
import io
import json
import re
import secrets
import threading
import time
import zipfile

from fetch_posters import build_media, read_records, write_media

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 96 * 1024 * 1024
EXPORT_FILES = {"catalog.ndjson", "journal.ndjson"}


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def identity(meta):
    if not isinstance(meta, dict) or any(not isinstance(meta.get(k), str) or not meta[k] for k in ("server", "username", "actor")):
        raise AppError("已拒绝", 403)
    return {k: meta[k] for k in ("server", "username", "actor")}


def export_identity(archive, name):
    with archive.open(name) as stream:
        first = stream.readline(65537)
    if len(first) > 65536:
        raise AppError("已拒绝", 403)
    return identity(json.loads(first.decode("utf-8-sig")))


def configure_owner(archive_path, config_path):
    """Local administrator bootstrap only. Never infer permission from an upload."""
    with zipfile.ZipFile(archive_path) as archive:
        owner = export_identity(archive, "catalog.ndjson")
        if owner != export_identity(archive, "journal.ndjson"):
            raise AppError("已拒绝", 403)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_media(config_path, owner)


def validate_export(raw, owner):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) != 2 or {m.filename for m in members} != EXPORT_FILES:
                raise AppError("仅接受目录和收藏日志，不上传账号资料或附件")
            if sum(m.file_size for m in members) > MAX_EXPANDED:
                raise AppError("解压后的收藏数据超过 96 MiB", 413)
            for name in EXPORT_FILES:
                if export_identity(archive, name) != owner:
                    raise AppError("已拒绝", 403)
            for record in read_records(archive, "journal.ndjson"):
                if record.get("type") in ("ShelfMember", "Rating", "Comment"):
                    content = record.get("content")
                    if not isinstance(content, dict) or content.get("attributedTo") != owner["actor"]:
                        raise AppError("已拒绝", 403)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise AppError("ZIP 无效或使用了不支持的压缩方式") from exc


class DataTools:
    """One bounded, expiring preview at a time; only commit writes collection data."""

    def __init__(self, app):
        self.app = app
        self.job = None
        self.state_path = app.private_dir / "web-import.json"

    def owner(self):
        if not self.app.owner_path.is_file():
            raise AppError("已拒绝", 403)
        return identity(json.loads(self.app.owner_path.read_text(encoding="utf-8")))

    def rows(self):
        rows = json.loads(self.app.data_path.read_text(encoding="utf-8")) if self.app.data_path.exists() else []
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise AppError("现有收藏数据格式错误，未更新", 500)
        return rows

    def status(self):
        with self.app.lock:
            self.expire()
            self.owner()
            job = self.job
            return {"csrf": self.app.csrf,
                    "count": len(self.rows()),
                    "job": {k: v for k, v in job.items() if k in
                            ("id", "state", "message", "preview", "offset")} if job else None}

    def expire(self):
        if self.job and time.monotonic() - self.job["touched"] > 1800:
            self.job = None

    def current(self, job_id, state=None):
        self.expire()
        if not self.job or self.job["id"] != job_id:
            raise AppError("操作已取消或过期，请重新开始", 409)
        if state and self.job["state"] != state:
            raise AppError("操作状态已改变，请重新打开数据管理", 409)
        self.job["touched"] = time.monotonic()
        return self.job

    def start(self, digest):
        with self.app.lock:
            self.expire()
            if self.job and self.job["state"] in ("uploading", "running", "ready"):
                raise AppError("已有导入等待处理，请先完成或取消", 409)
            rows = self.rows()
            self.job = {"id": secrets.token_urlsafe(20), "state": "uploading",
                        "touched": time.monotonic(), "before": rows, "revision": self.app.revision,
                        "owner": self.owner(),
                        "digest": digest, "message": "正在接收收藏数据"}
            return self.job

    def begin_import(self, payload):
        digest = payload.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise AppError("缺少有效的 ZIP 哈希")
        with self.app.lock:
            self.owner()
            state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
            if state.get("sha256") == digest and self.app.data_path.is_file():
                return {"unchanged": True, "message": "ZIP 哈希未变化，当前数据保持不变"}
            job = self.start(digest)
            job.update(buffer=bytearray(), offset=0)
            return {"id": job["id"]}

    def chunk(self, payload):
        try:
            chunk = base64.b64decode(payload.get("data", ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise AppError("上传分片格式错误") from exc
        if not chunk or len(chunk) > 32768:
            raise AppError("上传分片过大或为空", 413)
        with self.app.lock:
            job = self.current(payload.get("id"), "uploading")
            if type(payload.get("offset")) is not int or payload["offset"] != len(job["buffer"]):
                raise AppError("上传顺序错误，请取消后重新导入", 409)
            if len(job["buffer"]) + len(chunk) > MAX_ARCHIVE:
                raise AppError("收藏 ZIP 超过 64 MiB", 413)
            job["buffer"].extend(chunk)
            job["offset"] = len(job["buffer"])
            return {"offset": job["offset"]}

    def finish_import(self, payload):
        with self.app.lock:
            job = self.current(payload.get("id"), "uploading")
            raw = bytes(job.pop("buffer"))
            job["state"] = "running"
        self.launch(job, lambda: self.parse_import(raw, job["owner"]))
        return {"ok": True}

    def parse_import(self, raw, owner):
        validate_export(raw, owner)
        rows, _ = build_media(io.BytesIO(raw), self.app.data_path)
        uuids = [r["uuid"] for r in rows]
        if len(set(uuids)) != len(uuids):
            raise AppError("导出包中存在重复 UUID，未更新")
        return rows

    def launch(self, job, produce):
        def run():
            try:
                rows = produce()
                with self.app.lock:
                    self.current(job["id"], "running")
                    old = {r.get("uuid"): r for r in job["before"]}
                    new = {r["uuid"]: r for r in rows}
                    job.update(state="ready", rows=rows, message="请核对后更新本站收藏",
                               preview={"total": len(rows), "added": len(new.keys() - old.keys()),
                                        "removed": len(old.keys() - new.keys()),
                                        "updated": sum(old[k] != new[k] for k in old.keys() & new.keys())})
            except Exception as exc:
                # Never put remote responses, tokens or stack traces into the API.
                message = str(exc) if isinstance(exc, AppError) else "数据解析失败，请检查导出包"
                with self.app.lock:
                    if self.job is job:
                        job.update(state="error", message=message)
        threading.Thread(target=run, daemon=True).start()

    def cancel(self, payload):
        with self.app.lock:
            self.current(payload.get("id"))
            self.job = None
        return {"ok": True}

    def commit(self, payload):
        with self.app.lock:
            job = self.current(payload.get("id"), "ready")
            if self.owner() != job["owner"]:
                raise AppError("已拒绝", 403)
            if self.app.revision != job["revision"] or self.rows() != job["before"]:
                raise AppError("预览后收藏已变化，请取消并重新导入，避免覆盖新记录", 409)
            backups = self.app.private_dir / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4) + ".json"
            write_media(backups / name, job["before"])
            write_media(self.app.data_path, job["rows"])
            self.app.revision += 1
            job.update(state="done", message=f"已更新 {len(job['rows'])} 条收藏")
            if job["digest"]:
                try:
                    write_media(self.state_path, {"sha256": job["digest"]})
                except OSError:
                    job["message"] += "；ZIP 哈希保存失败，下次导入将重新核对"
            job.pop("rows", None)
            job.pop("before", None)
            return {"ok": True, "message": job["message"]}
