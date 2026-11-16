# -*- coding: utf-8 -*-
"""renhua-web —— 飞牛 fnOS 端 Web 面板。

物理屏（/dev/fb0）上的那套状态由 renhua-console.py 采集；本进程只负责：
  1) 用 HTTP 把页面发给浏览器（应用中心 iframe 打开）；
  2) 把浏览器动作（切页 / 起停扫描 / 起停测速）转给控制台的本地控制通道（127.0.0.1:8771）。

只依赖 python3 标准库，不需要额外 pip 包。
"""
import json
import os
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
CTRL_HOST = "127.0.0.1"
CTRL_PORT = int(os.environ.get("RENHUA_CTRL_PORT", "8771"))
PORT = int(os.environ.get("RENHUA_WEB_PORT", "8770"))

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def ctrl(obj, timeout=8.0):
    """向控制台发一条 JSON 命令，拿回 JSON。"""
    with socket.create_connection((CTRL_HOST, CTRL_PORT), timeout=timeout) as s:
        s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        s.settimeout(timeout)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8") or "{}")


def hostname():
    try:
        return open("/proc/sys/kernel/hostname").read().strip()
    except OSError:
        return ""


def fallback_status(detail):
    """控制台没起来时的兜底：至少把盘列出来，界面不至于空白。"""
    disks = []
    try:
        for name in sorted(os.listdir("/sys/block")):
            if not name.startswith("sd"):
                continue
            base = "/sys/block/" + name
            def rd(p, d=""):
                try:
                    return open(p).read().strip() or d
                except OSError:
                    return d
            try:
                sectors = int(rd(base + "/size", "0"))
            except ValueError:
                sectors = 0
            disks.append({"name": name, "model": rd(base + "/device/model"), "sn": rd(base + "/device/serial"),
                          "size_gb": round(sectors * 512 / 1e9, 1), "ata": None, "spd": "无盘",
                          "state": "", "temp": None, "health": "", "verified": False, "can_scan": False})
    except OSError:
        pass
    return {"ok": True, "console": False, "host": hostname(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "page": -1, "pages": ["F1 帮助", "F2 坏块扫描", "F3 端口识别", "F4 健康体检", "F5 报错翻译", "F6 测速"],
            "disks": disks, "links": {}, "scan": {}, "scan_advice": "", "speed": {},
            "hits": [], "verified": {}, "msg": detail, "stages": []}


class Handler(BaseHTTPRequestHandler):
    server_version = "renhua-web/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _api_status(self):
        try:
            res = ctrl({"cmd": "status"})
        except Exception as ex:
            return fallback_status("控制台未运行（%s）" % type(ex).__name__)
        # 控制台返回 {"ok":true,"data":{...}}；面板读的是 data，console 标记要放进 data 里
        data = res.get("data") if isinstance(res, dict) else None
        if isinstance(data, dict):
            data["console"] = True
        else:
            res["console"] = True
        return res

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/status":
            return self._send(200, self._api_status())
        if path in ("/", ""):
            path = "/index.html"
        # 静态文件：限制在 web 目录内
        name = urllib.parse.unquote(path.lstrip("/"))
        full = os.path.normpath(os.path.join(WEB, name))
        if not full.startswith(os.path.normpath(WEB)) or not os.path.isfile(full):
            return self._send(404, {"ok": False, "detail": "not found"})
        ctype = MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            obj = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except Exception:
            obj = {}
        if path != "/api/action":
            return self._send(404, {"ok": False, "detail": "not found"})
        cmd = str(obj.get("cmd") or "")
        try:
            res = ctrl(obj)
        except Exception as ex:
            return self._send(503, {"ok": False, "detail": "控制台未运行（%s），%s 未执行" % (type(ex).__name__, cmd)})
        return self._send(200, res)


def main():
    port = PORT
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print("renhua-web listening on 0.0.0.0:%d (web=%s, ctrl=%s:%d)" % (port, WEB, CTRL_HOST, CTRL_PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
