# -*- coding: utf-8 -*-
"""插件配置统一存储（E1）。

路径：<app_data_dir>/plugin_data/<plugin_name>/config.json
迁移：首次读取时若发现老路径 <app_data_dir>/plugins/<plugin_name>/config.json
存在且新路径不存在，自动迁移并写入 logger.info；与 cron-tasks 的 plugin_data
存储约定保持一致。
读取优先级：环境变量 → 存储值 → schema 默认（与 websearch 迁移前
_api_key 三级链逐点等价：env 最高、空串=清除、内置默认兜底）。
独立于主程序 Settings（插件数据不占用主程序配置文件）。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from app.plugins.contracts.plugin_config import PluginConfigField, PluginConfigSchema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

# 模块级锁：全部调用方每次 new 实例（无状态设计），实例锁无效；
# RLock 因 set_values 的 read-modify-write 需在锁内嵌套调用 _read_raw/_write_raw
_IO_LOCK = threading.RLock()


class PluginConfigStore:
    """按插件读写配置（无状态，可随时实例化）"""

    def _schema(self, plugin_name: str) -> Optional[PluginConfigSchema]:
        return PluginConfigRegistry.get_instance().get(plugin_name)

    # 插件名合法字符（与 plugin_manager._PLUGIN_NAME_RE 保持同一口径）
    _NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

    @classmethod
    def _safe_name(cls, plugin_name: str) -> str:
        """防路径穿越：plugin_name 参与 plugin_data/<name>/config.json 拼接。

        非法名（含分隔符 / `..` / 非字符串）一律降级为脱敏占位名，
        保证读写不会逃逸到 plugin_data/ 之外。
        """
        if isinstance(plugin_name, str) and cls._NAME_RE.match(plugin_name):
            return plugin_name
        logger.warning(f"[PluginConfigStore] 非法插件名 {plugin_name!r}，按 '__invalid__' 处理")
        return "__invalid__"

    def _path(self, plugin_name: str) -> Path:
        from app.utils.utils import get_app_data_dir

        plugin_name = self._safe_name(plugin_name)

        new_path = Path(get_app_data_dir()) / "plugin_data" / plugin_name / "config.json"
        legacy_path = Path(get_app_data_dir()) / "plugins" / plugin_name / "config.json"
        # 一次性迁移：旧路径 plugins/<name>/config.json → 新路径 plugin_data/<name>/config.json
        # 与 cron-tasks 一致：所有插件配置统一落 plugin_data/<plugin_name>/ 下
        if legacy_path.exists() and not new_path.exists():
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                legacy_path.rename(new_path)
                logger.info(
                    f"[PluginConfigStore] {plugin_name} 配置已迁移 "
                    f"plugins/ → plugin_data/"
                )
            except Exception as e:
                logger.warning(f"[PluginConfigStore] {plugin_name} 旧配置迁移失败: {e}")
        return new_path

    def _read_raw(self, plugin_name: str) -> Dict[str, Any]:
        # path 计算放在 try 外：_path 若抛异常，下方 except 的 .corrupt 分支引用
        # path 会 NameError 穿透（防御性修，R-G1 P3）
        path = self._path(plugin_name)
        try:
            with _IO_LOCK:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
        except Exception as e:
            # 半截 JSON（进程被杀/断电）不再无限吞：改名 .corrupt 保留现场供排查，
            # 之后读取按空处理（回退 schema 默认值），下次写入重建干净文件
            try:
                with _IO_LOCK:
                    corrupt = path.with_suffix(".json.corrupt")
                    if corrupt.exists():
                        corrupt.unlink()
                    path.rename(corrupt)
                logger.warning(f"[PluginConfigStore] {plugin_name} 配置损坏已改名 .corrupt 保留现场: {e}")
            except OSError:
                logger.warning(f"[PluginConfigStore] {plugin_name} 配置读取失败（按空处理）: {e}")
        return {}

    def _write_raw(self, plugin_name: str, data: Dict[str, Any]) -> bool:
        """原子写：先写 .json.tmp 再 os.replace，避免进程被杀/并发写留下半截 JSON"""
        try:
            with _IO_LOCK:
                path = self._path(plugin_name)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, path)
            return True
        except Exception as e:
            logger.warning(f"[PluginConfigStore] {plugin_name} 配置写入失败: {e}")
            return False

    # ── 密钥字段（type=password）透明走系统密钥库 ──────────
    #
    # 声明了 `"type": "password"` 的字段不进 config.json 明文：取值时先读
    # SecretStore，写入时直接落密钥库。插件代码与设置卡都不用改（两边都经
    # 本类），老数据首次读取时自动迁移。
    # 密钥库不可用（无 keyring 的 Linux）时整条链路回退明文 JSON，否则用户
    # 已配置的凭据会凭空消失。

    @staticmethod
    def _secret_account(plugin_name: str, key: str) -> str:
        return f"plugin:{plugin_name}:{key}"

    def _secret_store(self):
        try:
            from app.utils.secret_store import SecretStore

            return SecretStore()
        except Exception as e:  # noqa: BLE001 - 密钥库不可用按明文回退
            logger.warning(f"[PluginConfigStore] 密钥库不可用，password 字段回退明文: {e}")
            return None

    @staticmethod
    def _is_secret_field(field: Any) -> bool:
        return str(getattr(field, "type", "") or "") == "password"

    # ── 读 ──────────────────────────────────────────────

    def get(self, plugin_name: str, key: str) -> Any:
        """当前生效值：环境变量 → 存储值 → schema 默认

        password 字段额外多一层：密钥库优先，命中旧明文时懒迁移并清掉 JSON。
        """
        schema = self._schema(plugin_name)
        f = schema.get_field(key) if schema else None
        if f is not None and f.env:
            env_val = os.environ.get(f.env)
            if env_val:
                return self._normalize(f, env_val)
        raw = self._read_raw(plugin_name)
        if self._is_secret_field(f):
            store = self._secret_store()
            if store is not None and store.available:
                try:
                    stored = store.get(self._secret_account(plugin_name, key))
                except Exception:  # noqa: BLE001 - 密钥库读失败按未配置处理
                    stored = ""
                if stored:
                    return self._normalize(f, stored)
                legacy = raw.get(key)
                if isinstance(legacy, str) and legacy:
                    # 老数据：明文在 JSON 里，迁进密钥库后清掉 JSON 那一份
                    try:
                        if store.set(self._secret_account(plugin_name, key), legacy):
                            with _IO_LOCK:
                                data = self._read_raw(plugin_name)
                                if key in data:
                                    data.pop(key, None)
                                    self._write_raw(plugin_name, data)
                            logger.info(
                                f"[PluginConfigStore] {plugin_name}.{key} 已迁移进密钥库"
                            )
                    except Exception as e:  # noqa: BLE001 - 迁移失败不影响本次读取
                        logger.warning(f"[PluginConfigStore] {plugin_name}.{key} 迁移失败: {e}")
                    return self._normalize(f, legacy)
                return f.default
        if key in raw and raw[key] not in ("", None):
            return self._normalize(f, raw[key]) if f is not None else raw[key]
        return f.default if f is not None else None

    def get_all(self, plugin_name: str) -> Dict[str, Any]:
        """全部字段当前生效值（渲染卡回显用；无 schema 返回空 dict）"""
        return self.effective_values(plugin_name)

    def effective_values(self, plugin_name: str) -> Dict[str, Any]:
        """全部字段当前生效值，一次读盘（语义同 get：env → 存储 → schema 默认）。

        设置页状态判定（必填缺失 / 未配置）需要遍历整张 schema，逐字段调 get()
        会把同一个 config.json 读 N 遍；本方法把读盘收敛成一次。
        """
        schema = self._schema(plugin_name)
        if schema is None:
            return {}
        raw = self._read_raw(plugin_name)
        store = self._secret_store() if any(self._is_secret_field(f) for f in schema.fields) else None
        values: Dict[str, Any] = {}
        for f in schema.fields:
            env_val = os.environ.get(f.env) if f.env else None
            if env_val:
                values[f.key] = self._normalize(f, env_val)
            elif self._is_secret_field(f):
                stored = ""
                if store is not None and store.available:
                    try:
                        stored = store.get(self._secret_account(plugin_name, f.key)) or ""
                    except Exception:  # noqa: BLE001 - 读失败按未配置处理
                        stored = ""
                legacy = raw.get(f.key)
                if not stored and isinstance(legacy, str) and legacy:
                    stored = legacy
                values[f.key] = self._normalize(f, stored) if stored else f.default
            elif f.key in raw and raw[f.key] not in ("", None):
                values[f.key] = self._normalize(f, raw[f.key])
            else:
                values[f.key] = f.default
        return values

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
        """合并写；空串/None = 清除该键（回退默认）。只落 values 出现的键。

        password 字段单独分流：写系统密钥库，JSON 里不再保留明文。密钥库
        不可用或写入失败时回退明文，避免用户凭据丢失。
        """
        schema = self._schema(plugin_name)
        secret_vals: Dict[str, Any] = {}
        plain_vals: Dict[str, Any] = {}
        for key, val in values.items():
            field = schema.get_field(key) if schema else None
            if self._is_secret_field(field):
                secret_vals[key] = val
            else:
                plain_vals[key] = val

        store = self._secret_store() if secret_vals else None
        secret_ok = True
        if store is not None and store.available:
            for key, val in secret_vals.items():
                cleared = val is None or val == ""
                try:
                    if cleared:
                        store.delete(self._secret_account(plugin_name, key))
                        continue
                    if not store.set(self._secret_account(plugin_name, key), str(val)):
                        secret_ok = False
                        plain_vals[key] = val
                except Exception as e:  # noqa: BLE001 - 写失败回退明文
                    logger.warning(f"[PluginConfigStore] {plugin_name}.{key} 写密钥库失败: {e}")
                    secret_ok = False
                    plain_vals[key] = val
        elif secret_vals:
            # 密钥库不可用：整条链路回退明文，保证配置仍然生效
            plain_vals.update(secret_vals)

        with _IO_LOCK:  # read-modify-write 整体互斥，防并发互相覆盖
            raw = self._read_raw(plugin_name)
            for key, val in plain_vals.items():
                if val is None or val == "":
                    raw.pop(key, None)
                else:
                    raw[key] = val
            # 全部密钥项都进了密钥库，历史明文可以清掉
            if secret_ok and store is not None and store.available:
                for key in secret_vals:
                    raw.pop(key, None)
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
