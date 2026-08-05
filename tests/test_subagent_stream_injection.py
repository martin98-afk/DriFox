# -*- coding: utf-8 -*-
"""
回归测试：子智能体流式注入链路

测试目标：
1. _inject_subagent_completion_into_stream 把消息正确放入 backend._hook_message_queue
2. 消息格式包含 <system-reminder><sub-agent-finished-hook>...</sub-agent-finished-hook></system-reminder>
3. _format_hook_output("SubAgentFinished", content) 被正确调用
4. 不重复 emit _hook_messages_updated（由 backend.py:380 的 on_hook_finished 处理）
"""
import queue
import sys
from pathlib import Path
from unittest.mock import MagicMock

# 仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.backend import _format_hook_output


class TestSubAgentStreamInjection:
    """子智能体流式注入链路测试"""

    # =====================================================================
    # 辅助：构造一个极简 mock MainWidget，足够调用 _inject_subagent_completion_into_stream
    # =====================================================================
    @staticmethod
    def _make_mock_widget(hook_queue: queue.Queue) -> MagicMock:
        """返回一个只有 backend._hook_message_queue 的 mock MainWidget"""
        mock_backend = MagicMock()
        mock_backend._hook_message_queue = hook_queue
        # 不配置 _hook_messages_updated，这样如果代码误触 emit 会 AttributeError

        mock_widget = MagicMock()
        mock_widget.backend = mock_backend
        return mock_widget

    # =====================================================================
    # 直接测试 _format_hook_output（独立函数，无 Qt 依赖）
    # =====================================================================
    def test_format_hook_output_subagent_finished(self):
        """验证 _format_hook_output 正确生成 SubAgentFinished 标签"""
        content = "子智能体任务完成: ✅ [TestAgent] 写代码 (id: abc123)"
        result = _format_hook_output("SubAgentFinished", content)

        # 外层必须是 system-reminder
        assert result.startswith(
            "<system-reminder>"
        ), f"应以 <system-reminder> 开头，实际: {result}"
        assert result.endswith(
            "</system-reminder>"
        ), f"应以 </system-reminder> 结尾，实际: {result}"

        # 内层必须是 sub-agent-finished-hook（kebab-case: SubAgentFinished → sub-agent-finished）
        assert (
            "<sub-agent-finished-hook>" in result
        ), f"应包含 <sub-agent-finished-hook>，实际: {result}"
        assert (
            "</sub-agent-finished-hook>" in result
        ), f"应包含 </sub-agent-finished-hook>，实际: {result}"

        # 内容必须原样保留
        assert content in result, f"原始内容应出现在结果中，实际: {result}"

    # =====================================================================
    # 通过模块级 __getattr__ 动态派发，测试完整注入链路
    # =====================================================================
    def test_inject_message_into_queue(self):
        """验证消息被正确放入 backend._hook_message_queue"""
        q = queue.Queue()
        mock_widget = self._make_mock_widget(q)

        # _inject_subagent_completion_into_stream 是 OpenAIChatToolWindow 的方法
        import app.main_widget as mw
        inject_fn = mw.OpenAIChatToolWindow._inject_subagent_completion_into_stream

        inject_fn(
            mock_widget,
            task_id="task_abc123",
            result="完成",
            success=True,
            agent_name="TestAgent",
            task_description="写代码",
        )

        # 验证消息进入队列
        assert not q.empty(), "队列不应为空"
        msg = q.get_nowait()

        assert msg["role"] == "system", f"role 应为 system，实际: {msg['role']}"
        assert "_hook_event" in msg, "消息应包含 _hook_event"
        assert (
            msg["_hook_event"] == "SubAgentFinished"
        ), f"_hook_event 应为 SubAgentFinished，实际: {msg['_hook_event']}"

        # 验证 hook 内容格式
        hook_content = msg["content"]
        assert "<system-reminder>" in hook_content
        assert "<sub-agent-finished-hook>" in hook_content
        assert "</sub-agent-finished-hook>" in hook_content
        assert "</system-reminder>" in hook_content

        # 验证任务信息包含在内容中
        assert (
            "task_abc123" in hook_content
        ), f"task_id 应出现在 hook 内容中，实际: {hook_content}"
        assert "✅" in hook_content, f"成功状态图标 ✅ 应出现，实际: {hook_content}"
        assert (
            "[TestAgent]" in hook_content
        ), f"agent_name 应出现，实际: {hook_content}"

    def test_inject_failure_task(self):
        """验证失败任务也正确注入"""
        q = queue.Queue()
        mock_widget = self._make_mock_widget(q)

        import app.main_widget as mw
        inject_fn = mw.OpenAIChatToolWindow._inject_subagent_completion_into_stream

        inject_fn(
            mock_widget,
            task_id="task_fail_xyz",
            result="错误",
            success=False,
            agent_name="FailAgent",
            task_description="会失败的任务",
        )

        assert not q.empty()
        msg = q.get_nowait()

        # 失败时应该是 ❌ 图标
        assert "❌" in msg["content"], f"失败任务应包含 ❌，实际: {msg['content']}"
        assert "task_fail_xyz" in msg["content"]
        assert "[FailAgent]" in msg["content"]

    def test_batch_summary_message(self):
        """验证 __batch_summary__ 任务 ID 走汇总分支"""
        q = queue.Queue()
        mock_widget = self._make_mock_widget(q)

        import app.main_widget as mw
        inject_fn = mw.OpenAIChatToolWindow._inject_subagent_completion_into_stream

        summary_content = "已完成 2 个子任务，1 个失败"
        inject_fn(
            mock_widget,
            task_id="__batch_summary__",
            result=summary_content,
            success=False,
            agent_name="",
            task_description="",
        )

        assert not q.empty()
        msg = q.get_nowait()

        # 汇总内容应原样出现在 hook 中
        assert (
            summary_content in msg["content"]
        ), f"汇总内容应出现，实际: {msg['content']}"
        # 汇总消息不应出现 agent/desc 预览
        assert "[FailAgent]" not in msg["content"]
        assert "✅" not in msg["content"] and "❌" not in msg["content"]

    def test_no_duplicate_emit(self):
        """验证注入方法体内不再调用 backend._hook_messages_updated.emit

        该信号应由 backend.py:380 的 on_hook_finished 回调在 put 后统一发射，
        不应在 _inject_subagent_completion_into_stream 中重复发射。
        """
        q = queue.Queue()
        mock_backend = MagicMock()
        mock_backend._hook_message_queue = q
        # _hook_messages_updated 存在但配置为抛出——若代码调用了 emit 则测试失败
        mock_backend._hook_messages_updated.emit.side_effect = AssertionError(
            "emit 不应在 _inject_subagent_completion_into_stream 中被调用"
        )

        mock_widget = MagicMock()
        mock_widget.backend = mock_backend

        import app.main_widget as mw
        inject_fn = mw.OpenAIChatToolWindow._inject_subagent_completion_into_stream

        # 执行注入——如果内部调用 emit，side_effect 会抛出 AssertionError
        inject_fn(
            mock_widget,
            task_id="task_no_emit",
            result="测试",
            success=True,
            agent_name="EmitTest",
            task_description="验证不重复 emit",
        )

        # 确保 emit 没有被调用
        mock_backend._hook_messages_updated.emit.assert_not_called()

    def test_description_truncation(self):
        """验证超长任务描述被截断到 80 字符"""
        q = queue.Queue()
        mock_widget = self._make_mock_widget(q)

        import app.main_widget as mw
        inject_fn = mw.OpenAIChatToolWindow._inject_subagent_completion_into_stream

        long_desc = "A" * 200  # 200 字符
        inject_fn(
            mock_widget,
            task_id="task_truncate",
            result="完成",
            success=True,
            agent_name="TruncAgent",
            task_description=long_desc,
        )

        assert not q.empty()
        msg = q.get_nowait()

        # 截断后应包含 "..." 和原始 task_id
        assert "..." in msg["content"], f"超长描述应被截断为 ...，实际: {msg['content']}"
        assert "task_truncate" in msg["content"]
        # 不应包含完整 200 个 A
        assert "A" * 100 not in msg["content"]
