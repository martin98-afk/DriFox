# -*- coding: utf-8 -*-
"""子任务 #13 行为等价回归：prune_tool_result 输出逐字节一致。

断言口径：把「只探测前 4 行」的优化前语义写成独立参考实现，对本文件的
同一组 grep/list/read/通用样式语料，逐字节比对 prune_tool_result 与探测
判定结果；并钉住 split("\n", 4) 的等价陷阱（第 5 行及之后的标记不得
参与探测，第 5 段剩余原文必须被丢弃）。

不依赖外部快照文件，便于后续规模化修复直接复用为门禁用例。
"""
from __future__ import annotations

import hashlib

import pytest

from app.core.context.tool_prune import (
    TOOL_RESULT_HEAD_KEEP,
    TOOL_RESULT_TAIL_KEEP,
    _is_line_indexed_result,
    prune_tool_result,
)

_MARKERS = ("匹配文件: ", "目录: ")

# 改动前（HEAD 21b77723e）逐字节基线：取自 .drifox/tmp/rollback/prune_before.json，
# 值为 (输出长度, 输出 UTF-8 的 sha256 前 16 位)。内嵌以保证测试自包含、换机不丢。
_BEFORE_BASELINE = {
    "grep_head": (182, "13d2ce237b1f767d"),
    "list_head": (181, "a90cfb7b119d6460"),
    "count_in_4": (144, "ca88bdc6c2115324"),
    "marker_after_4": (156, "7230cda8e2774884"),
    "blank_then_marker": (154, "fd19fdc0a77c7609"),
    "read_style": (172, "93688087e53acaa2"),
    "generic": (157, "d6484d455de9b03d"),
    "exact_4_nl": (15, "5f28e9d4a7a2f101"),
    "exact_5_nl": (17, "487d1f81dc9115dd"),
    "under_limit": (27, "737c353420127d9c"),
    "huge_mb": (5114, "70f7fb5cb9f87a79"),
}


def _ref_is_line_indexed(content: str) -> bool:
    """改动前的参考实现：全量 split 后只取前 4 行。"""
    for line in content.split("\n")[:4]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_MARKERS):
            return True
        if "个文件:" in stripped:
            return True
    return False


def _ref_prune(content: str, max_len: int, head_keep: int, tail_keep: int) -> str:
    """改动前的参考实现：与产品函数同源，但探测走参考实现。

    用于验证「优化只发生在探测阶段，分层截断主体输出零变化」。
    """
    if not isinstance(content, str) or not content:
        return content
    if len(content) <= max_len:
        return content
    # 产品侧另有 PRUNE_SKIP_TOOLS / llm_config 分支；本组语料 tool_name 为空
    # 且显式传 max_len，故此处等价。
    from app.core.context.tool_prune import (
        _prune_generic,
        _prune_line_indexed,
        _prune_read_result,
    )

    if _ref_is_line_indexed(content):
        pruned = _prune_line_indexed(content, head_keep, tail_keep)
        if pruned is not None:
            return pruned
    pruned = _prune_read_result(content, head_keep, tail_keep)
    if pruned is not None:
        return pruned
    return _prune_generic(content, head_keep, tail_keep)


