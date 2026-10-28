# -*- coding: utf-8 -*-
"""
性能回归测试（#1 性能瓶颈报告 Top④）：启动初始化同步直调插件系统（未延迟）

(a) 对应瓶颈：主窗口构造时同步调用 backend.initialize，进而同步直调 _init_plugin_system
    与 reload_agents，阻塞首屏。第一批尚未延迟该同步链（记录当前现象，作为基线）。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取 app/main_widget.py 与
    app/core/backend.py 源码文本 + re 匹配，不 import PyQt5、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_MAIN = REPO_ROOT / "app" / "main_widget.py"
SRC_BACKEND = REPO_ROOT / "app" / "core" / "backend.py"


@pytest.fixture(scope="module")
def main_src() -> str:
    return SRC_MAIN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_src() -> str:
    return SRC_BACKEND.read_text(encoding="utf-8")


def test_static_sync_init_chain(main_src: str, backend_src: str):
    """静态扫描：确认启动同步初始化链存在。"""
    assert "self.backend.initialize(" in main_src
    assert "_init_plugin_system" in backend_src
    assert "reload_agents" in backend_src


def test_perf_init_not_deferred(backend_src: str):
    """性能/回归断言：_init_plugin_system 为同步直调（其前 6 行不含 singleShot 延迟），
    且 reload_agents 同步触发。"""
    assert "self._init_plugin_system()" in backend_src

    lines = backend_src.splitlines()
    # 定位 self._init_plugin_system() 所在行
    target_idx = None
    for i, line in enumerate(lines):
        if "self._init_plugin_system()" in line:
            target_idx = i
            break
    assert target_idx is not None, "未找到 self._init_plugin_system() 调用"

    # 检查其前 6 行（不含自身）不含 singleShot 延迟
    start = max(0, target_idx - 6)
    preceding = lines[start:target_idx]
    assert not any("singleShot" in ln for ln in preceding), (
        f"_init_plugin_system() 前应无 singleShot 延迟，实际前 6 行含: {preceding}"
    )

    assert "self._agent_manager.reload_agents()" in backend_src
