# -*- coding: utf-8 -*-
"""DRIFOX_DATA_DIR 环境变量隔离测试（S3a 任务一）

验证两处数据目录消费点都尊重该环境变量，且未设置时行为不变：
- app.utils.utils.get_app_data_dir
- app.utils.render_env.default_config_path（Qt 前最早消费点）
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_get_app_data_dir_env_override(monkeypatch, tmp_path):
    from app.utils.utils import get_app_data_dir

    target = tmp_path / "iso-a"
    monkeypatch.setenv("DRIFOX_DATA_DIR", str(target))
    assert get_app_data_dir() == Path(str(target))


def test_default_config_path_env_override(monkeypatch, tmp_path):
    from app.utils.render_env import default_config_path

    target = tmp_path / "iso-b"
    monkeypatch.setenv("DRIFOX_DATA_DIR", str(target))
    assert default_config_path() == os.path.join(str(target), "app.config")


@pytest.mark.parametrize("env_set", [False])
def test_env_unset_behavior_unchanged(monkeypatch, env_set):
    """未设置环境变量时行为与历史一致（开发态 = 相对 .drifox）。"""
    if env_set:  # pragma: no cover - 参数化占位，保持单一用例
        return
    from app.utils.render_env import default_config_path
    from app.utils.utils import get_app_data_dir

    monkeypatch.delenv("DRIFOX_DATA_DIR", raising=False)
    # 开发环境（非 frozen）下历史行为：相对 .drifox
    assert not Path(str(get_app_data_dir())).is_absolute() or sys_frozen_like()
    assert default_config_path().endswith(os.path.join(".drifox", "app.config")) or sys_frozen_like()


def sys_frozen_like() -> bool:  # pragma: no cover - frozen 环境兜底
    return getattr(__import__("sys"), "frozen", False)
