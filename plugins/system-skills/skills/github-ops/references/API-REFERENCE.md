# GitHub + Git 操作参考

## 本地 Git 命令

### 查看状态

```bash
git status --porcelain
# M  file.txt        (修改)
# A  new.txt         (新增)
# D  deleted.txt     (删除)
# ?? untracked.txt   (未跟踪)
```

### 查看差异

```bash
# 工作区 vs 暂存区
git diff

# 暂存区 vs 最新提交
git diff --staged

# 查看特定文件
git diff path/to/file
```

### 提交变更

```bash
# 暂存并提交
git add <files> && git commit -m "type: description"

# 常用 type:
# feat, fix, docs, style, refactor, perf, test, build, chore, revert
```

### 撤销修改

```bash
# 撤销工作区修改（恢复为暂存区/HEAD）
git checkout -- <file>

# 撤销暂存
git reset HEAD <file>

# 删除未跟踪文件
rm <file>
```

### 分支操作

```bash
# 查看分支差异
git log master..dev --oneline

# 查看所有分支
git branch -a

# 推送分支
git push -u origin dev
```

---

## GitHub API 参考

## 认证配置

```python
import os

# 从环境变量获取 Token（推荐）
TOKEN = os.environ.get('GITHUB_TOKEN')
if not TOKEN:
    raise ValueError("Please set GITHUB_TOKEN environment variable")

HEADERS = {
    'Authorization': f'token {TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
    'Content-Type': 'application/json'
}
BASE_URL = 'https://api.github.com'
```

### 设置 Token（Windows）

```bash
set GITHUB_TOKEN=your_token_here
```

### 设置 Token（Linux/Mac）

```bash
export GITHUB_TOKEN=your_token_here
```

## Issue 相关 API

### 获取 Issue 详情

```python
GET /repos/{owner}/{repo}/issues/{issue_number}

response = requests.get(
    f'{BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}',
    headers=HEADERS
)
issue = response.json()
# title, state, user, body, comments, html_url
```

### 获取 Issue 评论

```python
GET /repos/{owner}/{repo}/issues/{issue_number}/comments

response = requests.get(
    f'{BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments',
    headers=HEADERS
)
comments = response.json()
# [{id, user, body, created_at}, ...]
```

### 回复 Issue

```python
POST /repos/{owner}/{repo}/issues/{issue_number}/comments

data = {'body': '评论内容'}
response = requests.post(
    f'{BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments',
    headers=HEADERS,
    json=data
)
# Status 201 = 成功
```

## PR 相关 API

### 创建 PR

```python
POST /repos/{owner}/{repo}/pulls

data = {
    'title': 'PR 标题',
    'body': 'PR 描述（支持 Markdown）',
    'head': '源分支',
    'base': '目标分支'
}
response = requests.post(
    f'{BASE_URL}/repos/{owner}/{repo}/pulls',
    headers=HEADERS,
    json=data
)
# Status 201 = 成功, html_url
```

### 获取 PR 详情

```python
GET /repos/{owner}/{repo}/pulls/{pull_number}

response = requests.get(
    f'{BASE_URL}/repos/{owner}/{repo}/pulls/{pull_number}',
    headers=HEADERS
)
```

### 查看 PR 状态

```python
GET /repos/{owner}/{repo}/pulls

response = requests.get(
    f'{BASE_URL}/repos/{owner}/{repo}/pulls',
    headers=HEADERS,
    params={'state': 'open'}
)
```

## 仓库信息

### 查看远程仓库

```bash
git remote -v
# origin  https://github.com/{owner}/{repo}.git
```

### 查看分支差异

```bash
git log {target}..{source} --oneline
# 显示 source 领先于 target 的 commits
```

## 响应状态码

| 状态码 | 含义 |
|---|---|
| 200 | GET 成功 |
| 201 | POST 成功（创建资源） |
| 400 | 请求参数错误 |
| 401 | Token 无效或过期 |
| 404 | 资源不存在 |
| 422 | 验证失败（如分支不存在） |

## 错误处理

```python
if response.status_code >= 400:
    error = response.json()
    print(f"Error {response.status_code}: {error.get('message', 'Unknown')}")
```

## 常用工具

### gh CLI（备选）

```bash
# 查看当前 PR
gh pr view

# 创建 PR
gh pr create --base master --head dev --title "title"

# 注意：Windows 环境可能需要额外安装 gh CLI
```