# -*- coding: utf-8 -*-
"""Team Template 文件存储层。

提供 4 个核心操作的封装：
- save(template)        写入 YAML 文件到 user-custom 目录
- load(name)            按优先级读取（user-custom → plugin → system）
- list_templates()      列出所有来源的可用模板（含 source 标识）
- delete(name)          仅删除 user-custom 目录下的模板

模板来源（优先级从高到低）：
  1. user-custom  — .drifox/plugins/user-custom/team_templates/（可写、可删）
  2. plugin       — 各插件声明的 team_templates/ 目录（只读）
  3. system       — plugins/system/team_templates/（只读）

设计要点：
- 单例模式：与 TeamManager 风格保持一致
- 错误统一抛 TemplateError，由调用方负责转 InfoBar 提示
- 使用 PyYAML（项目已依赖），不带 ruamel 等额外依赖
- 模板文件名由 template_name 派生：
    允许字母/数字/下划线/中划线，以及中文等 Unicode 字母（放宽以支持中文名）；
    仍禁止 . / 反斜杠 与 ..，避免扩展名冲突与路径穿越（跨平台文件名为 UTF-8，安全）。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from loguru import logger

from app.core.team.template_schema import SUPPORTED_SCHEMA_VERSIONS, Template, TemplateError


# 模板名允许字符（Unicode 模式）：首字符为字母/数字（含中文），后续允许字母/数字/_/-；
# 天然排除 . / 反斜杠 与 ..（避免扩展名冲突与路径穿越）。长度 1-64。
_NAME_PATTERN = re.compile(r"^[^\W_][\w-]{0,63}$")


class TemplateManager:
    """团队模板管理器（单例）。"""

    _instance: Optional["TemplateManager"] = None
    _lock = threading.Lock()

    # 子目录名（模板文件存放的子目录名）
    _TEMPLATES_SUBDIR = "team_templates"

    # 来源标识
    SOURCE_SYSTEM = "system"
    SOURCE_PLUGIN = "plugin"
    SOURCE_USER = "user"

    def __init__(self):
        self._system_dir = self._resolve_system_templates_dir()
        self._system_dir.mkdir(parents=True, exist_ok=True)

    # ── 单例 ─────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "TemplateManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """测试 / 热重载场景下清空单例。"""
        cls._instance = None

    # ── 路径解析 ────────────────────────────────────

    @classmethod
    def _resolve_system_templates_dir(cls) -> Path:
        """解析系统模板根目录（<repo>/plugins/system/team_templates/）。"""
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        return project_root / "plugins" / "system" / cls._TEMPLATES_SUBDIR

    def _get_user_dir(self) -> Optional[Path]:
        """获取 user-custom 插件下的 team_templates/ 目录（不存在则创建）。

        直接基于应用数据目录解析路径，不依赖 PluginManager 是否注册了
        user-custom 插件：
        - user-custom 的 manifest（.drifox-plugin/plugin.json）是按需创建的
          （添加 MCP 服务器 / ShortcutManager 保存快捷键时才生成），用户仅
          保存过团队模板时插件未注册，get_plugin() 返回 None；
        - 若首次解析发生在 PluginManager 未初始化时，缓存 None 会导致后续
          永远解析失败（缓存毒化），模板列表缺失。
        因此与 PluginManager.get_command_files / _get_md_files 的
        "始终包含 user-custom 插件对应子目录（即使插件清单不存在）"约定一致。
        """
        try:
            from app.utils.utils import get_app_data_dir

            path = get_app_data_dir() / "plugins" / "user-custom" / self._TEMPLATES_SUBDIR
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            return None

    @classmethod
    def _get_plugin_template_dirs(cls) -> List[Path]:
        """获取所有已启用插件中声明了 team_templates 组件的目录。

        Returns:
            插件 team_templates/ 目录列表（按插件优先级排序）。
        """
        try:
            from app.core.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            return pm.get_plugin_dirs("team_templates")
        except Exception:
            return []

    # ── 文件名校验与路径 ────────────────────────────

    @staticmethod
    def _validate_name(name: str) -> str:
        """校验模板名合法，返回原值（去除首尾空白）。"""
        if not isinstance(name, str):
            raise TemplateError(f"模板名必须是字符串，得到: {type(name).__name__}")
        name = name.strip()
        if not name:
            raise TemplateError("模板名不能为空")
        if not _NAME_PATTERN.match(name):
            raise TemplateError(f"模板名非法: {name!r}（仅允许字母/数字/中文/下划线/中划线，禁止 . 与路径分隔符，长度 1-64）")
        return name

    def _template_path_in_dir(self, directory: Path, name: str) -> Path:
        return directory / f"{name}.yaml"

    # ── 单目录扫描 ─────────────────────────────────

    def _list_from_dir(self, directory: Path, source: str) -> List[Dict[str, Any]]:
        """扫描单个目录下的所有模板，添加 source 标识。"""
        results: List[Dict[str, Any]] = []
        if not directory or not directory.exists():
            return results

        # 已加载的模板名（同源同名去重，首个有效文件为准）
        seen_in_this_dir: set = set()

        for path in sorted(directory.glob("*.yaml")):
            name = path.stem
            if name in seen_in_this_dir:
                continue
            try:
                tpl = self._load_from_path(path, name)
            except TemplateError as e:
                logger.warning(f"[TemplateManager] 跳过损坏模板 {name}（{source}）: {e}")
                continue
            seen_in_this_dir.add(name)
            results.append(
                {
                    "name": tpl.template_name,
                    "description": tpl.description,
                    "agent_count": len(tpl.agents),
                    "agent_names": [a.agent_name for a in tpl.agents],
                    "path": str(path),
                    "source": source,
                }
            )
        return results

    def _load_from_path(self, path: Path, expected_name: str) -> Template:
        """从指定文件路径读取模板。

        Raises:
            TemplateError: 文件不存在、解析失败、字段非法
        """
        if not path.exists():
            raise TemplateError(f"模板不存在: {expected_name}（路径: {path}）")

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise TemplateError(f"模板 YAML 解析失败: {expected_name} ({e})") from e
        except OSError as e:
            raise TemplateError(f"读取模板文件失败: {path} ({e})") from e

        if raw is None:
            raise TemplateError(f"模板文件为空: {path}")
        if not isinstance(raw, dict):
            raise TemplateError(f"模板顶层必须是对象，得到: {type(raw).__name__}（文件: {path}）")

        try:
            template = Template.from_dict(raw)
        except TemplateError:
            raise
        except Exception as e:  # noqa: BLE001 — 兜底，避免未预期异常逃逸
            raise TemplateError(f"模板结构非法: {expected_name} ({e})") from e

        template.template_name = expected_name
        return template

    # ── 公开 API ─────────────────────────────────────

    def save(self, template: Template) -> Path:
        """保存模板到 user-custom YAML 文件。

        Returns:
            写入的文件路径。
        """
        if not isinstance(template, Template):
            raise TemplateError(f"save 需要 Template 实例，得到: {type(template).__name__}")

        name = self._validate_name(template.template_name)
        template.template_name = name  # 同步为校验后的值

        user_dir = self._get_user_dir()
        if not user_dir:
            raise TemplateError("user-custom 插件目录不可用，无法保存模板。请检查插件管理器状态。")

        path = self._template_path_in_dir(user_dir, name)
        if path.exists():
            logger.info(f"[TemplateManager] 覆盖已有模板: {path}")

        data = template.to_dict()
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    data,
                    f,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                    width=120,
                )
        except OSError as e:
            raise TemplateError(f"写入模板文件失败: {path} ({e})") from e

        logger.info(f"[TemplateManager] 已保存模板: {name} → {path}")
        return path

    def load(self, name: str) -> Template:
        """读取模板（按优先级：user-custom → plugin → system）。

        Raises:
            TemplateError: 所有来源均找不到模板
        """
        name = self._validate_name(name)

        search_dirs: List[tuple] = []
        # 用户目录最优先
        user_dir = self._get_user_dir()
        if user_dir and user_dir.exists():
            search_dirs.append((user_dir, self.SOURCE_USER))
        # 插件目录
        for plugin_dir in self._get_plugin_template_dirs():
            if plugin_dir.exists():
                search_dirs.append((plugin_dir, self.SOURCE_PLUGIN))
        # 系统目录
        if self._system_dir.exists():
            search_dirs.append((self._system_dir, self.SOURCE_SYSTEM))

        for directory, source in search_dirs:
            path = self._template_path_in_dir(directory, name)
            if path.exists():
                logger.debug(f"[TemplateManager] 从 {source} 加载模板 {name} → {path}")
                return self._load_from_path(path, name)

        raise TemplateError(f"模板不存在: {name}（已搜索 user-custom / plugin / system）")

    def list_templates(self) -> List[Dict[str, Any]]:
        """列出所有可用模板的元信息（聚合所有来源）。

        Returns:
            每项包含: name / description / agent_count / agent_names / path / source
            同名模板在多个来源出现时，只返回优先级最高的那个。
        """
        seen_names: set = set()
        results: List[Dict[str, Any]] = []

        # 顺序：user-custom → plugin → system（优先级从高到低）
        sources: List[tuple] = []
        user_dir = self._get_user_dir()
        if user_dir and user_dir.exists():
            sources.append((user_dir, self.SOURCE_USER))
        for plugin_dir in self._get_plugin_template_dirs():
            if plugin_dir.exists():
                sources.append((plugin_dir, self.SOURCE_PLUGIN))
        if self._system_dir.exists():
            sources.append((self._system_dir, self.SOURCE_SYSTEM))

        for directory, source in sources:
            for t in self._list_from_dir(directory, source):
                if t["name"] in seen_names:
                    continue
                seen_names.add(t["name"])
                results.append(t)

        return results

    def list_templates_by_source(self, source: str) -> List[Dict[str, Any]]:
        """仅列出指定来源的模板。

        Args:
            source: SOURCE_USER / SOURCE_PLUGIN / SOURCE_SYSTEM

        Returns:
            同 list_templates 格式，但只包含指定来源的模板。
        """
        results: List[Dict[str, Any]] = []

        if source == self.SOURCE_USER:
            user_dir = self._get_user_dir()
            if user_dir and user_dir.exists():
                results = self._list_from_dir(user_dir, source)
        elif source == self.SOURCE_SYSTEM:
            if self._system_dir.exists():
                results = self._list_from_dir(self._system_dir, source)
        elif source == self.SOURCE_PLUGIN:
            for plugin_dir in self._get_plugin_template_dirs():
                if plugin_dir.exists():
                    results.extend(self._list_from_dir(plugin_dir, source))
        return results

    def delete(self, name: str) -> bool:
        """删除 user-custom 目录下的模板文件。

        Returns:
            True 表示删除成功；False 表示文件本来就不存在。
        """
        name = self._validate_name(name)

        user_dir = self._get_user_dir()
        if not user_dir:
            raise TemplateError("user-custom 插件目录不可用，无法删除模板。")

        path = self._template_path_in_dir(user_dir, name)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as e:
            raise TemplateError(f"删除模板失败: {path} ({e})") from e
        logger.info(f"[TemplateManager] 已删除模板: {name}")
        return True

    def exists(self, name: str) -> bool:
        """检查模板是否存在于任何来源（不抛错）。"""
        try:
            name = self._validate_name(name)
        except TemplateError:
            return False
        try:
            self.load(name)
            return True
        except TemplateError:
            return False

    def get_source(self, name: str) -> Optional[str]:
        """查询模板的来源标识。

        Returns:
            SOURCE_USER / SOURCE_PLUGIN / SOURCE_SYSTEM / None
        """
        try:
            name = self._validate_name(name)
        except TemplateError:
            return None

        search: List[tuple] = []
        user_dir = self._get_user_dir()
        if user_dir and user_dir.exists():
            search.append((user_dir, self.SOURCE_USER))
        for plugin_dir in self._get_plugin_template_dirs():
            if plugin_dir.exists():
                search.append((plugin_dir, self.SOURCE_PLUGIN))
        if self._system_dir.exists():
            search.append((self._system_dir, self.SOURCE_SYSTEM))

        for directory, source in search:
            if self._template_path_in_dir(directory, name).exists():
                return source

        return None

    # ── 路径暴露（便于测试与调试）─────────────────

    @property
    def system_dir(self) -> Path:
        return self._system_dir

    @property
    def user_dir(self) -> Optional[Path]:
        return self._get_user_dir()
