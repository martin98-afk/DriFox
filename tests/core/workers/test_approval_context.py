# -*- coding: utf-8 -*-
"""_build_approval_context：AI 审核上下文的构造与降级

覆盖：
1. 用户目标取自最后一条 user 消息（含多模态 content 块形态）
2. 本轮轨迹取 _current_tool_calls，排除当前 tool_call_id（并行路径下它含自身）
3. 超长用户消息 / 参数按上限截断
4. 用户目标与轨迹都取不到时返回 None（prompt 退回无上下文形态）
5. 内部数据结构异常时不抛（上下文是增强项，不得阻断审批链）
"""

from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _make_worker(session_messages=None, tool_calls=None):
    """构造 worker 并预置上下文源（不启动线程）"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config={"模型名称": "gpt-4", "API_KEY": "k", "API_URL": "https://x/v1", "温度": 0},
    )
    w._current_session_messages = list(session_messages or [])
    w._current_tool_calls = dict(tool_calls or {})
    return w


def test_context_takes_last_user_goal():
    w = _make_worker(
        session_messages=[
            {"role": "user", "content": "旧目标"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "清理构建产物后重新打包"},
        ]
    )
    ctx = w._build_approval_context("Bash", "call_1")
    assert ctx is not None
    assert ctx["user_goal"] == "清理构建产物后重新打包"


def test_context_handles_multimodal_user_content():
    """用户消息为 content 块列表时取文本块（图片块跳过）"""
    w = _make_worker(
        session_messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看截图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
                ],
            }
        ]
    )
    ctx = w._build_approval_context("Bash", "call_1")
    assert ctx["user_goal"] == "看截图"


def test_context_excludes_current_tool_call():
    """当前待审工具必须排除，否则轨迹里会出现「正在审的自己」（纯噪声）"""
    w = _make_worker(
        session_messages=[{"role": "user", "content": "打包"}],
        tool_calls={
            "call_done": {"id": "call_done", "function": {"name": "Bash", "arguments": '{"command":"npm run build"}'}},
            "call_now": {"id": "call_now", "function": {"name": "Bash", "arguments": '{"command":"rm -rf ./dist"}'}},
        },
    )
    ctx = w._build_approval_context("Bash", "call_now")
    tools = [s["tool"] for s in ctx["round_steps"]]
    assert tools == ["Bash"]
    assert "rm -rf ./dist" not in ctx["round_steps"][0]["args"]
    assert "npm run build" in ctx["round_steps"][0]["args"]


def test_context_no_round_steps_omits_key():
    """只有用户目标、无轨迹时不出现 round_steps 键"""
    w = _make_worker(session_messages=[{"role": "user", "content": "看看结构"}])
    ctx = w._build_approval_context("Bash", "call_1")
    assert ctx == {"current_tool": "Bash", "user_goal": "看看结构"}


def test_context_none_when_no_goal_and_no_steps():
    """两项都取不到 → None（宿主按无上下文处理，prompt 形态不变）"""
    w = _make_worker(session_messages=[{"role": "assistant", "content": "无用户消息"}])
    assert w._build_approval_context("Bash", "call_1") is None


def test_context_truncates_long_user_goal():
    w = _make_worker(session_messages=[{"role": "user", "content": "甲" * 5000}])
    ctx = w._build_approval_context("Bash", "call_1")
    assert len(ctx["user_goal"]) == 1000


def test_context_truncates_long_arguments():
    w = _make_worker(
        session_messages=[{"role": "user", "content": "g"}],
        tool_calls={
            "c1": {"id": "c1", "function": {"name": "write", "arguments": '{"content":"' + "乙" * 5000 + '"}'}}
        },
    )
    ctx = w._build_approval_context("write", "call_1")
    assert len(ctx["round_steps"][0]["args"]) == 201  # 200 + 省略号
    assert ctx["round_steps"][0]["args"].endswith("…")


def test_context_accepts_dict_arguments():
    """arguments 为 dict（非 JSON 字符串）时序列化，不抛异常"""
    w = _make_worker(
        session_messages=[{"role": "user", "content": "g"}],
        tool_calls={"c1": {"id": "c1", "function": {"name": "read", "arguments": {"path": "a.py"}}}},
    )
    ctx = w._build_approval_context("read", "call_1")
    assert "a.py" in ctx["round_steps"][0]["args"]


def test_context_survives_malformed_messages():
    """消息列表含非 dict / 缺字段条目时安全降级"""
    w = _make_worker(
        session_messages=["坏条目", {"role": "user"}, {"content": "无 role"}, {"role": "user", "content": "好"}]
    )
    ctx = w._build_approval_context("Bash", "call_1")
    assert ctx is not None
    assert ctx["user_goal"] == "好"
