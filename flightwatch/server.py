"""本地 Web 服务：静态页面 + 一个 JSON 接口。零依赖（只用标准库）。"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import api, report


def _web_dir() -> str:
    """静态资源目录。PyInstaller 打包后资源在 sys._MEIPASS 下。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "flightwatch", "web")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


WEB_DIR = _web_dir()

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
# 允许少量并发查询；真正的频率控制由 provider 内部的限速器兜底。
# （之前用一个全局锁串行化整个查询，页面首次自动查询会把用户后来的查询堵住几十秒）
_RUN_SLOTS = threading.Semaphore(2)
CACHE_TTL = 600.0

# 异步任务：实时查询要几十秒，浏览器不能干等一个 HTTP 响应
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_INFLIGHT: dict[str, str] = {}       # 查询 key -> 正在跑的 job id（相同查询不重复打接口）
JOB_KEEP_SECONDS = 1800.0


def _prune_jobs() -> None:
    now = time.time()
    with _JOBS_LOCK:
        for jid in [k for k, v in _JOBS.items() if now - v.get("started", now) > JOB_KEEP_SECONDS]:
            _JOBS.pop(jid, None)


def _start_job(fn, key: str | None = None) -> str:
    """在后台线程跑一次查询，返回 job id；进度写进 _JOBS。"""
    _prune_jobs()
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "state": "running", "done": 0, "total": 0,
        "label": "正在准备查询…", "started": time.time(), "payload": None, "error": None,
    }
    with _JOBS_LOCK:
        _JOBS[jid] = job
        if key:
            _INFLIGHT[key] = jid

    def progress(done, total, label):
        job["done"] = int(done or 0)
        job["total"] = int(total or 0)
        job["label"] = str(label or "")

    def worker():
        try:
            with _RUN_SLOTS:
                job["payload"] = fn(progress)
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001
            job["state"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            job["finished"] = time.time()
            if key:
                with _JOBS_LOCK:
                    if _INFLIGHT.get(key) == jid:
                        _INFLIGHT.pop(key, None)

    threading.Thread(target=worker, daemon=True).start()
    return jid

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _execute(query, offline: bool, max_hubs: int, progress=None, refresh: bool = False) -> dict:
    """有返程日期就走往返整合，否则按单程处理。"""
    if getattr(query, "return_date", ""):
        return api.run_roundtrip(query, offline=offline, max_hubs=max_hubs,
                                 progress=progress, refresh=refresh)
    return api.run(query, offline=offline, max_hubs=max_hubs, progress=progress, refresh=refresh)


class Handler(BaseHTTPRequestHandler):
    server_version = "FlightWatch/0.1"

    def log_message(self, fmt, *args):  # 安静一点
        if "/api/plan" in (args[0] if args else ""):
            return
        print(f"[web] {fmt % args}")

    # ------------------------------------------------------------------ 输出
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    # ------------------------------------------------------------------ 路由
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/plan":
            return self._api_plan(parse_qs(parsed.query))
        if parsed.path == "/api/plan/start":
            return self._api_plan_start(parse_qs(parsed.query))
        if parsed.path.startswith("/api/job/"):
            return self._api_job(parsed.path.rsplit("/", 1)[-1])
        if parsed.path == "/api/health":
            return self._json(200, {"ok": True, "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return self._static(parsed.path)

    # ------------------------------------------------------------------ 业务
    @staticmethod
    def _parse(qs: dict):
        """把查询串解析成 (query, offline, max_hubs, cache_key)；失败抛 ValueError。"""
        def get(name, default=""):
            return (qs.get(name) or [default])[0]

        origin, destination = get("origin"), get("destination")
        date = get("date") or time.strftime("%Y-%m-%d")
        if not origin or not destination:
            raise ValueError("缺少 origin / destination 参数")

        hubs = [h for h in get("hubs").replace("，", ",").split(",") if h.strip()]
        offline = get("offline", "0") in ("1", "true", "yes", "on")
        refresh = get("refresh", "0") in ("1", "true", "yes", "on")
        try:
            window = int(get("window", "0") or 0)
            max_hubs = int(get("max_hubs", str(api.DEFAULT_MAX_HUBS)) or api.DEFAULT_MAX_HUBS)
        except ValueError:
            window, max_hubs = 0, api.DEFAULT_MAX_HUBS

        query = api.make_query(
            origin, destination, date, date_window=window, hubs=hubs,
            allow_remote=not offline, return_date=get("return_date") or get("ret"),
        )
        key = json.dumps(
            [origin, destination, date, hubs, window, offline, max_hubs, refresh,
             query.return_date],
            ensure_ascii=False,
        )
        return query, offline, max_hubs, refresh, key

    def _api_plan(self, qs: dict):
        """同步接口（命令行/脚本用）。浏览器走 /api/plan/start + /api/job/<id>。"""
        try:
            query, offline, max_hubs, refresh, key = self._parse(qs)
        except ValueError as e:
            return self._json(400, {"ok": False, "errors": [str(e)], "plans": []})

        if not refresh:
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
                if hit and time.time() - hit[0] < CACHE_TTL:
                    payload = dict(hit[1])
                    payload["cached"] = True
                    return self._json(200, payload)

        try:
            with _RUN_SLOTS:
                payload = _execute(query, offline, max_hubs, refresh=refresh)
        except Exception as e:  # noqa: BLE001  任何异常都返回结构化错误，别让前端白屏
            return self._json(500, {"ok": False, "errors": [f"{type(e).__name__}: {e}"], "plans": []})

        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), payload)
        payload["cached"] = False
        return self._json(200, payload)

    def _api_plan_start(self, qs: dict):
        """异步接口：立刻返回 job id，前端轮询进度，避免几十秒的白屏等待。"""
        try:
            query, offline, max_hubs, refresh, key = self._parse(qs)
        except ValueError as e:
            return self._json(400, {"ok": False, "errors": [str(e)], "plans": []})

        if not refresh:
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
                if hit and time.time() - hit[0] < CACHE_TTL:
                    payload = dict(hit[1])
                    payload["cached"] = True
                    jid = _start_job(lambda progress: payload)
                    return self._json(200, {"job": jid, "cached": True})

        # 相同查询已经在跑：直接把那个 job 交给前端，别重复打接口
        with _JOBS_LOCK:
            running = _INFLIGHT.get(key)
            if running and _JOBS.get(running, {}).get("state") == "running":
                return self._json(200, {"job": running, "cached": False, "deduped": True})

        def work(progress):
            payload = _execute(query, offline, max_hubs, progress=progress, refresh=refresh)
            with _CACHE_LOCK:
                _CACHE[key] = (time.time(), payload)
            return payload

        jid = _start_job(work, key=key)
        return self._json(200, {"job": jid, "cached": False})

    def _api_job(self, jid: str):
        with _JOBS_LOCK:
            job = _JOBS.get(jid)
            if job is None:
                return self._json(404, {"state": "missing", "error": "任务不存在或已过期，请重新查询"})
            state = job["state"]
            elapsed = round(time.time() - job["started"], 1)
            if state == "running":
                return self._json(200, {
                    "state": "running",
                    "done": job["done"],
                    "total": job["total"],
                    "label": job["label"],
                    "elapsed": elapsed,
                })
            if state == "error":
                return self._json(200, {"state": "error", "error": job["error"], "elapsed": elapsed})
            return self._json(200, {"state": "done", "payload": job["payload"], "elapsed": elapsed})

    # ------------------------------------------------------------------ 静态资源
    def _static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
            return self._json(404, {"ok": False, "errors": [f"not found: {path}"]})
        ctype = _MIME.get(os.path.splitext(full)[1].lower()) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._json(500, {"ok": False, "errors": [str(e)]})
        self._send(200, body, ctype)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    if not os.path.isdir(WEB_DIR):
        print(f"[warn] 找不到界面目录 {WEB_DIR}，只提供 /api/plan 接口")

    # 端口被占用时自动往后找一个空闲端口，别让用户看到 traceback
    httpd = None
    last_err = None
    for candidate in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
            port = candidate
            break
        except OSError as e:
            last_err = e
            print(f"  端口 {candidate} 被占用，换下一个……")
    if httpd is None:
        raise SystemExit(f"启动失败：{last_err}")

    url = f"http://{host}:{port}/"
    print("=" * 66)
    print(f"  分舱段购票助手已启动：{url}")
    print("  在页面里填出发/到达/日期即可；勾选「离线演示」可以不联网秒出结果")
    print("  关掉这个黑色窗口 = 停止服务")
    print("=" * 66)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


def export_report(origin, destination, date, out_path, offline=False, hubs=None, window=0,
                  return_date="") -> str:
    query = api.make_query(origin, destination, date, date_window=window, hubs=hubs or [],
                           return_date=return_date)
    payload = _execute(query, offline, api.DEFAULT_MAX_HUBS)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report.render_html(payload))
    return out_path
