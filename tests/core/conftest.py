# -*- coding: utf-8 -*-
"""core/ 测试共享 fixtures。

T19 批次1 上收：
- fresh_tm（6 个 team_* 测试）：teams 根指向 tmp_path，含 get_instance 防护
- team_manager（5 个测试）：teams 指向 tmp_path/teams 子目录
两者均走单例并在 yield 后复位 _instance。
"""
import pytest

from app.core import team_manager as tm_mod

# 模块导入时缓存真实 get_instance（对齐原 test_team_project.py 的隔离防护）
_ORIG_GET_INSTANCE = tm_mod.TeamManager.__dict__["get_instance"]


@pytest.fixture
def fresh_tm(tmp_path, monkeypatch):
    """指向 tmp_path 的全新 TeamManager 实例（隔离，不污染真实 ~/.drifox/）。"""
    monkeypatch.setattr(tm_mod.TeamManager, "get_instance", _ORIG_GET_INSTANCE)
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path))
    tm_mod.TeamManager._instance = None
    tm = tm_mod.TeamManager.get_instance()
    yield tm
    tm_mod.TeamManager._instance = None


@pytest.fixture
def team_manager(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager（避免污染真实 teams 目录）。"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    tm_mod.TeamManager._instance = None
    tm = tm_mod.TeamManager.get_instance()
    yield tm
    tm_mod.TeamManager._instance = None
