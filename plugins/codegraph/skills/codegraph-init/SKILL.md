---
name: codegraph-init
description: Initialize CodeGraph index for the current project. Use when codegraph_explore tool reports "project isn't indexed with codegraph" or when no .codegraph/ directory exists. Scans all project files, builds symbol index with call/reference edges, and enables precise code exploration. Also add .codegraph/ to .gitignore if missing.
---

# CodeGraph 初始化

为当前项目初始化 CodeGraph 符号索引，开启精准代码探索能力。

## 核心流程

### 1. 确认项目根目录
- 工作目录通常是项目根目录
- 如果知道项目路径，直接使用；否则询问用户确认

### 2. 检查是否已初始化
```bash
# 检查 .codegraph/ 目录是否存在
ls <project-root>/.codegraph/
```
如果已存在，告知用户 CodeGraph 已就绪，无需重复初始化。

### 3. 运行初始化
```bash
npx @colbymchenry/codegraph init <project-root>
```
> 此命令会扫描项目文件，解析代码符号，构建 7,000+ 节点和 20,000+ 引用的知识图谱。
> 耗时通常在 10-30 秒，取决于项目大小。

### 4. 更新 .gitignore
检查项目 `.gitignore` 是否包含 `.codegraph/`，如果没有则添加：
```gitignore
# CodeGraph 索引（自动生成，不提交）
.codegraph/
```

### 5. 验证索引状态
```bash
npx @colbymchenry/codegraph status <project-root>
```
确认索引已成功创建，统计信息正常。

### 6. 通知用户
告知用户 CodeGraph 索引已创建，包含以下能力：
- **符号查找**: 用自然语言或符号名快速查找类、函数、变量的定义和引用
- **调用链分析**: 查看函数调用关系（caller/callee）
- **影响范围**: 修改代码前查看受影响的上下游
- **源码直览**: 探索结果直接内联显示源码

## 注意事项

- **只需初始化一次**，后续所有会话自动生效。代码变更后，CodeGraph 的 MCP 服务器会自动同步
- 索引是项目本地文件（`.codegraph/`），不会影响代码运行
- 大型项目初始化可能稍慢，耐心等待完成
- 如果 `npx` 命令失败，先确保 Node.js 已安装
