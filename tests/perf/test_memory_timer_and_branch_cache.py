# -*- coding: utf-8 -*-
"""
性能回归测试（#1 性能瓶颈报告 Top⑤）：_branch_cache 淘汰/上限保护

(a) 对应瓶颈：_branch_cache 按路径缓存 git branch 但无淘汰/上限，
    长期运行内存只增不减。已通过增加上限常量 + `pop` 淘汰修复；
    本测试用于固化修复状态，防止回归。

    历史说明：本文件早期同时覆盖 `_memory_timer`（标题栏 RSS 内存标签），
    该标签已按需求下线，相关用例移除，仅保留 _branch_cache 部分。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取 app/main_widget.py 源码文本
    + re 匹配，不 import PyQt5、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# [T22] 扫描目标已从 main_widget.py 迁至 app/utils/git_worktree.py：
# 分支缓存随工作树插件化迁出主窗口（见 5f2cfcec），现由 GitWorktreeDetector 持有。
SRC = REPO_ROOT / "app" / "utils" / "git_worktree.py"


@pytest.fixture(scope="module")
def src_text() -> str:
    return SRC.read_text(encoding="utf-8")


def test_static_branch_cache_present(src_text: str):
    """静态扫描：确认 _branch_cache 仍存在于 git worktree 源码（按路径缓存 git branch）。"""
    assert "_branch_cache" in src_text


def test_perf_branch_cache_has_eviction_and_cap(src_text: str):
    """回归保护：_branch_cache 必须是有界缓存。

    历史变迁（考古结论，`git log -S` 定位）：
    - 0bb59798 在 main_widget.py 引入 `_MAX_BRANCH_CACHE = 64` 条目上限 +
      `pop` 淘汰，本测试即为此固化；
    - 5f2cfcec 随工作树插件化把分支缓存迁出主窗口，最终落到
      app/utils/git_worktree.py（GitWorktreeDetector）；
    - 迁移后**条目上限被有意替换为 TTL 有界**（`_CACHE_TTL = 30.0`，
      `git_worktree.py:141-151` 的 `_cache_get` 按 monotonic 时间戳判过期，
      另有 :430 处同类清理）。当前唯一的有界保证是 TTL，不再有 `_MAX_BRANCH*`
      条目上限，也不再有按插入顺序的 `pop` 淘汰。

    因此本断言改为校验 TTL 常量存在——这既贴合当前实现，又保留了
    「缓存必须有界」这一原始回归意图。
    """
    assert re.search(r"_CACHE_TTL\s*=", src_text) is not None, (
        "_branch_cache 的唯一有界保证 _CACHE_TTL 缺失：历史变迁为 "
        "0bb59798 引入 _MAX_BRANCH_CACHE 条目上限 + pop 淘汰 → 5f2cfcec 迁至 "
        "git_worktree.py 后改为 TTL 有界（条目上限与顺序 pop 均已移除）"
    )
