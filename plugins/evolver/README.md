# Evolver 自进化引擎 - DriFox 插件

将 [Evolver](https://github.com/EvoMap/evolver)（⭐ 8.7k）自进化引擎集成到 DriFox，让 AI Agent 通过捕获会话经验实现持续自我进化。

## 功能

| 功能 | 说明 |
|------|------|
| 🤖 **自动日志采集** | Hook 系统自动捕获工具调用和 AI 回复到 `memory/` |
| 🧬 **自进化循环** | 每 10 轮自动触发 Evolver 扫描，生成 GEP 进化 prompt |
| 🔌 **手动触发** | `/evolver --run` 随时运行进化循环 |
| 👁️ **审查模式** | `/evolver --review` 人工确认后再应用进化 |
| 📊 **状态查看** | `/evolver --status` 查看 Evolver 可用性和日志统计 |

## 安装

### 1. 安装 Evolver CLI

```bash
npm install -g @evomap/evolver
```

验证安装：
```bash
evolver --version
```

### 2. 启用插件

插件位于 `plugins/evolver/`，DriFox 启动时自动发现。在设置中确保 `evolver` 插件已启用。

### 3. 验证运行

```bash
/evolver --status
```

看到 `evolver_installed: true` 即表示就绪。

## 工作原理

```
DriFox 会话
  │  (Hook: PostToolUse / PostAssistantMessage)
  ▼
memory/*.jsonl           ← 自动记录日志
  │  (手动: /evolver --run 或 自动: 每10轮)
  ▼
Evolver CLI (GEP 协议)
  │
  ├─► 扫描 signals
  ├─► 匹配 Gene/Capsule
  ├─► 输出 GEP prompt
  └─► 记录 EvolutionEvent
```

## 目录结构

```
plugins/evolver/
├── .drifox-plugin/plugin.json    ← 插件清单
├── hooks/
│   ├── hooks.json                ← 4 个 Hook 定义
│   └── evolver_hook.py           ← Hook 处理脚本
├── commands/
│   └── evolver.md                ← /evolver 命令（6 个子命令）
├── skills/
│   └── evolver/SKILL.md          ← AI 技能文档
└── README.md                     ← 本文件
```

## Hook 事件

| DriFox 事件 | 触发 Hook | 动作 |
|-------------|-----------|------|
| `SessionStart` | 会话创建 | 检查 evolver，创建 memory/ |
| `PostToolUse` (write/edit/bash/multi_edit) | 工具执行后 | 记录到 `memory/tools_*.jsonl` |
| `PostAssistantMessage` | AI 回复后 | 记录到 `memory/assistant_*.jsonl`，每 10 轮触发进化 |

## 命令

| 子命令 | 功能 |
|--------|------|
| `/evolver --status` | 查看状态 |
| `/evolver --run` | 单次进化 |
| `/evolver --review` | 审查模式 |
| `/evolver --loop` | 持续进化守护 |
| `/evolver --install` | 安装 CLI |
| `/evolver --log` | 查看日志 |

## 配置

通过环境变量定制进化策略（推荐在 `.env` 中设置）：

```bash
EVOLVE_STRATEGY=balanced     # 日常（默认）
EVOLVE_STRATEGY=innovate     # 创新优先
EVOLVE_STRATEGY=harden       # 稳定性优先
EVOLVE_STRATEGY=repair-only  # 紧急修复
```

（可选）连接 EvoMap Hub 获取网络功能：
```bash
A2A_HUB_URL=https://evomap.ai
A2A_NODE_ID=your_node_id
```

## 参考

- [Evolver GitHub](https://github.com/EvoMap/evolver)
- [Evolver 论文 (arXiv:2604.15097)](https://arxiv.org/abs/2604.15097)
- [EvoMap 官网](https://evomap.ai)
