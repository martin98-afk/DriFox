# -*- coding: utf-8 -*-
"""Team Template 数据结构与校验。

负责：
- 定义 Template / TemplateAgent dataclass
- YAML dict ↔ dataclass 双向转换
- 基础字段校验（schema_version、agents 非空、agent_name 唯一）
- agent_name 在系统中存在性的语义校验（交给调用方传入 agent_manager）

为保持纯净，本模块不导入 app.core.agent / agent_manager，
仅依赖 dataclass + typing，方便单测中独立构造和断言。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class TemplateError(ValueError):
    """模板相关错误（不存在 / YAML 格式错误 / 字段非法等）。"""


# 当前支持的 schema_version；老版本加载时若发现不一致应抛 TemplateError
SUPPORTED_SCHEMA_VERSIONS = (1,)


@dataclass
class TemplateAgent:
    """模板中的一个智能体条目。

    字段：
    - agent_name: 引用 plugins/system/agents/ 下的角色名（如 build、review）
    """

    agent_name: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemplateAgent":
        if not isinstance(data, dict):
            raise TemplateError(f"TemplateAgent 字段必须是对象，得到: {type(data).__name__}")
        name = data.get("agent_name")
        if not isinstance(name, str) or not name.strip():
            raise TemplateError("TemplateAgent.agent_name 必须是非空字符串")
        return cls(agent_name=name.strip())

    def to_dict(self) -> Dict[str, Any]:
        return {"agent_name": self.agent_name}


@dataclass
class Template:
    """一个团队模板。

    字段：
    - schema_version: 当前固定为 1
    - template_name: 模板名（与文件名 stem 一致）
    - description: 一句话描述（可空）
    - agents: TemplateAgent 列表（至少 1 个，按顺序对应窗口 1..N）
    """

    schema_version: int = 1
    template_name: str = ""
    description: str = ""
    agents: List[TemplateAgent] = field(default_factory=list)

    # ── 序列化 ─────────────────────────────────────

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Template":
        if not isinstance(data, dict):
            raise TemplateError(f"Template 顶层必须是对象，得到: {type(data).__name__}")

        schema_version = data.get("schema_version", 1)
        if not isinstance(schema_version, int) or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise TemplateError(f"不支持的 schema_version: {schema_version}（支持: {list(SUPPORTED_SCHEMA_VERSIONS)}）")

        template_name = data.get("template_name", "")
        if not isinstance(template_name, str):
            raise TemplateError("template_name 必须是字符串")

        description = data.get("description", "")
        if not isinstance(description, str):
            raise TemplateError("description 必须是字符串")

        raw_agents = data.get("agents", [])
        if not isinstance(raw_agents, list):
            raise TemplateError("agents 必须是列表")
        if not raw_agents:
            raise TemplateError("agents 列表不能为空（至少包含一个智能体）")

        agents = [TemplateAgent.from_dict(item) for item in raw_agents]

        # 唯一性：同一 agent_name 不应重复
        names = [a.agent_name for a in agents]
        if len(names) != len(set(names)):
            seen: Dict[str, int] = {}
            for n in names:
                seen[n] = seen.get(n, 0) + 1
            duplicates = [k for k, v in seen.items() if v > 1]
            raise TemplateError(f"agents 列表中 agent_name 重复: {duplicates}")

        return cls(
            schema_version=schema_version,
            template_name=template_name,
            description=description,
            agents=agents,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "template_name": self.template_name,
            "description": self.description,
            "agents": [a.to_dict() for a in self.agents],
        }

    # ── 语义校验 ──────────────────────────────────

    def validate_agent_names(
        self,
        available_agent_names: Optional[List[str]] = None,
    ) -> List[str]:
        """校验模板中所有 agent_name 在系统中存在。

        Args:
            available_agent_names: 系统中已知的 agent_name 列表（None 时跳过此校验）

        Returns:
            不存在的 agent_name 列表（空列表表示全部合法）
        """
        if available_agent_names is None:
            return []
        available_set = set(available_agent_names)
        return [a.agent_name for a in self.agents if a.agent_name not in available_set]
