# -*- coding: utf-8 -*-
"""回归测试：流式观感（帧级打字机 + 结束态 FLIP 遮盖）的接线完整性

背景（用户反馈）
----------------
1. 流式输出没有打字机效果：正文按**网络 chunk**整块塞进 DOM（上游 80ms 批处理，
   中文长段落还常因无 ``\\n\\n`` 闭合段落到 150~500ms 安全定时器），观感是
   "一块一块蹦出来"。
2. 流式结束时"最后的工具与思考弹到最顶上"且卡顿：简洁模式下流式期间工具区沉底
   （``body.streaming-dock`` 纯 CSS order 调换），结束时**归位 + 最终全量重排 +
   自动折叠**三个动作叠加，各自带 200ms 过渡 → 瞬移 + 三动画抢帧。

方案（apps/widgets/message_card.py 骨架资产）
--------------------------------------------
* ``_TYPEWRITER_JS``：rAF 揭示队列。Python 只 push 文本，JS 按帧揭示并把积压在
  ``CATCHUP_MS`` 内排空；与渲染路径的交接点是 ``window._twReset()``
  （updateContent / updateTailHtml / updateContentAppend 会整体替换增量节点，
  且 Python 侧 markdown 已含全部文本，丢弃缓冲不会丢字）。
* ``_FLIP_JS``：``_flipCapture`` / ``_flipPlay`` 把位置突变补间成位移动画，
  ``_animEnqueue`` 把「归位 → 重排 → 折叠」串成一条时间线。

本测试是**源码级接线校验**（不启动 Chromium）：断言这些调用点真实存在于
生成的 JS 中。JS 资产本身的语法由 ``node --check`` 在改动时人工校验。

运行：
    python -m pytest tests/widgets/test_streaming_smoothness_wiring.py -v
"""

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "app" / "widgets" / "message_card.py"


@pytest.fixture(scope="module")
def src_text() -> str:
    return _SRC.read_text(encoding="utf-8")


def _func_body(src: str, header: str) -> str:
    """截取一个 def 的函数体（到下一个顶层/方法 def 为止）。"""
    start = src.find(header)
    assert start != -1, f"未找到定义：{header}"
    end = src.find("\n    def ", start + 1)
    return src[start : end if end != -1 else len(src)]


class TestTypewriterQueue:
    """打字机揭示队列的接线"""

    def test_assets_declared_and_injected(self, src_text: str):
        """_TYPEWRITER_JS / _FLIP_JS 必须定义并注入骨架（否则 JS 里根本没有队列）"""
        assert "_TYPEWRITER_JS = \"\"\"" in src_text
        assert "_FLIP_JS = \"\"\"" in src_text
        # 注入点：骨架 f-string 内（与 _STREAMING_DOCK_JS 同处）
        assert "{_TYPEWRITER_JS}" in src_text
        assert "{_FLIP_JS}" in src_text

    def test_skeleton_version_bumped(self, src_text: str):
        """骨架 JS 结构变了，缓存版本号必须递增（否则旧骨架缓存不失效 → 静默无效果）"""
        start = src_text.find("_SKELETON_CACHE_VERSION = ")
        assert start != -1
        line = src_text[start : src_text.find("\n", start)]
        version = int(line.split("=")[1].strip())
        assert version >= 27, "新增骨架 JS 后必须递增 _SKELETON_CACHE_VERSION（>=27）"

    def test_queue_api_present(self, src_text: str):
        """队列需具备 push / step / flush / reset 四个入口"""
        for fn in ("window._twPush", "window._twStep", "window._twFlush", "window._twReset"):
            assert fn in src_text, f"缺少打字机队列入口 {fn}"

    def test_incremental_append_goes_through_queue(self, src_text: str):
        """_append_text_incremental 必须把文本交给队列，而非直接整块追加"""
        body = _func_body(src_text, "def _append_text_incremental(self, text: str):")
        assert "window._dfxAppendStreamText" in body, "追加逻辑必须注册为可复用函数供队列按帧调用"
        assert "window._twPush" in body, "流式文本必须推入打字机揭示队列（否则仍是整块蹦字）"

    def test_reveal_does_not_explode_text_nodes(self, src_text: str):
        """帧级揭示必须合并文本节点（否则 2000 字回复产出 600+ Text 节点 → 内存膨胀）"""
        body = _func_body(src_text, "def _append_text_incremental(self, text: str):")
        assert "_appendTextMerged" in body, "追加文本必须走合并函数（appendData），不能每次新建 Text 节点"

    def test_reveal_throttles_height_report(self, src_text: str):
        """揭示每帧调用，高度上报必须节流（否则 reportHeight→setFixedHeight 回环频率翻数倍）"""
        start = src_text.find("_TYPEWRITER_JS = \"\"\"")
        assert start != -1
        body = src_text[start : src_text.find("\n\"\"\"", start)]
        assert "skipReport" in body, "揭示队列需给 _dfxAppendStreamText 传 skipReport 节流高度上报"

    @pytest.mark.parametrize(
        "fn",
        ["function updateContent(newHtml)", "function updateTailHtml(html)", "function updateContentAppend(newHtml, tailHtml)"],
    )
    def test_render_entry_resets_queue(self, src_text: str, fn: str):
        """整体替换增量节点的渲染入口必须复位队列，防止未揭示文本被重复追加"""
        start = src_text.find(fn)
        assert start != -1, f"未找到 {fn}"
        body = src_text[start : start + 1500]
        assert "window._twReset" in body, f"{fn} 必须在替换 DOM 前调用 window._twReset()"


