# AutoLoop 插件

> AutoLoop 自动循环模式 — 规划 → 执行 → 归档 三阶段长任务自动执行。
> 对话引擎插件化首例：从主程序 `app/core/engines/auto_loop/` 整体迁移而来。

## 架构

```
plugins/autoloop/
├── .drifox-plugin/plugin.json     # manifest（ui + agents）
├── agents/auto_loop.md            # @auto_loop 智能体定义
├── icons/                         # 工具栏按钮图标（深/浅）
├── autoloop_core/                 # 核心逻辑（插件根注入 sys.path）
│   ├── config.py                  # AutoLoopConfig
│   ├── engine.py                  # AutoLoopEngine 状态机（纯逻辑，无 Qt）
│   ├── prompt_composer.py         # 三阶段提示词模板
│   ├── adapter.py                 # 线程同步对话适配器
│   └── worker.py                  # AutoLoopWorker（QThread 主循环）
└── ui/
    ├── __init__.py                # register_ui：config/running 双卡 + 输入按钮
    ├── cards.py                   # 配置卡 + 运行卡（full 覆盖层）
    └── controller.py              # Worker 生命周期/信号接线/窗口会话管理
```

## 与主程序的边界

对话能力全部经 **ui context services**（`main_widget._build_ui_services` 注入）：

| 服务 | 用途 |
|---|---|
| `get_model_config` / `get_tool_executor` / `get_agent_manager` | 驱动自建 ConversationCore |
| `get_tools_schema("auto_loop")` | agent 视角工具集（deny 过滤） |
| `set_workdir` / `get_workdir` / `sync_working_directory` | 工作目录管理 |
| `enter/exit_exclusive_ui_mode` | 运行期独占锁定（隐藏输入区/禁新建） |
| `save_messages_to_session` | 结束后消息并入当前会话 |
| `hide_card` / `notify` | 卡片切换与 InfoBar 通知 |

Worker 自建 `ConversationCore.create()` 执行栈，不依赖主程序 UIEngine 实例。

## 使用

- 输入区工具栏 ♾ 按钮 → 配置卡（或 `/autoloop:config`）
- 填任务描述 → 开始 → 运行卡显示进度（可停止/归档）
- 全局单会话：任一时刻仅一个 AutoLoop 循环

## 热重载

- ui/core 文件改动 → watchfiles 自动重载
- 运行中卸载插件 → controller 经卡片 destroyed 信号取消 worker 并收尾
