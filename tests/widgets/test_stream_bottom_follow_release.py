# -*- coding: utf-8 -*-
"""流式卡内「已到底却不跟随」回归：跟随标志的复位链。

症状：流式输出时消息卡片内部滚轮已经停在底部，后续内容有时不再自动置底。
根因链（三个独立失效点，均指向同一个标志 `_userScrolledUp` / `_userScrolledWithin`
被误置为上滚阅读后没有复位机会）：

1. **下滚也置位**：scroll 监听原本只看 `_recentUserScrollInput`（任何方向），
   而 wheel 下滚同样刷新时间戳。滚向底部的途中若恰逢内容增长，最后一次
   scroll 事件的 atBottom 判定为 false → 被记成「用户在上方阅读」→ 停止跟随。
2. **内容增长吃掉 atBottom**：流式 chunk 常在 scroll 事件派发前就把容器撑高，
   事件读到的距底恒 > 阈值。即使滚到底的那一刻也被判成「离底」，且用户停止
   滚动后不再有 scroll 事件来纠正。
3. **快照回写与复用残留**：DOM 重建用重建前的 `up` 快照覆盖标志；WebEngine
   池化复用时只清 innerHTML，挂在容器元素上的 expando 跨卡片残留。

修法：下滚输入记录方向并连续数帧检测贴底解封；scroll 置位只认上滚方向；
自动置底前按几何自愈；标志翻转即时上报；复用清理复位 expando。
"""
import inspect

from app.widgets import card_render_core as crc
from app.widgets.card_viewers import CodeWebViewer


def test_down_scroll_intent_released_follow():
    """下滚到底必须能解封跟随（不能只靠 scroll 事件的 atBottom）。"""
    js = crc._CONTENT_AUTOSCROLL_JS
    assert "function _dfxNearBottom" in js
    assert "function _dfxReleaseFollowOnDown" in js
    assert "function _dfxReleaseBodyFollowOnDown" in js
    # 方向记录 + 下滚走解封路径
    assert "el._lastInputDir = 1;" in js
    assert "_dfxReleaseFollowOnDown(el);" in js
    # 连续数帧复查（滚轮应用是异步的，本帧读不到真实落点）
    assert "el._dfxReleaseFrames < 6" in js
    # 判据必须扣除「下滚之后内容增长」的部分，否则被新内容顶开几屏也判不离底
    assert "el._downScrollBaseMax" in js
    assert "el._lastInputDir === 1" in js
    # 解封后必须回传阅读态，外层才知道可以让位
    assert "reportHeightDebounced" in js
    # 下滚停在半途 = 要读那一段：滚动停止后按（扣除内容增长的）距底值补置位
    assert "function _dfxSettleDownScroll" in js
    assert "_dfxSettleDownScroll(el)" in js
    assert "function _dfxSettleBodyDownScroll" in js


def test_scroll_listener_requires_upward_intent():
    """scroll 事件置位上滚标志必须带方向门控（下滚永不置位）。"""
    js = crc._CONTENT_AUTOSCROLL_JS
    assert "else if (cp._lastInputDir === -1 && _recentUserScrollInput(cp))" in js, (
        "正文容器：只有上滚输入才允许置位上滚阅读标志"
    )


def test_autoscroll_self_heals_stale_flag():
    """自动置底前按几何自愈：标志与位置矛盾时以位置为准。"""
    js = crc._CONTENT_AUTOSCROLL_JS
    assert "if (_cp._userScrolledUp && _dfxNearBottom(_cp))" in js
    # 坞态归位路径同样自愈
    assert "_dfxNearBottom(tc)" in crc._STREAMING_DOCK_JS


def test_dom_update_restore_keeps_reached_bottom():
    """DOM 重建恢复快照时，不得把「重建窗口内已滚到底」覆盖回上滚态。"""
    js = crc._CONTENT_AUTOSCROLL_JS
    assert "cp._userScrolledUp = s.cp.up && !_dfxNearBottom(cp);" in js
    assert "tc._userScrolledUp = s.tc.up && !_tsGone && !_dfxNearBottom(tc);" in js


def test_reuse_reset_clears_container_expando():
    """WebEngine 池化复用只清 innerHTML，容器 expando 必须显式复位。"""
    js = crc._RESET_CONTENT_FOR_REUSE_JS
    for flag in ("_userScrolledUp", "_progDepth", "_lastInputAt", "_lastInputDir"):
        assert f"_cpR.{flag}" in js, f"正文容器复用必须复位 {flag}"
        assert f"_tcR0.{flag}" in js, f"工具区容器复用必须复位 {flag}"


def test_skeleton_js_wires_body_direction_and_tool_self_heal():
    """骨架接入：body 方向门控 + 工具区自愈（防止资产改了没接进骨架）。"""
    src = inspect.getsource(CodeWebViewer._load_skeleton)
    assert "_dfxReleaseBodyFollowOnDown" in src
    assert "_b._lastInputDir" in src
    assert "document.body._lastInputDir === -1" in src
    assert "tc._lastInputDir === -1" in src
    assert "_dfxNearBottom(tc)" in src


def test_skeleton_version_bumped():
    """骨架 JS 资产变更必须递增缓存版本，否则复用旧骨架行为不生效。"""
    assert crc._SKELETON_CACHE_VERSION >= 87
