# 项目开发规范

本文件为 AI Agent 提供项目操作手册与约束清单，确保 Agent 行为可控、可复现。
 
---

## 1. 目标与边界

### 允许的操作
- **有关键文档存在时，优先以关键文档作为项目路径进行探索**
- 读取、修改顶层文档：`README.md`、`AGENTS.md`、`CONTRIBUTING.md` 等
- 读取、修改 `docs/`、`prompts/`、`skills/`、`tools/config/`、`tools/external/` 下的文档与代码
- 执行项目规定的 lint、检查、构建命令
- 新增/修改功能、修复问题
- 提交符合规范的 commit

### 禁止的操作
- 修改 `.github/workflows/` 中的 CI 配置（除非任务明确要求）
- 修改 `LICENSE`、`CODE_OF_CONDUCT.md`
- 在代码中硬编码密钥、Token 或敏感凭证
- 未经确认的大范围重构

### 敏感区域（禁止自动修改）
- `.github/workflows/*.yml` - CI/CD 配置
- `.env*` 文件（如存在）

---

## 2. 推荐执行路径

```bash
# 1. 拉取最新代码
git pull --rebase origin develop

# 2. 初始化依赖（如有需要）
# ... 项目特有命令

# 3. 运行 lint 检查
# ... 项目特有命令

# 4. 执行修改任务
# ...

# 5. 再次验证
# ... 项目特有检查命令

# 6. 提交变更
git add -A
git commit -m "feat|fix|docs|chore: scope - summary"
git push origin develop
```

---

## 3. 修改约束

### 架构原则
- 保持根目录扁平，避免巨石文件
- 遵循项目现有架构，不随意改动

### 禁止行为
- 禁止"顺手重构/大范围改动"除非任务明确要求
- 禁止删除现有测试用例（除非任务要求）
- 禁止在代码中硬编码敏感信息

---

## 4. 风格与质量标准

### 格式化工具
- 遵循项目现有代码风格
- 使用项目已有的格式化工具

### 命名约定
- 文档、注释、日志使用中文
- 代码符号统一英文且语义直白
- 文件名小写加中划线或下划线（遵循现有风格）

### 设计品味
- 优先消除分支与重复
- 函数单一职责且短小

---

## 5. 提交规范

遵循简化 Conventional Commits：
```
feat|fix|docs|chore|refactor|test: scope - summary
```

---

## 6. 强制同步规则

**任何功能/命令/配置/目录/工作流变化必须同步更新相关文档**

不确定的内容用 TODO 标注，不允许猜测。

---

### 已修复（2026-06-26）
- **hook 消息双重注入（严重 bug）**:
  - 根因：`on_hook_finished` 回调把 hook 输出同时写入**两个路径**：
    - `session.messages.append(raw_msg)` — raw 版本直接持久化到会话（无格式包装）
    - `_hook_message_queue.put(formatted_msg)` — 格式化版本推入队列供 worker 注入
  - 结果：context_builder 从 session.messages 读取 raw 版本送入 LLM，worker 再从队列读取格式化版本送入 LLM → **每轮翻倍累积**
  - 修复 1（`backend.py`）：移除 raw 版本写入，改为只写入格式化版本（带 `---🔌 Hook 内部通知---` 包装）+ 通用 `_hook_event` 去重 + 旧 raw 消息迁移清理
  - 修复 2（`chat_worker.py`）：`_inject_pending_hook_messages` 增加 `_hook_event` 去重检查，当 session 已包含同类型 hook 消息时跳过队列注入
  - 修复 3：PostUserMessage 从 command 类型改为 Python 类型（`current_datetime.py`），同步执行确保在 worker 启动前完成
  - 影响文件：`app/core/backend.py`、`app/core/workers/chat_worker.py`、`plugins/system/hooks/hooks.json`、`plugins/system/hooks/current_datetime.py`（新建）
  - 测试：全部 42 个测试通过 ✅

- **chat_worker 工具调用循环导致会话永久卡死**:
  - 根因：循环检测触发后 `finished_with_messages` 发射的消息列表**仍包含重复的工具调用轮次**，持久化后下次发消息再次触发检测 → 会话死锁
  - 修复 V1 → V2 迭代（V1 有两个 bug）:
    - V1 bug1：截断 `messages[:second_round_index]` 丢弃了用户的新消息 → 用户消息丢失 → 永远无法推进
    - V1 bug2：只取最后 threshold 轮截断，超过阈值的重复轮次（如5轮）没清理干净 → 下次又触发
    - V2 修复：重写 `_truncate_repetitive_tool_calls()`，找出从末尾开始的完整连续相同签名区间，保留第1轮 + 终止提示 + **后续所有消息**（含用户新消息）
  - 测试：`tests/test_chat_worker_tool_loop.py` 共 21 个测试全部通过 ✅
  - 已卡死会话恢复方法：重启 DriFox → 发一条消息（触发截断清理，会报错但消息已清理）→ 再发一条即可正常对话；或使用 `/compact` 命令压缩上下文绕过

### 已修复（2026-06-18）
- **tool_control_card 信号链 / 统计数字不更新**:
  - `_on_active_toggles_changed` 完整实现（含 `rebuild` + `update()` + `togglesChanged.emit()`）
  - 删除 109 行重复的 `_on_active_toggles_changed` 定义
  - `main_widget.py` 双向绑定兜底：`setup_ui` 创建卡片后检查 controller，`__init__` 创建 controller 后检查卡片
  - `_on_tool_toggled` 改为直接调 `rebuild()`，不再依赖信号链延迟
  - 日志格式统一：`[ToolCard] _rebuild: agent=xxx, toggles_enabled=X/32`
  - 所有用户开关/组开关/agent 权限注入/恢复功能已验证通过 ✅
