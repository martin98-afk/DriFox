# -*- coding: utf-8 -*-
"""
性能回归测试（#1 性能瓶颈报告 Top⑤）：_memory_timer 常驻主线程 5s + _branch_cache 无上限

(a) 对应瓶颈：主窗口常驻 _memory_timer（父对象 self，5s 间隔）每 5 秒刷新内存显示，
    且 _branch_cache 按路径缓存 git branch 但无淘汰/上限，长期运行内存只增不减。
    第一批尚未修复，本文件作为「修复前基线」，记录当前现象；待修复后需更新/新增断言。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取 app/main_widget.py 源码文本
    + re 匹配，不 import PyQt5、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "app" / "main_widget.py"


@pytest.fixture(scope="module")
def src_text() -> str:
    return SRC.read_text(encoding="utf-8")


def test_static_memory_timer_and_branch_cache(src_text: str):
    """静态扫描：确认 _memory_timer 父对象为 self、间隔 5s，且存在 _branch_cache。"""
    assert re.search(r"self\._memory_timer\s*=\s*QTimer\(self\)", src_text) is not None
    assert "_memory_timer.setInterval(5000)" in src_text
    assert "self._branch_cache" in src_text


def test_perf_timer_parent_and_cache_no_eviction(src_text: str):
    """回归/基线断言（当前应 PASS，记录未修复现状）：
    _memory_timer 父对象为 self；_branch_cache 无淘汰（pop/clear 不存在）、无上限（MAX_BRANCH 不存在）。"""
    assert re.search(r"self\._memory_timer\s*=\s*QTimer\(self\)", src_text) is not None
    assert "_branch_cache.pop" not in src_text
    assert "_branch_cache.clear" not in src_text
    assert "MAX_BRANCH" not in src_text
