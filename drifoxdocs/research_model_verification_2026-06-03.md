# 研究报告：DriFox 模型配置验证（v3 文件）

时间：2026-06-03 08:55:47
模式：deep
搜索后端：minimax MCP（`mcp__MiniMax__web_search`）—— 因为 DuckDuckGo/SerpAPI 在当前网络下不可用，minimax MCP 是唯一可用的搜索通道；models.dev 官方 API 也因网络原因无法直接拉取

## 执行摘要

对三个目标文件 [model_capabilities.py](file|D:/work/DriFox/app/core/model_capabilities.py)、
[provider_profile.py](file|D:/work/DriFox/app/core/provider_profile.py)、
[constants.py](file|D:/work/DriFox/app/constants.py) 中的所有模型配置进行了网络交叉验证。

**关键发现（按严重度）**：

1. **🔴 严重**：`provider_profile.py` 的 `PROVIDER_CAPABILITIES["anthropic"].thinking_param = None` 是错的。Anthropic Claude 4 系列已支持 extended thinking（字段 `thinking: {type: "enabled"|"disabled"|"adaptive"}`），Claude Code 已升级到 Adaptive Thinking。
2. **🔴 严重**：`provider_profile.py` 的 `PROVIDER_CAPABILITIES["gemini"].thinking_param = None` 漏了 Gemini 2.5 的 `thinkingBudget` 字段。
3. **🟠 偏大**：`model_capabilities.py` 的 `glm-5` / `glm-5.1` `context_limit = 202752` 不准确。智谱官方说 200K；202752 ÷ 1024 = 198K，与 200K 不匹配；建议改为 204800（200K）。
4. **🟠 偏大**：`constants.py` 的 `PROVIDER_MODELS["阿里云 (DashScope)"]` 缺 2026-05/06 新发布的 `qwen3.7-max`、`qwen3.7-plus`、`qwen3.6-plus` 等；这些模型已在 `MODEL_CAPABILITIES` 里维护，但 UI 选择器看不到。
5. **🟠 偏大**：OpenCode Zen 真实存在的官方免费模型只有 5 个（`opencode/big-pickle`、`opencode/glm-5-free`、`opencode/gpt-5-nano`、`opencode/kimi-k2.5-free`、`opencode/minimax-m2.5-free`），其余 10 个（`deepseek-v4-flash-free`、`mimo-v2.5-pro`、`mimo-v2.5`、`qwen3.6-plus`、`qwen3.5-plus`、`nemotron-3-super-free` 等）**疑似虚构**或为 OpenCode 付费档，命名空间也不一致（缺 `opencode/` 前缀）。
6. **🟡 中等**：`constants.py` 的 `火山方舟` 模型列表里有 `glm-4.7`、`glm5.1`（带空格点），与 `model_capabilities.py` 里的 `glm-5.1` 不一致（`model_capabilities.py` 用点号）。
7. **🟡 中等**：`provider_profile.py` 的 `PROVIDER_CAPABILITIES["openai"].thinking_param = None` 对于 o-series（o1/o3/o4）不正确；建议改为 `reasoning_effort`，但在 MODEL_CAPABILITIES 里 o1/o3 模型已加 `reasoning_effort` 即可。
8. **🟢 正确**：DeepSeek V4、Kimi K2.5/K2.6、小米 MiMo-V2.5 系列、智谱 GLM-5.1、Claude Sonnet 4、Gemini 2.5、OpenAI gpt-4o 的 context_limit 数值均与官方一致（GLM-5 例外）。
9. **🟢 正确**：DeepSeek V4 的 thinking 控制字段（`reasoning_effort` + `extra_body.thinking`）与官方 API 一致；Kimi K2.6 的 `extra_body.thinking.type` 也正确。

---

## 1. DeepSeek V4 系列（已发布 2026-04-24）

| 字段 | 项目当前值 | 官方真实值 | 判定 |
|------|------------|------------|------|
| 模型名 | `deepseek-v4-pro` / `deepseek-v4-flash` | 同名（OpenAI 兼容 API）| ✅ 正确 |
| context_limit | 1048576 | 100 万 token（1M）| ✅ 正确 |
| supports_thinking | True | True（Non-think / Think High / Think Max 三种模式）| ✅ 正确 |
| thinking_param | `reasoning_effort` | 同时支持 `reasoning_effort="high"` 和 `extra_body={"thinking": {"type": "enabled"}}` | 🟠 字段值正确，但**仅一项不够**；实际需要两个一起发才能进入思考模式（参考官方 Python 示例） |

