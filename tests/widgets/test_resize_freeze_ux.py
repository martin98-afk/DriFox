# -*- coding: utf-8 -*-
"""#RES-1 配对测试：「[UX] 连续 resize 冻结」三件套接线与行为。

覆盖（环境无关：源码静态断言 + __new__ 桩对象纯逻辑，不建 WebEngine）：
- 骨架资产：JS 三函数（_dfxSetResizing/_dfxApplyWidgetH/_dfxRestoreFade）
  + CSS 两规则（.dfx-resizing / dfx-restore-fade）+ 骨架缓存版本 bump；
- Python 侧：resizeEvent 挂冻结态且在 _streaming 早退之前、150ms 静默复位、
  池化复用复位（防幂等短路失效）、_push_doc_resizing JS 未就绪静默跳过；
- message_card：宽度 4px 量化（在宽度变化守卫之前）+ 退出占位「先落高度
  后显示」顺序。
"""
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_CORE = _REPO_ROOT / "app" / "widgets" / "card_render_core.py"
_VIEWERS = _REPO_ROOT / "app" / "widgets" / "card_viewers.py"
_CARD = _REPO_ROOT / "app" / "widgets" / "message_card.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── 骨架资产（JS + CSS）─────────────────────────────────────────

def test_skeleton_has_three_ux_functions():
    src = _read(_CORE)
    for fn in ("window._dfxSetResizing", "window._dfxApplyWidgetH", "window._dfxRestoreFade"):
        assert fn in src, f"骨架缺 {fn}"
    # 4px 死区
    m = re.search(r"_dfxApplyWidgetH = function.*?Math\.abs\(h - prev\) < 4", src, re.S)
    assert m, "_dfxApplyWidgetH 缺 4px 死区守卫"


def test_skeleton_has_css_rules_and_version_bump():
    src = _read(_CORE)
    assert re.search(r"\.dfx-resizing\b", src), "缺 .dfx-resizing 冻结规则"
    assert "dfx-restore-fade" in src, "缺 dfx-restore-fade 淡入规则"
    m = re.search(r"_SKELETON_CACHE_VERSION = (\d+)", src)
    assert m and int(m.group(1)) >= 84, f"骨架资产变更须 bump 版本 ≥84，实际 {m.group(1) if m else 'None'}"


# ── card_viewers：挂接顺序 / 复位 / 幂等 / 静默 ────────────────

def test_resize_event_hooks_before_streaming_early_return():
    src = _read(_VIEWERS)
    m = re.search(r"def resizeEvent\(self, event\):.*?(?=\n    def )", src, re.S)
    assert m, "resizeEvent 找不到"
    body = m.group(0)
    hook = body.find("self._set_doc_resizing(True)")
    assert hook >= 0, "resizeEvent 未挂冻结态"
    early = body.find("if self._streaming:")
    assert early > hook >= 0, "冻结挂接必须先于 _streaming 早退（流式期拖窗同样要冻结）"
    assert "self._doc_resize_quiet_timer.start()" in body, "静默定时器未随 resize 重启"


def test_quiet_window_is_150ms():
    src = _read(_VIEWERS)
    m = re.search(r"_doc_resize_quiet_timer\.setInterval\((\d+)\)", src)
    assert m and m.group(1) == "150", "静默复位窗口应为 150ms"


def test_reuse_resets_resizing_state():
    """池化复用会重载骨架：Python 侧状态必须同步归零，否则幂等短路让冻结失效。"""
    src = _read(_VIEWERS)
    m = re.search(r"def reset_for_reuse\(self\).*?(?=\n    def )", src, re.S)
    assert m, "reset_for_reuse 找不到"
    body = m.group(0)
    assert "self._doc_resizing_state = False" in body, "复用未复位冻结态记录"
    assert "self._doc_resize_quiet_timer.stop()" in body, "复用未停静默定时器"


def test_set_doc_resizing_is_idempotent(monkeypatch):
    """状态未变时短路：重复 True 不重复下发（JS 幂等语义的 Python 侧守卫）。"""
    from app.widgets.card_viewers import CodeWebViewer

    v = CodeWebViewer.__new__(CodeWebViewer)
    v._doc_resizing_state = False
    calls = []
    monkeypatch.setattr(v, "_push_doc_resizing", lambda on: calls.append(on))

    v._set_doc_resizing(True)
    v._set_doc_resizing(True)  # 状态已 True：必须短路
    assert calls == [True], f"幂等短路失效，下发序列 {calls}"

    v._set_doc_resizing(False)
    v._set_doc_resizing(False)
    assert calls == [True, False], f"False 侧幂等失效，下发序列 {calls}"


def test_push_doc_resizing_silent_without_page(monkeypatch):
    """JS 未就绪 / page 为 None：静默跳过不抛（渲染尚未建页是合法时序）。"""
    from app.widgets.card_viewers import CodeWebViewer

    v = CodeWebViewer.__new__(CodeWebViewer)
    monkeypatch.setattr(CodeWebViewer, "page", lambda self: None, raising=False)
    v._push_doc_resizing(True)  # 不应抛


# ── message_card：4px 量化 / 退出占位顺序 ──────────────────────

def test_width_quantized_to_4px_grid_before_change_guard():
    src = _read(_CARD)
    m = re.search(r"target_width = max\(0, int\(target_width\) & ~3\)", src)
    assert m, "缺 4px 量化"
    quant = src.find("int(target_width) & ~3")
    guard = src.find("if not force and target_width == self._last_synced_width")
    assert 0 < quant < guard, "量化必须发生在宽度变化守卫之前（否则量化值被旧值比较吃掉）"


def test_exit_placeholder_applies_height_before_show():
    """退出占位：目标高度在 viewer 仍隐藏时先应用，show 首帧即新布局。"""
    src = _read(_CARD)
    m = re.search(r"── 退出占位：先落高度、后显示 ──.*?(?=\n    def )", src, re.S)
    assert m, "退出占位重构段找不到（注释锚点变更须同步本守卫）"
    body = m.group(0)
    apply_h = body.find("_pending_viewer_height")
    show = body.find("self.viewer.show()")
    assert 0 <= apply_h < show, "高度应用必须先于 viewer.show()（先落高度后显示）"
    assert "setUpdatesEnabled(False)" in body, "show 前须禁更新防中间帧出图"


def test_restore_fade_wired_before_show():
    """淡入必须在 show 之前置位（show 后再下发 CSS 会先闪不透明内容）。"""
    src = _read(_CARD)
    m = re.search(r"── 退出占位：先落高度、后显示 ──.*?(?=\n    def )", src, re.S)
    assert m
    body = m.group(0)
    fade = body.find("restore_content_fade")
    show = body.find("self.viewer.show()")
    assert 0 <= fade < show, "restore_content_fade 必须先于 show()"
