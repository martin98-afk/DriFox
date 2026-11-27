# -*- coding: utf-8 -*-
"""插件工具模块的编译产物（code object）跨进程缓存。

动机（实测 2026-11-27）：`plugin_tool_loader._load_module` 冷启动期对 19 个
工具模块逐个 `compile(source, path, "exec")`，CPU 合计 35~38ms，占
`load_plugin_tools` 的七成以上。compile 成本与源码体积线性相关（实测约
120 ns/byte），而 `exec` 本身只占 3.8ms。即瓶颈在**重复编译**，不在 import。

⚠️ 为什么需要自建，不能直接用 `__pycache__`：
`plugin_tool_loader._load_module` 的文档字符串明确记录了它**刻意绕过** pyc
的原因——Windows 文件系统 mtime 精度低，快速写入后 exec 可能误复用旧 pyc，
导致热重载内容不更新。实测复现：同尺寸改写 + 强制恢复 mtime 后，标准
`SourceFileLoader` 仍然加载旧字节码（VALUE 读到 1 而非 2）。
故本缓存的键**不用 mtime**，改用源码内容哈希 + 解释器版本 + 显式版本号；
任一变化即重新编译。宁可多编译一次，绝不执行旧代码。

复用条件（全部满足才复用）：
- schema 版本一致（本模块变更语义时自增）
- 解释器版本一致（code object 跨版本二进制不兼容）
- 源码 sha256 一致

⚠️ 只缓存 code object，**绝不缓存 module 对象**：
每次加载仍会重新 `exec(code, module.__dict__)`，模块体照常执行一次，语义与
没有本缓存时完全一致。若将来有人为了「连 exec 也省掉」改成缓存 module，
模块级副作用会立刻变成缺陷——例如 `atexit.register()` 会在每次加载时重复
注册（真实样本：`.drifox/plugins/desktop-automation/tools/desktop.py` 的
`atexit.register(_shutdown_server)`），退出时被反复调用。改这一层前先想清楚
模块级副作用的幂等性。

调试开关：`PLUGIN_CODE_CACHE=0` 关闭缓存（只走即时 compile），用于 A/B 对照。

存盘格式取舍（有意识的决定，非随手写的）：当前用 JSON + base64 存 code object，
约 655KB。改存纯 marshal 二进制可省约 175KB 体积与一次 base64 解码，但会失去
可读性与形状校验（二进制坏掉只能整份丢弃，定位不到坏在哪）。实测稳态
0.126ms/模块（19 模块合计 2.4ms），base64 解码在该量级里最多占零点几毫秒，
收益不足以抵风险，故保持 JSON。

多实例并发（实测 2026-11-27，两个进程各写不同条目）：
- 读：安全。写方用 `os.replace` 原子替换，读者只会看到旧完整版或新完整版。
- 写：**最后一个写入者胜出（last-writer-wins）**，另一方独有的条目会丢，
  下次启动重新编译。这是有意识的取舍——跨进程合并 entries 需要「读到别人
  正在写的文件」，复杂度和收益不成比例，而多开 DriFox 的用户本就少见。
- 临时文件必须每进程唯一（`<stem>.<pid>.tmp`）。曾用固定名 `.tmp`，导致
  两个实例的写入交错到同一文件、产出两个 JSON 文档的混合体，复现率 3/5；
  这种损坏会整份回落重新编译（功能无损），但会让本优化在多实例场景下
  长期 0 命中——即被自己的目标场景废掉。`_sweep_stale_tmp()` 负责回收
  被强杀进程留下的 tmp。
"""
from __future__ import annotations

import base64
import hashlib
import json
import marshal
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger

# code object 布局随解释器版本变化，必须进键；判定语义变更时自增 SCHEMA。
# v2（2026-11-27）：键从「仅源码哈希」改为「源码哈希 + filename」，
# 自增让旧条目整体作废（旧键与新键不重叠，留着只占空间）。
_SCHEMA = 2
_FILENAME = "plugin_tool_code_cache.json"
_MAX_ENTRIES = 256
_FLUSH_DELAY = 2.0

_state: Dict[str, Any] = {
    "loaded": False,
    "enabled": os.environ.get("PLUGIN_CODE_CACHE", "1") != "0",
    "dirty": False,
    "scheduled": False,
    "entries": {},
}
_lock = threading.Lock()


def _cache_path() -> Optional[Path]:
    """落点：<app_data>/cache/plugin_tool_code_cache.json（与其它缓存同目录）。"""
    try:
        from app.utils.app_paths import get_app_data_dir

        return Path(get_app_data_dir()) / "cache" / _FILENAME
    except Exception:
        return None


