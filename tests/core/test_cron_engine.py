# -*- coding: utf-8 -*-
"""Cron Engine 测试套件（已停用）

覆盖范围：
- CronTask 数据模型校验
- CronScheduler 添加/删除/启停/持久化
- CronConversationAdapter 线程同步
- CronEngine 单例、任务管理
- CronTools 工具方法（需要 owner mock）

历史状态（2026-07-18 測試體系整改）：
    本文件覆盖的 ``app.core.engines.cron.*``、``app.tools.cron_tools`` 等
    模块已在某次重構中被刪除（``app/core/engines/cron/`` 現爲空目錄），
    在 CI 中全部 25 個測試因 ``ModuleNotFoundError`` 失敗。

    因 Cron 功能整體已被移除（v0.4.x 系列不再提供定時任務），
    本文件改爲 ``pytest.skip`` 形式留檔，而非刪除，以便：
    1. 文檔化曾經存在的能力與測試設計；
    2. 未來如需重新引入 Cron 功能時可恢復此套件作爲起點。
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 添加项目根目录
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

# 歷史原因：cron 子系統已被移除。整體 skip 全部測試。
# 解除本行可恢復對歷史 cron 模塊的測試。
pytest.skip(
    "Cron 引擎子系統已被移除（v0.4.x），app.core.engines.cron.* 模塊不存在。"
    "如需重新引入，請移除此 skip 並相應實現被測模塊。",
    allow_module_level=True,
)


class TestCronTask:
    def test_default_values(self):
        from app.core.engines.cron.models import CronTask

        t = CronTask()
        assert t.id != ""
        assert len(t.id) == 8
        assert t.enabled is True
        assert t.recurring is True
        assert t.created_at == 0.0

    def test_validate_required_fields(self):
        from app.core.engines.cron.models import CronTask

        t = CronTask(name="", prompt="", cron_expression="")
        errs = t.validate()
        assert "name 不能为空" in errs
        assert "prompt 不能为空" in errs
        assert "cron_expression 不能为空" in errs

    def test_validate_valid(self):
        from app.core.engines.cron.models import CronTask

        t = CronTask(name="t", prompt="p", cron_expression="* * * * *")
        assert t.validate() == []

    def test_describe(self):
        from app.core.engines.cron.models import CronTask

        t = CronTask(name="check", prompt="say hi", cron_expression="*/5 * * * *")
        d = t.describe()
        assert "check" in d
        assert "*/5 * * * *" in d
        assert "重复" in d

    def test_to_from_dict(self):
        from app.core.engines.cron.models import CronTask

        t = CronTask(name="t", prompt="p", cron_expression="* * * * *")
        d = t.to_dict()
        restored = CronTask.from_dict(d)
        assert restored.name == t.name
        assert restored.prompt == t.prompt
        assert restored.cron_expression == t.cron_expression

    def test_id_uniqueness(self):
        from app.core.engines.cron.models import CronTask

        ids = {CronTask().id for _ in range(100)}
        # 大多数应该是唯一的（UUID 8 字符碰撞概率约 1/2^32）
        assert len(ids) >= 99


class TestCronScheduler:
    def setup_method(self):
        self.fired: list = []

    def _on_trigger(self, task):
        self.fired.append(task.id)

    def test_start_stop(self):
        from app.core.engines.cron.scheduler import CronScheduler

        s = CronScheduler(on_trigger=self._on_trigger)
        s.start()
        assert s.is_running()
        s.stop()
        assert not s.is_running()

    def test_add_remove_task(self):
        from app.core.engines.cron.scheduler import CronScheduler
        from app.core.engines.cron.models import CronTask

        s = CronScheduler(on_trigger=self._on_trigger)
        s.start()
        t = CronTask(name="t1", prompt="hi", cron_expression="* * * * *")

        assert s.add_task(t) is True
        assert len(s.list_tasks()) == 1

        assert s.remove_task(t.id) is True
        assert len(s.list_tasks()) == 0

        s.stop()

    def test_invalid_cron_rejected(self):
        from app.core.engines.cron.scheduler import CronScheduler
        from app.core.engines.cron.models import CronTask

        s = CronScheduler(on_trigger=self._on_trigger)
        s.start()

        t = CronTask(name="t", prompt="p", cron_expression="not a cron")
        assert s.add_task(t) is False
        assert len(s.list_tasks()) == 0

        s.stop()

    def test_enable_disable(self):
        from app.core.engines.cron.scheduler import CronScheduler
        from app.core.engines.cron.models import CronTask

        s = CronScheduler(on_trigger=self._on_trigger)
        s.start()
        t = CronTask(name="t", prompt="p", cron_expression="* * * * *")
        s.add_task(t)

        assert s.disable_task(t.id) is True
        assert s.get_task(t.id).enabled is False

        assert s.enable_task(t.id) is True
        assert s.get_task(t.id).enabled is True

        s.stop()

    def test_persistence(self, tmp_path):
        """测试任务持久化（保存+重载）"""
        from app.core.engines.cron.scheduler import CronScheduler
        from app.core.engines.cron.models import CronTask

        tasks_file = tmp_path / "tasks.json"

        # 第一次：启动 → 添加 → 停止
        s1 = CronScheduler(on_trigger=self._on_trigger, tasks_file=tasks_file)
        s1.start()
        t = CronTask(name="persist", prompt="p", cron_expression="0 9 * * *")
        s1.add_task(t)
        s1.stop()

        assert tasks_file.exists()

        # 第二次：启动 → 应自动加载
        s2 = CronScheduler(on_trigger=self._on_trigger, tasks_file=tasks_file)
        s2.start()
        tasks = s2.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].name == "persist"
        assert tasks[0].cron_expression == "0 9 * * *"
        s2.stop()

    def test_get_task_returns_copy(self):
        from app.core.engines.cron.scheduler import CronScheduler
        from app.core.engines.cron.models import CronTask

        s = CronScheduler(on_trigger=self._on_trigger)
        s.start()
        t = CronTask(name="t", prompt="p", cron_expression="* * * * *")
        s.add_task(t)

        got = s.get_task(t.id)
        got.name = "modified"
        got2 = s.get_task(t.id)
        assert got2.name == "t"  # 原始任务未被影响
        s.stop()


class TestCronConversationAdapter:
    def test_reset(self):
        from app.core.conversation.adapters.cron import CronConversationAdapter

        # mock core+executor
        core = MagicMock()
        executor = MagicMock()

        adapter = CronConversationAdapter(core, executor)
        adapter._response = "old"
        adapter._error = "old_err"
        adapter.reset()

        assert adapter._response == ""
        assert adapter._error is None
        assert adapter._start_ts > 0

    def test_callbacks(self):
        from app.core.conversation.adapters.cron import CronConversationAdapter

        core = MagicMock()
        executor = MagicMock()
        adapter = CronConversationAdapter(core, executor)

        callbacks = adapter.get_callbacks()
        assert "content_received" in callbacks
        assert "finished" in callbacks
        assert "error" in callbacks

        # 累积内容
        callbacks["content_received"]("hello ")
        callbacks["content_received"]("world")
        assert adapter._response == "hello world"

        # 触发 finished
        adapter._worker_done_event.clear()
        callbacks["finished"]("final response")
        assert adapter._worker_done_event.is_set()
        assert adapter._response == "final response"

    def test_error_callback(self):
        from app.core.conversation.adapters.cron import CronConversationAdapter

        core = MagicMock()
        executor = MagicMock()
        adapter = CronConversationAdapter(core, executor)
        adapter._worker_done_event.clear()

        callbacks = adapter.get_callbacks()
        callbacks["error"]("oops")
        assert adapter._worker_done_event.is_set()
        assert adapter._error == "oops"


class TestCronEngine:
    def test_get_singleton(self):
        from app.core.engines.cron.engine import CronEngine

        # 没初始化时返回 None
        CronEngine._global_instance = None
        assert CronEngine.get_instance() is None

        # mock init 到单例
        eng = CronEngine(get_model_config=lambda: {})
        CronEngine._global_instance = eng

        assert CronEngine.get_instance() is eng

        # cleanup
        CronEngine._global_instance = None

    def test_init_basic(self, tmp_path):
        from app.core.engines.cron.engine import CronEngine

        # 避免 singleton 副作用：先存一份原 instance
        original = CronEngine._global_instance

        eng = CronEngine(
            get_model_config=lambda: {"model": "test"},
            workdir=tmp_path,
        )
        # 不能直接比较 is original（破坏别的测试），只验证可以创建
        assert eng is not None
        assert eng._workdir == tmp_path

        # 清理：避免 singleton 在此测试外影响
        CronEngine._global_instance = original

    def test_resolve_llm_config_with_override(self):
        from app.core.engines.cron.engine import CronEngine

        eng = CronEngine.__new__(CronEngine)  # 跳过 __init__
        eng._get_model_config = lambda: {"model": "default", "temperature": 0.7, "api_key": "k"}

        # 无覆盖
        cfg = eng._resolve_llm_config(None)
        assert cfg["model"] == "default"
        assert cfg["temperature"] == 0.7

        # 部分覆盖
        cfg = eng._resolve_llm_config({"model": "gpt-4o-mini"})
        assert cfg["model"] == "gpt-4o-mini"
        assert cfg["temperature"] == 0.7  # 其他字段保留

    def test_resolve_llm_config_ignores_none_values(self):
        from app.core.engines.cron.engine import CronEngine

        eng = CronEngine.__new__(CronEngine)
        eng._get_model_config = lambda: {"model": "x"}

        # override 中的 None 不应覆盖
        cfg = eng._resolve_llm_config({"model": "override", "temperature": None})
        assert cfg["model"] == "override"
        assert "temperature" not in cfg or cfg.get("temperature") is None


class TestCronTools:
    def test_no_engine_raises(self):
        """未注入 CronEngine 时调用工具应返回错误 ToolResult"""
        from app.tools.cron_tools import CronTools
        from app.tools.result import ToolResult

        # 创建一个 mock owner（无 _cron_engine）
        owner = MagicMock()
        # 关键：拿掉 _cron_engine 属性，模拟返回 None
        if hasattr(owner, "_cron_engine"):
            del owner._cron_engine

        tools = CronTools(owner)
        result = tools.cron_list()
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "CronEngine" in (result.error or "")

    def test_cron_create_via_engine(self):
        from app.tools.cron_tools import CronTools

        owner = MagicMock()
        mock_engine = MagicMock()
        mock_engine.add_task.return_value = True
        owner._cron_engine = mock_engine

        tools = CronTools(owner)
        result = tools.cron_create(
            cron_expression="*/5 * * * *",
            prompt="say hi",
            name="test_task",
        )

        assert result.success is True
        # 验证 add_task 被调用
        mock_engine.add_task.assert_called_once()
        call_args = mock_engine.add_task.call_args[0][0]
        assert call_args.cron_expression == "*/5 * * * *"
        assert call_args.prompt == "say hi"
        assert call_args.name == "test_task"

    def test_cron_list_no_tasks(self):
        from app.tools.cron_tools import CronTools

        owner = MagicMock()
        mock_engine = MagicMock()
        mock_engine.list_tasks.return_value = []
        owner._cron_engine = mock_engine

        tools = CronTools(owner)
        result = tools.cron_list()
        assert result.success is True
        assert "无定时任务" in result.content

    def test_cron_delete_validates(self):
        from app.tools.cron_tools import CronTools

        owner = MagicMock()
        mock_engine = MagicMock()
        owner._cron_engine = mock_engine

        tools = CronTools(owner)
        # 空 ID
        result = tools.cron_delete("")
        assert result.success is False
        # 不存在的任务
        mock_engine.get_task.return_value = None
        result = tools.cron_delete("nonexistent")
        assert result.success is False

        # 删除成功路径
        mock_engine.get_task.return_value = MagicMock(name="t")
        mock_engine.remove_task.return_value = True
        result = tools.cron_delete("valid_id")
        assert result.success is True


class TestCronIntegration:
    """集成测试：scheduler + 实际触发（不依赖 LLM）"""

    def test_short_interval_fires(self):
        """添加一个 1 秒触发的任务，验证 _on_job_fired 调用"""
        from app.core.engines.cron.scheduler import CronScheduler
        from app.core.engines.cron.models import CronTask
        import time

        fired = []

        def cb(t):
            fired.append(t)

        # 创建一个 5 字段 cron 每分钟触发（cron 的最小粒度是 1 分钟，分钟级可用）
        # 为测试速度用休眠观察：改用间隔触发比 cron 更快，但这里先用现有 cron 接口
        s = CronScheduler(on_trigger=cb)
        s.start()

        # 模拟直接调用 _on_job_fired（绕过时间等待）
        task = CronTask(name="manual", prompt="x", cron_expression="* * * * *", recurring=True)
        s._tasks[task.id] = task

        s._on_job_fired(task.id)
        # 手动 fire 应该调用 on_trigger 一次
        assert len(fired) == 1
        assert fired[0].name == "manual"

        s.stop()

    def test_one_shot_cleanup(self):
        """一次性任务触发后应自动清理"""
        from app.core.engines.cron.scheduler import CronScheduler
        from app.core.engines.cron.models import CronTask

        fired = []
        s = CronScheduler(on_trigger=lambda t: fired.append(t))
        s.start()

        task = CronTask(
            name="once",
            prompt="x",
            cron_expression="* * * * *",
            recurring=False,
        )
        s._tasks[task.id] = task

        s._on_job_fired(task.id)
        # 一次性任务触发后应自动从 _tasks 删除
        assert s.get_task(task.id) is None
        assert len(fired) == 1

        s.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