class TestFinishTickCost:
    """结束这一拍的主线程开销（卡顿的隐藏来源）"""

    def test_final_render_must_not_be_async(self, src_text: str):
        """最终渲染**不能**丢给线程池：finish_streaming 之后紧随的
        _cleanup_render_cache() 会 `self._render_seq += 1`，异步结果回来时已被判
        过期丢弃 → 最终渲染永不落地（卡片停在流式形态、高度不收敛）。

        曾按"长内容走 _sequence_render"优化并踩了这个坑，此处锁定回退。
        """
        body = _func_body(src_text, "def _perform_update(self):")
        non_streaming = body.split("if not self._streaming:", 1)[1].split("以下为流式模式", 1)[0]
        assert "self._sequence_render(" not in non_streaming, (
            "最终渲染不能异步（会被 _cleanup_render_cache 的 seq 递增判为过期）"
        )
        assert "_cleanup_render_cache" in non_streaming or "_cleanup_render_cache" in body, "需保留为何不能异步的说明"

    def test_finish_timing_probe_available(self, src_text: str):
        """结束态耗时打点：render/dumps 慢时默认就打，js_land+layout 也要能测"""
        assert 'os.environ.get("DRIFOX_FINISH_TIMING", "0")' in src_text, "需要全量打点开关"
        assert "[finish-render]" in src_text
        # 主线程两段
        assert "render={_render_ms" in src_text and "dumps={_ser_ms" in src_text
        # WebEngine 侧（innerHTML 解析 + 重排）靠"派发 → 首个 reportHeight"度量
        assert "js_land+layout=" in src_text
        # 两条渲染路径（裸 updateContent / save+restore）都要覆盖
        assert "path=bare" in src_text and "path=save_restore" in src_text

    def test_stop_anim_no_forced_repaint(self, src_text: str):
        """stop_streaming_anim 不应 repaint()（同步强制重绘会把整卡 paint 挤进结束这一拍）"""
        body = _func_body(src_text, "def stop_streaming_anim(self):")
        assert "self.repaint()" not in body, "结束态不应同步强制重绘整卡"
        assert "self.update()" in body

    def test_card_style_apply_is_idempotent(self, src_text: str):
        """_apply_card_style 幂等：setStyleSheet 会触发 style polish + 子控件 relayout"""
        body = _func_body(src_text, "def _apply_card_style(self, border: str = None, bg: str = None):")
        assert "_applied_card_style_key" in body


