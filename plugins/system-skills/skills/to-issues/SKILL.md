---
name: to-issues
description: 自动分析代码问题并提交规范化的 GitHub Issues。遵循统一格式（Summary/Environment/Description/Impact/Steps/Expected/Actual/Proposed Fix），末尾自动添加智能体生成标记。无需用户交互，直接提交。
---

# to-issues

**快速将分析结果提交为 GitHub Issues。无需用户交互，自动完成。**

## 核心能力

1. **智能分析** - 分析代码发现问题并描述
2. **自动提交** - 使用项目配置的 token 和 repo 直接提交
3. **支持多种输入**:
   - `@to-issues 分析XXX` - 分析并提交
   - `@to-issues #29` - 获取已有 issue 并补充分析
   - `@to-issues fix:xxx` - 提交 bug fix issue
   - `@to-issues feat:xxx` - 提交功能增强 issue

## 自动推断配置

技能会按以下顺序获取配置：

1. **GitHub Token** (优先级):
   - 从长期记忆 `minimax apikey` 或 `github token` 中读取
   - 或从项目 `.drifox/app.config` 中读取
   - 或从环境变量 `GITHUB_TOKEN` 获取

2. **仓库地址**:
   - 从项目 `.git/config` 中的 `remote.origin.url` 获取
   - 或从项目配置中读取

3. **仓库名称格式**: `owner/repo` (自动从 git URL 解析)

## 使用流程

### 模式 1: 快速分析提交 (推荐)

```
@to-issues 分析当前项目的内存泄漏问题
```

**执行步骤:**
1. 扫描项目代码
2. 识别问题点
3. 生成 issue 描述
4. 直接提交到 GitHub

### 模式 2: Bug 报告

```
@to-issues fix: BackgroundTaskManager 线程安全问题
```

**自动生成:**
- 标题: `[Bug] BackgroundTaskManager 线程安全问题`
- 标签: `bug, high-priority`
- 模板: 包含问题描述、复现步骤、预期行为

### 模式 3: 功能增强

```
@to-issues feat: 添加上下文压缩可视化
```

**自动生成:**
- 标题: `[Enhancement] 添加上下文压缩可视化`
- 标签: `enhancement`
- 模板: 包含功能描述、验收标准、技术方案

### 模式 4: 代码审查issue (参考已有issue)

```
@to-issues 参考 #29 分析代码并提交新issue
```

## 提交脚本模板

技能内置提交脚本，可在项目根目录执行：

```python
#!/usr/bin/env python3
"""
快速提交 issue 到 GitHub
"""
import requests
import json
import sys
import re

# ========== 配置 (自动获取) ==========
TOKEN = ''  # 将在运行时从以下来源获取
REPO = ''   # 将在运行时从 git config 获取

def get_config():
    """自动获取配置"""
    config = {}
    
    # 1. 从环境变量
    config['token'] = os.environ.get('GITHUB_TOKEN', '')
    config['repo'] = os.environ.get('GITHUB_REPO', '')
    
    # 2. 从 .drifox/app.config
    config_file = Path('.drifox/app.config')
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                config['token'] = config.get('token') or data.get('github_token', '')
                config['repo'] = config.get('repo') or data.get('github_repo', '')
        except:
            pass
    
    # 3. 从 .git/config 解析 repo
    git_config = Path('.git/config')
    if git_config.exists() and not config.get('repo'):
        with open(git_config, 'r', encoding='utf-8') as f:
            content = f.read()
            match = re.search(r'git@github\.com:([^/]+)/([^.]+)\.git', content)
            if match:
                config['repo'] = f"{match.group(1)}/{match.group(2)}"
    
    # 4. 从长期记忆读取 token
    # 注意: 需要在调用时传入 token
    
    return config

def submit_issue(title, body, labels=None):
    """提交单个 issue"""
    config = get_config()
    if not config.get('token'):
        print("❌ 未找到 GitHub Token")
        return None
    
    repo = config.get('repo') or 'martin98-afk/DriFox'  # 默认值
    
    headers = {
        'Authorization': f"token {config['token']}",
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json'
    }
    
    data = {
        'title': title,
        'body': body,
        'labels': labels or []
    }
    
    try:
        resp = requests.post(
            f'https://api.github.com/repos/{repo}/issues',
            headers=headers,
            json=data,
            timeout=15
        )
        
        if resp.status_code == 201:
            result = resp.json()
            print(f"✅ Created #{result['number']}: {result['html_url']}")
            return result
        elif resp.status_code == 403:
            # 尝试无 labels 重试
            data['labels'] = []
            resp = requests.post(
                f'https://api.github.com/repos/{repo}/issues',
                headers=headers,
                json=data,
                timeout=15
            )
            if resp.status_code == 201:
                result = resp.json()
                print(f"✅ Created #{result['number']} (no labels): {result['html_url']}")
                return result
            print(f"❌ 403: {resp.text[:200]}")
        else:
            print(f"❌ Failed: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Error: {e}")
    
    return None

def batch_submit(issues):
    """批量提交 issues"""
    results = []
    for issue in issues:
        print(f"\n📝 提交: {issue['title']}")
        result = submit_issue(
            title=issue['title'],
            body=issue['body'],
            labels=issue.get('labels', [])
        )
        results.append(result)
        if result:
            time.sleep(0.5)  # 避免限流
    return results

if __name__ == '__main__':
    # 示例: 从命令行参数获取 issues
    issues_json = sys.argv[1] if len(sys.argv) > 1 else '[]'
    issues = json.loads(issues_json)
    batch_submit(issues)
```

