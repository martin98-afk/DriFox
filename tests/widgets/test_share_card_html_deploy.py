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


# ── 主题适配（深浅主题都要能看清）────────────────────────────────


def test_theme_tokens_cover_role_and_code_colors(share_src: str):
    """角色 / 代码 / 工具配色必须从主题取，不得硬编码（否则亮色主题下白字不可见）"""
    for key in (
        "user_card_bg",
        "user_card_accent",
        "assistant_card_bg",
        "assistant_card_accent",
        "syntax_step",
        "syntax_tool",
        "tag_accent_text",
        "divider_color",
    ):
        assert f'_pick("{key}"' in share_src, f"缺少主题 token 映射: {key}"


def test_no_white_hardcode_in_css_body(share_src: str):
    """导出模板的 CSS 里不得再用 #fff / 白色半透明叠加（亮色主题下会白底白字）"""
    css_start = share_src.find('return f"""<!DOCTYPE html>')
    assert css_start > 0
    css = share_src[css_start : share_src.find("class ShareCardContent")]
    for bad in ("color: #fff;", "color: #c4cedd", "color: #9bddff", "color: #5fd18c", "rgba(255, 255, 255, 0.04)"):
        assert bad not in css, f"仍存在硬编码颜色: {bad}"


def test_markdown_uses_theme_aware_pygments(share_src: str):
    """代码块走 codehilite + 按主题明暗选择高亮配色"""
    assert '"codehilite"' in share_src
    assert "def _pygments_style_for_theme" in share_src
    assert '"friendly" if theme_manager.is_light_theme() else "dracula"' in share_src


# ── 侧栏导航（乱飞回归）──────────────────────────────────────────


def test_nav_highlight_not_using_intersection_observer_entries(share_src: str):
    """导航高亮不得依赖 IntersectionObserver 的 entries 做「取最靠上可见项」

    entries 只含状态发生变化的元素，滚动中未跨界的目标不在集合里，
    取「最靠上的可见项」会取到残缺集合的结果导致高亮来回跳。
    """
    assert "new IntersectionObserver" not in share_src


def test_nav_highlight_computes_from_document_order(share_src: str):
    """高亮按 DOM 顺序全量比较 getBoundingClientRect，而非对象键顺序"""
    assert "entries.push(" in share_src
    assert "getBoundingClientRect().top <= baseY" in share_src


def test_sidebar_auto_scrolls_to_active_item(share_src: str):
    """高亮项滚出侧栏可视区时，侧栏需自动滚动跟随"""
    assert "sidebar.scrollTop = Math.max(0, linkTop - 8)" in share_src
    assert "linkBottom - sidebar.clientHeight + 8" in share_src


# ── 窄屏布局（导航消失回归）──────────────────────────────────────


def test_narrow_breakpoint_keeps_sidebar_sticky(share_src: str):
    """窄屏断点不得把侧栏改为 static

    原实现 .sidebar 在 <=760px 时 position: static，滚动越过它之后导航
    永久消失、正文撑满全宽（用户感知为「消息突然占满屏幕、左边列表没了」）。
    """
    m = re.search(r"@media \(max-width: 760px\) \{\{(.*?)\n\}}\n", share_src, re.S)
    assert m is not None, "缺少窄屏断点"
    block = m.group(1)
    assert "position: static" not in block, "窄屏侧栏不能是 static（滚走即消失）"
    assert "position: sticky" in block, "窄屏侧栏应保持 sticky 常驻"


def test_narrow_breakpoint_uses_horizontal_nav(share_src: str):
    """窄屏导航转为横向滚动条，并有横向溢出滚动"""
    m = re.search(r"@media \(max-width: 760px\) \{\{(.*?)\n\}}\n", share_src, re.S)
    block = m.group(1)
    assert "flex-direction: row" in block
    assert "overflow-x: auto" in block
    # .nav-snip（摘要）在横向条里隐藏，避免撑爆
    assert ".nav-snip {{ display: none; }}" in block


def test_narrow_breakpoint_stretches_children(share_src: str):
    """窄屏 column 布局下子元素须拉伸，否则正文只剩侧栏宽度"""
    m = re.search(r"@media \(max-width: 760px\) \{\{(.*?)\n\}}\n", share_src, re.S)
    block = m.group(1)
    assert "align-items: stretch" in block
    assert ".main {{ width: 100%; }}" in block


def test_sidebar_follow_supports_horizontal(share_src: str):
    """侧栏跟随需同时处理纵向（宽屏列表）与横向（窄屏条）"""
    assert "sidebar.scrollLeft = Math.max(0, linkLeft - 8)" in share_src
    assert "linkRight - sidebar.clientWidth + 8" in share_src
