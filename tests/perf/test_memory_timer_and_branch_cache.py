# -*- coding: utf-8 -*-
"""
性能回归测试（#1 性能瓶颈报告 Top⑤）：_branch_cache 淘汰/上限保护

(a) 对应瓶颈：_branch_cache 按路径缓存 git branch 但无淘汰/上限，
    长期运行内存只增不减。已通过增加上限常量 + `pop` 淘汰修复；
    本测试用于固化修复状态，防止回归。

    历史说明：本文件早期同时覆盖 `_memory_timer`（标题栏 RSS 内存标签），
    该标签已按需求下线，相关用例移除，仅保留 _branch_cache 部分。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取 app/main_widget.py 源码文本
    + re 匹配，不 import PySide6、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "app" / "main_widget.py"


@pytest.fixture(scope="module")
def src_text() -> str:
    return SRC.read_text(encoding="utf-8")


def test_static_branch_cache_present(src_text: str):
    """静态扫描：确认 _branch_cache 仍存在于主窗口源码（按路径缓存 git branch）。"""
    assert "self._branch_cache" in src_text


def test_perf_branch_cache_has_eviction_and_cap(src_text: str):
    """修复后回归保护：_branch_cache 设有淘汰（pop 调用）以及上限常量
    （名称形如 `_MAX_BRANCH*`），防止后续重构误删性能修复。"""
    assert re.search(r"_branch_cache\.pop\b", src_text) is not None
    assert re.search(r"_MAX_BRANCH\w*\s*=", src_text) is not None
