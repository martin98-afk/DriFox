# -*- coding: utf-8 -*-
"""U123 — 协议块前缀差量（R1a）回归测试。

背景：差量快路径里只要闭合段中含任一协议块（think/tool/注册 tag），整帧
让位全量渲染（full_protocol_seg），未闭合 think/tool 长流式期间每帧全量
（实测 86KB=25.4ms/帧）。R1a：首个协议段**之前**的纯文本闭合段照常差量
追加并推进基线（_stable_md_len += Σ(len(seg)+2)，与 _extract_closed_segments
口径一致），其余段保持整帧让位语义。

打桩方式（tests/widgets 惯例）：CodeWebViewer.__new__ 绕过 __init__（重依赖
WebEngine/profile），手工补齐 _perform_update 差量路径读取的最小状态集；
page/可见性/看门狗/字体/图表打桩为记录器，_sequence_render 打 spy。

断言编号对应施工验收口径（R1a 修前 U1 的 1/2/3/4/6 红、守卫断言
U1-5 / U2-2,3,5,6 / U3-3,5 绿；修后全绿）。

验证面：
- U1 协议段前缀差量：前缀段走 updateContentAppend、基线推进、partial_* 计数、
  截断式 _last_rendered_markdown；协议段 rest 仍整帧让位（防过度优化）
- U2 未闭合协议基线防护：基线绝不越过未闭合区、未闭合内容不入差量、
  让位整帧全量保持
- U3 fence 与协议块交错：协议段前 fence 照常差量；协议段后 fence 仍是
  让位边界（不偷跑差量）
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections import Counter

import pytest
from PyQt5.QtWidgets import QApplication

from app.widgets.card_viewers import CodeWebViewer


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _StubPage:
    """记录 runJavaScript 载荷（不执行 JS）。"""

    def __init__(self):
        self.js = []

    def runJavaScript(self, js, callback=None):  # noqa: N802
        self.js.append(js)


def _make_diff_viewer(markdown_text: str):
    """最小状态集的差量路径 viewer（绕过 __init__，见模块 docstring）。

    未跑 QObject C++ 构造时，任何不在实例 __dict__ 的属性读取都会抛
    RuntimeError（getattr 默认值挡不住）——按 __init__ 源码批量预填全部
    属性名（None 兜底），再覆盖差量路径的分支控制项。
    """
    import inspect
    import re

    v = CodeWebViewer.__new__(CodeWebViewer)  # QObject 子类安全 __new__
    for name in set(re.findall(r"self\.(\w+)\s*=", inspect.getsource(CodeWebViewer.__init__))):
        if name not in v.__dict__:
            try:
                setattr(v, name, None)
            except Exception:
                pass

    page = _StubPage()
    v.page = lambda: page  # truthy，_perform_update 首道门
    v.isVisible = lambda: True  # noqa: N802
    v.parent = lambda: None  # v2 块源探测 → 无块源 → 关闭 v2
    v._render_v2 = False
    v._arm_render_watchdog = lambda *a, **k: None
    v._refresh_viewer_font_css = lambda: None
    v._mark_has_charts = lambda html: None
    v._has_pending_tool_dom = lambda: False
    v._render_tail_inline = lambda: None

    # 差量路径分支控制项
    v._streaming = True
    v._needs_full_render = False
    v._stable_md_len = 0
    v._stable_html = ""
    v._markdown_text = markdown_text
    v._last_rendered_markdown = ""
    v._lazy_markdown_cb = None
    v._last_render_ts = 0.0
    v._render_deferred = False
    v._light_skeleton = False
    v._dock_sync_requested = True  # 跳过坞态同步分支
    v._tool_dom_dirty = False
    v._restore_finished_ids = set()
    # _tool_compact_mode / _tool_lazy_body 是只读 property（Settings 读取，
    # 异常回退 True），裸实例直接用类 property，不赋值
    v._rdiag = Counter()

    # spy：整帧让位（全量渲染提交）记录器
    v._seq_render_calls = []
    v._sequence_render = lambda md, compact, _v=v: _v._seq_render_calls.append(md)
    return v


def _append_payloads(v):
    """提取 updateContentAppend 的 JS 载荷列表。"""
    return [js for js in v.page().js if js.startswith("updateContentAppend(")]


def test_u1_protocol_prefix_diff(qapp):
    """U1：协议段之前的纯文本闭合段照常差量，基线推进 + 让位语义保持"""
    md = "前缀段落一。\n\n前缀段落二。\n\n<think>思考内容</think>\n\n后缀正文。\n\n"
    v = _make_diff_viewer(md)

    CodeWebViewer._perform_update(v)

    prefix_len = len("前缀段落一。\n\n前缀段落二。\n\n")
    payloads = _append_payloads(v)

    # 断言1：差量追加发生（修前 0 次 → 红）
    assert len(payloads) >= 1, "前缀段应走 updateContentAppend 差量追加"
    # 断言2：载荷含两个前缀段、不含协议段内容（修前红）
    joined = "".join(payloads)
    assert "前缀段落一" in joined and "前缀段落二" in joined
    assert "思考内容" not in joined
    # 断言3：基线推进到协议段起点（修前 0 → 红）
    assert v._stable_md_len == prefix_len
    # 断言4：partial 计数生效（修前无 → 红）
    assert v._rdiag.get("partial_prefix") == 2
    assert v._rdiag.get("partial_protocol") == 1
    # 断言5：rest 含协议段 → 整帧让位语义保持（守卫，修前绿）
    assert len(v._seq_render_calls) == 1
    assert v._seq_render_calls[0] == md
    # 断言6：_last_rendered_markdown 截断赋值（修前为全文 → 红）
    assert v._last_rendered_markdown == md[:prefix_len]


def test_u2_unclosed_protocol_baseline_guard(qapp):
    """U2：未闭合协议区基线绝不越过，未闭合内容不入差量，让位整帧全量"""
    md = "前缀段落。\n\n<think>已闭合思考</think>\n\n中段正文。\n\n尾部正文 <tool>未闭合工具"
    v = _make_diff_viewer(md)

    CodeWebViewer._perform_update(v)

    payloads = _append_payloads(v)
    joined = "".join(payloads)

    # 断言1：基线只推进到协议段起点（修后新增）
    assert v._stable_md_len == len("前缀段落。\n\n")
    # 断言2：未闭合区存在 → 整帧让位发生（守卫，修前绿）
    assert len(v._seq_render_calls) == 1
    # 断言3：未闭合工具内容不入差量载荷（守卫，修前绿——修前差量为 0）
    assert "未闭合工具" not in joined
    # 断言4：partial 计数生效（修后新增）
    assert v._rdiag.get("partial_prefix") == 1
    assert v._rdiag.get("partial_protocol") == 1
    # 断言5：基线未越过未闭合区（守卫，修前绿——stable=0）
    assert v._stable_md_len < md.find("<tool>")
    # 断言6：让位收到全文（整帧全量语义保持，守卫，修前绿）
    assert v._seq_render_calls[0] == md


def test_u3_fence_protocol_interleave(qapp):
    """U3：协议段前 fence 照常差量；协议段后 fence 仍是让位边界（防偷跑）"""
    md = (
        "```python\ncode1\n```\n\n<think>x</think>\n\n"
        "```text\nchart2\n```\n\n文字段。\n\n"
    )
    v = _make_diff_viewer(md)

    CodeWebViewer._perform_update(v)

    payloads = _append_payloads(v)
    joined = "".join(payloads)

    # 断言1：协议段前的 fence 段差量渲染（修后新增）
    assert len(payloads) >= 1
    assert "code1" in joined
    # 断言2：基线推进到协议段起点（修后新增）
    assert v._stable_md_len == len("```python\ncode1\n```\n\n")
    # 断言3：协议段之后的 fence 不差量（守卫，修前绿——修前差量为 0）
    assert "chart2" not in joined
    # 断言4：partial 计数生效（修后新增）
    assert v._rdiag.get("partial_prefix") == 1
    # 断言5：fence 段仍处让位边界，整帧全量发生（守卫，修前绿）
    assert len(v._seq_render_calls) == 1
    assert v._seq_render_calls[0] == md
    # 断言6：协议段内容不入差量（守卫，修前绿）
    assert "<think>" not in joined