def _load() -> Dict[str, Any]:
    """惰性载入（进程内一次）。任何异常 → 空表，退化为即时编译。"""
    st = _state
    if st["loaded"]:
        return st["entries"]
    st["loaded"] = True
    if not st["enabled"]:
        return st["entries"]
    path = _cache_path()
    if path is None:
        st["enabled"] = False
        return st["entries"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return st["entries"]
        # 版本/解释器不匹配 → 整份作废，不做逐条判断
        if payload.get("schema") != _SCHEMA or payload.get("pyver") != sys.version:
            return st["entries"]
        entries = payload.get("entries")
        if isinstance(entries, dict):
            st["entries"] = entries
    except FileNotFoundError:
        pass
    except Exception as e:  # 损坏/权限问题不能影响加载
        logger.debug(f"[CodeCache] 读取失败，回落即时编译: {e}")
    _sweep_stale_tmp(path)
    return st["entries"]


def _sweep_stale_tmp(path: Path) -> None:
    """清理已消亡进程残留的临时文件。

    tmp 改为 <stem>.<pid>.tmp 后，被强杀（SIGKILL/断电）的进程会留下自己的
    tmp（正常路径 os.replace/unlink 已覆盖）。这里按「pid 是否还活着」判断，
    只删确认已退出的，绝不猜。任何异常静默跳过——清理不能影响加载。
    """
    import glob

    for leftover in glob.glob(str(path.with_suffix(".*.tmp"))):
        try:
            pid_part = leftover.rsplit(".", 2)[-2]
            if not pid_part.isdigit():
                continue
            pid = int(pid_part)
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)  # 探测存活：不存在抛 ProcessLookupError
                continue  # 还活着，不是残留
            except ProcessLookupError:
                os.unlink(leftover)
            except PermissionError:
                continue  # 长得像 pid 但不是我们权限内的进程，保守跳过
        except OSError:
            continue
        except Exception:
            continue


def _key(source: str, filename: str) -> str:
    """缓存键 = 源码内容哈希 + 文件名。

    filename 必须进键：同一个键复用 code object 会连 `co_filename` 一起复用，
    而 traceback/日志靠它定位文件。真实样本：`system-core/tools/_path_utils.py`
    与 `system-diagnostics/tools/_path_utils.py` 逐字节相同，只按源码哈希时
    第二个插件加载会命中第一个的 code object，traceback 指向错误的插件目录。
    不影响执行正确性，但排障会被彻底误导。
    """
    h = hashlib.sha256()
    h.update(source.encode("utf-8", "surrogatepass"))
    h.update(b"\x00")  # 分隔符：防「源码结尾 + 文件名开头」拼接碰撞
    h.update(filename.encode("utf-8", "surrogatepass"))
    return h.hexdigest()[:32]


def get_code(source: str, filename: str) -> Tuple[Any, bool]:
    """取 code object。

    Args:
        source: 模块源码（调用方已读入）
        filename: 传给 compile 的文件名（用于 traceback 定位）

    Returns:
        (code_object, hit) — hit=False 表示本次是现场编译，调用方应择机落盘。
    """
    st = _state
    if not st["enabled"]:
        return compile(source, filename, "exec"), False
    k = _key(source, filename)
    entries = _load()
    raw = entries.get(k)
    if raw is not None:
        try:
            return marshal.loads(base64.b64decode(raw)), True
        except Exception:
            # 条目损坏：丢弃后重建，绝不放行可疑对象
            entries.pop(k, None)
    code = compile(source, filename, "exec")
    try:
        if len(entries) >= _MAX_ENTRIES:
            entries.clear()
        entries[k] = base64.b64encode(marshal.dumps(code)).decode("ascii")
        st["dirty"] = True
    except Exception:
        pass
    return code, False


def flush() -> None:
    """原子落盘。仅在非启动关键路径择机调用。"""
    st = _state
    # 无论是否写入，调度标志都要复位，否则后续新条目永不落盘
    st["scheduled"] = False
    if not st["dirty"] or not st["enabled"]:
        return
    path = _cache_path()
    if path is None:
        return
    # tmp 必须每进程唯一：固定名会让多个实例的写入交错到同一个临时文件，
    # 产物是两个 JSON 文档的混合体（实测双实例并发复现率 60%，报
    # "Extra data"），随后 os.replace 把损坏内容扶正。加 pid 后各写各的。
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            payload = {"schema": _SCHEMA, "pyver": sys.version, "entries": st["entries"]}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
            st["dirty"] = False
    except Exception as e:
        logger.debug(f"[CodeCache] 写入失败（不影响加载）: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def schedule_flush(delay: float = _FLUSH_DELAY) -> None:
    """非阻塞落盘：daemon Timer 延时写一次，不占启动路径。"""
    st = _state
    if st["scheduled"]:
        return
    try:
        timer = threading.Timer(delay, flush)
        timer.daemon = True
        timer.start()
        st["scheduled"] = True
    except Exception:
        pass