def _corpus():
    kw = dict(max_len=200, head_keep=48, tail_keep=24)
    return [
        ("grep_head", "匹配文件: a.py\n" + "\n".join(f"src/a.py:{i}: hit-{i}" for i in range(120)), kw),
        ("list_head", "目录: /proj\n" + "\n".join(f"  file_{i:03d}.py  4.2K" for i in range(120)), kw),
        ("count_in_4", "结果\n共 120 个文件:\n" + "\n".join(f"f{i}.txt" for i in range(120)), kw),
        ("count_at_4th", "a\nb\nc\n共 9 个文件:\n" + "\n".join(f"f{i}" for i in range(120)), kw),
        ("marker_after_4", "\n".join(["head", "x", "y", "z", "匹配文件: late.py"] + [f"l{i}" for i in range(120)]), kw),
        ("marker_at_5th_as_4th_segment", "a\nb\nc\n匹配文件: ok.py\n" + "\n".join(f"l{i}" for i in range(120)), kw),
        ("blank_then_marker", "\n\n匹配文件: a.py\n" + "\n".join(f"p{i}" for i in range(120)), kw),
        ("all_blank_prefix", "\n\n\n\n匹配文件: too_late.py\n" + "\n".join(f"p{i}" for i in range(120)), kw),
        ("read_style", "#File: big.py (Lines 1-400 of 400)\n" + "\n".join(f"    line_{i} = {i}" for i in range(1, 401)), kw),
        ("generic", "x" * 50 + "\n" + "\n".join(f"word{i} lorem ipsum dolor" for i in range(120)), kw),
        ("no_newline", "匹" * 500, dict(max_len=200, head_keep=48, tail_keep=24)),
        ("exact_3_nl", "匹配文件: a\nb\nc", kw),
        ("exact_4_nl", "匹配文件: a\nb\nc\nd\ne", kw),
        ("exact_5_nl", "匹配文件: a\nb\nc\nd\ne\nf", kw),
        ("under_limit", "目录: /x\nonly two lines\nthird", dict(max_len=99999)),
        ("empty", "", dict(max_len=200)),
        ("huge_mb", "匹配文件: huge\n" + "\n".join("z" * 64 for _ in range(20000)), dict(max_len=8192)),
    ]


@pytest.mark.parametrize("name,content,kw", _corpus(), ids=[c[0] for c in _corpus()])
def test_output_byte_identical(name, content, kw):
    """同组语料：优化后 prune_tool_result 与改动前参考实现逐字节一致。"""
    actual = prune_tool_result(content, **kw)
    expected = _ref_prune(
        content,
        kw["max_len"],
        kw.get("head_keep", TOOL_RESULT_HEAD_KEEP),
        kw.get("tail_keep", TOOL_RESULT_TAIL_KEEP),
    )
    assert actual == expected, f"{name}: 输出逐字节不一致"
    assert _is_line_indexed_result(content) == _ref_is_line_indexed(content), f"{name}: 探测判定不一致"


def test_fifth_segment_is_discarded():
    """split("\\n", 4) 的第 5 段是剩余原文，不得让它参与前 4 行判定。

    第 5 行带 "匹配文件: " 时，改动前后都必须返回 False（走通用截断分支）。
    """
    content = "a\nb\nc\nd\n匹配文件: late.py\n" + "\n".join(f"l{i}" for i in range(120))
    assert _ref_is_line_indexed(content) is False
    assert _is_line_indexed_result(content) is False
    # 且输出仍走通用截断，不含行式条目省略标记
    out = prune_tool_result(content, max_len=200, head_keep=48, tail_keep=24)
    assert "已省略中间第" not in out
    assert out == _ref_prune(content, 200, 48, 24)


@pytest.mark.parametrize(
    "name,expected",
    sorted(_BEFORE_BASELINE.items()),
    ids=[n for n, _ in sorted(_BEFORE_BASELINE.items())],
)
def test_matches_pre_change_snapshot(name, expected):
    """改动后输出与改动前留档快照逐字节一致（长度 + sha256 双校验）。"""
    corpus = {n: (c, kw) for n, c, kw in _corpus()}
    content, kw = corpus[name]
    out = prune_tool_result(content, **kw)
    exp_len, exp_digest = expected
    assert len(out) == exp_len, f"{name}: 输出长度由 {exp_len} 变为 {len(out)}"
    digest = hashlib.sha256(out.encode("utf-8")).hexdigest()[:16]
    assert digest == exp_digest, f"{name}: 输出字节由 {exp_digest} 变为 {digest}"


def test_probe_does_not_build_full_line_list():
    """探测阶段只对前 4 行做切分：数 MB 语料下判定结果与参考实现一致。

    该用例保证优化不改变判定结果（性能收益本身由 Gate/基准负责量化）。
    """
    content = "匹配文件: huge\n" + "\n".join("z" * 64 for _ in range(20000))
    assert len(content) > 1_000_000
    assert _is_line_indexed_result(content) is _ref_is_line_indexed(content) is True
    out = prune_tool_result(content, max_len=8192)
    ref = _ref_prune(content, 8192, 4096, 1024)
    assert out == ref
