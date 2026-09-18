# -*- coding: utf-8 -*-
"""
分享卡片 HTML 走 EdgeOne 部署的回归测试

覆盖：
  - 按钮文案随格式切换（HTML → 发布网页，其它 → 生成链接）
  - _on_upload 对 HTML 分派到 _on_deploy_html，非 HTML 不触发部署
  - 部署线程类存在且继承 QThread（后台执行，不阻塞主线程）
  - 部署成功提示含有效期说明（30 分钟）
  - 失败时保留本地文件路径提示

静态分析 + 轻量 import，不实例化 GUI。

Run: pytest tests/widgets/test_share_card_html_deploy.py -v
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_SHARE = REPO_ROOT / "app" / "widgets" / "cards" / "floating" / "share_card.py"


@pytest.fixture(scope="module")
def share_src() -> str:
    return SRC_SHARE.read_text(encoding="utf-8")


def test_upload_button_text_switches_by_format(share_src: str):
    """HTML 格式按钮文案为「发布网页」，其它格式为「生成链接」"""
    assert '"🌐 发布网页" if fmt_id == "html" else "🔗 生成链接"' in share_src


def test_on_upload_dispatches_html_to_deploy(share_src: str):
    """_on_upload 中 HTML 分支调用 _on_deploy_html 并提前返回"""
    m = re.search(r"if fmt == \"html\":\s*\n\s*self\._on_deploy_html\([^)]*\)\s*\n\s*return", share_src)
    assert m is not None, "_on_upload 缺少 HTML 分派分支"


def test_deploy_thread_is_background_qthread(share_src: str):
    """部署走 QThread 子类，不阻塞 UI"""
    assert re.search(r"class\s+_EdgeOneDeployThread\s*\(\s*QThread\s*\)", share_src) is not None


def test_deploy_thread_calls_edgeone_deployer(share_src: str):
    """线程内调用 EdgeOneDeployer.deploy_html"""
    assert "from app.gateway.utils.edgeone_deployer import EdgeOneDeployer" in share_src
    assert "EdgeOneDeployer.get_instance().deploy_html(" in share_src


def test_deploy_button_shows_pending_and_restores(share_src: str):
    """发布期间按钮禁用并提示，完成后恢复可点"""
    assert '"⏳ 发布中…"' in share_src
    assert '"🌐 发布网页"' in share_src


def test_success_message_mentions_expiry(share_src: str):
    """成功提示必须说明链接有效期，不误导用户"""
    assert "30 分钟内有效" in share_src


def test_failure_keeps_local_file_hint(share_src: str):
    """失败提示保留本地文件可用性说明"""
    m = re.search(r"发布失败: \{err\}（文件已保存到本地）", share_src)
    assert m is not None


def test_html_deploy_records_upload_url(share_src: str):
    """部署成功写入分享记录，format 为 html"""
    m = re.search(r"def _on_deploy_finished.*?insert_record\(", share_src, re.S)
    assert m is not None
    deploy_body = share_src[m.start() : m.start() + 2000]
    assert 'format_="html"' in deploy_body
    assert "upload_url=url" in deploy_body


def test_gitee_branch_untouched(share_src: str):
    """Gitee 上传链路对非 HTML 格式保持原样"""
    assert "_ShareUploadThread(uploader, str(save_path))" in share_src
    assert "uploader.is_configured()" in share_src
