# -*- coding: utf-8 -*-
"""插件配置统一存储（E1）。

路径：<app_data_dir>/plugins/<plugin_name>/config.json
读取优先级：环境变量 → 存储值 → schema 默认（与 websearch 迁移前
_api_key 三级链逐点等价：env 最高、空串=清除、内置默认兜底）。
独立于主程序 Settings（插件数据不占用主程序配置文件）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from app.plugins.contracts.plugin_config import PluginConfigField, PluginConfigSchema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry


class PluginConfigStore:
    """按插件读写配置（无状态，可随时实例化）"""

    def _schema(self, plugin_name: str) -> Optional[PluginConfigSchema]:
        return PluginConfigRegistry.get_instance().get(plugin_name)

    def _path(self, plugin_name: str) -> Path:
        from app.utils.utils import get_app_data_dir

        return Path(get_app_data_dir()) / "plugins" / plugin_name / "config.json"

    def _read_raw(self, plugin_name: str) -> Dict[str, Any]:
        try:
            path = self._path(plugin_name)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"[PluginConfigStore] {plugin_name} 配置读取失败（按空处理）: {e}")
        return {}

    def _write_raw(self, plugin_name: str, data: Dict[str, Any]) -> bool:
        try:
            path = self._path(plugin_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"[PluginConfigStore] {plugin_name} 配置写入失败: {e}")
            return False

    # ── 读 ──────────────────────────────────────────────

    def get(self, plugin_name: str, key: str) -> Any:
        """当前生效值：环境变量 → 存储值 → schema 默认"""
        schema = self._schema(plugin_name)
        f = schema.get_field(key) if schema else None
        if f is not None and f.env:
            env_val = os.environ.get(f.env)
            if env_val:
                return self._normalize(f, env_val)
        raw = self._read_raw(plugin_name)
        if key in raw and raw[key] not in ("", None):
            return self._normalize(f, raw[key]) if f is not None else raw[key]
        return f.default if f is not None else None

    def get_all(self, plugin_name: str) -> Dict[str, Any]:
        """全部字段当前生效值（渲染卡回显用；无 schema 返回空 dict）"""
        schema = self._schema(plugin_name)
        if schema is None:
            return {}
        return {f.key: self.get(plugin_name, f.key) for f in schema.fields}

    @staticmethod
    def _normalize(f: Optional[PluginConfigField], value: Any) -> Any:
        if f is None:
            return value
        if f.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("1", "true", "yes", "on")
        return str(value)

    # ── 写 ──────────────────────────────────────────────

    def set_values(self, plugin_name: str, values: Dict[str, Any]) -> bool:
        """合并写；空串/None = 清除该键（回退默认）。只落 values 出现的键。"""
        raw = self._read_raw(plugin_name)
        for key, val in values.items():
            if val is None or val == "":
                raw.pop(key, None)
            else:
                raw[key] = val
        return self._write_raw(plugin_name, raw)

    def reset(self, plugin_name: str) -> bool:
        """删除存储文件（全部回默认）"""
        try:
            path = self._path(plugin_name)
            if path.exists():
                path.unlink()
            return True
        except Exception as e:
            logger.warning(f"[PluginConfigStore] {plugin_name} 重置失败: {e}")
            return False

    # ── 一次性迁移 ──────────────────────────────────────

    def migrate(self, plugin_name: str, legacy_path, key_map: Dict[str, str]) -> bool:
        """旧配置文件迁移（旧键 → 新键）。

        语义：新存储文件已有内容 → 不覆盖返回 False；
        旧文件不存在 → False；迁移成功后旧文件改名 .bak。
        """
        legacy = Path(legacy_path)
        if not legacy.exists():
            return False
        target = self._path(plugin_name)
        if target.exists():
            existing = self._read_raw(plugin_name)
            if existing:
                return False
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[PluginConfigStore] 迁移源解析失败 {legacy}: {e}")
            return False
        if not isinstance(data, dict):
            return False
        values = {new_key: data[old_key] for old_key, new_key in key_map.items() if data.get(old_key)}
        if not values:
            return False
        if not self._write_raw(plugin_name, values):
            return False
        try:
            legacy.rename(legacy.with_name(legacy.name + ".bak"))
        except Exception as e:
            logger.warning(f"[PluginConfigStore] 旧文件改名失败（不影响迁移结果）: {e}")
        return True
