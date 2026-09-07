# state.json 字段与 CLI 参考

> SKILL.md 在任务涉及「读写 state.json、查状态、记录坑点/决策」时分派到这里。

---

## 一、state.json 字段

| 字段 | 用途 | 写入方式 | 容量上限 |
|------|------|---------|---------|
| `version` | schema 版本 | 自动 | — |
| `current_focus` | 当前正在处理的任务/模块/分支 | Agent 手动（`focus`） | 1 条 |
| `recent_decisions` | 最近架构 / 技术决策 | Agent 手动（`decision`） | 50 条 |
| `user_preferences` | 用户编码风格偏好 | 用户 / Agent（`preference`） | 无硬限 |
| `known_pitfalls` | 已踩过的坑（symptom/cause/fix） | Agent 手动（`pitfall`） | 50 条 |
| `open_questions` | 待澄清的开放问题（**已拆 blocking / non-blocking**） | Agent 手动（`question`） | 20 条 |
| `auto_snapshot` | 实时快照：行数/git/github_issues | 自动（`snapshot_project.py`） | — |

### 1.1 open_questions 的拆分

为减少摘要噪音，`open_questions` 每条带 `blocking: bool` 字段：

```json
{
  "id": "Q001",
  "question": "...",
  "context": "...",
  "blocking": true,            // 缺这个回答任务就推不动
  "created_at": "...",
  "resolved_at": null
}
```

| 类型 | 含义 | 摘要里展示 |
|------|------|----------|
| `blocking: true` | 阻塞当前任务推进 — 必须尽快问用户 | ✅ 置顶，全部展示 |
| `blocking: false` | 已记录但不阻塞，下次再说 | ⏸️ 折叠，仅显示数量 |

## 二、state_manager.py CLI

```bash
# 初始化（首次）
python scripts/state_manager.py init

# 查看完整 JSON
python scripts/state_manager.py show

# 给 AI 看的精简摘要（**加载技能时调这个**）
python scripts/state_manager.py show --summary

# 路径
python scripts/state_manager.py path

# 设置当前焦点
python scripts/state_manager.py focus \
  --task "重构 tool_control_card" \
  --module tool_control_card \
  --branch dev

# 清除焦点（任务结束 / 切换）
python scripts/state_manager.py focus --clear

# 记录决策（自动截断到 50 条）
python scripts/state_manager.py decision \
  --scope "agent" \
  --decision "把 drifox-dev 升级为有状态技能" \
  --rationale "跨会话需要记住项目状态"

# 记录坑点（按 module+cause 去重）
python scripts/state_manager.py pitfall \
  --module tool_control_card \
  --symptom "用户开关不更新" \
  --cause "_on_active_toggles_changed 缺 rebuild 链路" \
  --fix "完整实现 + 直接调 rebuild()"

# 记录开放问题（**默认 blocking=false，加 --blocking 升级**）
python scripts/state_manager.py question \
  --question "状态文件存技能目录还是用户数据目录？" \
  --context "技能目录迁移时会丢" \
  [--blocking]

# 解决问题（移除）
python scripts/state_manager.py question --resolve Q001

# 修改偏好（值支持 JSON 字符串）
python scripts/state_manager.py preference --key no_unrelated_refactor --value true
```

## 三、snapshot_project.py 自动化

```bash
# 采集并写入 state.json
python scripts/snapshot_project.py

# 只预览不写入
python scripts/snapshot_project.py --json

# 指定项目根
python scripts/snapshot_project.py --project-root D:/work/DriFox
```

**自动采集的字段**（写入 `auto_snapshot`）：
- `branch`：当前 git 分支
- `key_files_lines`：关键文件行数（KEY_FILES 列表硬编码在脚本里）
- `recent_commits`：`git log -n 10` 解析
- `uncommitted_changes`：`git status --porcelain` 解析
- `github_issues`：从 https://github.com/martin98-afk/DriFox/issues 拉取最近 10 条 open issue（需联网）

**何时刷新**：
- 任务开始时（建立基线）
- 重要 commit 后
- 关键文件大改后（行数变化）
- state.json 行数明显对不上时
- 想看 GitHub 最新 issue 时

## 四、Agent 使用规约

| 场景 | 操作 |
|------|------|
| **加载本技能时** | `state_manager.py show --summary` → 注入上下文 |
| **明确本轮任务** | `focus --task "..."` |
| **做出架构决策** | `decision ...` |
| **发现 / 修复一个坑** | `pitfall ...` |
| **遇到不确定但暂不深究** | `question ...`（默认 non-blocking）|
| **缺回答就推不动** | `question ... --blocking` |
| **完成任务 / 切换任务** | `focus --clear` |
| **上下文长任务** | 中途 `snapshot_project.py` 刷新 |

## 五、API 用法（Python 模块）

```python
import sys
sys.path.insert(0, "plugins/system/skills/drifox-dev/scripts")
from state_manager import (
    load_state, save_state, set_focus, add_decision,
    add_pitfall, add_question, set_preference,
    render_state_summary,
)
from snapshot_project import take_snapshot, update_snapshot
```

## 六、设计原则

参考 eliteai.tools / Anthropic Agent Skills 实践：

1. **原子写入**：`tempfile + os.replace` 防崩溃损坏
2. **跨平台文件锁**：Windows `msvcrt`、Linux/Mac `fcntl`
3. **版本迁移**：schema 升级自动迁移
4. **去重 / 限额**：坑点 50 / 决策 50 / 问题 20
5. **动态事实由脚本采集**：AI 不手抄会过期的事实
6. **优雅降级**：模板 / 锁 / 迁移 / 网络任何环节失败不阻塞主流程
