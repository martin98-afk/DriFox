# -*- coding: utf-8 -*-
"""[render-watchdog] 渲染落地看门狗回归测试。

覆盖用户 2026-11-13 报告的「偶发个别卡片永久空白，重新加载会话才恢复」：
链路里多条静默丢渲染分支命中后无人发现，这里补的看门狗要能
1. 判出「有内容但两个容器都空」并复位全部已渲染基线后强制全量重渲
2. 有子节点（图片/图表块）或有文本时不误判
3. 一级无效时升级重载骨架，三级无效才请求重建 viewer
4. 欢迎卡（light 骨架）与空渲染产物不参与自愈

无 WebEngine 依赖（__new__ 桩 + 实例属性遮蔽，与 test_height_storm_fix 同法）。
"""
import pytest

from app.widgets.card_viewers import CodeWebViewer


class TestRenderWatchdog:
    def _make_stub(self):
        w = CodeWebViewer.__new__(CodeWebViewer)
        w._light_skeleton = False
        w._live_watch_attempts = 0
        w._live_probe_gen = 3
        w._last_render_output_bytes = 4096
        w._markdown_text = "x" * 4000
        w._lazy_markdown_cb = None
        w._render_deferred = False
        w._needs_full_render = False
        w._stable_md_len = 120
        w._stable_html = "old"
        w._last_rendered_markdown = "x" * 4000
        w._v2_last = object()
        w._render_inflight = False
        w._streaming = False
        w._is_history = True
        w._is_js_ready = True
        w.calls = []
        w.height = lambda: 400  # 遮蔽 QWidget.height（未初始化桩）
        w._schedule_render = lambda immediate=False: w.calls.append(("render", immediate))
        w._load_skeleton = lambda: w.calls.append(("skeleton",))
        w._arm_render_watchdog = lambda restart=False: w.calls.append(("arm", restart))

        class _Sig:
            def __init__(self):
                self.count = 0

            def emit(self, *a):
                self.count += 1

        w.needRecreate = _Sig()
        w._live_probe_timer = type(
            "_T", (), {"stop": lambda self: None, "start": lambda self, ms: None}
        )()
        return w

    @staticmethod
    def _result(gen_stub, payload):
        """直接喂探针回音（gen 必须与桩当前代际一致才生效）。"""
        CodeWebViewer._on_live_probe_result(gen_stub, gen_stub._live_probe_gen, payload)

    def test_blank_doc_forces_full_render_with_baseline_reset(self):
        w = self._make_stub()
        self._result(w, "0|0|0|0|47")
        assert ("render", True) in w.calls
        assert w._needs_full_render is True
        assert w._v2_last is None
        assert w._stable_md_len == 0
        assert w._last_rendered_markdown == ""
        assert w._live_watch_attempts == 1

    @pytest.mark.parametrize("payload", ["1|0|0|0|47", "0|0|42|0|47", "0|1|0|0|47"])
    def test_content_present_does_not_heal(self, payload):
        """子节点或有文本 = 已落地（仅文本为 0 的图片/图表块也不误判）。"""
        w = self._make_stub()
        self._result(w, payload)
        assert w.calls == []
        assert w._live_watch_attempts == 0

    def test_second_attempt_reloads_skeleton(self):
        w = self._make_stub()
        w._live_watch_attempts = 1
        self._result(w, "0|0|0|0|47")
        assert ("skeleton",) in w.calls
        assert ("render", True) not in w.calls
        assert w._live_watch_attempts == 2

    def test_skeleton_reload_reasserts_transparency_before_navigation(self):
        """看门狗重载骨架走 setHtml 导航，透明重申必须在导航之前。

        renderer 被 T31 误杀后 Chromium 合成面回退默认白底；若重申落在
        setHtml 之后，本次导航不生效 → HTML 区白底固化（2026-11-13 截图实证）。
        直接读生产源码顺序，避免测试桩自证。
        """
        import inspect

        src = inspect.getsource(CodeWebViewer._load_skeleton)
        i_ensure = src.find("self.ensure_transparent_composition()")
        i_sethtml = src.find("self.setHtml(")
        assert i_ensure != -1, "_load_skeleton 必须前置透明重申"
        assert i_sethtml != -1
        assert i_ensure < i_sethtml, "透明重申必须先于首个 setHtml 导航"

        w = self._make_stub()
        CodeWebViewer._reload_skeleton_for_heal(w)
        assert ("skeleton",) in w.calls
        assert w._is_js_ready is False

    def test_probe_timeout_heal_reloads_skeleton(self):
        """探针无回音分支同样汇聚到统一前置重申的 _load_skeleton。"""
        w = self._make_stub()
        CodeWebViewer._on_live_probe_timeout(w)
        assert ("skeleton",) in w.calls
        assert w._live_watch_attempts == 1

    def test_third_attempt_requests_recreate(self):
        w = self._make_stub()
        w._live_watch_attempts = 2
        self._result(w, "0|0|0|0|47")
        assert w.needRecreate.count == 1
        assert w._live_watch_attempts == 0

    def test_empty_render_output_skips_heal(self):
        """产物本身 0 字节（插件自定义块渲染为空）不是渲染丢失。"""
        w = self._make_stub()
        w._last_render_output_bytes = 0
        self._result(w, "0|0|0|0|47")
        assert w.calls == []
        assert w._live_watch_attempts == 0

    def test_light_skeleton_not_watched(self):
        """欢迎卡（light 骨架）空内容不算故障。"""
        w = self._make_stub()
        w._light_skeleton = True
        w.isVisible = lambda: True
        CodeWebViewer._check_render_landed(w)
        assert w.calls == []

    def test_stale_probe_generation_ignored(self):
        """池化复用/骨架重载后迟到的探针回音必须作废。"""
        w = self._make_stub()
        CodeWebViewer._on_live_probe_result(w, -1, "0|0|0|0|47")
        assert w.calls == []
