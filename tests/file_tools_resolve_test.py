# -*- coding: utf-8 -*-
"""
_resolve 路径解析回归测试（read/write/edit/multi_edit/grep/glob/list 等工具共用）

覆盖：
- 环境变量展开：Windows %VAR% / POSIX $VAR（os.path.expandvars 平台语义）
- 未定义变量保持字面量：回退 workdir 拼接，不崩溃
- 原有行为回归：相对路径拼 workdir、绝对路径直用、~ 展开、空路径回退

运行方式: python -m pytest tests/file_tools_resolve_test.py -v
"""
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from plugins.system.tools.file_tools import _resolve


# ========== 环境变量展开 ==========

def test_resolve_expands_env_var(monkeypatch, tmp_path):
    """已定义变量展开为绝对路径（Win 用 %VAR%，POSIX 用 $VAR）"""
    monkeypatch.setenv("DRIFOX_RESOLVE_TEST", str(tmp_path))
    raw = "%DRIFOX_RESOLVE_TEST%/x.txt" if os.name == "nt" else "$DRIFOX_RESOLVE_TEST/x.txt"
    assert _resolve(None, raw) == tmp_path / "x.txt"


def test_resolve_undefined_var_falls_back_to_workdir(tmp_path):
    """未定义变量保持字面量，按相对路径拼 workdir（与旧行为一致，不崩）"""
    raw = "%DRIFOX_UNDEFINED_VAR_XYZ%/a.txt" if os.name == "nt" else "$DRIFOX_UNDEFINED_VAR_XYZ/a.txt"
    assert _resolve(tmp_path, raw) == tmp_path / raw


def test_resolve_percent_literal_without_var(tmp_path):
    """含 % 但非变量语法（如 100%.txt）不受影响，落 workdir 拼接"""
    p = _resolve(tmp_path, "100%.txt")
    assert p == tmp_path / "100%.txt"


# ========== 原有行为回归 ==========

def test_resolve_relative_joins_workdir(tmp_path):
    assert _resolve(tmp_path, "sub/a.txt") == tmp_path / "sub" / "a.txt"


def test_resolve_absolute_used_directly(tmp_path):
    absolute = tmp_path / "a.txt"
    assert _resolve(tmp_path, str(absolute)) == absolute


def test_resolve_tilde_expands():
    home = Path.home()
    assert _resolve(None, "~/x.txt") == home / "x.txt"


def test_resolve_empty_returns_workdir(tmp_path):
    assert _resolve(tmp_path, "") == tmp_path
