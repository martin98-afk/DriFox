---
description: Storages（会话存储引擎）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Storages（会话存储引擎）组件开发

替换主程序的会话持久化后端（默认 sqlite）。消费方：HistoryManager（UI 历史）、API 网关历史、MemoryManager（长期记忆，探底 `engine.store._db`）。

### 文件位置

```
your-plugin/
├── .drifox-plugin/plugin.json   ← components.storages: true
└── storages/*.py                ← 每个文件暴露 register(registry)
```

### 关键约束（成败在此）

- **方法面必须完整对齐默认 sqlite 引擎**：6 个主接口（save/get/get_all/get_by_project/get_projects/delete）+ 消费方探测属性（`is_initialized`/`store`/`_db_path`）+ `save_session/get_session/get_sessions/get_sessions_lightweight/delete_session/get_session_count/update_session_project/archive_sessions_by_project/record_file_operation/clear_old_subagent_tasks` 等 ~20 个方法。缺一个，某条历史路径就崩
- 激活经设置卡"会话存储"选择，或 `config_schema` + `PluginConfigStore` 自激活（参照 jsonl-storage）
- 第三方对齐成本高——jsonl-storage 实现约 588 行是正常体量，动手前先读它

### 参考

- 完整案例：`drifox-plugins2` 仓库 `plugins/jsonl-storage/`（逐方法对齐 sqlite 的活例）
- 消费方接线：`app/utils/history_manager.py` / `app/core/memory_manager.py` / `app/gateway/local_service/session_handler.py`
- 注册表：`app/plugins/registries/storage_registry.py`
