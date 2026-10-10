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

_SOURCES = [
    Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_render_core.py",
    Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_viewers.py",
    Path(__file__).resolve().parents[2] / "app" / "widgets" / "message_card.py",
]


@pytest.fixture(scope="module")
def src_text() -> str:
    # [T22] 符号已跨文件分布：JS 资产与注入点在 card_render_core / card_viewers，
    # 卡方法（finish_streaming/_update_height 等）在 message_card / card_viewers。
    # 拼接供 src_text.find 系断言统一检索。
    return "\n".join(p.read_text(encoding="utf-8") for p in _SOURCES)


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
        # updateContent 走 _twFlush（方案C：未揭示缓冲先上屏再替换，零丢字），
        # 其余入口仍为 _twReset——两者皆为「替换前复位队列」的合法形态
        assert "window._twReset" in body or "window._twFlush" in body, f"{fn} 必须在替换 DOM 前复位打字机队列"


class TestFinishTickCost:
    """结束这一拍的主线程开销（卡顿的隐藏来源）"""

    def test_only_history_render_may_be_async(self, src_text: str):
        """长内容异步只允许给**历史渲染**；流式结束的终渲染必须同步。

        原因：finish_streaming 之后紧随的 _cleanup_render_cache() 会
        `self._render_seq += 1`，异步结果回来即被 _apply_render_result 判为过期
        丢弃 → 终渲染永不落地（卡片停在流式形态、高度不收敛）。曾踩过。
        """
        body = _func_body(src_text, "def _perform_update(self):")
        # [T22] 差量收尾分支（_incremental_finalize）插入后，「以下为流式模式」分隔
        # 注释已不存在；三点核心判据改在整函数体上断言（守卫表达式仍唯一指向
        # 异步提交前的结束态排除）。
        assert "self._sequence_render(" in body, "长历史卡应走线程池（真机 40~120ms/张）"
        # 异步分支必须带着"非结束态"守卫（getattr 形式防御历史实例缺属性）；
        # 守卫调用的换行排版不固定，空白归一后断言
        import re as _re

        assert _re.search(
            r'not\s+getattr\(\s*self,\s*"_final_render_pending",\s*False\s*\)', body
        ), ("异步分支必须排除流式结束的终渲染（_final_render_pending）")
        assert "_cleanup_render_cache" in body, "需保留为何结束态不能异步的说明"
        # finish_streaming 必须打开该守卫
        fin = _func_body(src_text, "def finish_streaming(self, keep_dock: bool = False, immediate: bool = True):")
        assert "_final_render_pending = True" in fin, "finish_streaming 必须标记终渲染为同步"

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
        body = _func_body(src_text, "def finish_streaming(self, history: bool = False, force_dock_off: bool = False, immediate: bool = True):")
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
        start = src_text.find("function _setStreamingDock(active, collapseAfter)")
        assert start != -1
        body = src_text[start : src_text.find("\n                function ", start)]
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
        fin = _func_body(src_text, "def finish_streaming(self, keep_dock: bool = False, immediate: bool = True):")
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


