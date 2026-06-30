# drifox-dev（有状态技能 · 渐进式披露）

DriFox 项目开发技能——**升级为有状态 + 渐进式披露版本**。

- **有状态**：跨会话记忆项目状态、用户偏好、决策、坑点、自动快照、GitHub open issues。
- **渐进式披露**：`SKILL.md` 仅保留入口 + 任务分派决策树（< 200 行），
  按用户任务分派到 `references/` 下对应文件，避免一次性塞 480+ 行进上下文。

## 与无状态技能的区别

| 维度 | 无状态技能 | 本技能（drifox-dev） |
|------|-----------|---------------------|
| 加载内容 | 静态 SKILL.md | 静态骨架（决策树 + 索引）+ 持久化 state.json |
| references/ | 无 | 按任务类型按需加载（节省首屏 token） |
| 跨会话 | 全部丢失 | 记住：焦点 / 决策 / 偏好 / 坑点 / 项目快照 / GitHub issues |
| 项目状态感知 | 现场扫描 | 自动缓存（`snapshot_project.py`） |
| 文件行数 | 手抄会过期 | 每次刷新都准确 |
| 用户偏好 | 每次重提 | 一次设置永久生效 |

## 任务分派决策树（节选自 SKILL.md）

| 用户说 | 第一步 | 必读 references |
|--------|-------|-----------------|
| 加功能 / 做新东西 | `brainstorming` | `scenarios.md` § 一 |
| 改架构 / 拆分模块 | `brainstorming` | `scenarios.md` § 五 |
| 不工作 / 报错 / 回归 / 性能 | `diagnose` | `scenarios.md` § 二 |
| 加 UI / 卡片 / 主题 | 直读 `dev-ui.md` | UI 流程 |
| 加 Agent / 改 Skill / 写插件 | 直读 `dev-agent-skill.md` | 组件流程 |
| 加工具 / 调权限 / 改 Hook | 直读 `dev-tools-hooks.md` | 工具流程 |
| 跑测试 / 打包 / 提 PR | 直读 `testing-build.md` | 工程流程 |
| 查 state / 改偏好 / 录决策 | 直读 `state-reference.md` | 元操作 |
| 命名 / 格式 / 提交规范 | 直读 `conventions.md` | 编码规范 |
| 模式 / 多窗口 / 信号槽 | 直读 `patterns.md` | 模式 |
| 定位要改的文件 | 直读 `architecture.md` | 全局 |

## 目录结构

```
drifox-dev/
├── SKILL.md                       # 入口（任务分派决策树 + 加载流程）
├── README.md                      # 本文件
├── references/                    # 按需加载的详细参考
│   ├── architecture.md            # 四层架构 + 目录 + 信号
│   ├── patterns.md                # 设计模式 + 多窗口 + 信号槽
│   ├── conventions.md             # 命名 / 风格 / 提交 / AGENTS 铁律
│   ├── dev-ui.md                  # UI 组件开发要点
│   ├── dev-agent-skill.md         # Agent / Skill / 插件开发
│   ├── dev-tools-hooks.md         # 工具 / Hook / 权限
│   ├── scenarios.md               # 常见开发场景流程
│   ├── testing-build.md           # 测试 / 构建 / 提 PR
│   └── state-reference.md         # state.json 字段 + CLI 详解
├── state/
│   ├── state.json                 # 持久化状态（运行时生成）
│   └── state.template.json        # 初始模板
├── scripts/
│   ├── __init__.py
│   ├── state_manager.py           # 状态读写（CLI + API）
│   └── snapshot_project.py        # 自动扫描项目 + GitHub issues
└── evals/                         # 评估数据（保留）
```

## 快速开始

