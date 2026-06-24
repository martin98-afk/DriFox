# drifox-dev（有状态技能）

DriFox 项目开发技能——**升级为有状态版本**，跨会话记忆项目状态、用户偏好、决策、坑点。

## 与无状态技能的区别

| 维度 | 无状态技能 | 本技能（drifox-dev） |
|------|-----------|---------------------|
| 加载内容 | 静态 SKILL.md | 静态骨架 + 持久化 state.json |
| 跨会话 | 全部丢失 | 记住：焦点/决策/偏好/坑点/项目快照 |
| 项目状态感知 | 需 Agent 现场扫描 | 自动缓存（`snapshot_project.py`） |
| 文件行数 | 手抄会过期 | 每次刷新都准确 |
| 用户偏好 | 每次重提 | 一次设置永久生效 |

## 目录结构

```
drifox-dev/
├── SKILL.md                    # 静态骨架（AI 加载时必读）
├── README.md                   # 本文件
├── state/
│   ├── state.json              # 持久化状态（运行时生成）
│   └── state.template.json     # 初始模板
├── scripts/
│   ├── __init__.py
│   ├── state_manager.py        # 状态读写（CLI + API）
│   └── snapshot_project.py     # 自动扫描项目
└── evals/                      # 评估数据（保留）
```

## 快速开始

### 1. 初始化（首次）

```bash
cd D:/work/DriFoxx
python plugins/system/skills/drifox-dev/scripts/state_manager.py init
```

### 2. 采集项目快照

```bash
python plugins/system/skills/drifox-dev/scripts/snapshot_project.py
```

### 3. 查看当前状态

```bash
# 完整 JSON
python plugins/system/skills/drifox-dev/scripts/state_manager.py show

# 给 AI 看的精简摘要
python plugins/system/skills/drifox-dev/scripts/state_manager.py show --summary
```

### 4. 记录工作进展

```bash
# 设置当前焦点
python scripts/state_manager.py focus --task "实现状态管理" --module state_manager

# 记录决策
python scripts/state_manager.py decision \
    --scope "drifox-dev" \
    --decision "采用 JSON 状态 + auto-snapshot 方案" \
    --rationale "轻量、可读、易于版本迁移"

# 记录坑点
python scripts/state_manager.py pitfall \
    --module tool_control_card \
    --symptom "用户开关点击后不更新" \
    --cause "_on_active_toggles_changed 缺少完整 rebuild 链路" \
    --fix "补全实现 + 删除重复定义 + 改为直接调 rebuild()"

# 记录待澄清问题
python scripts/state_manager.py question \
    --question "状态文件存技能目录还是用户数据目录？" \
    --context "技能目录迁移时会丢，用户数据目录跨项目不隔离"

# 修改用户偏好
python scripts/state_manager.py preference --key log_language --value zh
```

## state.json 结构

```json
{
  "version": "1.0.0",
  "current_focus": {
    "task": "当前任务名",
    "module": "相关模块",
    "branch": "git 分支",
    "started_at": "ISO 时间",
    "last_touched": "ISO 时间"
  },
  "recent_decisions": [
    {"id": "D001", "date": "...", "scope": "...",
     "decision": "...", "rationale": "..."}
  ],
  "user_preferences": {
    "log_language": "zh",
    "comment_language": "zh",
    "naming_style": "snake_case",
    "no_unrelated_refactor": true,
    "auto_sync_docs": true
  },
  "known_pitfalls": [
    {"id": "P001", "module": "...",
     "symptom": "...", "cause": "...", "fix": "...",
     "discovered_at": "..."}
  ],
  "open_questions": [
    {"id": "Q001", "question": "...", "context": "..."}
  ],
  "auto_snapshot": {
    "last_updated": "...",
    "key_files_lines": {"app/main_widget.py": 12265},
    "recent_commits": [{"hash": "abc1234", "date": "...", "message": "..."}],
    "uncommitted_changes": {"dirty": false, "files": []}
  }
}
```

## 最佳实践

### 何时刷新快照

- 任务开始时（建立基线）
- 重要 commit 后
- 关键文件大改后
- state.json 中行数明显对不上时

### 何时记录决策

- 涉及模块边界划分
- 选择了某种技术方案（SQLite vs JSON、信号 vs 回调）
- 确定了命名/目录/接口约定
- 排除了某种方案（写出排除原因）

### 何时记录坑点

- 调试超过 30 分钟才解决的问题
- 涉及项目特有的隐式约定
- 修复方式不够直观、未来容易重新踩的

### 何时记录开放问题

- 任务进行中遇到但不阻塞当前步骤的问题
- 模糊需求需要后续澄清
- 设计上的二选一/三选一尚未决定

## 设计原则

参考 [eliteai.tools/state-management-patterns](https://eliteai.tools/agent-skills/state-management-patterns) 和
[Anthropic Agent Skills](https://github.com/anthropics/skills)：

1. **原子写入**：`tempfile + os.replace` 防崩溃
2. **跨平台文件锁**：Windows `msvcrt`、Linux `fcntl`
3. **版本迁移**：schema 升级自动迁移
4. **去重/限额**：坑点 50、决策 50、问题 20
5. **动态事实由脚本采集**：AI 不手抄会过期的事实
6. **优雅降级**：任何环节失败不阻塞主流程

## API 用法（Python）

除了 CLI，也可以作为 Python 模块使用：

```python
import sys
sys.path.insert(0, "plugins/system/skills/drifox-dev/scripts")
from state_manager import (
    load_state, save_state, set_focus, add_decision,
    add_pitfall, add_question, render_state_summary,
)
from snapshot_project import take_snapshot, update_snapshot

# 加载状态
state = load_state()

# 设置焦点
set_focus("重构 ChatBackend", module="backend", branch="dev")

# 记录决策
add_decision("agent", "用 JSON 而非 SQLite", "单技能数据量小，JSON 更直观")

# 记录坑点
add_pitfall(
    module="chat_worker",
    symptom="消息顺序错乱",
    cause="信号跨线程未加锁",
    fix="在 emit 前加 _stop_lock 保护",
)

# 刷新快照
update_snapshot(take_snapshot())
```

## 迁移说明

如果是从旧版（无状态）升级：

1. 旧 SKILL.md 已自动备份到 `state/state.template.json` 同目录或通过 git 历史找回
2. 运行 `init` 初始化新 state.json
3. 把之前对话中重要的决策/坑点/偏好手动录入
4. 加载技能时从 `state_manager.py show --summary` 开始
