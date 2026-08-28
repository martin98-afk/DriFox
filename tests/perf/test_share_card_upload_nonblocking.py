# -*- coding: utf-8 -*-
"""
性能回归测试（#1 性能瓶颈报告 Top③）：分享卡片上传改为后台线程，不再阻塞主线程

(a) 对应瓶颈：分享卡片点击上传时，Gitee 上传（requests.post，阻塞 30s 超时）直接在主线程
    执行，导致 UI 卡死。第一批修复已将上传改为后台 QThread（_ShareUploadThread），并切换
    按钮状态（禁用 + "上传中…" 提示）。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取
    app/widgets/cards/floating/share_card.py 与 app/gateway/utils/gitee_uploader.py 源码文本
    + re 匹配，不 import PySide6、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_SHARE = REPO_ROOT / "app" / "widgets" / "cards" / "floating" / "share_card.py"
SRC_UPLOADER = REPO_ROOT / "app" / "gateway" / "utils" / "gitee_uploader.py"


@pytest.fixture(scope="module")
def share_src() -> str:
    return SRC_SHARE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def uploader_src() -> str:
    return SRC_UPLOADER.read_text(encoding="utf-8")


def test_static_upload_call_present(share_src: str):
    """静态扫描：确认分享卡片存在上传入口与上传器调用。"""
    assert "def _on_upload" in share_src
    assert "uploader.upload_file(" in share_src


def test_perf_upload_nonblocking_with_button_toggle(share_src: str, uploader_src: str):
    """性能/回归断言：上传已后台线程化（_ShareUploadThread）并切换按钮状态；
    底层阻塞上传来源（requests.post, timeout=30）仍存在。"""
    # 后台上传线程类存在（宽松匹配 class ...Upload... (QThread)）
    assert re.search(r"class\s+\w*Upload\w*\s*\(\s*QThread\s*\)", share_src) is not None
    # 按钮反馈：点击后禁用并提示上传中
    assert "setEnabled(False)" in share_src
    assert "上传中" in share_src
    # 底层阻塞上传来源
    assert "requests.post(" in uploader_src
    assert "timeout=30" in uploader_src
