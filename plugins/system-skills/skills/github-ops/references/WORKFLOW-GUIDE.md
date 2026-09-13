# GitHub 操作场景化指南

## 场景 1：回复 GitHub Issue

### 适用场景
- 用户提到"回复 Issue"、"回复 GitHub"、"评论 Issue"
- 讨论新功能后需要跟进

### 操作步骤

```
Step 1: 理解上下文
   ├─ 查看 Issue 详情（get-issue）
   ├─ 查看已有评论（get-comments）
   └─ 确认回复策略

Step 2: 准备回复内容
   ├─ 草拟回复内容
   ├─ 询问用户确认（如内容复杂）
   └─ 创建临时 body 文件

Step 3: 执行回复
   ├─ 调用 reply-issue
   └─ 检查 Status 201

Step 4: 确认并清理
   ├─ 显示评论 URL
   └─ 删除临时文件
```

### 示例对话

```
用户：帮我回复 GitHub Issue #63
助手：
  1. 查看 Issue #63 详情...
  2. 查看已有评论...
  3. 准备回复内容
  4. 是否确认发送？
用户：确认
助手：调用 API → ✓ 评论已发布: https://github.com/.../issues/63#issuecomment-xxx
```

---

## 场景 2：创建 Pull Request

### 适用场景
- 用户提到"创建 PR"、"提交 PR"
- dev 分支开发完成后合并到 master

### 操作步骤

```
Step 1: 检查分支状态
   ├─ git status --porcelain（查看改动）
   ├─ git log master..dev（查看差异）
   └─ 确认目标分支

Step 2: 确认提交范围
   ├─ 显示改动文件
   ├─ 询问用户确认
   └─ [Checkpoint Confirm]

Step 3: 创建 PR
   ├─ 准备 title.txt
   ├─ 准备 body.txt（PR 描述）
   └─ 调用 create-pr

Step 4: 返回结果
   ├─ 显示 PR URL
   └─ 清理临时文件
```

### 示例 PR Body 模板

```markdown
## Summary

简要说明本次 PR 的目的和主要变更。

## Changes

### 新增
- [功能点 1]

### 修改
- [修改点 1]

### 修复
- [修复点 1]

## Testing

- [ ] 测试通过

## Related Issues

Closes #XXX
```

---

## 场景 3：推送并创建 PR（一站式）

### 适用场景
- 用户提到"推送并创建 PR"、"提交代码并创建 PR"
- 本地已完成开发，需要推送到远程并发起 PR

### 操作步骤

```
Step 1: 准备提交
   ├─ git status（查看改动）
   ├─ git diff（查看具体变更）
   └─ [Checkpoint] 确认提交内容

Step 2: 执行提交
   ├─ git add <files>
   ├─ git commit -m "<type>: <description>"
   └─ git push origin <branch>

Step 3: 创建 PR
   ├─ 获取分支差异
   ├─ 生成 PR 标题和描述
   ├─ 调用 create-pr
   └─ 显示 PR URL

Step 4: 清理
   └─ 移除临时文件
```

### 完整示例

```bash
# 1. 查看改动
git status --porcelain

# 2. 询问确认后提交
git add requirements.txt
git commit -m "chore(deps): add pypinyin dependency"

# 3. 推送
git push origin dev

# 4. 创建 PR
py -3 github_ops.py create-pr martin98-afk DriFox title.txt body.txt dev master
```

---

## 场景 4：查询 Issue/PR 状态

### 适用场景
- 用户提到"查看 issue 状态"、"查看 PR"
- 需要了解某个 Issue 或 PR 的当前状态

### 操作步骤

```
Step 1: 确定查询目标
   ├─ Issue 编号
   └─ PR 编号

Step 2: 调用 API
   ├─ get-issue：获取 Issue 详情
   └─ 使用 gh CLI 作为备选

Step 3: 整理输出
   ├─ 显示标题、状态、作者
   ├─ 显示 URL
   └─ 显示评论数/更新时间
```

### 示例输出

```
Title: 功能建议：添加 xxx
State: Open
Author: xxx
URL: https://github.com/.../issues/63
Comments: 2
Updated: 2026-05-18
```

---

## 常见问题处理

### 问题 1：gh CLI 不可用

**症状**：`gh: 无法识别命令`

**解决方案**：使用 Python requests 直接调用 GitHub API

```python
# 替代方案
import requests
requests.post('https://api.github.com/...', headers=HEADERS, json=data)
```

### 问题 2：Token 无效

**症状**：`401 Unauthorized`

**解决方案**：检查 Token 是否正确，或刷新 Token

```python
# 检查 Token 有效性
requests.get('https://api.github.com/user', headers=HEADERS)
```

### 问题 3：分支不存在

**症状**：`422 Unprocessable Entity`

**解决方案**：确保分支已推送到远程

```bash
git push -u origin <branch>
```