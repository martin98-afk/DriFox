# -*- coding: utf-8 -*-
"""auto_enable 条目跳过 enabled_skills 过滤，随 project_dir 出现/消失"""

from unittest.mock import patch

class _StubAgentManager:
    """最小 AgentManager 桩：只暴露被测方法"""

    from app.core.conversation.agent import AgentManager as _AM

    get_enabled_skills_content = _AM.get_enabled_skills_content

def test_auto_enable_skill_included_without_enabled_list():
    mgr = _StubAgentManager()
    skills = [
        {"name": "normal-skill", "qualified_name": "normal-skill", "description": "N",
         "plugin_name": None, "is_system": True, "path": "", "source": None, "auto_enable": False},
        {"name": "proj-skill", "qualified_name": "proj-skill", "description": "P",
         "plugin_name": None, "is_system": False, "path": "/x", "source": "project", "auto_enable": True},
    ]
    with patch("app.utils.utils.get_local_skills", return_value=skills):
        out = mgr.get_enabled_skills_content([], project_dir="/proj")
    assert "proj-skill" in out
    assert "normal-skill" not in out

def test_enabled_list_still_respected():
    mgr = _StubAgentManager()
    skills = [
        {"name": "a", "qualified_name": "a", "description": "A",
         "plugin_name": None, "is_system": True, "path": "", "source": None, "auto_enable": False},
    ]
    with patch("app.utils.utils.get_local_skills", return_value=skills):
        out = mgr.get_enabled_skills_content(["a"], project_dir=None)
        assert "### a" in out
        out2 = mgr.get_enabled_skills_content([], project_dir=None)
        assert out2 == ""

def test_auto_enable_respects_project_dir():
    """get_local_skills 收到 project_dir 才返回动态条目 → 换项目自然消失（集成语义）"""
    mgr = _StubAgentManager()
    calls = []

    def fake_lookup(project_dir=None):
        calls.append(project_dir)
        if project_dir == "/proj":
            return [{"name": "proj-skill", "qualified_name": "proj-skill", "description": "P",
                     "plugin_name": None, "is_system": False, "path": "/x",
                     "source": "project", "auto_enable": True}]
        return []

    with patch("app.utils.utils.get_local_skills", side_effect=fake_lookup):
        assert "proj-skill" in mgr.get_enabled_skills_content([], project_dir="/proj")
        assert mgr.get_enabled_skills_content([], project_dir=None) == ""
    assert calls == ["/proj", None]
