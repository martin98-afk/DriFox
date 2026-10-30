# -*- coding: utf-8 -*-
"""回归测试：v2 简洁模式搬移必须按身份复用，禁止每批全删重放。

症状（2026-10-30 用户实测）：v2 渲染 + 简洁模式下，工具完成框与思考卡在流式
输出期间「只要流式一更新就闪烁」，外层滚动条乱跳。

根因：``_v2AfterPatch`` 紧凑模式搬移段原实现 = 每批先把工具区搬入节点全删、
再从正文重放。活动 text 块走 live 纯文本尾态时正文没有协议卡 → 工具区被清空；
下一次正式化整块重渲又把卡搬回 → 每批一删一加，且旧副本（带 data-block-key）
不被清理 → 重复累积。探针
``tests/debug/_probe_v2_tool_flicker.py`` 实测：旧实现每批工具区 +2 节点
（think + tool），scrollHeight 70 → 463；修复后 n 恒定 2、added/removed 全 0。

修复：同身份（data-tool-call-id / data-flip-key）节点保留原地，正文重渲出的
新副本丢弃；删除语义延后到非流式终态。骨架行为变化 → 必须递增
``_SKELETON_CACHE_VERSION``。
"""

import re
from pathlib import Path

from app.widgets import card_viewers
from app.widgets.card_render_core import _SKELETON_CACHE_VERSION


def _after_patch_block() -> str:
    """截取 _v2AfterPatch 定义体（到下一个顶层 window 赋值或骨架串尾）"""
    src = Path(card_viewers.__file__).read_text(encoding="utf-8")
    start = src.find("window._v2AfterPatch = function")
    assert start >= 0, "未找到 _v2AfterPatch 定义"
    tail = src[start : start + 20000]
    # 骨架串在同一 f-string 内，按下一个 window.* 赋值切段更稳
    m = re.search(r"window\._v2AfterPatch = function \(streaming\) \{\{.*?\n                \}\};", tail, re.S)
    assert m, "未找到 _v2AfterPatch 函数体边界"
    return m.group(0)


def test_after_patch_moves_by_identity():
    """搬移必须按身份去重（_mvKey + data-moved），不再全删重放"""
    body = _after_patch_block()
    assert "_mvKey" in body, "搬移段应使用身份键 _mvKey（防止每批重建）"
    assert "data-moved" in body, "搬入节点应标记 data-moved（终态清理依据）"
    assert "staleSel" not in body, (
        "禁止恢复「每批先全删搬入节点再从正文重放」的旧实现："
        "活动块 live 尾态期间正文无协议卡 → 工具区被清空，正式化后又搬回，"
        "表现为完成框/思考卡每批一删一加（闪烁 + 高度抖动）"
    )


def test_after_patch_dedupes_repeated_copy():
    """同身份重复副本必须丢弃（正文重渲每批都会产出新副本）"""
    body = _after_patch_block()
    assert "existing[sk]" in body or "existing[sk] = " in body, "应按身份索引复用已有节点"
    assert "removeChild(sEl)" in body, "正文侧同身份新副本应直接丢弃，不能再 append 造成累积"


def test_after_patch_cleanup_only_when_not_streaming():
    """删除语义只在非流式终态执行（流式中正文集合不完整，不可作为删除依据）"""
    body = _after_patch_block()
    assert "window._streamingActive !== true" in body, "搬入节点清理必须以非流式终态为条件"


def test_same_identity_updates_in_place():
    """同身份必须原地更新内容，禁止整块替换（折叠态 / 展开 body 会随之丢失）

    2026-10-30 实测：首版修复用 replaceChild 换思考块 → 思考卡每批被销毁重建，
    用户展开后看思考/工具内容时每批闪一次。改为「保壳换瓤」：节点身份不动，
    think-compact 原地换文本，折叠框换 innerHTML 后回填展开态与已加载 body。
    """
    body = _after_patch_block()
    assert "replaceChild(sEl" not in body, "同身份禁止 replaceChild 整块替换"
    assert "cur.textContent = sEl.textContent" in body or "cur.innerHTML = sEl.innerHTML" in body, (
        "同身份内容变化应原地更新"
    )
    assert "data-expanded" in body, "折叠框更新后必回填展开态"


def test_skeleton_cache_version_bumped_for_move_identity():
    """改 _v2AfterPatch 行为必须递增骨架缓存版本（v66）"""
    assert _SKELETON_CACHE_VERSION >= 66, (
        f"_SKELETON_CACHE_VERSION={_SKELETON_CACHE_VERSION}，搬移改身份复用后必须 ≥66"
    )
