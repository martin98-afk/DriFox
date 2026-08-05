"""冒烟测试：手工运行（不需要 pytest）

目的：验证整个集成链路
- CronEngine 初始化
- 添加/列出任务
- 通过 CronTools 调用
- 持久化

历史状态（2026-07-18 測試體系整改）：
    本文件对应的 ``app.core.engines.cron``、``app.tools.cron_tools``
    已被移除，运行时所有调用都会因 ``ModuleNotFoundError`` 失败。
    但本文件本身是 ``if __name__ == "__main__"`` 手工运行脚本，不在
    pytest 收集中，所以默认无副作用。保留以备未来重新启用 Cron 子系统。
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# 添加项目根目录
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


def run_smoke_test():
    print("==== 1. Backend -> CronEngine 初始化 ====")
    workdir = tempfile.mkdtemp(prefix="cron_test_")
    print(f"  workdir = {workdir}")

    def get_model_config():
        return {"model": "claude-sonnet-4-5", "temperature": 0.7, "api_key": "test"}

    def get_tools_schema():
        return [{"type": "function", "function": {"name": "read"}}]

    from app.core.engines.cron import CronEngine, CronTask

    eng = CronEngine(
        get_model_config=get_model_config,
        workdir=Path(workdir),
        get_builtin_tools_schema=get_tools_schema,
    )
    eng.start()
    print(f"  Engine initialized, scheduler running: {eng._scheduler.is_running()}")

    print("\n==== 2. 添加定时任务 ====")
    task = CronTask(
        name="check_disk_space",
        prompt="检查磁盘空间并报告",
        cron_expression="*/5 * * * *",
        recurring=True,
        model_config={"model": "gpt-4o-mini"},
    )
    success = eng.add_task(task)
    print(f"  add_task: {success}")
    print(f"  task id: {task.id}")
    print("  describe:")
    for line in eng.get_task(task.id).describe().splitlines():
        print(f"    {line}")

    print("\n==== 3. 列出所有任务 ====")
    for t in eng.list_tasks():
        print(f"  - [{t.id}] {t.name} @ {t.cron_expression}")

    print("\n==== 4. 持久化文件 ====")
    persist_file = Path(workdir) / "cron_results" / ".tasks.json"
    print(f"  Tasks file: {persist_file}")
    print(f"  Exists: {persist_file.exists()}")
    if persist_file.exists():
        print("  Content (first 300 chars):")
        print("  " + persist_file.read_text(encoding="utf-8")[:300].replace("\n", "\n  "))

    print("\n==== 5. CronTools 工具调用模拟 ====")
    from app.tools.cron_tools import CronTools

    mock_owner = MagicMock()
    mock_owner._cron_engine = eng
    mock_owner.workdir = Path(workdir)

    ct = CronTools(mock_owner)

    # cron_create
    result = ct.cron_create(
        cron_expression="0 9 * * *",
        prompt="早上好，现在几点？",
        name="morning_greeting",
        recurring=True,
    )
    print(f"  cron_create: success={result.success}")
    print(f"  Content:")
    print("    " + result.content.replace("\n", "\n    "))

    # cron_list
    result = ct.cron_list()
    print(f"  cron_list (truncated): {result.content[:100]}...")

    # cron_delete
    result = ct.cron_delete(task.id)
    print(f"  cron_delete: success={result.success}")

    print("\n==== 6. Engine stopped ====")
    eng.stop()
    print(f"  is_running after stop: {eng._scheduler.is_running()}")

    print("\n[OK] All smoke tests passed")


if __name__ == "__main__":
    run_smoke_test()