## Issue 提交格式标准

所有通过本技能提交的 Issue 必须遵循以下格式规范：

### 标题格式
```
[问题类型] 简洁描述（不超过80字符）
```
- 类型: `Bug`, `Enhancement`, `Performance`, `Refactoring`, `Documentation`
- 描述: 清晰表达问题，避免缩写

### Body 格式（参考 #51, #30）

```markdown
## Summary
一句话描述问题和影响

## Environment
- Python 版本
- DriFox 版本
- 涉及文件: xxx, xxx

## Description
### 问题 1: [标题]
[详细描述]

```python
# 问题代码（可选）
```

### 问题 2: [标题]（如有多个问题）
[详细描述]

## Impact
1. [影响1]
2. [影响2]

## Steps to Reproduce（如适用）
1. [步骤1]
2. [步骤2]

## Expected Behavior
[期望的正确行为]

## Actual Behavior
[实际发生的错误行为]

## Proposed Fix（如有）
1. [修复建议1]
2. [修复建议2]

---

> 🤖 DriFox 智能体自动生成

## 快速执行示例

### 分析并提交单个问题
```python
submit_issue(
    title="[Bug] 虚拟滚动内存泄漏",
    body="""## Summary

长会话后内存持续增长，消息批次数据未被清理。

## Environment
- Python: 3.x
- DriFox 版本: 最新
- 涉及文件: `app/main_widget.py`

## Description

### 问题: _message_batch 未清理
虚拟滚动回收消息卡片时，对应的批次数据仍保留在内存中。

## Impact

1. 长时间运行内存持续增长
2. 可能导致 OOM 错误

## Steps to Reproduce

1. 启动 DriFox 进行长时间对话
2. 观察内存占用持续增长

## Expected Behavior

回收的消息批次数据也被清理，长时间运行内存稳定。

## Actual Behavior

内存持续增长，未释放已回收消息的数据。

---

> 🤖 DriFox 智能体自动生成
""",
    labels=["bug", "performance"]
)
```

### 批量提交
```python
issues = [
    {"title": "[Bug] Hook重复触发", "body": "...", "labels": ["bug"]},
    {"title": "[Enhancement] 权限缓存失效", "body": "...", "labels": ["enhancement"]},
]
batch_submit(issues)
```

## 错误处理

| 错误码 | 处理方式 |
|--------|----------|
| 401 | 提示 Token 无效，建议检查配置 |
| 403 | 移除 labels 后重试，或提示权限不足 |
| 404 | 提示仓库不存在，检查 repo 配置 |
| 422 | 提示请求格式错误，检查 issue 内容 |
| 网络错误 | 自动重试 2 次，间隔 2 秒 |

## 标签推荐

| 场景 | 标签 |
|------|------|
| Bug 修复 | `bug` |
| 功能增强 | `enhancement` |
| 性能优化 | `performance` |
| 代码重构 | `refactoring` |
| 文档改进 | `documentation` |
| 测试相关 | `tests` |
| 高优先级 | `high-priority` |
| 需要审查 | `needs-review` |
| 讨论中 | `discussion` |

## 最佳实践

1. **标题格式**: `[类型] 简洁描述` (如 `[Bug] 内存泄漏`)
2. **一个 issue 一个问题**: 便于追踪和管理
3. **包含验收标准**: 让实现者清楚知道完成条件
4. **添加相关标签**: 便于筛选和优先级排序
5. **引用相关 issue**: 使用 `#number` 关联相关问题

## 与其他技能配合

- `@brainstorming` 后 → 用 `@to-issues` 拆分为可执行任务
- `@diagnose` 后 → 用 `@to-issues` 提交发现的问题
- 代码审查后 → 直接用 `@to-issues` 提交改进建议