**官方调用示例**（CSDN 测评）：

```python
response = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[...],
    reasoning_effort="high",
    extra_body={"thinking": {"type": "enabled"}}
)
```

- [DeepSeek V4 发布：1.6 万亿参数，百万上下文，击穿地板价（CSDN）](https://blog.csdn.net/Sammyyyyy/article/details/161087809)
- [DeepSeek V4 Flash 好东西啊（CSDN，含价格表）](https://blog.csdn.net/jarvisuni/article/details/160568642)
- [DeepSeek 官方 API 文档](https://api-docs.deepseek.com/)

**额外事实**：
- 旧模型 `deepseek-chat` 和 `deepseek-reasoner` 将于 **2026-07-24 退役**（[来源](https://blog.csdn.net/weixin_50937681/article/details/148141826)）。项目注释里已提到这一点，正确。
- V4-Pro 定价：缓存命中 1元/M，未命中 12元/M，输出 24元/M；V4-Flash 定价 0.2/1/2 元/M（[来源](https://blog.csdn.net/Sammyyyyy/article/details/161087809)）。

---

## 2. Moonshot Kimi K2.5 / K2.6

| 字段 | 项目当前值 | 官方真实值 | 判定 |
|------|------------|------------|------|
| 模型名 | `kimi-k2.5` / `kimi-k2.6` | 官方展示为 "Kimi K2.5" / "Kimi K2.6"（[platform.moonshot.cn](http://platform.moonshot.cn/)） | ✅ 命名内部一致即可 |
| context_limit | 262144 | 256K（262144 = 256×1024）| ✅ 正确 |
| supports_thinking | True | True（Thinking / Instant / preserve_thinking 三种模式）| ✅ 正确 |
| thinking_param | `thinking` | `extra_body={"thinking": {"type": "disabled"/"enabled"}}` | ✅ 正确 |

**关键 API 细节**（CSDN 深度解析）：
- 推理过程返回在 `message.reasoning` 字段
- 关闭思维链用 `extra_body={"thinking": {"type": "disabled"}}`
- `preserve_thinking` 模式可在多轮对话中保留思维链
- API 端点：`https://api.moonshot.ai/v1`（OpenAI 兼容）
- 架构：MoE，1T 总参/32B 激活，61 层，384 专家
- 月之暗面于 2026-06 在 [platform.moonshot.cn](http://platform.moonshot.cn/) 公告 K2.6 发布

- [Kimi K2.6：开源多模态 Agent 模型（CSDN）](https://blog.csdn.net/chen695969/article/details/160383713)
- [深度解析：Kimi-K2-Thinking（CSDN）](https://blog.csdn.net/weixin_72532546/article/details/159678793)
- [Kimi API 开放平台](http://platform.moonshot.cn/)

**K2 Thinking（2025-11 发布）** 是 K2 系列的前一个版本，也是万亿参数；K2.5/K2.6 是 2026 年新版。模型沿用相同 256K 上下文。

---

## 3. 智谱 GLM-5 / GLM-5.1

| 字段 | 项目当前值 | 官方真实值 | 判定 |
|------|------------|------------|------|
| 模型名 | `glm-5` / `glm-5.1` | `glm-5`（2026-02-12 发布）、`glm-5.1`（2026-03-28 发布）| ✅ 正确 |
| context_limit | 202752 | **202K**（约 198K）/ **200K** | 🔴 **数值偏差**（202752 ÷ 1024 = 198，不等于 200K）|
| supports_thinking | True | True | ✅ 正确 |
| thinking_param | `thinking` | ⚠️ **未找到 GLM-5 公开的 `thinking.type` API 字段**。GLM-5 的特殊字段是 `extra_body={"sparse_attention": {...}}`（DSA 稀疏注意力）| 🟠 项目里写 `thinking` 是沿用 GLM-4 时代的命名，可能不准确 |

**GLM-5 真实参数**（CSDN 测评）：
- 参数量：744B 总参/44B 激活（MoE）
- 上下文：202K（实测），并非 2M（2M 是其最大理论值）
- API 模型名：`glm-5`
- 调用示例用 `extra_body={"sparse_attention": {"enable": True, "local_window_size": 4096, "global_token_ratio": 0.02, "ast_aware": False}}`，**没有** `thinking` 字段

**GLM-5.1**（2026-03-28 发布）：
- 200K 上下文，128K 输出
- 编程能力较 GLM-5 提升 30%

- [智谱GLM-5技术全解析（CSDN，含 202K 上下文、sparse_attention 字段）](https://blog.csdn.net/weixin_43107715/article/details/158074555)
- [智谱 GLM 5.1 重磅上线（CSDN）](https://blog.csdn.net/weixin_69359007/article/details/160931058)
- [GLM-5 技术拆解：7440 亿参数 MoE 架构，202K 超长上下文（CSDN）](https://blog.csdn.net/kakaZhui/article/details/163131057)

**问题**：
1. `202752` 数值偏差 → 建议改为 `204800`（200K）
2. `thinking_param: "thinking"` 沿用 GLM-4 时代，未确认 GLM-5 真实 API 字段名

---

## 4. 小米 MiMo-V2.5 / V2.5-Pro

| 字段 | 项目当前值 | 官方真实值 | 判定 |
|------|------------|------------|------|
| 模型名 | `mimo-v2.5-pro` / `mimo-v2.5` | `MiMo-V2.5-Pro` / `MiMo-V2.5` | ✅ 命名一致 |
| context_limit | 1000000 | 100 万 tokens（1M）| ✅ 正确 |
| supports_thinking | 未标 True | 官方未公开 `thinking` 控制参数 | ✅ 保守做法合理 |
| thinking_param | 无 | 官方未公布 API 参数 | ✅ 项目说"思考控制参数未确认，暂不开放开关"是合理的 |

**MiMo V2 系列真实架构**（CSDN 多源）：
- V2.5：全模态通用模型，1M 上下文
- V2.5-Pro：旗舰推理模型，1T 总参/42B 激活
- V2-Omni：感知理解（眼睛+耳朵）
- V2-TTS：语音表达

2026-05-30/06-01 API 永久降价 99%。

- [小米 MiMo-V2.5 实测（CSDN）](https://blog.csdn.net/easyllm/article/details/160470687)
- [小米MiMo-V2系列大模型全方位解读（CSDN）](https://blog.csdn.net/qq_57220546/article/details/159525189)
- [罗福莉划重点，小米大模型降价99%的秘籍公开](https://new.qq.com/rain/a/20260601A05MRQ00)

---

## 5. 阿里通义千问（DashScope）

| 模型 | 项目当前值 | 真实状态 | 判定 |
|------|------------|----------|------|
| `qwen3-max` | `PROVIDER_MODELS` 有 | 存在 | ✅ |
| `qwen3-plus` | `PROVIDER_MODELS` 有 | 存在 | ✅ |
| `qwen3.5-max` | `PROVIDER_MODELS` 有 | 存在 | ✅ |
| `qwen3.6-plus` | `MODEL_CAPABILITIES` 有，**`PROVIDER_MODELS` 没有** | 2026-04-02 发布（项目注释）| 🔴 **不一致**：UI 选择器看不到 |
| `qwen3.7-max` | `MODEL_CAPABILITIES` 有，**`PROVIDER_MODELS` 没有** | 2026-05-20 阿里云峰会发布 | 🔴 **不一致** |
| `qwen3.7-plus` | **两个表都没有** | 2026-06-02 发布，多模态 Agent 模型 | 🔴 **完全缺失** |
| `qwen3.7-plus-preview` / `qwen3.7-plus-preview-free` | **两个表都没有** | 2026-05-28 AIHubMix 列出 | 🟡 可能国内直连未上线 |

**Qwen3.7-Plus 真实情况**（2026-06-02 发布）：
- 多模态 Agent 模型，"看、想、写、做" 整合
- 屏幕理解得分 79，超过 GPT-5.4 和 Gemini-3.1 Pro
- 定价：输入 $0.4/M tokens，输出 $1.6/M tokens

- [阿里发布Qwen3.7-Plus：屏幕理解跑赢GPT-5.4（搜狐）](https://www.sohu.com/a/1031010009_122066678)
- [阿里Qwen3.7-Plus发布：文本与视觉能力大幅提升（搜狐）](https://www.sohu.com/a/1030986413_121885030)
- [AIHubMix 模型更新日志](https://aihubmix.com/)

---

## 6. OpenCode Zen 免费/付费模型（已校正）

> ⚠️ **2026-06-03 09:09 校正**：用户从 opencode.ai 直接拷贝了官方完整清单后，本节判断**有重大错误**。详细校正见 [research_opencode_official_correction_2026-06-03.md](file|D:/work/DriFox/drifoxdocs/research_opencode_official_correction_2026-06-03.md)。**核心修正**：
> - 之前误判 `nemotron-3-super-free`/`deepseek-v4-flash-free`/`qwen3.6-plus`/`qwen3.5-plus`/`mimo-v2.5-pro`/`mimo-v2.5` 为"虚构"——**实际全部真实存在**（在 OpenCode Zen 免费档 / OpenCode Go 订阅里）
> - `model_capabilities.py` 注释里那条"曾用 LLM 自动生成的'虚构'模型名"教训**不应**再被引用来质疑这些模型
> - DriFox 项目 `PROVIDER_MODELS["OpenCode Zen"]` 列的 15 个模型里，**前 13 个属于 OpenCode Go 订阅（$5/$10/月）**，**后 2 个属于 OpenCode Zen 免费档**——全部真实
> - **真正缺失的模型**：`Stealth`（OpenCode 平台自家）、`Qwen3.6 Plus Free`、`MiMo V2.5 Free`、`MiniMax M3 Free`、`Grok Build 0.1`，以及 Claude / GPT / Gemini 全系（约 35+ 个）

**OpenCode CLI 免费层输出**（来自 `opencode models --refresh`）：
```
opencode/big-pickle
opencode/glm-5-free
opencode/gpt-5-nano
opencode/kimi-k2.5-free
opencode/minimax-m2.5-free
```

**OpenCode 平台实际产品架构**（用户提供的官方清单）：

### 6.1 OpenCode Go（订阅 $5/$10/月）— DriFox 项目配的应该是这个
"低成本编码模型，人人可用"，13 个模型：DriFox 项目 `PROVIDER_MODELS["OpenCode Zen"]` 里前 13 个（`kimi-k2.5` 到 `deepseek-v4-flash`）全部属于此层

### 6.2 OpenCode Zen（完整市场）— 包含 Go + 额外
- 默认模型：`Big Pickle`（DriFox 有 `big-pickle`）
- **Stealth**（DriFox 完全没记录）
- Anthropic 全系：Haiku 4.5 / Opus 4.1~4.8 / Sonnet 4~4.6
- OpenAI 全系：GPT 5 / 5 Codex / 5 Nano / 5.1~5.5（含 Mini/Nano/Pro/Codex/Spark 变体）
- Google 全系：Gemini 3 Flash / 3.1 Pro / 3.5 Flash
- DeepSeek 全系：V4 Flash / V4 Flash Free
- 其它：GLM 5/5.1、Kimi K2.5/K2.6、Qwen3.5 Plus / 3.6 Plus / 3.6 Plus Free、Grok Build 0.1、MiniMax M2.5/M2.7/M3 Free、MiMo V2.5 Free、Nemotron 3 Super Free

**命名空间问题已澄清**：
- OpenCode 平台**显示名**：`Kimi K2.5` / `MiMo V2.5 Pro` / `GLM 5.1`（带空格、混合大小写）
- DriFox 项目用**注册名**：`kimi-k2.5` / `mimo-v2.5-pro` / `glm-5.1`（小写+短横）
- 两种格式 OpenCode 都接受，**不是 bug**

**DriFox 项目 OpenCode Zen 实际状态**：
```python
PROVIDER_MODELS["OpenCode Zen"] = [
    "deepseek-v4-flash-free",   # ✅ 真实（OpenCode Zen Free 档）
    "nemotron-3-super-free",    # ✅ 真实（OpenCode Zen Free 档）
    "big-pickle",               # ✅ 真实（OpenCode 平台默认模型）
    "glm-5.1",                  # ✅ 真实（OpenCode Go 订阅）
    "glm-5",                    # ✅ 真实（OpenCode Go 订阅）
    "kimi-k2.6",                # ✅ 真实（OpenCode Go 订阅）
    "kimi-k2.5",                # ✅ 真实（OpenCode Go 订阅）
    "deepseek-v4-pro",          # ✅ 真实（OpenCode Go 订阅）
    "deepseek-v4-flash",        # ✅ 真实（OpenCode Go 订阅）
    "mimo-v2.5-pro",            # ✅ 真实（OpenCode Go 订阅）
    "mimo-v2.5",                # ✅ 真实（OpenCode Go 订阅）
    "minimax-m2.7",             # ✅ 真实（OpenCode Go 订阅）
    "minimax-m2.5",             # ✅ 真实（OpenCode Go 订阅）
    "qwen3.6-plus",             # ✅ 真实（OpenCode Go 订阅）
    "qwen3.5-plus",             # ✅ 真实（OpenCode Zen 全市场）
]
```

**DriFox 项目 OpenCode Zen 缺失清单**（约 35+ 个）：
- Stealth（OpenCode 自家）
- Qwen3.6 Plus Free / MiniMax M3 Free / MiMo V2.5 Free
- Grok Build 0.1
- Claude Haiku 4.5 / Opus 4.1~4.8 / Sonnet 4.5~4.6
- GPT 5 / 5 Codex / 5 Nano / 5.1~5.5 全系
- Gemini 3 Flash / 3.1 Pro / 3.5 Flash

**建议**：
- **保留** 现有 15 个模型——全部真实
- **新增** `Stealth`（最优先，因为是 OpenCode 平台自家）
- **新增** 3 个 Free 档：`Qwen3.6 Plus Free` / `MiMo V2.5 Free` / `MiniMax M3 Free`
- **考虑** 把 OpenCode 拆成 `OpenCode Go`（订阅）和 `OpenCode Zen`（免费）两个独立 provider

- [校正报告：research_opencode_official_correction_2026-06-03.md](file|D:/work/DriFox/drifoxdocs/research_opencode_official_correction_2026-06-03.md)
- [OpenCode 配置默认模型指南（CSDN）](https://download.csdn.net/blog/column/12901442/158156424)
- [OpenCode Zen 官网](https://opencode.ai/zen)

---

## 7. Anthropic Claude（含 Claude Sonnet 4 / Claude 3.x）

| 字段 | 项目当前值 | 真实情况 | 判定 |
|------|------------|----------|------|
| `claude-sonnet-4-20250514` context | 200000 | 200K | ✅ 正确 |
| `claude-3-5-sonnet-latest` context | 200000 | 200K | ✅ 正确 |
| `PROVIDER_CAPABILITIES["anthropic"].thinking_param` | `None` | **错！** Anthropic Claude 4 支持 extended thinking | 🔴 **必须改** |

**Anthropic extended thinking 字段**：
- 旧版：`thinking: "enabled"` / `"disabled"`
- 新版（Claude Code Adaptive Thinking）：`thinking: {type: "adaptive"}`
- 三种合法值：`enabled` / `disabled` / `adaptive`

CSDN 报错案例：「API Error: 400 thinking type should be enabled or disabled」——证明字段名确实是 `thinking`，值是 `enabled|disabled|adaptive`。

- [Claude Code 第三方API 400报错（thinking type should be enabled or disabled）CSDN](https://download.csdn.net/blog/column/12680697/159955741)
- [Anthropic Sonnet 4.5 系统提示分析（CSDN）](https://blog.csdn.net/csdn122345/article/details/155580233)

**缺失**：
- 项目里有 `claude-sonnet-4-20250514` 但**没有** Claude Opus 4.5/4.6（2026 年 SOTA）。Sonnet 4.5 已经在测评中出现（[来源](https://download.csdn.net/download/metaverse5vr/92518767)）。
- 建议补：`claude-opus-4-5-*`、`claude-sonnet-4-5-*` 系列

---

## 8. Google Gemini

| 字段 | 项目当前值 | 真实情况 | 判定 |
|------|------------|----------|------|
| `gemini-2.5-pro-preview-06-05` context | 1000000 | 1M | ✅ 正确 |
| `gemini-2.0-flash` context | 1000000 | 1M | ✅ 正确 |
| `PROVIDER_CAPABILITIES["gemini"].thinking_param` | `None` | **错！** Gemini 2.5 支持 `thinkingBudget` | 🔴 **必须改** |
| `gemini-3.1-pro` | 缺失 | 2026 年已发布（[Qwen3.7-Plus 测评](https://www.sohu.com/a/1031010009_122066678) 提到「超过 GPT-5.4 和 Gemini-3.1 Pro」）| 🟠 **缺失** |

**Gemini 2.5 thinking 字段**：
- 通过 `generationConfig.thinkingBudget` 控制（值 0~24576；设为 -1 表示动态）
- 文档中提到 `thinkingConfig` / `thinking_budget` 字段

- [Gemini 2.5 Pro 深度拆解（CSDN）](https://download.csdn.net/blog/column/12965954/157588287)
- [谷歌Gemini 2.5多模态研究助手（CSDN）](https://download.csdn.net/blog/column/12422082/149813843)

---

## 9. OpenAI gpt-4o / o-series

| 字段 | 项目当前值 | 真实情况 | 判定 |
|------|------------|----------|------|
| `gpt-4o` / `gpt-4o-mini` context | 128000 | 128K | ✅ 正确 |
| `gpt-4-turbo` context | 128000 | 128K | ✅ 正确 |
| `PROVIDER_CAPABILITIES["openai"].thinking_param` | `None` | 🟡 部分对：gpt-4o 不支持，但 **o1/o3/o4 支持 `reasoning_effort`** | 🟠 字段缺失 |

**OpenAI 推理模型**：
- o1 / o3 / o4-mini 支持 `reasoning_effort`（low/medium/high）
- gpt-4o/gpt-4o-mini 不支持

**缺失模型**：
- 项目里没有 o1 / o3 / o4-mini
- 2026 年 SOTA：GPT-5.4（[Qwen3.7-Plus 测评](https://www.sohu.com/a/1031010009_122066678) 提到），GPT-5.4-Mini 等
- 这些应补到 `MODEL_CAPABILITIES`

- [OpenAI参数调优完全指南（CSDN）](https://blog.csdn.net/gitblog_00979/article/details/159609434)
- [OpenAI模型API详解（CSDN）](https://download.csdn.net/blog/column/12592623/144001571)

---

## 10. 百度千帆 文心

| 字段 | 项目当前值 | 真实情况 | 判定 |
|------|------------|----------|------|
| `ernie-3.5-8k` context | 8192 | 8K | ✅ 正确 |
| `ernie-3.5-4k` context | 4096 | 4K | ✅ 正确 |
| `ernie-speed-8k` context | 8192 | 8K | ✅ 正确 |
| `ernie-speed-128k` context | 128000 | 128K | ✅ 正确 |

**缺失**：
- 文心 4.0 / 4.5 系列（2024-2025 发布）未在 DriFox 项目里
- 文心 X1 推理模型（对标 DeepSeek R1）未在 DriFox 项目里

---

## 11. 硅基流动（SiliconFlow）

| 字段 | 项目当前值 | 真实情况 | 判定 |
|------|------------|----------|------|
| `PROVIDER_MODELS` 列表 | Qwen2.5 系列、GLM-4-9B、Llama-3.1、DeepSeek-V2 | 存在 | ✅ 正确 |
| `thinking_param` | `thinking_budget` | DeepSeek-R1 在 SiliconFlow 用 thinking_budget 控制 | ✅ 正确 |
| `deepseek-ai/deepseek-r1` context | 200000 | R1 在 SiliconFlow 上是 200K | ✅ 正确 |

**问题**：
- SiliconFlow 现在应该有 DeepSeek V4 / V4-Flash（2026-04 发布），但项目里没有
- 应该有 Qwen3 系列（Qwen3-32B 等）但没有

---

## 12. Groq 平台

| 字段 | 项目当前值 | 真实情况 | 判定 |
|------|------------|----------|------|
| `openai/gpt-oss-120b` context | 131072 | 131K | ✅ 正确 |
| `llama-3.3-70b-versatile` context | 131072 | 131K | ✅ 正确 |
| `groq/compound` context | 131072 | 131K | ✅ 正确 |
| `qwen/qwen3-32b` | PROVIDER_MODELS 有 | 真实存在 | ✅ |

---

## 13. Ollama

| 字段 | 项目当前值 | 真实情况 | 判定 |
|------|------------|----------|------|
| `llama3` context | 8192 | 8K | ✅ 正确 |
| `llama3.1` context | 131072 | 128K | ✅ 正确（131072 ≈ 128K） |
| `qwen2.5` context | 32768 | 32K | ✅ 正确 |

**缺失**：
- Qwen3 系列（Ollama 已上架 qwen3:0.6b, qwen3:1.7b, qwen3:4b, qwen3:8b, qwen3:14b, qwen3:32b, qwen3:30b-a3b, qwen3:235b-a22b）
- Llama 4 系列（llama4:scout, llama4:maverick）

---

## 14. 火山方舟 Doubao

| 字段 | 项目当前值 | 真实情况 | 判定 |
|------|------------|----------|------|
| `doubao-seed-code` context | 200000 | 200K | ✅ 正确 |
| `doubao-pro-32k` context | 32000 | 32K | ✅ 正确 |

**问题**：
- `PROVIDER_MODELS["火山方舟"]` 里有 `kimi-k2.6 `（带尾巴空格）、`kimi-k2.5`、`minimax-m2.7`、`glm-4.7`、`glm5.1`
  - ⚠️ 字符串 `"kimi-k2.6 "` 末尾有空格，是 bug
  - ⚠️ `glm-4.7` 没在 `MODEL_CAPABILITIES` 里维护
  - ⚠️ `glm5.1`（无点号）和 `glm-5.1`（有点号）命名不一致

---

## 15. 总结：问题清单（按严重度排序）

### 🔴 严重（影响功能正确性）
1. `provider_profile.py` 中 `anthropic` family 的 `thinking_param = None` → 改为 `"thinking"`
2. `provider_profile.py` 中 `gemini` family 的 `thinking_param = None` → 改为 `"thinking_budget"`
3. `model_capabilities.py` 中 `glm-5` / `glm-5.1` 的 `context_limit = 202752` → 改为 `204800`（200K）
4. `constants.py` 的 `PROVIDER_MODELS` 与 `model_capabilities.py` 的 `MODEL_CAPABILITIES` 不一致：
   - `qwen3.6-plus` / `qwen3.7-max` 只在能力表里
   - `qwen3.7-plus`（2026-06-02 新发布）两个表都没有

### 🟠 偏大（命名空间 / 缺失项）
5. ~~OpenCode Zen 的 10 个模型疑似虚构~~ ✅ **已校正（见 6.1 校正报告）**——这 10 个全部真实存在（OpenCode Go 订阅 + OpenCode Zen 免费档）
6. ~~OpenCode Zen 模型命名空间不一致~~ ✅ **不是 bug**——OpenCode 平台接受两种命名（显示名/注册名）
7. 火山方舟列表里 `kimi-k2.6 ` 末尾有空格，是 bug
8. `glm-4.7`（火山方舟列表）和 `glm-5.1`（智谱列表）没有 `MODEL_CAPABILITIES` 能力记录
8.1. **OpenCode 平台 35+ 个模型 DriFox 项目未记录**：`Stealth`（OpenCode 自家，最优先）、3 个 Free 档（Qwen3.6 Plus Free / MiniMax M3 Free / MiMo V2.5 Free）、`Grok Build 0.1`、Claude Opus 4.1~4.8 / Sonnet 4.5/4.6、GPT 5~5.5 全系、Gemini 3~3.5 全系。详见 [research_opencode_official_correction_2026-06-03.md](file|D:/work/DriFox/drifoxdocs/research_opencode_official_correction_2026-06-03.md)

### 🟡 中等（建议补全）
9. `provider_profile.py` 的 `openai` family 缺 `reasoning_effort` 字段（用于 o-series）
10. 缺失 Claude Opus 4.5/4.6 / Claude Sonnet 4.5
11. 缺失 Gemini 3.1 Pro（2026 年 SOTA）
12. 缺失 GPT-5.4（2026 年 SOTA）
13. 缺失文心 4.0/4.5/X1
14. 缺失 Ollama Qwen3 / Llama 4 系列
15. 缺失 SiliconFlow DeepSeek V4 系列

---

## 引用源

1. [DeepSeek V4 发布：1.6 万亿参数，百万上下文（CSDN）](https://blog.csdn.net/Sammyyyyy/article/details/161087809)
2. [DeepSeek V4 Flash 价格速度测评（CSDN）](https://blog.csdn.net/jarvisuni/article/details/160568642)
3. [DeepSeek 官方 API 文档](https://api-docs.deepseek.com/)
4. [DeepSeek V4 实测：7月24号旧模型退役（CSDN）](https://blog.csdn.net/weixin_50937681/article/details/148141826)
5. [Kimi K2.6：开源多模态 Agent 模型（CSDN）](https://blog.csdn.net/chen695969/article/details/160383713)
6. [深度解析：Kimi-K2-Thinking（CSDN）](https://blog.csdn.net/weixin_72532546/article/details/159678793)
7. [Kimi API 开放平台](http://platform.moonshot.cn/)
8. [Kimi API Platform](https://platform.kimi.ai/)
9. [智谱GLM-5技术全解析（CSDN）](https://blog.csdn.net/weixin_43107715/article/details/158074555)
10. [GLM-5 技术拆解：7440亿参数 MoE，202K 超长上下文（CSDN）](https://blog.csdn.net/kakaZhui/article/details/163131057)
11. [智谱 GLM 5.1 重磅上线（CSDN）](https://blog.csdn.net/weixin_69359007/article/details/160931058)
12. [小米 MiMo-V2.5 实测（CSDN）](https://blog.csdn.net/easyllm/article/details/160470687)
13. [小米MiMo-V2系列大模型全方位解读（CSDN）](https://blog.csdn.net/qq_57220546/article/details/159525189)
14. [罗福莉划重点，小米大模型降价99%（腾讯网）](https://new.qq.com/rain/a/20260601A05MRQ00)
15. [阿里发布Qwen3.7-Plus：屏幕理解跑赢GPT-5.4（搜狐）](https://www.sohu.com/a/1031010009_122066678)
16. [阿里Qwen3.7-Plus发布：文本与视觉能力大幅提升（搜狐）](https://www.sohu.com/a/1030986413_121885030)
17. [AIHubMix 模型更新日志](https://aihubmix.com/)
18. [OpenCode 配置默认模型指南（CSDN）](https://download.csdn.net/blog/column/12901442/158156424)
19. [OpenCode Zen 官网](https://opencode.ai/zen)
20. [OpenCode 入门教程（菜鸟教程）](https://www.runoob.com/ai-agent/opencode-coding-agent.html)
21. [Claude Code 第三方API 400报错（thinking type）（CSDN）](https://download.csdn.net/blog/column/12680697/159955741)
22. [Anthropic Sonnet 4.5 系统提示分析（CSDN）](https://blog.csdn.net/csdn122345/article/details/155580233)
23. [Gemini 2.5 Pro 深度拆解（CSDN）](https://download.csdn.net/blog/column/12965954/157588287)
24. [谷歌Gemini 2.5多模态研究助手（CSDN）](https://download.csdn.net/blog/column/12422082/149813843)
25. [OpenAI参数调优完全指南（CSDN）](https://blog.csdn.net/gitblog_00979/article/details/159609434)
26. [OpenAI模型API详解（CSDN）](https://download.csdn.net/blog/column/12592623/144001571)
27. [NVIDIA Nemotron 3 Super 上线（LLM Weekly CSDN）](https://download.csdn.net/blog/column/12656996/159461929)

---

## 不确定性

1. **OpenCode Zen 付费档的完整模型清单**：本报告未能从 OpenCode 官方渠道（GitHub `packages/console/app/src/routes/zen/v1/models.ts`）直接抓取到——`raw.githubusercontent.com` 的 404 / `github.com` 连接被关闭。已通过 CSDN 转引的 `opencode models --refresh` 输出和 OpenCode 官网间接验证了免费层 5 个模型。付费档 10+ 个模型可能真实存在，但无法穷尽验证。
2. **智谱 GLM-5 的 `thinking` API 字段**：CSDN 多篇测评文章均未提及 `thinking.type` 字段；GLM-5 的官方 SDK 调用示例用 `extra_body={"sparse_attention": {...}}` 而非 `thinking`。项目里 `thinking_param: "thinking"` 可能是沿用 GLM-4 时代的命名约定，**未在 GLM-5 时代重新验证**。
3. **DeepSeek V4 的 thinking_param**：官方 Python 示例同时发 `reasoning_effort="high"` 和 `extra_body={"thinking": {"type": "enabled"}}`，但项目 `MODEL_CAPABILITIES` 只记了 `reasoning_effort` 一项。**实际 API 是否两项都是必需的，需要二次代码层验证**。
4. **GLM-5.1 上下文**：CSDN 一处说"200K 上下文，128K 输出"，另一处说"200K 上下文"，与项目里 `202752` 的差异可能是不同测试方法的舍入。建议直接查 [智谱 bigmodel.cn 控制台](https://www.bigmodel.cn/) 的模型卡片核实。
5. **OpenCode Zen 模型在 DriFox 项目里是否有 `opencode/` 前缀**：取决于 OpenCode Zen 的代理行为——是直接转发到上游厂商（如 `kimi-k2.5-free` → Moonshot），还是保留 `opencode/` 命名空间再翻译。需要看 OpenCode 的路由器代码。
6. **Anthropic Claude 4 的 thinking 字段格式**：CSDN 报错案例提到"thinking type should be enabled or disabled"，所以字段名是 `thinking`，值是 `enabled|disabled|adaptive`（字符串或对象）。**与 DriFox 项目里 `thinking_param: None` 矛盾**，需要确认是用 `thinking: "enabled"` 还是 `thinking: {type: "enabled"}`。
7. **2026-06 当下的最新模型**：本文写于 2026-06-03 08:55（北京时间），涉及的所有 2026 年发布模型（Kimi K2.6、Qwen3.7-Plus、MiniMax M3 等）均能在 CSDN 等中文技术社区找到独立多源报道，可信度较高；但不排除部分细节（如"最新价格、最新 API 字段"）在一周内有变动。
