# -*- coding: utf-8 -*-
"""
性能回归测试（Qt 6.9 WebEngine 离屏冻结）：滚出可视区的卡片 renderer 冻结。

(a) 对应瓶颈：虚拟滚动体系把「可视区 ± 缓冲区」之外的批次整批卸载，但缓冲区内
    （可视区外最多 ±8 批，几十张卡）的 QWebEngineView 页面保持完整活跃——
    JS 定时器照跑、合成器照跑、renderer 内存不释放。滚动停止 500ms 后的
    _recycle_out_of_view_batches 是天然的限频挂点。

    修复三处（Qt 6.5+ LifecycleState API，PySide6>=6.9 满足）：
      1. CodeWebViewer.set_page_suspended：Suspended/Active 切换，含守卫
         （流式中 / 在途渲染 / JS 未就绪 / 渲染被推迟 / 上下文丢失时拒绝冻结）。
      2. MessageCard.set_viewer_suspended：转发；welcome 卡与纯 Qt 渲染器
         （MarkdownBlockViewer，无 renderer）不参与。
      3. MainWidget._recycle_out_of_view_batches：可视区 ±1 批恢复 Active，
         可视区 ±2 批之外、active 缓冲区之内冻结 Suspended（留 1 批过渡带）。

    另有两项配套 profile 级调优：
      - app/core/webengine_profile.py：关闭 WebGL / PDF Viewer / Plugins /
        ScrollAnimator（消息卡渲染场景用不到，ScrollAnimator 在软件合成下
        每帧 CPU 光栅，关闭后滚轮直接步进、更跟手）。
      - main.py：GPU 光栅参数化（DRIFOX_WEBENGINE_GPU=1 试用 GPU 模式，
        默认保持 --disable-gpu 纯软件光栅，规避 DirectComposition 历史闪烁）。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取 app/ 源码文本 +
    re 匹配，不 import PySide6、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MESSAGE_CARD = REPO_ROOT / "app" / "widgets" / "message_card.py"
MAIN_WIDGET = REPO_ROOT / "app" / "main_widget.py"
WEB_PROFILE = REPO_ROOT / "app" / "core" / "webengine_profile.py"
MAIN_ENTRY = REPO_ROOT / "main.py"


@pytest.fixture(scope="module")
def card_src() -> str:
    return MESSAGE_CARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def widget_src() -> str:
    return MAIN_WIDGET.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def profile_src() -> str:
    return WEB_PROFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def main_src() -> str:
    return MAIN_ENTRY.read_text(encoding="utf-8")


def test_perf_code_web_viewer_has_lifecycle_freeze(card_src: str):
    """CodeWebViewer.set_page_suspended 存在且使用官方 LifecycleState API。"""
    start = card_src.find("def set_page_suspended(self, suspended: bool) -> None:")
    assert start != -1, "未找到 CodeWebViewer.set_page_suspended 定义"
    # 函数体边界：下一个方法定义或类内注释分隔线（set_page_suspended 后是注释块）
    e1 = card_src.find("\n    def ", start + 1)
    e2 = card_src.find("\n    # ──", start + 1)
    ends = [e for e in (e1, e2) if e != -1]
    body = card_src[start:min(ends) if ends else len(card_src)]
    # 枚举名兼容：Qt 6.10+ 改名 Frozen，Qt 6.5~6.9 叫 Suspended，两者任一即合规
    assert "Frozen" in body or "Suspended" in body, "冻结未使用官方 LifecycleState"
    assert "Active" in body, "恢复未使用官方 Active 状态"
    # Chromium 硬性前置：冻结前必须 page.setVisible(False)（page 级可见性覆盖），
    # 否则 Qt isVisible()==True 的离屏卡片冻结被静默忽略（实测 6.11）
    assert "setVisible(False)" in body, "冻结前缺少 page.setVisible(False) 前置"
    assert "setVisible(True)" in body, "恢复后缺少 page.setVisible(True) 还原"
    # 守卫：流式中/在途渲染/JS 未就绪/渲染被推迟时拒绝冻结（防丢内容）
    for guard in ("_context_lost", "_streaming", "_render_inflight", "_is_js_ready", "_render_deferred"):
        assert guard in body, f"set_page_suspended 缺少守卫条件 {guard}"


def test_perf_message_card_forwards_suspend_and_skips_welcome(card_src: str):
    """MessageCard.set_viewer_suspended 转发 + welcome 卡不参与冻结。"""
    start = card_src.find("def set_viewer_suspended(self, suspended: bool) -> None:")
    assert start != -1, "未找到 MessageCard.set_viewer_suspended 定义"
    end = card_src.find("\n    def ", start + 1)
    body = card_src[start:end if end != -1 else len(card_src)]
    assert "set_page_suspended" in body, "未转发到 CodeWebViewer.set_page_suspended"
    assert "_is_welcome" in body, "welcome 卡未豁免（JS 交互复杂不可冻结）"


def test_perf_recycle_hook_suspends_out_of_view(widget_src: str):
    """滚动回收路径接入冻结：_recycle_out_of_view_batches 内调用 set_viewer_suspended。"""
    start = widget_src.find("def _recycle_out_of_view_batches(self):")
    assert start != -1, "未找到 _recycle_out_of_view_batches 定义"
    end = widget_src.find("\n    def ", start + 1)
    body = widget_src[start:end if end != -1 else len(widget_src)]
    assert "set_viewer_suspended" in body, "滚动回收未接入离屏冻结"
    # 恢复与冻结两个方向都要有（否则卡片冻结后无法恢复）
    assert "False if in_core" in body, "可视区核心批次未恢复 Active"


def test_perf_shared_profile_disables_unused_web_features(profile_src: str):
    """共享 profile 关闭消息卡用不到的 Web 能力（profile 级一次设置）。"""
    start = profile_src.find("def init_shared_web_profile(")
    assert start != -1, "未找到 init_shared_web_profile 定义"
    end = profile_src.find("\ndef ", start + 1)
    body = profile_src[start:end if end != -1 else len(profile_src)]
    for attr in ("WebGLEnabled", "PdfViewerEnabled", "PluginsEnabled", "ScrollAnimatorEnabled"):
        assert f"WebAttribute.{attr}" in body, f"共享 profile 未关闭 {attr}"


def test_perf_gpu_flags_behind_env_switch(main_src: str):
    """GPU 光栅参数化：默认禁用（历史闪烁兜底），DRIFOX_WEBENGINE_GPU=1 可试用。"""
    assert "DRIFOX_WEBENGINE_GPU" in main_src, "main.py 缺少 GPU 模式环境变量开关"
    assert "--disable-gpu-compositing" in main_src, "默认软件合成 flags 丢失"
    # 遮挡误判修复与 GPU 无关，任何模式都必须保留
    assert "CalculateNativeWinOcclusion,NativeWindowOcclusionTracking" in main_src, "遮挡误判双禁 flags 丢失"