```bash
cd D:/work/DriFox

# 1. 初始化（首次）
python plugins/system/skills/drifox-dev/scripts/state_manager.py init

# 2. 采集项目快照（含 GitHub open issues — 公开仓库免 token）
python plugins/system/skills/drifox-dev/scripts/snapshot_project.py

# 离线 / CI 环境跳过 GitHub 拉取：
python plugins/system/skills/drifox-dev/scripts/snapshot_project.py --no-network

# 3. 查看当前状态摘要
python plugins/system/skills/drifox-dev/scripts/state_manager.py show --summary

# 4. 记录工作进展
python scripts/state_manager.py focus --task "重构 drifox-dev 技能"
python scripts/state_manager.py decision --scope "drifox-dev" \
    --decision "SKILL.md 拆分为 references/ 子文件" \
    --rationale "渐进式披露，节省首屏 token"
python scripts/state_manager.py pitfall --module <name> \
    --symptom "..." --cause "..." --fix "..."
python scripts/state_manager.py question --question "..." --context "..."      # non-blocking
python scripts/state_manager.py question --question "..." --context "..." --blocking  # 阻塞任务
python scripts/state_manager.py preference --key <name> --value <value>
```

## state.json 关键字段

```json
{
  "version": "1.0.0",
  "current_focus": { "task": "...", "module": "...", "branch": "..." },
  "recent_decisions":   [...],
  "user_preferences":  { ... },
  "known_pitfalls":    [...],
  "open_questions": [
    { "id": "Q001", "question": "...",
      "blocking": true,            // ← 拆开阻塞/非阻塞，避免摘要噪音
      "context": "...", "created_at": "..." }
  ],
  "auto_snapshot": {
    "last_updated": "...",
    "key_files_lines": { ... },
    "recent_commits": [ ... ],
    "uncommitted_changes": { "dirty": false, "files": [] },
    "github_issues": {            // ← 新增
      "ok": true,
      "issues": [ { "number": 180, "title": "[Bug] ...", "labels": [...] } ],
      "repo": "martin98-afk/DriFox",
      "count": 10,
      "fetched_at": "..."
    }
  }
}
```

加载技能时（`show --summary`）会按「焦点 → 偏好 → 决策 → 坑点 → 阻塞问题 → 非阻塞问题 → 快照（含 GitHub issues）」顺序渲染。

完整字段说明见 `references/state-reference.md`。

## GitHub issues 集成

`snapshot_project.py` 在每次跑自动从 https://github.com/martin98-afk/DriFox/issues
拉取最近 10 条 open issue，存到 `state.json.auto_snapshot.github_issues`。

- 公开仓库**免 token**；私有仓库设置环境变量 `GITHUB_TOKEN`
- 失败优雅降级（超时 / 断网 / 403 rate limit）→ `ok=False, error="..."`
- 过滤掉 PR（GitHub `/issues` 接口同时返回 PR）
- 加 `--no-network` 跳过

## 设计原则

参考 eliteai.tools / Anthropic Agent Skills：

1. **渐进式披露**：SKILL.md 只放骨架，详细内容按需从 references/ 加载
2. **任务分派优先**：bug 走 diagnose、新功能走 brainstorming，二者不混
3. **原子写入**：`tempfile + os.replace` 防崩溃损坏
4. **跨平台文件锁**：Windows `msvcrt`、Linux/Mac `fcntl`
5. **版本迁移 + backfill**：schema 升级自动迁移，缺字段自动补齐
6. **去重 / 限额**：坑点 50 / 决策 50 / 问题 20
7. **动态事实由脚本采集**：行数 / commit / issues，AI 不手抄
8. **优雅降级**：模板 / 锁 / 迁移 / 网络任何环节失败不阻塞主流程

## API 用法（Python 模块）

```python
import sys
sys.path.insert(0, "plugins/system/skills/drifox-dev/scripts")
from state_manager import (
    load_state, save_state, set_focus, add_decision,
    add_pitfall, add_question, set_preference,
    render_state_summary,
)
from snapshot_project import take_snapshot, update_snapshot

set_focus("...", module="...", branch="dev")
add_decision("scope", "decision", "rationale")
add_pitfall("module", "symptom", "cause", "fix")
add_question("q", "ctx", blocking=True)
update_snapshot(take_snapshot())  # 含 GitHub issues
```

## 迁移说明（旧版 → 当前版）

如果是从旧版（无 references/ 单 SKILL.md）升级：

1. `state.json` 加载时自动 backfill 新增字段（`open_questions[].blocking`、`auto_snapshot.github_issues`）——无需手动操作
2. 想看 GitHub issues 只需再跑一次 `snapshot_project.py`
3. 旧决策 / 坑点不动，新版 `show --summary` 会自动按新格式渲染
