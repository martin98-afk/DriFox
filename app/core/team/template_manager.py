# -*- coding: utf-8 -*-
"""Team Template 文件存储层。

提供 4 个核心操作的封装：
- save(template)        写入 YAML 文件
- load(name)            读取 YAML → Template
- list_templates()      列出所有可用模板（name + description + agent_count）
- delete(name)          删除模板文件

设计要点：
- 存储根目录：<项目根>/plugins/system/team_templates/（git 可追踪）
- 单例模式：与 TeamManager 风格保持一致
- 错误统一抛 TemplateError，由调用方负责转 InfoBar 提示
- 使用 PyYAML（项目已依赖），不带 ruamel 等额外依赖
- 模板文件名由 template_name 派生：
    只允许 [a-zA-Z0-9_-]，否则拒收（避免路径穿越 / 跨平台问题）
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from loguru import logger

from app.core.team.template_schema import SUPPORTED_SCHEMA_VERSIONS, Template, TemplateError


# 模板名允许字符（不含路径分隔符、不含 ..）
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class TemplateManager:
    """团队模板管理器（单例）。"""

    _instance: Optional["TemplateManager"] = None
    _lock = threading.Lock()

    # 子目录名（位于 system 插件下）
    _TEMPLATES_SUBDIR = "team_templates"

    def __init__(self):
        self._templates_dir = self._resolve_templates_dir()
        self._templates_dir.mkdir(parents=True, exist_ok=True)

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

    # ── 路径 ─────────────────────────────────────────

    @classmethod
    def _resolve_templates_dir(cls) -> Path:
        """解析模板根目录（项目内 <repo>/plugins/system/team_templates/）。

        解析策略：
        1. 从本文件位置向上 3 层得到项目根
        2. 若该目录不可写（PyInstaller 打包后常见），fallback 到 ~/.drifox/team_templates/
        """
        # app/core/team/template_manager.py
        #   parent (1) → app/core/team/
        #   parent (2) → app/core/
        #   parent (3) → app/
        #   parent (4) → 项目根
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        candidate = project_root / "plugins" / "system" / cls._TEMPLATES_SUBDIR

        # 探测：目录已存在 → 直接用；不存在但可写 → 创建并用；
        # 不存在且父目录不可写 → fallback 到用户目录
        if candidate.exists():
            return candidate
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # 用一个临时文件探测可写性
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            from app.utils.utils import get_app_data_dir

            fallback = get_app_data_dir() / "team_templates"
            logger.warning(f"[TemplateManager] 项目内路径不可写 ({candidate})，fallback 到用户目录: {fallback}")
            return fallback

    def _template_path(self, name: str) -> Path:
        return self._templates_dir / f"{name}.yaml"

    @staticmethod
    def _validate_name(name: str) -> str:
        """校验模板名合法，返回原值（去除首尾空白）。"""
        if not isinstance(name, str):
            raise TemplateError(f"模板名必须是字符串，得到: {type(name).__name__}")
        name = name.strip()
        if not name:
            raise TemplateError("模板名不能为空")
        if not _NAME_PATTERN.match(name):
            raise TemplateError(f"模板名非法: {name!r}（仅允许字母/数字/下划线/中划线，且以字母或数字开头，长度 1-64）")
        return name

    # ── 公开 API ─────────────────────────────────────

    def save(self, template: Template) -> Path:
        """保存模板到 YAML 文件。

        Returns:
            写入的文件路径。
        """
        if not isinstance(template, Template):
            raise TemplateError(f"save 需要 Template 实例，得到: {type(template).__name__}")

        name = self._validate_name(template.template_name)
        template.template_name = name  # 同步为校验后的值

        path = self._template_path(name)
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
        """读取模板 YAML → Template 对象。

        Raises:
            TemplateError: 模板不存在、YAML 解析失败、字段非法
        """
        name = self._validate_name(name)
        path = self._template_path(name)
        if not path.exists():
            raise TemplateError(f"模板不存在: {name}（路径: {path}）")

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise TemplateError(f"模板 YAML 解析失败: {name} ({e})") from e
        except OSError as e:
            raise TemplateError(f"读取模板文件失败: {path} ({e})") from e

        if raw is None:
            raise TemplateError(f"模板文件为空: {path}")
        if not isinstance(raw, dict):
            raise TemplateError(f"模板顶层必须是对象，得到: {type(raw).__name__}（文件: {path}）")

        try:
            template = Template.from_dict(raw)
        except TemplateError:
            # 保留文件路径上下文，便于排查
            raise
        except Exception as e:  # noqa: BLE001 — 兜底，避免未预期异常逃逸
            raise TemplateError(f"模板结构非法: {name} ({e})") from e

        # 校验后再覆盖 template_name，保证一致（文件路径已用过 name 校验）
        template.template_name = name
        return template

    def list_templates(self) -> List[Dict[str, Any]]:
        """列出所有可用模板的元信息。

        Returns:
            每项包含: name / description / agent_count / path
            agent_count 缺失/损坏文件跳过（不抛错，只是不显示）
        """
        results: List[Dict[str, Any]] = []
        if not self._templates_dir.exists():
            return results

        for path in sorted(self._templates_dir.glob("*.yaml")):
            name = path.stem
            try:
                tpl = self.load(name)
            except TemplateError as e:
                logger.warning(f"[TemplateManager] 跳过损坏模板 {name}: {e}")
                continue
            results.append(
                {
                    "name": tpl.template_name,
                    "description": tpl.description,
                    "agent_count": len(tpl.agents),
                    "agent_names": [a.agent_name for a in tpl.agents],
                    "path": str(path),
                }
            )
        return results

    def delete(self, name: str) -> bool:
        """删除模板文件。

        Returns:
            True 表示删除成功；False 表示文件本来就不存在。
        """
        name = self._validate_name(name)
        path = self._template_path(name)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as e:
            raise TemplateError(f"删除模板失败: {path} ({e})") from e
        logger.info(f"[TemplateManager] 已删除模板: {name}")
        return True

    def exists(self, name: str) -> bool:
        """检查模板是否存在（不抛错）。"""
        try:
            name = self._validate_name(name)
        except TemplateError:
            return False
        return self._template_path(name).exists()

    # ── 路径暴露（便于测试与调试）─────────────────

    @property
    def templates_dir(self) -> Path:
        return self._templates_dir
