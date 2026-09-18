# -*- coding: utf-8 -*-
"""
沙箱拦截核心（L1）

职责：
- SandboxConfig：sandbox_config.json 的加载/保存/单例（点路径 get/set）
- check_path / check_command / check_network：三分法判定，返回 "allow"|"confirm"|"deny"
- sandbox_check_tool：工具名 + 参数 → 判定（worker 层唯一入口）
- snapshot_paths_before_delete：删除类命令执行前快照

本模块不弹 UI、不做 OS 级隔离；审批呈现由 chat_worker 既有弹窗链负责。
设计文档：docs/superpowers/specs/2026-09-18-sandbox-l1-security-center-design.md
"""
from __future__ import annotations

import copy
import json
import os
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
