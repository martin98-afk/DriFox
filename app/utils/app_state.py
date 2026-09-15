# -*- coding: utf-8 -*-
"""应用临时状态存储（app_state.json）

存放「上次状态」类数据（上次所在项目、上次欢迎 tab、工作区树折叠态）。
这些值只描述「上一次会话停在哪」，不是用户偏好，与系统配置（app.config）分离：
不进入设置界面、不参与云端配置同步、不污染用户手改的配置文件。

存储位置：<app_data_dir>/cache/app_state.json（与 commands_cache.json 同目录同模式）。
迁移：state 文件不存在时，从旧 app.config 对应字段一次性迁入（幂等）。
"""

import json
import threading
from typing import Any, Optional

from loguru import logger

from app.utils.utils import get_app_data_dir

_lock = threading.Lock()
_cache: Optional[dict] = None

# 旧 app.config 字段迁移表：state key → (group, name)
_MIGRATE_FROM_CONFIG = {
    "current_project": ("Session", "CurrentProject"),
    "welcome_plugin_tab": ("UI", "WelcomePluginTab"),
    "workspace_tree_expansion": ("UI", "WorkspaceTreeExpansion"),
}


def _state_file():
    return get_app_data_dir() / "cache" / "app_state.json"


def _load_locked() -> dict:
    """加载 state 到进程内缓存（调用方须持锁）；首次加载执行旧配置迁移"""
    global _cache
    if _cache is not None:
        return _cache
    data: dict = {}
    try:
        f = _state_file()
        if f.exists():
            loaded = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[AppState] 读取失败，使用空状态: {e}")
    _migrate_from_config_locked(data)
    _cache = data
    return _cache


def _migrate_from_config_locked(data: dict) -> None:
    """state 文件不存在时，从旧 app.config 一次性迁入（文件已存在则跳过）"""
    if _state_file().exists():
        return
    try:
        config_file = get_app_data_dir() / "app.config"
        if not config_file.exists():
            return
        old = json.loads(config_file.read_text(encoding="utf-8"))
        changed = False
        for key, (group, name) in _MIGRATE_FROM_CONFIG.items():
            if key in data:
                continue
            try:
                value = (old.get(group) or {}).get(name)
            except Exception:  # noqa: BLE001
                value = None
            if value is not None:
                data[key] = value
                changed = True
        if changed:
            _save_locked(data)
            logger.info("[AppState] 已从 app.config 迁移上次状态字段")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[AppState] 旧配置迁移失败（忽略）: {e}")


def _save_locked(data: dict) -> None:
    """原子落盘（tmp + replace，防写坏）"""
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def get(key: str, default: Any = None) -> Any:
    """读状态值"""
    with _lock:
        data = _load_locked()
        return data.get(key, default)


def set(key: str, value: Any, save: bool = True) -> None:
    """写状态值（值未变化短路；save=False 仅改内存不落盘）"""
    with _lock:
        data = _load_locked()
        if data.get(key) == value:
            return
        data[key] = value
        if save:
            _save_locked(data)
