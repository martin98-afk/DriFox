---
name: github-ops
description: GitHub 仓库操作工具集，支持 Issue 评论回复、PR 创建、Issue/PR 详情查询、代码推送和 PR 创建联动。使用 Python requests 库调用 GitHub API，配合 gh CLI 工具完成分支操作。Use when 用户提到回复 GitHub Issue、创建 PR、推送并创建 PR、查看 issue 状态、GitHub 操作。
---

# GitHub Operations

GitHub 仓库操作工具集，通过 GitHub API + Python 脚本实现自动化操作。

## 快速开始

### 场景 1：本地 Git 操作

```bash
# 查看工作区状态
py -3 <skill>/scripts/github_ops.py status

# 查看文件差异
py -3 <skill>/scripts/github_ops.py diff

# 查看暂存区差异
py -3 <skill>/scripts/github_ops.py diff-staged

# 提交文件
py -3 <skill>/scripts/github_ops.py commit . "feat: description"

# 撤销文件修改
py -3 <skill>/scripts/github_ops.py discard path/to/file

# 查看分支差异
py -3 <skill>/scripts/github_ops.py branch-log dev master

# 推送分支
py -3 <skill>/scripts/github_ops.py push dev
```

### 场景 2：创建 Issue

```bash
# 方法1：使用文件路径
echo "【优化建议】标题" > title.txt
echo "Issue 内容..." > body.txt
py -3 <skill>/scripts/github_ops.py create-issue owner repo title.txt body.txt

# 方法2：直接传入文本
py -3 <skill>/scripts/github_ops.py create-issue owner repo "标题" "内容"
```

### 场景 3：回复 Issue

```python
# 1. 查看 issue 上下文
py -3 <skill>/scripts/github_ops.py get-issue owner repo 63

# 2. 查看已有评论
py -3 <skill>/scripts/github_ops.py get-comments owner repo 63

# 3. 回复 issue（创建临时 body 文件）
echo "感谢你的提议！" > body.txt
py -3 <skill>/scripts/github_ops.py reply-issue owner repo 63 body.txt
```

### 场景 4：创建 PR

```bash
# 1. 查看分支差异
py -3 <skill>/scripts/github_ops.py branch-log dev master

# 2. 创建 PR
py -3 <skill>/scripts/github_ops.py create-pr owner repo title.txt body.txt dev master
```

### 场景 5：推送并创建 PR

```bash
# 1. 提交并推送
py -3 <skill>/scripts/github_ops.py commit . "feat: description" "file1.txt file2.txt"
py -3 <skill>/scripts/github_ops.py push dev

# 2. 创建 PR
py -3 <skill>/scripts/github_ops.py create-pr owner repo title.txt body.txt dev master
```

## 工作流程

### 本地 Git 操作流程

```
Phase 1   查看状态
   1.1  git status（查看改动）
   1.2  git diff（查看具体变更）
   │
   ▼
[Checkpoint Scope]  ← 确认改动范围
   │
   ▼
Phase 2   决策
   2.1  提交（commit）
   2.2  撤销（discard）
   2.3  继续其他操作
   │
   ▼
Phase 3   执行并推送
   3.1  git push origin <branch>
   3.2  创建 PR（如需）
```

### GitHub Issue 操作流程

```
[Checkpoint Before]  ← 操作前确认
   │
   ▼
Phase 1   准备
   1.1  确定操作类型（issue/pr/其他）
   1.2  收集必要信息（owner/repo/number/branch）
   1.3  准备 body 内容（如需）
   │
   ▼
[Checkpoint Confirm]  ← 确认操作意图
   │
   ▼
Phase 2   执行
   2.1  GitHub API 操作（Python 脚本）
   2.2  Git 分支操作（如需 gh CLI）
   │
   ▼
Phase 3   验证
   3.1  检查 API 响应状态
   3.2  确认 URL 返回正确
   3.3  清理临时文件
```

## 自检清单

### Git 操作自检
- [ ] 状态查看正确（status）
- [ ] 差异分析完整（diff/diff-staged）
- [ ] 提交信息规范（Conventional Commits）
- [ ] 推送成功确认

### GitHub 操作自检
- [ ] Token 有效（Status 200/201）
- [ ] Owner/Repo/Number 参数正确
- [ ] Body 内容非空
- [ ] 临时文件已清理
- [ ] 操作结果已汇报（URL/状态）

## 约束与规范

| 规范 | 说明 |
|---|---|
| Windows 环境 | 使用 `py -3` 执行 Python 脚本，避免 curl 兼容性问题 |
| Token 配置 | 通过环境变量 `GITHUB_TOKEN` 设置，不存储在代码中 |
| API 版本 | v3 (`application/vnd.github.v3+json`) |

## 相关资源

| 文件 | 用途 |
|---|---|
| [references/API-REFERENCE.md] | GitHub API 详细文档 |
| [references/WORKFLOW-GUIDE.md] | 场景化工作流指南 |
| [scripts/github_ops.py] | Python 操作脚本 |