class TestDiffScopeQuery:
    """[T28/P0-1] 差量渲染 roots 作用域查询接线（消除 O(n²) 累积）"""

    def test_nodes_since_used_by_both_diff_entries(self, src_text: str):
        """两个差量入口都必须经 _nodesSince 取新增区间"""
        assert "window._nodesSince(container, _from)" in src_text, "updateContentAppend 须取 roots"
        assert "var roots = [tailDiv];" in src_text, "updateTailHtml 的 roots 恰为 tailDiv"

    def test_from_baseline_precedes_incremental_removal(self, src_text: str):
        """_from 基线必须先于旧增量节点删除（否则 roots 区间污染）"""
        s = src_text.find("function updateContentAppend(newHtml, tailHtml)")
        e = src_text.find("function finalizeStreamingBlocks", s)
        body = src_text[s:e]
        from_at = body.find("var _from = container.children.length;")
        del_at = body.find('[data-incremental="true"]')
        roots_at = body.find("var roots = window._nodesSince(container, _from);")
        assert -1 not in (from_at, del_at, roots_at)
        assert from_at < roots_at < del_at, "时序须为 _from → 取 roots → 删旧增量节点"

    def test_scope_query_in_five_functions(self, src_text: str):
        """五个后处理函数必须支持 roots 作用域（_scopeQuery 退化全文档兼容全量路径）"""
        for header in (
            "window._initEchartsIn = function (roots)",
            "window.renderWidgetToolbars = function(roots)",
            "window._runFenceAssets = function (roots)",
            "window._initWidgets = function (roots)",
            "function _scanFenceLangs(roots)",
        ):
            assert header in src_text, f"缺少 roots 形参: {header}"
            s = src_text.find(header)
            seg = src_text[s : s + 2000]
            # [T28] _runFenceAssets 委托 _scanFenceLangs(roots)；renderWidgetToolbars
            # 无参退化走顶层集合；其余直接 _scopeQuery(roots)
            probe = {
                "window._runFenceAssets": "_scanFenceLangs(roots)",
                "window.renderWidgetToolbars": "roots = _cp.children",
            }.get(header.split(" = ")[0], "_scopeQuery(roots")
            assert probe in seg, f"{header} 体内应走 {probe}"

    def test_wrap_tables_in_roots_both_entries(self, src_text: str):
        """两入口的表格包裹必须走 _wrapTablesIn(roots)（原 inline 全文循环移除）"""
        s = src_text.find("function updateContentAppend(newHtml, tailHtml)")
        e = src_text.find("function finalizeStreamingBlocks", s)
        append_body = src_text[s:e]
        s2 = src_text.find("function updateTailHtml(html)")
        e2 = src_text.find("_CONTENT_AUTOSCROLL_JS", s2)
        tail_body = src_text[s2:e2]
        assert "window._wrapTablesIn(roots);" in append_body
        assert "window._wrapTablesIn(roots);" in tail_body
        assert "table:not(.code-table):not(.layout-table)').forEach" not in append_body, (
            "差量入口不得保留 inline 全文表格循环"
        )

    def test_skeleton_version_bumped_for_roots(self, src_text: str):
        """骨架 JS 结构变更必须 bump 版本（roots 接线 = v41）"""
        import re as _re

        m = _re.search(r"^_SKELETON_CACHE_VERSION = (\d+)", src_text, _re.M)
        assert m is not None, "找不到 _SKELETON_CACHE_VERSION 定义"
        version = int(m.group(1))
        assert version >= 41, f"roots 接线后 _SKELETON_CACHE_VERSION 必须 >=41，实际 {version}"


class TestHeightTickAlignment:
    """[T34] 高度回环节拍与打字机 rAF 同频，且防抖不得另立一拍"""

    def test_tick_matches_frame_budget(self):
        """追踪 tick 必须落在单帧量级（<=20ms），与打字机 rAF(~17ms) 同频。

        文字按 rAF 每 ~17ms 揭示一次，容器高度若按 40ms 一格逼近，就会出现
        「文字连续长、容器跳格」的台阶感 —— 这是 [T34] 修复的体感来源。
        """
        from pathlib import Path

        widgets_dir = Path(__file__).resolve().parents[2] / "app" / "widgets"
        core_text = (widgets_dir / "card_render_core.py").read_text(encoding="utf-8")
        card_text = (widgets_dir / "message_card.py").read_text(encoding="utf-8")
        s = core_text.find("STREAM_HEIGHT_TICK_MS = ")
        assert s != -1
        tick = int(core_text[s : core_text.find("\n", s)].split("=")[1].strip())
        assert tick <= 20, f"STREAM_HEIGHT_TICK_MS 应 <=20ms（单帧量级），实际 {tick}"
        # 防抖不得硬编码成另一拍：会把它喂给追踪的目标值切成阶梯
        assert (
            "self._stream_height_timer.setInterval(STREAM_HEIGHT_TICK_MS)" in card_text
        ), "防抖须复用 STREAM_HEIGHT_TICK_MS，不得另立硬编码节拍"
        assert "setInterval(32)" not in card_text, "旧 32ms 防抖应已移除"
        assert "setInterval(40)" not in card_text, "旧 40ms 防抖应已移除"

    def test_min_delta_no_worse_than_epsilon(self):
        """snap 阈值不得高于落定阈值，否则大部分高度变化会被硬跳。

        实测流式期高度变化幅度 p50=4px；阈值 8px 时它们全被 snap 成硬跳。
        """
        from app.widgets import card_render_core as core

        assert core.STREAM_HEIGHT_ANIM_MIN_DELTA <= core.STREAM_HEIGHT_TRACK_EPSILON + 1, (
            f"MIN_DELTA({core.STREAM_HEIGHT_ANIM_MIN_DELTA}) 高于 "
            f"EPSILON({core.STREAM_HEIGHT_TRACK_EPSILON}) 会让小步增长全部退化成硬跳"
        )
