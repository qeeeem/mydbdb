"""收藏浏览和 ZIP 导入；部署时由 Nginx 统一验证网站密码。"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import threading
from urllib.parse import urlsplit

from fetch_posters import ROOT
from data_tools import AppError, DataTools, configure_owner


class Application:
    def __init__(self, root=ROOT, *, state_dir=None):
        self.root = Path(root)
        self.data_path = (Path(state_dir) if state_dir else self.root) / "media_data.json"
        self.private_dir = (Path(state_dir) if state_dir else
                            self.root.parent / ("." + self.root.name + "-local"))
        self.owner_path = self.private_dir / "import-owner.json"
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.revision = 0
        self.data = DataTools(self)


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
                if url.path == "/api/import/start":
                    result = app.data.begin_import(payload)
                elif url.path == "/api/import/chunk":
                    result = app.data.chunk(payload)
                elif url.path == "/api/import/finish":
                    result = app.data.finish_import(payload)
                elif url.path == "/api/data/commit":
                    result = app.data.commit(payload)
                elif url.path == "/api/data/cancel":
                    result = app.data.cancel(payload)
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
            elif url.path == "/api/data":
                self.send_body(200, app.data.status())
            elif url.path == "/api/import/library.js":
                self.send_body(200, (app.root / "vendor" / "fflate.js").read_bytes(), "text/javascript; charset=utf-8")
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
            self.send_body(exc.status, {"ok": False, "error": str(exc)})
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


def create_server(port=8000, root=ROOT, *, state_dir=None, public_origin=None):
    if public_origin:
        parsed = urlsplit(public_origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("公开地址必须是 HTTPS origin，例如 https://8.148.157.132:18443")
    app = Application(root, state_dir=state_dir)
    server = LocalHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    server.public_origin = public_origin
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state-dir", type=Path, help="持久化收藏和私有导入配置目录")
    parser.add_argument("--public-origin", help="受登录保护的 HTTPS 反向代理地址；后端仍仅监听本机")
    parser.add_argument("--set-import-owner", type=Path, help="由管理员从可信 ZIP 配置允许导入的账号，不启动服务")
    args = parser.parse_args()
    if args.set_import_owner:
        app = Application(state_dir=args.state_dir)
        configure_owner(args.set_import_owner, app.owner_path)
        print("已配置导入权限")
        return
    try:
        server = create_server(args.port, state_dir=args.state_dir, public_origin=args.public_origin)
    except OSError:
        parser.exit(1, f"无法启动端口 {args.port}，可改用 python server.py --port 8001\n")
    print(f"打开 http://127.0.0.1:{server.server_port} ，使用「数据管理」导入 ZIP。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
