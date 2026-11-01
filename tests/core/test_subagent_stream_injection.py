# -*- coding: utf-8 -*-
"""
回归测试：子智能体结果注入链路（插件所有物）

迁移说明：注入逻辑原为 main_widget._inject_subagent_completion_into_stream
（已删），现归 plugins/system-tools/tools/subagent_tools.py 的
_inject_subagent_result（经 ToolRegistry metadata["subagent_result_injector"]
被发现，主程序只做接线）。

测试目标：
1. 文案构建：任务编号/状态图标/agent 名/执行结果齐全，4096 截断
2. __batch_summary__ 汇总内容原样透传
3. send 调用参数：mode 透传、hook_event="SubAgentFinished"
4. register 的 metadata 注册注入器（主程序发现机制）
"""
import importlib.util
import queue
import sys
from pathlib import Path

# 仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PLUGIN_TOOLS = _REPO_ROOT / "plugins" / "system-tools" / "tools"

def _load_subagent_tools():
    """直载插件模块（幂等：已加载返回缓存，先例 test_team_capability.py）"""
    mod_name = "_subagent_stream_injection_subagent_tools"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_TOOLS / "subagent_tools.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod

class _SendRecorder:
    """session.send 裸 callable 替身：捕获 (text, kwargs) 并记录"""

    def __init__(self, return_value=True):
        self.calls = []
        self.return_value = return_value

    def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return self.return_value

def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            break
    return out

def test_format_hook_output_subagent_finished():
    """验证 _format_hook_output 正确生成 SubAgentFinished 标签"""
    from app.core.conversation.backend import _format_hook_output

    content = "子智能体任务完成: ✅ [TestAgent] 写代码 (id: abc123)"
    result = _format_hook_output("SubAgentFinished", content)

    assert result.startswith("<system-reminder>"), f"应以 <system-reminder> 开头，实际: {result}"
    assert result.endswith("</system-reminder>"), f"应以 </system-reminder> 结尾，实际: {result}"
    assert "<sub-agent-finished-hook>" in result, f"应包含 sub-agent-finished-hook 标签，实际: {result}"
    assert content in result, f"原始内容应出现在结果中，实际: {result}"

def test_inject_single_task_content_and_send_args():
    """单任务：文案齐全 + mode/hook_event 正确传给 send"""
    mod = _load_subagent_tools()
    rec = _SendRecorder()
    ok = mod._inject_subagent_result(
        rec, task_id="task_abc123", result="完成", success=True,
        agent_name="TestAgent", task_description="写代码", mode="hook",
    )
    assert ok is True
    assert len(rec.calls) == 1
    text, kwargs = rec.calls[0]

    assert kwargs["mode"] == "hook"
    assert kwargs["hook_event"] == "SubAgentFinished"
    assert "task_abc123" in text
    assert "✅" in text and "[TestAgent]" in text
    assert "执行结果" in text and "完成" in text

def test_inject_failure_task_icon():
    mod = _load_subagent_tools()
    rec = _SendRecorder()
    mod._inject_subagent_result(
        rec, task_id="task_fail_xyz", result="错误", success=False,
        agent_name="FailAgent", task_description="会失败的任务",
    )
    text, kwargs = rec.calls[0]
    assert "❌" in text
    assert "task_fail_xyz" in text and "[FailAgent]" in text
    assert kwargs["mode"] == "hook"  # 缺省 mode

def test_inject_batch_summary_passthrough():
    """__batch_summary__：汇总内容原样作为 text，不拼任务模板"""
    mod = _load_subagent_tools()
    rec = _SendRecorder()
    summary = "已完成 2 个子任务，1 个失败。本次完成的任务：..."
    mod._inject_subagent_result(
        rec, task_id="__batch_summary__", result=summary, success=False,
        agent_name="", task_description="",
    )
    text, _kwargs = rec.calls[0]
    assert text == summary, f"汇总应原样透传，实际: {text}"

def test_inject_truncates_long_result():
    """结果超 4096 字符：截断 + 省略标记，不含尾部内容"""
    mod = _load_subagent_tools()
    rec = _SendRecorder()
    long_result = "B" * 6000
    mod._inject_subagent_result(
        rec, task_id="task_long", result=long_result, success=True,
        agent_name="A", task_description="d",
    )
    text, _kwargs = rec.calls[0]
    assert "已省略" in text
    assert "B" * 5000 not in text, "截断后不应包含完整超长尾部"
    assert "B" * 100 in text  # 前缀保留

def test_register_metadata_exposes_injector():
    """register 时 metadata 带注入器 callable（主程序发现机制）"""

    class _Registry:
        def __init__(self):
            self.registered = []

        def register(self, name, schema, **meta):
            self.registered.append((name, meta))

    mod = _load_subagent_tools()
    reg = _Registry()
    mod.register(reg)
    by_name = dict(reg.registered)
    meta = by_name["subagent_para"]["metadata"]
    injector = meta.get("subagent_result_injector")
    assert callable(injector), f"metadata 应注册注入器，实际: {meta}"
    assert injector is mod._inject_subagent_result
