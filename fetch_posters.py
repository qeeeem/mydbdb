"""将 NeoDB NDJSON 导出 ZIP 转成现有 index.html 读取的 media_data.json。"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urljoin
import zipfile


ROOT = Path(__file__).resolve().parent
CATEGORIES = {"movie", "tv", "book", "game", "music", "performance", "podcast"}
STATUSES = {"wishlist", "progress", "complete", "dropped"}


class ExportError(ValueError):
    """导出数据不完整或不符合页面的数据约定。"""


def read_records(archive, name):
    """只读取需要的两个 NDJSON 文件，不解压账号资料或附件。"""
    if name not in archive.namelist():
        raise ExportError(f"ZIP 中缺少 {name}")
    with archive.open(name) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExportError(f"{name} 第 {number} 行不是有效 JSON：{exc.msg}") from exc
            if not isinstance(record, dict):
                raise ExportError(f"{name} 第 {number} 行必须是 JSON 对象")
            # NeoDB 每个文件的第一条记录是导出元信息。
            if "type" not in record and "server" in record and "created_time" in record:
                continue
            yield record


def index_records(records, key, label):
    indexed = {}
    for record in records:
        identity = record.get(key)
        if not isinstance(identity, str) or not identity:
            raise ExportError(f"{label} 缺少 {key}")
        if identity in indexed:
            raise ExportError(f"{label} 中条目标识重复：{identity}")
        indexed[identity] = record
    return indexed


def publication_year(item):
    for key in ("year", "pub_year", "release_year", "release_date", "opening_date"):
        value = item.get(key)
        if value is not None:
            match = re.match(r"^(\d{4})(?:\D|$)", str(value))
            if match:
                return match.group(1)
    return ""


def mark_date(value):
    if not isinstance(value, str) or not value:
        raise ExportError("ShelfMember 缺少 published 标记时间")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        raise ExportError(f"无效的标记时间：{value}") from exc


def read_poster_cache(path):
    """复用已匹配的海报；旧文件没有 UUID 时仅匹配唯一的标题、分类和年份。"""
    if not path.exists():
        return {}, {}
    with path.open(encoding="utf-8-sig") as stream:
        rows = json.load(stream)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ExportError(f"海报缓存 {path} 必须是 JSON 对象数组")
    by_uuid = defaultdict(list)
    by_title = defaultdict(list)
    for row in rows:
        if row.get("uuid"):
            by_uuid[row["uuid"]].append(row)
        by_title[(row.get("title"), row.get("category"), str(row.get("year") or ""))].append(row)
    return by_uuid, by_title


def build_media(archive_path, poster_cache=None):
    with zipfile.ZipFile(archive_path) as archive:
        catalog = index_records(read_records(archive, "catalog.ndjson"), "id", "catalog.ndjson")
        groups = {kind: [] for kind in ("ShelfMember", "Rating", "Comment")}
        for row in read_records(archive, "journal.ndjson"):
            kind = row.get("type")
            if kind in groups:
                content = row.get("content")
                if not isinstance(content, dict):
                    raise ExportError(f"{kind} 缺少 content 对象")
                groups[kind].append(content)
        shelves, ratings, comments = (
            index_records(groups[kind], "withRegardTo", kind) for kind in groups
        )

    if not shelves:
        raise ExportError("导出包中没有 ShelfMember 收藏记录，未覆盖现有数据")
    cache_uuid, cache_title = read_poster_cache(poster_cache) if poster_cache else ({}, {})
    # 有同名同年份条目时，旧缓存无法确定身份，使用各自的 NeoDB 封面。
    title_counts = Counter(
        (item.get("display_title") or item.get("title"), item.get("category"), publication_year(item))
        for identity, item in catalog.items() if identity in shelves
    )
    result = []
    reused_posters = 0
    for identity, shelf in shelves.items():
        if identity not in catalog:
            raise ExportError(f"收藏条目在 catalog.ndjson 中不存在：{identity}")
        item = catalog[identity]
        title = item.get("display_title") or item.get("title")
        category = item.get("category")
        status = shelf.get("status")
        if not isinstance(title, str) or not title:
            raise ExportError(f"条目缺少标题：{identity}")
        if category not in CATEGORIES:
            raise ExportError(f"页面不支持分类 {category!r}：{identity}")
        if status not in STATUSES:
            raise ExportError(f"页面不支持状态 {status!r}：{identity}")
        rating = ratings.get(identity, {}).get("value")
        if rating is not None and (type(rating) not in (int, float) or not 1 <= rating <= 10):
            raise ExportError(f"个人评分必须在 1–10 之间：{identity}")
        comment = comments.get(identity, {}).get("content") or ""
        if not isinstance(comment, str):
            raise ExportError(f"短评必须是文本：{identity}")
        year = publication_year(item)
        uuid = item.get("uuid") or identity.rstrip("/").rsplit("/", 1)[-1]
        poster = item.get("cover_image_url") or None
        if poster:
            poster = urljoin(identity, poster)
        key = (title, category, year)
        candidates = cache_uuid.get(uuid, [])
        if not candidates and title_counts[key] == 1:
            candidates = cache_title.get(key, [])
        if len(candidates) == 1 and candidates[0].get("poster"):
            poster = candidates[0]["poster"]
            reused_posters += 1
        result.append({
            "title": title,
            "category": category,
            "status": status,
            "rating": rating,
            "comment": comment,
            "year": year,
            "timestamp": mark_date(shelf.get("published")),
            "poster": poster,
            "uuid": uuid,
        })
    result.sort(key=lambda row: row["timestamp"], reverse=True)
    return result, reused_posters


def write_media(path, rows):
    """先完整生成临时文件，再替换；解析失败时保留上一次可用数据。"""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(rows, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def archive_hash(path):
    """以 ZIP 内容而非名称、大小或修改时间判断是否更新。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pending_archive(data_dir, output):
    """返回待导入 ZIP 和成功后要保存的状态；无变化时不写任何文件。"""
    matches = sorted(
        path for path in data_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".zip"
    ) if data_dir.exists() else []
    if not matches:
        print("data/ 中没有 ZIP，当前数据保持不变")
        return None
    if len(matches) > 1:
        raise ExportError(f"data/ 中有 {len(matches)} 个 ZIP，请只保留一个；当前数据保持不变")
    state_path = data_dir / ".import-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ExportError("data/.import-state.json 必须是 JSON 对象")
    digest = archive_hash(matches[0])
    output_key = str(output.resolve())
    if state.get(output_key) == digest and output.is_file():
        print("ZIP 哈希未变化，当前数据保持不变")
        return None
    state[output_key] = digest
    return matches[0], state_path, state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?", type=Path, help="指定 ZIP 时直接解析；省略时检查 data/ 中唯一 ZIP 的哈希，仅变化时更新")
    parser.add_argument("-o", "--output", type=Path, default=ROOT / "media_data.json", help="输出路径（默认脚本旁的 media_data.json）")
    parser.add_argument("--poster-cache", type=Path, help="从指定 JSON 复用海报（默认使用现有输出文件）")
    parser.add_argument("--no-poster-cache", action="store_true", help="全部使用导出包中的 NeoDB 封面地址")
    args = parser.parse_args(argv)
    if args.poster_cache and args.no_poster_cache:
        parser.error("--poster-cache 与 --no-poster-cache 不能同时使用")
    try:
        archive = args.archive
        pending = None
        if archive is None:
            pending = pending_archive(ROOT / "data", args.output)
            if pending is None:
                return 0
            archive, state_path, state = pending
        if args.output.resolve() == archive.resolve():
            raise ExportError("输出路径不能与输入 ZIP 相同")
        cache = None if args.no_poster_cache else (args.poster_cache or args.output)
        if args.poster_cache and not args.poster_cache.is_file():
            raise ExportError(f"海报缓存不存在：{args.poster_cache}")
        rows, reused = build_media(archive, cache)
        write_media(args.output, rows)
        # 只有输出成功才记录哈希；失败的 ZIP 下次仍会重试。
        if pending is not None:
            try:
                write_media(state_path, state)
            except OSError as exc:
                print(f"数据已更新，但保存 ZIP 哈希失败，下次运行将重试：{exc}", file=sys.stderr)
                return 1
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"解析失败：{exc}", file=sys.stderr)
        return 1
    print(f"已生成 {args.output}：{len(rows)} 条收藏记录")
    print("分类：" + "，".join(f"{key} {count}" for key, count in sorted(Counter(row["category"] for row in rows).items())))
    print("状态：" + "，".join(f"{key} {count}" for key, count in sorted(Counter(row["status"] for row in rows).items())))
    print(f"复用海报 {reused} 条；无封面 {sum(not row['poster'] for row in rows)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
