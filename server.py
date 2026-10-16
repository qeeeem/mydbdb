"""本机书影音服务：现有添加条目表单 → NeoDB → media_data.json。"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import socket
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fetch_posters import CATEGORIES, ROOT, STATUSES, mark_date, publication_year, write_media

NEODB = "https://neodb.social"


class AppError(Exception):
    def __init__(self, message, status=400, synced=False):
        super().__init__(message)
        self.status = status
        self.synced = synced


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不将授权令牌转发到重定向地址，也不自动重放写请求。
        return None


class NeoDBClient:
    def __init__(self):
        self.opener = build_opener(NoRedirect)

    def request(self, method, path, token=None, payload=None):
        headers = {"Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9", "User-Agent": "mydbdb/1.0"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(Request(NEODB + path, data=body, headers=headers, method=method), timeout=25) as response:
                return json.load(response)
        except HTTPError as exc:
            exc.close()
            if exc.code == 401:
                raise AppError("NeoDB 授权已失效或缺少读写权限，请重新连接", 401) from exc
            if exc.code == 403:
                raise AppError("NeoDB 拒绝了请求，请检查账号授权和访问权限", 403) from exc
            if exc.code in (301, 302, 307, 308):
                raise AppError("条目地址已变化，请重新搜索并选择", 409) from exc
            if exc.code == 404:
                raise AppError("NeoDB 未找到该条目或收藏", 404) from exc
            if exc.code == 429:
                raise AppError("NeoDB 请求过于频繁，请稍后重试", 429) from exc
            raise AppError(f"NeoDB 请求失败（HTTP {exc.code}），请稍后重试", 502) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AppError("无法确认 NeoDB 请求结果，请检查网络后重试", 502) from exc
        except (ValueError, UnicodeError) as exc:
            raise AppError("NeoDB 返回了无效数据，请稍后重试", 502) from exc


def safe_poster(value):
    if not value:
        return None
    url = urljoin(NEODB, value)
    return url if urlsplit(url).scheme in ("https", "http") else None


def item_summary(item):
    return {
        "uuid": item["uuid"], "title": item.get("title") or item.get("display_title") or "未命名条目",
        "category": item["category"], "year": publication_year(item),
        "poster": safe_poster(item.get("cover_image_url")),
    }


class Application:
    def __init__(self, root=ROOT, client=None, *, state_dir=None):
        self.root = Path(root)
        self.data_path = (Path(state_dir) if state_dir else self.root) / "media_data.json"
        self.client = client or NeoDBClient()
        # 放在网站目录之外，避免旧的通用静态服务器暴露账号令牌。
        self.config_path = (Path(state_dir) if state_dir else
                            self.root.parent / ("." + self.root.name + "-local")) / "neodb.json"
        config = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}
        self.token = config.get("token")
        self.items = {}
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.RLock()

    def require_token(self):
        if not self.token:
            raise AppError("请先在添加条目窗口连接 NeoDB", 401)
        return self.token

    def session(self):
        result = {"csrf": self.csrf, "connected": False, "username": None}
        if self.token:
            try:
                profile = self.client.request("GET", "/api/me", self.token)
                result.update(connected=True, username=profile["username"])
            except AppError as exc:
                result["error"] = str(exc)
        return result

    def connect(self, payload):
        token = payload.get("token")
        if not isinstance(token, str) or not token.strip() or len(token) > 4096 or any(c.isspace() for c in token.strip()):
            raise AppError("请输入有效的 NeoDB 访问令牌")
        token = token.strip()
        profile = self.client.request("GET", "/api/me", token)
        # 验证成功后再替换现有授权；不将令牌返回浏览器。
        with self.lock:
            self.config_path.parent.mkdir(exist_ok=True)
            write_media(self.config_path, {"token": token})
            self.token = token
        return {"ok": True, "username": profile["username"]}

    def search(self, query, category):
        if category not in CATEGORIES or not query.strip() or len(query) > 300:
            raise AppError("请输入标题并选择有效分类")
        response = self.client.request("GET", "/api/catalog/search?" + urlencode({"query": query.strip(), "category": category}))
        data = response.get("data")
        if not isinstance(data, list):
            raise AppError("NeoDB 搜索结果格式不正确", 502)
        items = [item for item in data if item.get("category") == category and re.fullmatch(r"[A-Za-z0-9]{1,64}", item.get("uuid", ""))]
        with self.lock:
            self.items.update({item["uuid"]: item for item in items})
        return [item_summary(item) for item in items]

    def get_mark(self, uuid):
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", uuid):
            raise AppError("无效的条目标识")
        try:
            return self.client.request("GET", "/api/me/shelf/item/" + uuid, self.require_token())
        except AppError as exc:
            if exc.status == 404:
                return None
            raise

    def selection(self, uuid):
        self.require_token()
        item = self.items.get(uuid)
        if not item:
            raise AppError("搜索结果已失效，请重新搜索并选择条目", 409)
        # 搜索接口可能只返回通用字段；选择后读取详情，补齐发行年份。
        path = item.get("api_url", "")
        if not re.fullmatch(r"/api/(?:book|movie|tv(?:/season|/episode)?|album|game|podcast(?:/episode)?|performance(?:/production)?)/" + re.escape(uuid), path):
            raise AppError("NeoDB 返回的条目地址无效", 502)
        detail = self.client.request("GET", path)
        if detail.get("uuid") != uuid or detail.get("category") != item.get("category"):
            raise AppError("NeoDB 条目信息已变化，请重新搜索", 409)
        with self.lock:
            self.items[uuid] = detail
        return {"mark": self.get_mark(uuid), "item": item_summary(detail)}

    def add(self, payload):
        uuid = payload.get("uuid")
        status, rating, comment, visibility = (payload.get(k) for k in ("status", "rating", "comment", "visibility"))
        if not isinstance(uuid, str) or uuid not in self.items:
            raise AppError("搜索结果已失效，请重新搜索并选择条目", 409)
        if not isinstance(status, str) or status not in STATUSES:
            raise AppError("请选择有效的收藏状态")
        if rating is not None and (type(rating) is not int or not 1 <= rating <= 10):
            raise AppError("评分必须是 1–10 的整数，或留空")
        if type(visibility) is not int or visibility not in (0, 1, 2):
            raise AppError("请选择可见范围")
        if not isinstance(comment, str) or len(comment) > 10000:
            raise AppError("短评须为文本，且不超过 10000 字")
        with self.lock:
            token = self.require_token()
            output = self.data_path
            rows = json.loads(output.read_text(encoding="utf-8")) if output.exists() else []
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise AppError("本地数据格式不正确，未提交 NeoDB", 500)
            previous = self.get_mark(uuid)
            mark = {"shelf_type": status, "rating_grade": rating or 0,
                    "comment_text": comment, "visibility": visibility,
                    "tags": previous.get("tags", []) if previous else [], "post_to_fediverse": False}
            # 已有条目保留原标记日期和标签；只有表单中的字段会修改。
            if previous:
                mark["created_time"] = previous["created_time"]
            same = bool(previous) and previous.get("visibility") == visibility and all(
                (previous.get(key) or default) == mark[key]
                for key, default in (("shelf_type", ""), ("rating_grade", 0), ("comment_text", ""))
            )
            if not same:
                self.client.request("POST", "/api/me/shelf/item/" + uuid, token, mark)
                try:
                    confirmed = self.get_mark(uuid)
                except AppError as exc:
                    raise AppError("已同步 NeoDB，但读取确认结果失败；请再次确认添加以刷新本地数据", 502, synced=True) from exc
            else:
                confirmed = previous
            if not confirmed or confirmed.get("item", {}).get("uuid") != uuid:
                raise AppError("已提交 NeoDB，但无法确认条目；请重新搜索后重试", 502, synced=True)
            if (confirmed.get("shelf_type") != status or confirmed.get("visibility") != visibility
                    or (confirmed.get("rating_grade") or 0) != (rating or 0)
                    or (confirmed.get("comment_text") or "") != comment):
                raise AppError("NeoDB 返回的收藏状态与提交内容不一致，请重新选择条目确认", 409, synced=True)
            row = item_summary({**self.items[uuid], **confirmed["item"]})
            row.update(status=confirmed["shelf_type"], rating=confirmed.get("rating_grade") or None,
                       comment=confirmed.get("comment_text") or "", timestamp=mark_date(confirmed["created_time"]))
            old = next((r for r in rows if r.get("uuid") == uuid), None)
            if old and old.get("poster"):
                row["poster"] = old["poster"]
            rows = [r for r in rows if r.get("uuid") != uuid] + [row]
            rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
            try:
                write_media(output, rows)
            except OSError as exc:
                raise AppError("已同步 NeoDB，但本地保存失败；请检查文件权限后再次确认添加", 500, synced=True) from exc
            return {"ok": True, "synced": True, "item": row}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        # 不记录查询词、短评、请求体或令牌。
        pass

    def send_body(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def check_origin(self, writing=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise AppError("仅允许本机访问", 403)
        origin = self.headers.get("Origin")
        origins = {"http://" + host for host in hosts}
        if self.server.public_origin:
            origins.add(self.server.public_origin)
        if origin is not None and origin not in origins:
            raise AppError("不允许跨站访问本机服务", 403)
        if writing and not secrets.compare_digest(self.headers.get("X-Mydbdb-CSRF", ""), self.server.app.csrf):
            raise AppError("页面会话已失效，请刷新页面", 403)

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, writing):
        try:
            raw_body = None
            if writing:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise AppError("请求体过大或为空", 413)
                # 先接收有界请求体，避免 Windows 因未读数据重置连接、吞掉错误响应。
                raw_body = self.rfile.read(length)
            self.check_origin(writing)
            url = urlsplit(self.path)
            app = self.server.app
            if writing:
                if self.headers.get_content_type() != "application/json":
                    raise AppError("请求须使用 JSON", 415)
                payload = json.loads(raw_body)
                if not isinstance(payload, dict):
                    raise AppError("请求须为 JSON 对象")
                if url.path == "/api/connect":
                    result = app.connect(payload)
                elif url.path == "/api/add":
                    result = app.add(payload)
                else:
                    raise AppError("接口不存在", 404)
                self.send_body(200, result)
            elif url.path == "/_health":
                rows = json.loads(app.data_path.read_text(encoding="utf-8"))
                if not isinstance(rows, list):
                    raise AppError("收藏数据格式不正确", 500)
                release_file = app.root / "RELEASE"
                self.send_body(200, {"ok": True, "count": len(rows),
                                     "release": release_file.read_text().strip() if release_file.exists() else "local"})
            elif url.path == "/api/session":
                self.send_body(200, app.session())
            elif url.path == "/api/search":
                params = parse_qs(url.query)
                self.send_body(200, app.search(params.get("q", [""])[0], params.get("cat", [""])[0]))
            elif url.path == "/api/mark":
                self.send_body(200, app.selection(parse_qs(url.query).get("uuid", [""])[0]))
            elif url.path in ("/", "/index.html", "/add.js", "/media_data.json"):
                name = "index.html" if url.path == "/" else url.path[1:]
                file = app.data_path if name == "media_data.json" else app.root / name
                if not file.is_file():
                    raise AppError("文件不存在", 404)
                mime = {".html": "text/html", ".js": "text/javascript", ".json": "application/json"}[file.suffix]
                self.send_body(200, file.read_bytes(), mime + "; charset=utf-8")
            else:
                raise AppError("文件或接口不存在", 404)
        except AppError as exc:
            self.send_body(exc.status, {"ok": False, "error": str(exc), "synced": exc.synced})
        except (ValueError, UnicodeError):
            self.send_body(400, {"ok": False, "error": "数据格式不正确，请检查输入和本地 JSON 文件"})
        except OSError:
            self.send_body(500, {"ok": False, "error": "无法读写本地文件，请检查文件权限"})


class LocalHTTPServer(ThreadingHTTPServer):
    # Unix permits quick service restarts after TIME_WAIT without sharing an
    # active listener. Windows uses exclusive binding instead.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(port=8000, root=ROOT, client=None, *, state_dir=None, public_origin=None):
    if public_origin:
        parsed = urlsplit(public_origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("公开地址必须是 HTTPS origin，例如 https://8.148.157.132:18443")
    app = Application(root, client, state_dir=state_dir)
    server = LocalHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    server.public_origin = public_origin
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state-dir", type=Path, help="持久化数据与 NeoDB 授权目录；默认保持本地目录布局")
    parser.add_argument("--public-origin", help="受登录保护的 HTTPS 反向代理地址；后端仍仅监听本机")
    args = parser.parse_args()
    try:
        server = create_server(args.port, state_dir=args.state_dir, public_origin=args.public_origin)
    except OSError:
        parser.exit(1, f"无法启动端口 {args.port}，可改用 python server.py --port 8001\n")
    print(f"打开 http://127.0.0.1:{server.server_port} ，在「＋ 添加条目」中连接 NeoDB。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
