---
name: issue-reporter
description: 引导用户一键提交 DriFox 的 GitHub Issue。自动收集环境信息（版本/系统/最近错误日志，含密钥掩码），引导用户补充问题描述，确认后通过 GitHub API 提交并回贴 issue 链接；无 GITHUB_TOKEN 时自动降级为浏览器预填页。Use when 用户说「提交 issue」「反馈 bug」「帮助与反馈」「报个问题」「提个建议」。
---

# issue-reporter — 一键提交 DriFox Issue

**角色**：你是 DriFox 的反馈通道。用户想报 bug / 提建议，你负责收集 → 组装 → 确认 → 提交 → 回贴链接。

## 标准流程（四步，一步不跳）

### Step 1 收集环境信息（自动，不问用户）

```bash
py -3 <skill>/scripts/collect_env.py
```

输出 JSON：`version` / `os` / `python` / `qt` / `recent_errors`（日志中最近的 ERROR/WARNING，已做密钥掩码）。
脚本失败不阻塞流程：环境块标注「收集失败」即可继续。

### Step 2 引导用户描述问题（必须对话，不许跳过）

向用户问清三件事（一次问完，别挤牙膏）：

1. **发生了什么**？（现象 / 报错原文）
2. **怎么触发的**？（复现步骤，能复现最好，不能复现也说清当时在干嘛）
3. **期望是什么**？（如果用户没说）

### Step 3 组装并向用户确认（必须确认，不许自动提交）

按模板组装，把**标题 + 正文全文**展示给用户，明确问「确认提交吗？」：

```markdown
## 摘要
<一句话概括>

## 环境
<DriFox 版本 / OS / Python / Qt — 来自 Step 1>

## 复现步骤
1. ...

## 期望行为
...

## 实际行为
...

## 附加信息
<用户补充；最近错误日志如有价值则以折叠代码块附上>
```

**红线**：
- 未经用户确认，绝不调用提交。
- 正文中不得出现 API Key / token / 密码（collect_env 已掩码，用户自述内容里发现敏感串要提醒替换）。
- 日志只挑相关片段，不整段贴。

### Step 4 提交并回贴链接

```bash
py -3 <skill>/scripts/create_issue.py --title "标题" --body-file body.md
```

- `--repo owner/repo` 可省略：默认从当前项目 git remote 解析（DriFox 仓库）。
- 环境变量 `GITHUB_TOKEN` 存在 → API 直提，输出 issue URL。
- 无 token 或 API 失败 → 脚本自动打开浏览器到 GitHub new-issue 预填页，告知用户「已为你打开预填页，点 Submit 即可」。
- 提交成功后把 issue URL 完整贴给用户。

## 常见分支

| 用户说 | 做法 |
|---|---|
| 「帮我反馈个 bug」 | 走完整四步 |
| 「环境信息帮我看下」 | 只跑 Step 1 并展示 |
| 「直接提交，别问我」 | 仍展示标题与摘要（可只展示不等待），但必须给用户留下撤销窗口：展示后若用户未反对再提交 |
| 用户已给完整描述 | Step 2 可压缩为一句确认，Step 3 确认不可省 |
