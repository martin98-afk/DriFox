---
description: 完成开发工作分支
type: prompt
argument-hint:
  "[--merge]": "合并到基础分支"
  "[--pr]": "推送并创建 Pull Request"
  "[--keep]": "保持分支不变"
  "[--discard=]": "丢弃工作（输入 discard 确认）"
---

## 参数说明

`$ARGUMENTS` 是用户输入的参数字符串（不含 `--flag` 部分）。

- **`--merge`**：将当前分支合并到基础分支（main/master）
- **`--pr`**：推送分支并创建 Pull Request
- **`--keep`**：保持分支不变，稍后处理
- **`--discard`**：丢弃所有工作（需确认）

无参数时，交互式选择上述选项。

## 执行流程

### Step 1: 验证测试

**在展示选项前，先验证测试通过：**

```bash
# 运行项目测试
npm test / cargo test / pytest / go test ./...
```

**如果测试失败：**
```
测试失败 (<N> 个失败)。必须在修复后才能完成：

[显示失败项]

测试通过前不能继续合并/PR。
```

停止。不要进入 Step 2。

**测试通过后：** 继续 Step 2。

### Step 2: 检测环境

**在展示选项前确定工作区状态：**

```bash
GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
```

根据状态显示不同菜单：

| 状态 | 菜单 | 清理 |
|------|------|------|
| `GIT_DIR == GIT_COMMON`（普通仓库） | 标准 4 选项 | 无需清理 worktree |
| 工作树 | 标准 4 选项 | 按来源清理（见 Step 6） |
| 分离 HEAD | 减少 3 选项（无合并） | 不清理（外部管理） |

### Step 3: 确定基础分支

```bash
# 尝试常见基础分支
git merge-base HEAD main 2>/dev/null || git merge-base HEAD master 2>/dev/null
```

或询问："此分支从 main 分叉，是否正确？"

### Step 4: 展示选项

**普通仓库和命名分支工作树 — 展示这 4 个选项：**

```
实现完成。您想做什么？

1. 合并到 <基础分支>
2. 推送并创建 Pull Request
3. 保持分支不变
4. 丢弃工作

选择哪个选项？
```

**分离 HEAD — 展示这 3 个选项：**

```
实现完成。您处于分离 HEAD 状态（外部管理工作区）。

1. 推送为新分支并创建 Pull Request
2. 保持不变
3. 丢弃工作

选择哪个选项？
```

### Step 5: 执行选择

#### 选项 1: 本地合并

```bash
# 获取主仓库根目录
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"

# 先合并 — 确认成功后再删除任何内容
git checkout <基础分支>
git pull
git merge <功能分支>

# 验证合并后的测试
<测试命令>

# 合并成功后：清理 worktree（Step 6），然后删除分支
git branch -d <功能分支>
```

#### 选项 2: 推送并创建 PR

```bash
# 推送分支
git push -u origin <功能分支>

# 创建 PR
gh pr create --title "<标题>" --body "## 摘要
<2-3 条变更说明>

## 测试计划
- [ ] <验证步骤>"
```

**不要清理 worktree** — 用户需要它来迭代 PR 反馈。

#### 选项 3: 保持不变

报告："保持分支 <name>。Worktree 保留在 <路径>。"

#### 选项 4: 丢弃

**先确认：**
```
这将永久删除：
- 分支 <name>
- 所有提交：<提交列表>
- 工作树：<路径>

输入 'discard' 确认。
```

等待准确确认。如果确认：
```bash
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"
```

然后：清理 worktree（Step 6），强制删除分支：
```bash
git branch -D <功能分支>
```

### Step 6: 清理工作区

**仅在选项 1 和 4 执行。** 选项 2 和 3 始终保留 worktree。

```bash
GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
WORKTREE_PATH=$(git rev-parse --show-toplevel)
```

**如果 `GIT_DIR == GIT_COMMON`：** 普通仓库，无 worktree 清理。完成。

**如果 worktree 路径在 `.worktrees/`、`worktrees/` 或 `~/.config/superpowers/worktrees/` 下：** 由本工具创建的工作树 — 我们负责清理。

```bash
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"
git worktree remove "$WORKTREE_PATH"
git worktree prune
```

**否则：** 工作区由主机环境（harness）管理。不要删除它。

## 快速参考

| 选项 | 合并 | 推送 | 保留 Worktree | 清理分支 |
|------|------|------|---------------|----------|
| 1. 本地合并 | 是 | - | - | 是 |
| 2. 创建 PR | - | 是 | 是 | - |
| 3. 保持不变 | - | - | 是 | - |
| 4. 丢弃 | - | - | - | 是（强制） |

## 禁止事项

**永远不要：**
- 在测试失败时继续
- 不验证结果测试就合并
- 未经确认就删除工作
- 未经明确请求就强制推送
- 确认合并成功前删除 worktree
- 清理非自己创建的 worktree
- 从 worktree 内部运行 `git worktree remove`

**始终：**
- 在展示选项前验证测试
- 在展示菜单前检测环境
- 获取选项 4 的输入确认
- 仅在选项 1 和 4 清理 worktree
- 删除分支前先移除 worktree
- 删除后运行 `git worktree prune`