class TestFinishHeightTransition:
    """Qt 侧卡片高度：结束态缓动（默认开、可一键关）"""

    def test_finish_window_opened_only_for_streaming_end(self, src_text: str):
        """只有流式结束打开窗口；history 加载是首帧建卡，不需要过渡"""
        body = _func_body(src_text, "def finish_streaming(self, history: bool = False):")
        assert "_finish_height_anim_until = time.monotonic() + FINISH_HEIGHT_ANIM_WINDOW_S" in body
        assert "_finish_height_anim_left = FINISH_HEIGHT_ANIM_MAX_USES" in body
        assert "if not history:" in body

    def test_update_height_uses_anim_in_window(self, src_text: str):
        """_update_height 在窗口内且变化够大时走缓动，而不是 snap"""
        body = _func_body(src_text, "def _update_height(self, h):")
        assert "_in_finish_height_window()" in body
        assert "FINISH_HEIGHT_ANIM_MIN_DELTA" in body
        assert "_start_finish_height_anim(" in body

    def test_anim_loop_guard(self, src_text: str):
        """动画期间必须挡掉 reportHeight 回环送来的中间高度，否则补间被打断成锯齿"""
        body = _func_body(src_text, "def _update_height(self, h):")
        assert "self._is_height_animating" in body
        assert "self._height_anim.endValue()" in body

    def test_kill_switch_exists(self, src_text: str):
        """必须能一键关：环境变量 + 运行时 setter"""
        assert 'os.environ.get("DRIFOX_FINISH_HEIGHT_ANIM", "1")' in src_text
        assert "def set_finish_height_anim_enabled(" in src_text
        anim = _func_body(src_text, "def _start_finish_height_anim(self, start_h: int, end_h: int) -> None:")
        # 动画不可用时必须退回原有 snap 行为
        assert "noContainerAnimation" in anim


class TestFlipTransitions:
    """结束态 FLIP 遮盖与动画串行"""

    def test_dock_toggle_wrapped_by_flip(self, src_text: str):
        """_setStreamingDock 的 order 换位是瞬移，必须用 FLIP 补间"""
        start = src_text.find("function _setStreamingDock(active)")
        assert start != -1
        body = src_text[start : src_text.find("\n}", start)]
        assert "_flipCapture" in body, "坞态切换前必须记录位置"
        assert "_flipPlay" in body, "坞态切换后必须播放位移动画"

    def test_update_content_wrapped_by_flip(self, src_text: str):
        """最终全量重排（reorganizeContent 搬移工具/思考块）同样需要 FLIP"""
        start = src_text.find("function updateContent(newHtml)")
        assert start != -1
        # 函数体范围：到下一个同级 function 定义为止（骨架 JS 缩进 16 空格）
        end = src_text.find("\n                function ", start + 1)
        body = src_text[start : end if end != -1 else start + 30000]
        assert "_flipCapture" in body and "_flipPlay" in body
        # Play 必须在 capture 之后（重排完成后补间），顺序颠倒则动画无意义
        assert body.index("_flipCapture") < body.index("_flipPlay")

    def test_capture_is_gated_by_arm(self, src_text: str):
        """capture 会强制同步布局，流式期间每次全量渲染都做代价过高 → 必须 arm 才采集"""
        start = src_text.find("_FLIP_JS = \"\"\"")
        assert start != -1
        body = src_text[start : src_text.find("\n\"\"\"", start)]
        assert "window._flipArm" in body, "缺少 arm 入口"
        assert "_flipArmedUntil > performance.now()" in body, "未 arm 时 _flipCapture 必须直接返回 null"
        # 结束态重排前 Python 必须 arm（否则 updateContent 的 capture 永远拿不到位置）
        fin = _func_body(src_text, "def finish_streaming(self, keep_dock: bool = False):")
        assert "_flipArm" in fin, "finish_streaming 必须在最终渲染前 arm FLIP"

    def test_think_block_carries_positional_flip_key(self, src_text: str):
        """思考块流式态/完成态 DOM 结构与 block-key 都不同，必须有位置键才能 FLIP 配对"""
        start = src_text.find("def _render_think_block(")
        assert start != -1
        body = src_text[start : src_text.find("\ndef ", start + 1)]
        assert "data-flip-key" in body, "思考块需要 data-flip-key（按消息内序号）供 FLIP 跨形态配对"
        assert body.count("{_flip_attr}") >= 3, "三种形态（compact / block / streaming）都要带上位置键"
        inj = src_text[src_text.find("def _inject_think_cards(") : src_text.find("def _render_tag_block(")]
        assert "flip_idx=think_ordinal" in inj, "_inject_think_cards 必须传入思考块序号"

    def test_anim_queue_serializes(self, src_text: str):
        """动画串行队列存在，且折叠走队列（不与归位/重排同时开跑）"""
        assert "window._animEnqueue" in src_text
        assert "window._animNext" in src_text
        body = _func_body(src_text, "def _auto_collapse_tool_section(self):")
        assert "window._animEnqueue" in body, "自动折叠必须入队，排在归位/重排之后"
