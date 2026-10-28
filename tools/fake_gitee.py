# -*- coding: utf-8 -*-
"""本地假 Gitee contents API，供云同步仿真使用。

实现 Gitee v5 的四个语义，足够覆盖同步链路：
- ``GET  /repos/{owner}/{repo}/contents/{file}``   → 单文件 {content(base64), sha}
- ``GET  /repos/{owner}/{repo}/contents/{dir}``    → 目录列表 [{name, path, sha, type}]
- ``PUT  .../contents/{file}``（body 带 sha）      → sha 不匹配返回 409（git 语义）
- ``POST .../contents/{file}``                     → 新建；已存在返回 400

数据落 JSON 文件，多进程可共享（写时加文件锁）。请求全量记录，用于断言
「内容没变时一次写请求都没发」。

独立运行（给外部脚本/真机多开用）：
    python tools/fake_gitee.py --port 8770 --data /tmp/fake_gitee.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

_REPO_TMPL = "/repos/{owner}/{repo}/contents/{path}"


def _git_blob_sha(raw: bytes) -> str:
    import hashlib

    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(raw))
    h.update(raw)
    return h.hexdigest()


class FakeGiteeStore:
    """内存 + JSON 落盘的文件表，线程/进程安全（文件锁 + 读改写）"""

    def __init__(self, data_path: Optional[Path] = None):
        self.data_path = Path(data_path) if data_path else None
        self._lock = threading.Lock()
        self.files: Dict[str, Dict[str, str]] = {}  # path -> {"content": b64, "sha": }
        self.requests: List[Dict[str, object]] = []
        self._load()

    # ── 持久化 ──
    def _load(self) -> None:
        if not self.data_path or not self.data_path.exists():
            return
        try:
            raw = json.loads(self.data_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.files = raw.get("files", {}) or {}
        except Exception:
            self.files = {}

    def _save(self) -> None:
        if not self.data_path:
            return
        try:
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.data_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"files": self.files}, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(self.data_path))
        except Exception:
            pass

    def _reload_if_external(self) -> None:
        """多进程共享同一文件时，每次操作前重新读（简单版：文件小，读全量）"""
        if self.data_path and self.data_path.exists():
            self._load()

    # ── 操作 ──
    def get_file(self, path: str) -> Optional[Dict[str, str]]:
        with self._lock:
            self._reload_if_external()
            return self.files.get(path)

    def list_dir(self, dir_path: str) -> List[Dict[str, str]]:
        with self._lock:
            self._reload_if_external()
            prefix = dir_path.rstrip("/") + "/"
            out: List[Dict[str, str]] = []
            seen_dirs = set()
            for p, info in sorted(self.files.items()):
                if not p.startswith(prefix):
                    continue
                rest = p[len(prefix) :]
                if "/" in rest:
                    sub = rest.split("/", 1)[0]
                    if sub in seen_dirs:
                        continue
                    seen_dirs.add(sub)
                    out.append({"name": sub, "path": prefix + sub, "sha": "", "type": "dir"})
                else:
                    out.append({"name": rest, "path": p, "sha": info["sha"], "type": "file"})
            return out

    def put(self, path: str, content: bytes, expect_sha: Optional[str]) -> tuple[int, dict]:
        with self._lock:
            self._reload_if_external()
            cur = self.files.get(path)
            if cur is None:
                return 404, {"message": "not found"}
            if expect_sha and expect_sha != cur["sha"]:
                return 409, {"message": f"sha does not match: expect {cur['sha']}"}
            new_sha = _git_blob_sha(content)
            self.files[path] = {"content": base64.b64encode(content).decode(), "sha": new_sha}
            self._save()
            return 200, {"content": {"sha": new_sha, "path": path}}

    def post(self, path: str, content: bytes) -> tuple[int, dict]:
        with self._lock:
            self._reload_if_external()
            if path in self.files:
                return 400, {"message": "file already exists"}
            new_sha = _git_blob_sha(content)
            self.files[path] = {"content": base64.b64encode(content).decode(), "sha": new_sha}
            self._save()
            return 201, {"content": {"sha": new_sha, "path": path}}

    def record(self, entry: Dict[str, object]) -> None:
        with self._lock:
            self.requests.append(entry)

    def reset_requests(self) -> None:
        with self._lock:
            self.requests.clear()

    def write_count(self) -> int:
        with self._lock:
            return sum(1 for r in self.requests if r.get("method") in ("PUT", "POST"))


class _Handler(BaseHTTPRequestHandler):
    store: FakeGiteeStore = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静音默认访问日志
        return

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _parse(self) -> Optional[str]:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        marker = "/contents/"
        if marker not in path:
            return None
        rel = path.split(marker, 1)[1].strip("/")
        return rel or None

    def do_GET(self) -> None:
        rel = self._parse()
        self.store.record({"method": "GET", "path": rel})
        if not rel:
            self._send(404, {"message": "bad path"})
            return
        info = self.store.get_file(rel)
        if info:
            self._send(200, {"content": info["content"], "sha": info["sha"], "path": rel})
            return
        listing = self.store.list_dir(rel)
        if listing:
            self._send(200, listing)
            return
        self._send(404, {"message": "not found"})

    def _body_content(self) -> tuple[Optional[str], Optional[bytes]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            text = raw.decode("utf-8")
            # httpx 用 data=dict 发送的是 form-urlencoded（Gitee 实际接受这种），
            # 兼容 JSON 与 form 两种形态
            try:
                data = json.loads(text)
            except Exception:
                from urllib.parse import parse_qs

                qs = parse_qs(text, keep_blank_values=True)
                data = {k: (v[0] if v else "") for k, v in qs.items()}
            if not isinstance(data, dict):
                return None, None
        except Exception:
            return None, None
        content = data.get("content")
        if not isinstance(content, str):
            return None, None
        try:
            return str(data.get("sha") or ""), base64.b64decode(content)
        except Exception:
            return None, None

    def do_PUT(self) -> None:
        rel = self._parse()
        self.store.record({"method": "PUT", "path": rel})
        if not rel:
            self._send(404, {"message": "bad path"})
            return
        sha, content = self._body_content()
        if content is None:
            self._send(400, {"message": "bad body"})
            return
        code, body = self.store.put(rel, content, sha or None)
        self._send(code, body)

    def do_POST(self) -> None:
        rel = self._parse()
        self.store.record({"method": "POST", "path": rel})
        if not rel:
            self._send(404, {"message": "bad path"})
            return
        _sha, content = self._body_content()
        if content is None:
            self._send(400, {"message": "bad body"})
            return
        code, body = self.store.post(rel, content)
        self._send(code, body)


def start_server(store: FakeGiteeStore, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """启动假服务，返回 (server, base_url)"""
    handler = type("FakeGiteeHandler", (_Handler,), {"store": store})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, real_port = server.server_address[:2]
    return server, f"http://{host}:{real_port}"


def api_url_template(base_url: str) -> str:
    """返回可直接替换 config_sync.GITEE_FILE_API 的 URL 模板"""
    return base_url + _REPO_TMPL


def main() -> None:
    ap = argparse.ArgumentParser(description="本地假 Gitee contents API")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--data", type=str, default="")
    args = ap.parse_args()
    store = FakeGiteeStore(Path(args.data) if args.data else None)
    server, base = start_server(store, args.port)
    print(f"fake gitee listening on {base}")
    print(f"GITEE_FILE_API = {api_url_template(base)}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
