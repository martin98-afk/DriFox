# -*- coding: utf-8 -*-
"""
沙箱拦截核心（L1）

职责：
- SandboxConfig：sandbox_config.json 的加载/保存/单例（点路径 get/set）
- check_path / check_command / check_network：三分法判定，返回 "allow"|"confirm"|"deny"
- sandbox_check_tool：工具名 + 参数 → 判定（worker 层唯一入口）
- snapshot_paths_before_delete：删除类命令执行前快照
- snapshot_paths_before_delete：删除类命令执行前快照

本模块不弹 UI、不做 OS 级隔离；审批呈现由 chat_worker 既有弹窗链负责。
设计文档：docs/superpowers/specs/2026-09-18-sandbox-l1-security-center-design.md
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Optional, Union

from loguru import logger

# 三分法判定值
ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"

Verdict = str

DEFAULT_CONFIG = {
    "sandbox_enabled": True,
    "path": {"whitelist": [], "blacklist": []},
    "command": {"allow_prefixes": [], "confirm_prefixes": []},
    "network": {"enabled": True, "blacklist_domains": []},
    "sys_tools_bypass": False,
    "delete_protection": True,
    "delete": {"exempt_paths": []},
    "job_limits": {"memory_mb": 2048, "active_process": 64, "cpu_time_ms": 0},
    "backup_limit_mb": 3000,
}


def _deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base，缺键由 base 补齐（递归）"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class SandboxConfig:
    """sandbox_config.json 配置（点路径读写，进程级单例）"""

    _instance: Optional["SandboxConfig"] = None

    def __init__(self, config_path: Union[str, Path, None] = None):
        if config_path == ":memory:":
            self._path: Optional[Path] = None
        else:
            from app.utils.utils import get_app_data_dir

            self._path = Path(config_path) if config_path else get_app_data_dir() / "sandbox_config.json"
        self._data: dict = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    @classmethod
    def get_instance(cls) -> "SandboxConfig":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """测试用：强制下次 get_instance 重建"""
        cls._instance = None

    def load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            disk = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = _deep_merge(DEFAULT_CONFIG, disk if isinstance(disk, dict) else {})
        except Exception as e:  # noqa: BLE001 - 配置损坏回落默认，不阻断启动
            logger.warning(f"[Sandbox] 配置加载失败，使用默认值: {e}")

    def save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, dotted: str, default=None):
        """点路径读取：get("path.whitelist")"""
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        """点路径写入：set("path.blacklist", [...])，不自动落盘（显式 save）"""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


# ============================================================
# 路径边界（写窄读宽）
# ============================================================
# 读/写工具的归属由 ToolRegistry 分组驱动（sandbox_check_tool），
# 分组名与插件侧 GROUP_WRITE/GROUP_READ（plugins/system-tools/tools/file_tools.py:109）
# 对齐；不在此写死工具名清单——插件增删热生效，写死必漏。


def _norm(p: Union[str, Path]) -> str:
    """展开环境变量/~ 后 resolve，统一大小写与分隔符（Windows 比较用）"""
    raw = str(p)
    expanded = os.path.expandvars(raw)
    return os.path.normcase(str(Path(expanded).expanduser().resolve()))


def _under(child: str, parent: str) -> bool:
    """child 是否位于 parent 目录内（含相等）"""
    if parent == child:
        return True
    return child.startswith(parent.rstrip("\\/") + os.sep)


def _match_blacklist(norm_path: str, blacklist: list) -> bool:
    """黑名单条目：绝对目录前缀匹配，或裸文件名匹配（如 ".env" 命中任意层级 .env）"""
    for item in blacklist:
        item_str = str(item)
        if os.path.isabs(os.path.expandvars(item_str)):
            norm_item = _norm(item_str)
            if norm_path.startswith(norm_item):
                return True
        else:
            if Path(norm_path).name.lower() == item_str.lower():
                return True
    return False


def check_path(
    workdir: Union[str, Path],
    path: str,
    mode: str,
    cfg: Optional[SandboxConfig] = None,
) -> Verdict:
    """路径边界判定。mode: "read"|"write"。优先级：黑名单 > 白名单 > workdir 边界"""
    cfg = cfg or SandboxConfig.get_instance()
    if not cfg.get("sandbox_enabled"):
        return ALLOW
    if not path or not str(path).strip():
        return ALLOW

    norm_path = _norm(path)
    blacklist = cfg.get("path.blacklist") or []
    whitelist = cfg.get("path.whitelist") or []

    # 黑名单最高优先（读写都拦）
    if blacklist and _match_blacklist(norm_path, blacklist):
        return CONFIRM
    # 白名单放行出界写
    if any(_under(norm_path, _norm(item)) for item in whitelist):
        return ALLOW
    if mode == "write":
        workdir_norm = _norm(workdir)
        return ALLOW if _under(norm_path, workdir_norm) else CONFIRM
    return ALLOW


# ============================================================
# 命令边界（递归解壳 + 用户名单叠加）
# ============================================================
# 系统级工具：沙箱管不住其内部行为（如 wsl 内的 Linux 侧操作）
SYS_LEVEL_TOOLS = frozenset({"wsl", "wmic", "sc", "reg", "schtasks", "diskpart", "bcdedit"})


def _first_token(command: str) -> str:
    """首 token 归一化：去路径前缀、去 .exe、小写"""
    token = command.strip().split()[0].lower().replace("\\", "/").split("/")[-1]
    return token[:-4] if token.endswith(".exe") else token


def check_command(command: str, cfg: Optional[SandboxConfig] = None) -> Verdict:
    """命令判定：递归解壳 + 用户名单叠加 + 系统级工具豁免"""
    cfg = cfg or SandboxConfig.get_instance()
    if not cfg.get("sandbox_enabled"):
        return ALLOW
    if not command or not command.strip():
        return ALLOW

    from app.tools.command_safety import classify_command_deep

    stripped = command.strip()

    # 用户 confirm 名单优先于 allow 名单（显式加严）
    for prefix in cfg.get("command.confirm_prefixes") or []:
        if stripped.lower().startswith(str(prefix).lower()):
            return CONFIRM
    # 系统级工具豁免
    if cfg.get("sys_tools_bypass") and _first_token(stripped) in SYS_LEVEL_TOOLS:
        return ALLOW
    # 用户 allow 名单：放行，但 block 命令仍拦
    for prefix in cfg.get("command.allow_prefixes") or []:
        if stripped.lower().startswith(str(prefix).lower()):
            return DENY if classify_command_deep(stripped) == "block" else ALLOW

    verdict = classify_command_deep(stripped)
    if verdict == "block":
        return DENY
    if verdict == "confirm":
        return CONFIRM
    return ALLOW


# ============================================================
# 网络外传（弱防护）
# ============================================================
# L1 是特征检测不是 egress 隔离，目标是拦误用（如 curl -d @secret.txt），
# 不追求绕过完备性；宁多问一次。
_NET_TOOL_TOKENS = (
    "curl",
    "wget",
    "invoke-webrequest",
    "invoke-restmethod",
    "iwr",
    "irm",
    "nc",
    "ncat",
    "netcat",
    "ftp",
    "scp",
    "bitsadmin",
    "certutil",
)
_NET_UPLOAD_FLAGS = (
    "-d",
    "--data",
    "--data-binary",
    "--data-raw",
    "-f",
    "--form",
    "--upload-file",
    "-t",
    "--post-file",
    "--body",
)
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")


def check_network(command: str, cfg: Optional[SandboxConfig] = None) -> Optional[Verdict]:
    """网络外传判定：命中返回 CONFIRM，无风险返回 None"""
    cfg = cfg or SandboxConfig.get_instance()
    if not cfg.get("sandbox_enabled") or not cfg.get("network.enabled"):
        return None
    if not command:
        return None

    lowered = command.lower()
    has_net_tool = any(token in lowered for token in _NET_TOOL_TOKENS)
    urls = _URL_PATTERN.findall(command)
    if not has_net_tool and not urls:
        return None

    # 域名黑名单（后缀匹配）
    for url in urls:
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
        for domain in cfg.get("network.blacklist_domains") or []:
            d = str(domain).lower().lstrip(".")
            if d and (host == d or host.endswith("." + d)):
                return CONFIRM

    # 上传/POST 体形态
    if has_net_tool and any(f" {flag}" in lowered for flag in _NET_UPLOAD_FLAGS):
        return CONFIRM
    # 带 URL 的网络命令一律过一道审批（弱防护定位）
    if urls:
        return CONFIRM
    return None


# ============================================================
# 工具路由（worker 层唯一入口）
# ============================================================
# 分组名与插件侧 GROUP_WRITE/GROUP_READ（plugins/system-tools/tools/file_tools.py:109）
# 对齐；"文件写入" 同时是 FileRecorder 追踪组的同源字符串。
# 不写死工具名清单——插件增删热生效，写死必漏。
GROUP_WRITE_NAME = "文件写入"
GROUP_READ_NAME = "文件读取"


def _tools_in(group_name: str) -> frozenset:
    """registry 分组成员查询；registry 不可用时返回空集（fail-open）"""
    try:
        from app.tools.registry import ToolRegistry

        return ToolRegistry.get_instance().tools_in_group(group_name)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Sandbox] registry 分组查询失败({group_name}): {e}")
        return frozenset()


def _current_workdir() -> Path:
    """当前有效工作目录：BackgroundTaskManager 单例通道，回退 cwd"""
    try:
        from app.tools.bg_manager import BackgroundTaskManager

        inst = BackgroundTaskManager._instance
        if inst is not None:
            wd = inst._effective_workdir()
            if wd is not None:
                return Path(wd)
    except Exception:  # noqa: BLE001
        pass
    return Path.cwd()


def sandbox_check_tool(tool_name: str, arguments: dict, cfg: Optional[SandboxConfig] = None) -> Verdict:
    """worker 层唯一入口：工具名 + 参数 → 三分判定（deny/confirm/allow）

    路由规则（元数据驱动，不写死工具名）：
    - 参数带 command 键 → 命令工具 → check_command + check_network
    - tool_name ∈ registry "文件写入" 分组 → check_path(write)
    - tool_name ∈ registry "文件读取" 分组 → check_path(read)
    - 其余 → allow
    """
    cfg = cfg or SandboxConfig.get_instance()
    if not cfg.get("sandbox_enabled"):
        return ALLOW
    args = dict(arguments or {})

    command = args.get("command")
    if isinstance(command, str) and command.strip():
        verdict = check_command(command, cfg)
        if verdict == CONFIRM and is_delete_command(command) and delete_exempt(command, cfg):
            return ALLOW
        if verdict == ALLOW:
            net = check_network(command, cfg)
            return net if net else ALLOW
        return verdict

    if tool_name in _tools_in(GROUP_WRITE_NAME):
        mode = "write"
    elif tool_name in _tools_in(GROUP_READ_NAME):
        mode = "read"
    else:
        return ALLOW

    path = args.get("path") or args.get("file_path") or ""
    if not path:
        return ALLOW
    return check_path(_current_workdir(), str(path), mode, cfg)


# ============================================================
# 删除保护（审批放行后、执行前快照）
# ============================================================
DELETE_COMMANDS = frozenset({"rm", "del", "erase", "rd", "rmdir", "deltree", "unlink", "remove-item", "ri"})

# rm/del 等常见 flag（快照时剔除）
_DELETE_FLAGS = {"-r", "-rf", "-f", "-fr", "-d", "-rd", "-recursive", "/s", "/q", "/f"}


# 命令分词：shlex.split(posix=True) 会吞掉 Windows 反斜杠
# （C:\\tmp\\a.txt → C:tmpa.txt）导致快照路径失真，故用正则分词：
# 成对引号整段保留，其余按空白切；去壳引号在取值时处理。
_TOKEN_PATTERN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _split_tokens(command: str) -> list:
    """命令分词（保留 Windows 路径反斜杠，成对引号作单 token）"""
    if not command:
        return []
    return _TOKEN_PATTERN.findall(command)


def _strip_quotes(token: str) -> str:
    stripped = token.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        return stripped[1:-1]
    return stripped


def delete_targets(command: str) -> dict:
    """解析删除命令的目标路径（供审批展示与豁免判定共用）

    Returns:
        {"existing": [Path, ...], "missing": [str, ...]}——
        existing 为真实存在的目标（去重），missing 为命令里写了但当前不存在的候选
    """
    result: dict = {"existing": [], "missing": []}
    if not is_delete_command(command):
        return result
    seen: set = set()
    for token in _split_tokens(command):
        raw = _strip_quotes(token)
        lowered = raw.lower()
        if lowered in _DELETE_FLAGS or lowered in DELETE_COMMANDS:
            continue
        if lowered.startswith(("-", "/")) and not _looks_like_path(raw):
            continue
        # 引号内多词（如 powershell -c "Remove-Item x"）逐个试
        candidates = _split_tokens(raw) if " " in raw.strip() else [raw]
        for item in candidates:
            item = _strip_quotes(item)
            if not item or item.lower() in DELETE_COMMANDS or item.lower() in _DELETE_FLAGS:
                continue
            if item.startswith(("-", "/")) and not _looks_like_path(item):
                continue
            candidate = Path(os.path.expandvars(item)).expanduser()
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists():
                result["existing"].append(candidate)
            else:
                result["missing"].append(item)
    return result


def _looks_like_path(token: str) -> bool:
    """-/开头但形似路径的特例（/s /q 是 flag，/tmp/x 是 Unix 绝对路径）

    含路径分隔符或扩展名点号才视为路径，否则按 flag 处理。
    """
    return ("/" in token[1:] or "\\" in token or "." in token[1:]) and len(token) > 2


def delete_exempt(command: str, cfg: Optional[SandboxConfig] = None) -> bool:
    """删除豁免判定：命令的删除目标全部落在豁免路径内时，confirm 降级为 allow

    豁免条目支持绝对路径（workdir 外）或相对 workdir 的路径（如 tests/）。
    任一目标不在豁免内即不豁免（宁多问一次）；无目标可解析则不豁免。
    """
    cfg = cfg or SandboxConfig.get_instance()
    exempt = [str(p) for p in (cfg.get("delete.exempt_paths") or [])]
    if not exempt:
        return False
    targets = delete_targets(command)
    candidates = targets["existing"] + [Path(m) for m in targets["missing"]]
    if not candidates:
        return False
    workdir = _current_workdir()
    for target in candidates:
        norm_target = _norm(target)
        if not any(
            _under(norm_target, _norm(item)) or _under(norm_target, _norm(workdir / str(item))) for item in exempt
        ):
            return False
    return True


def is_delete_command(command: str) -> bool:
    """是否删除类命令（含壳命令包裹形态）

    保守判定：任一 token（含引号内内容后再拆一次）命中删除命令名即算
    （git rm / powershell -c Remove-Item 均覆盖）；误报方向是「多问一次」。
    """
    if not command:
        return False
    for token in _split_tokens(command):
        inner = _strip_quotes(token)
        if inner.lower() in DELETE_COMMANDS:
            return True
        # 引号内可能是完整子命令（powershell -c "Remove-Item x"）
        for sub in inner.split():
            head = sub.lower().replace("\\", "/").split("/")[-1]
            if head in DELETE_COMMANDS:
                return True
    return False


def snapshot_paths_before_delete(command: str, backup_root: Union[str, Path]) -> list:
    """删除命令执行前，把命令中「存在」的路径快照到 backup_root，返回快照路径列表

    只在审批放行后调用；非删除命令返回 []。快照失败不阻断（用户已批准删除）。
    """
    if not is_delete_command(command):
        return []
    backup_root = Path(backup_root)
    snapshots: list = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    seen: set = set()
    for token in _split_tokens(command):
        raw = _strip_quotes(token)
        lowered = raw.lower()
        if lowered in _DELETE_FLAGS or lowered in DELETE_COMMANDS:
            continue
        if lowered.startswith(("-", "/")):
            continue
        # 引号内多词（如 powershell -c "Remove-Item x"）逐个试
        candidates = _split_tokens(raw) if " " in raw.strip() else [raw]
        for item in candidates:
            item = _strip_quotes(item)
            if not item or item.lower() in DELETE_COMMANDS or item.lower() in _DELETE_FLAGS:
                continue
            if item.startswith(("-", "/")):
                continue
            candidate = Path(os.path.expandvars(item)).expanduser()
            if not candidate.exists() or str(candidate) in seen:
                continue
            seen.add(str(candidate))
            try:
                target = backup_root / f"{stamp}_{candidate.name}"
                if candidate.is_dir():
                    shutil.copytree(candidate, target, dirs_exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, target)
                snapshots.append(target)
                logger.info(f"[Sandbox] 删除前已快照: {candidate} -> {target}")
            except Exception as e:  # noqa: BLE001 - 快照失败不阻断用户已批准的删除
                logger.warning(f"[Sandbox] 删除前快照失败({e}): {candidate}")
    return snapshots
