---
description: 项目笔记初始化
type: prompt
argument-hint:
   "[--refine]": 是否在原笔记基础上进行完善
   "[--limit]": 限定最大字数
---
请分析此代码库并编写项目笔记，包含以下内容：
1. 构建/lint/测试命令 - 特别是运行单个测试的方法
2. 代码风格规范，包括导入、格式化、类型、命名约定、错误处理等

你创建的文件将被提供给在此仓库中操作的 AI 编码智能体（如你自己）。内容约 150 行。
如果有 Cursor 规则（在 .cursor/rules/ 或 .cursorrules 中）或 Copilot 规则（在 .github/copilot-instructions.md 中），请确保包含它们。

如果已有项目笔记，请改进它。