"""本地 TCP 端口探测判重（替代 GUI 版的 QSharedMemory+QLocalServer 单实例）。

判据：bind 127.0.0.1:{port} 失败 = 端口已被占用；再用 /api/health 复核
响应体含 drifox_server 字段即确认是本服务实例。
"""
import json
import socket
import urllib.request


def pick_port(preferred: int = 0) -> int:
    """preferred=0 时向系统申请一个空闲端口（bind 0 → 读回实际端口）。"""
    if preferred != 0:
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def is_in_use(port: int) -> bool:
    """尝试 bind 判重：成功（并立即释放）= 空闲；失败 = 已占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def probe_existing(port: int, timeout: float = 1.0) -> dict | None:
    """/api/health 复核：确认占用者确为 DriFox 服务则返回其握手信息。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("drifox_server")
    except Exception:
        return None
