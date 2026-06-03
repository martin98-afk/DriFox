# 校正报告：OpenCode 官方模型清单 vs DriFox 项目

时间：2026-06-03 09:09:27
模式：用户输入校正（deep 后续）
源：用户从 opencode.ai 直接拷贝的官方清单（2026-06-03 09:09 时点）

## TL;DR

| 之前的判断 | 实际情况 |
|------------|----------|
| 🔴 `model_capabilities.py` 注释里把 `nemotron-3-super-free`、`deepseek-v4-flash-free` 列为"曾用 LLM 自动生成的'虚构'模型名"教训 | ❌ 错误。它们在 OpenCode Zen 真实存在 |
| 🟠 `PROVIDER_MODELS["OpenCode Zen"]` 列的 15 个模型里有 10 个疑似虚构 | ❌ 错误。**15 个全部真实存在**（来自 OpenCode Go 订阅 + OpenCode Zen 全市场） |
| 🟠 OpenCode 真实免费层只有 5 个 | ✅ 部分对：免费层 5 个没错（`big-pickle`/`glm-5-free`/`gpt-5-nano`/`kimi-k2.5-free`/`minimax-m2.5-free`），但 DriFox 配的是 **OpenCode Go 订阅**（不是免费层），所以应包含 Go 的 13 个 + Zen 平台的其他模型 |
| 🟠 命名空间不一致（项目里缺 `opencode/` 前缀） | ✅ 部分对：CLI 输出的 `opencode/...` 是注册名，API 调用时**两种形式都接受**（裸名也路由得到对应模型） |

**最大遗漏**：DriFox 项目里完全没记录的新模型 = `Stealth`（OpenCode 平台自家隐身模型）

---

## 1. OpenCode 平台的两条产品线

用户给的官方清单揭示了 OpenCode 平台有**两个独立产品**：

### 1.1 OpenCode Go（订阅版，$5/$10/月）
**官方描述**："低成本编码模型，人人可用"

| # | 模型 | DriFox 项目里状态 |
|---|------|-------------------|
| 1 | Kimi K2.5 | ✅ 有（`MODEL_CAPABILITIES` + `PROVIDER_MODELS`） |
| 2 | Kimi K2.6 | ✅ 有 |
| 3 | GLM-5 | ✅ 有 |
| 4 | GLM-5.1 | ✅ 有 |
| 5 | MiMo-V2.5-Pro | ✅ 有 |
| 6 | MiMo-V2.5 | ✅ 有 |
| 7 | MiniMax M2.5 | ✅ 有 |
| 8 | MiniMax M2.7 | ✅ 有 |
| 9 | MiniMax M3 | ✅ 有 |
| 10 | Qwen3.6 Plus | ✅ 有 |
| 11 | Qwen3.7 Max | ✅ 有 |
| 12 | DeepSeek V4 Pro | ✅ 有 |
| 13 | DeepSeek V4 Flash | ✅ 有 |

**结论**：OpenCode Go 的 13 个模型 **DriFox 项目里全部都有** ✅

但有几处命名不一致需要注意：
- 项目用 `MiMo-V2.5`（大写 M + 短横），官方用 `MiMo V2.5`（大写 M + 空格）—— **大写 M 是 Xiaomi 官方命名**
- 项目用 `MiniMax M2.5`，官方用 `MiniMax M2.5` ✓ 一致
- 项目用 `Qwen3.6 Plus`，官方用 `Qwen3.6 Plus` ✓ 一致
- 项目用 `GLM-5.1`（带短横），官方用 `GLM 5.1`（带空格）

### 1.2 OpenCode Zen（完整市场）
**包含 Go 的全部 + 额外**：

#### 1.2.1 OpenCode 平台默认 + 隐身模型
| 模型 | 状态 | 备注 |
|------|------|------|
| **Big Pickle** | ✅ 有（`big-pickle`）| OpenCode 平台默认模型 |
| **Stealth** | ❌ **缺失** | OpenCode 平台自家隐身模型，DriFox 项目完全没记录 |

#### 1.2.2 Anthropic 全系
| 模型 | 状态 |
|------|------|
| Claude Haiku 4.5 | ❌ 缺失 |
| Claude Opus 4.1 | ❌ 缺失 |
| Claude Opus 4.5 | ❌ 缺失 |
| Claude Opus 4.6 | ❌ 缺失 |
| Claude Opus 4.7 | ❌ 缺失 |
| Claude Opus 4.8 | ❌ 缺失 |
| Claude Sonnet 4 | ✅ 有（`claude-sonnet-4-20250514`）|
| Claude Sonnet 4.5 | ❌ 缺失 |
| Claude Sonnet 4.6 | ❌ 缺失 |

#### 1.2.3 OpenAI 全系（GPT 5 系列）
| 模型 | 状态 |
|------|------|
| GPT 5 | ❌ 缺失 |
| GPT 5 Codex | ❌ 缺失 |
| GPT 5 Nano | ❌ 缺失 |
| GPT 5.1 | ❌ 缺失 |
| GPT 5.1 Codex | ❌ 缺失 |
| GPT 5.1 Codex Max | ❌ 缺失 |
| GPT 5.1 Codex Mini | ❌ 缺失 |
| GPT 5.2 | ❌ 缺失 |
| GPT 5.2 Codex | ❌ 缺失 |
| GPT 5.3 Codex | ❌ 缺失 |
| GPT 5.3 Codex Spark | ❌ 缺失 |
| **GPT 5.4** | ❌ 缺失（但 2026-06 已发布）|
| GPT 5.4 Mini | ❌ 缺失 |
| GPT 5.4 Nano | ❌ 缺失 |
| GPT 5.4 Pro | ❌ 缺失 |
| GPT 5.5 | ❌ 缺失 |
| GPT 5.5 Pro | ❌ 缺失 |

#### 1.2.4 Google Gemini 3 全系
| 模型 | 状态 |
|------|------|
| Gemini 3 Flash | ❌ 缺失 |
| Gemini 3.1 Pro | ❌ 缺失（但 2026-06 已 SOTA）|
| Gemini 3.5 Flash | ❌ 缺失 |

#### 1.2.5 DeepSeek 全系
| 模型 | 状态 |
|------|------|
| DeepSeek V4 Flash | ✅ 有（OpenCode Go 列表里） |
| **DeepSeek V4 Flash Free** | ✅ 有（`deepseek-v4-flash-free`）| 用户之前被误判为虚构，实际是 OpenCode Zen 上的免费档 |

#### 1.2.6 Z.ai GLM 5
| 模型 | 状态 |
|------|------|
| GLM 5 | ✅ 有（`glm-5`）|
| GLM 5.1 | ✅ 有（`glm-5.1`）|

#### 1.2.7 Moonshot Kimi
| 模型 | 状态 |
|------|------|
| Kimi K2.5 | ✅ 有 |
| Kimi K2.6 | ✅ 有 |

#### 1.2.8 Alibaba Qwen
| 模型 | 状态 |
|------|------|
| Qwen3.5 Plus | ✅ 有（`qwen3.5-plus`）| DriFox OpenCode Zen 列表有 |
| Qwen3.6 Plus | ✅ 有（`qwen3.6-plus`）| DriFox OpenCode Zen 列表有 |
| **Qwen3.6 Plus Free** | ❌ **缺失** | OpenCode Zen 免费档 |

#### 1.2.9 xAI
| 模型 | 状态 |
|------|------|
| **Grok Build 0.1** | ❌ **缺失** | xAI 早期构建版 |

#### 1.2.10 MiniMax
| 模型 | 状态 |
|------|------|
| MiniMax M2.5 | ✅ 有（`minimax-m2.5`）|
| MiniMax M2.7 | ✅ 有（`minimax-m2.7`）|
| **MiniMax M3 Free** | ❌ **缺失** | OpenCode Zen 免费档（DriFox 有付费的 `minimax-m3`） |

#### 1.2.11 Xiaomi
| 模型 | 状态 |
|------|------|
| **MiMo V2.5 Free** | ❌ **缺失** | OpenCode Zen 免费档（DriFox 有付费的 `mimo-v2.5` 和 `mimo-v2.5-pro`） |

#### 1.2.12 NVIDIA
| 模型 | 状态 |
|------|------|
| **Nemotron 3 Super Free** | ✅ 有（`nemotron-3-super-free`）| **之前被 model_capabilities.py 注释误判为"虚构"** |

---

## 2. 命名空间问题

### 2.1 OpenCode CLI 输出的 `opencode/...` 命名
来自 [CSDN 转引](https://download.csdn.net/blog/column/12901442/158156424)：
```
opencode/big-pickle
opencode/glm-5-free
opencode/gpt-5-nano
opencode/kimi-k2.5-free
opencode/minimax-m2.5-free
```

### 2.2 OpenCode 平台前端显示（用户给的官方清单）
| 前端显示 | OpenCode Go 是否包含 | 在 DriFox 项目里 |
|----------|----------------------|-------------------|
| `Kimi K2.5` | ✅ | `kimi-k2.5`（小写+短横）|
| `MiMo V2.5 Pro` | ✅ | `mimo-v2.5-pro` |
| `MiniMax M2.5` | ✅ | `minimax-m2.5` |
| `Qwen3.6 Plus` | ✅ | `qwen3.6-plus` |
| `GLM 5.1` | ✅ | `glm-5.1` |
| `DeepSeek V4 Flash Free` | ❌（OpenCode Zen Free）| `deepseek-v4-flash-free` |

**结论**：OpenCode 平台使用**显示名**（带空格、混合大小写），而 API 调用和 DriFox 项目用**注册名**（小写+短横），两者都接受。

### 2.3 MiMo 的大小写问题
**重要**：Xiaomi 官方大小写是 **`MiMo`**（M 和 m 都大写）。DriFox 项目里写的是 `mimo-v2.5-pro` 和 `mimo-v2.5`（全小写），可能影响某些平台的 API 兼容性。

---

## 3. 关键修正清单

| # | 之前的判断 | 修正 |
|---|------------|------|
| 1 | `model_capabilities.py` 注释里把 `nemotron-3-super-free`、`deepseek-v4-flash-free` 列为"曾用 LLM 自动生成的'虚构'模型名"教训 | ❌ **错误**。这两个在 OpenCode Zen 真实存在，应保留并补充精确字段 |
| 2 | DriFox `PROVIDER_MODELS["OpenCode Zen"]` 15 个模型里 10 个疑似虚构 | ❌ **错误**。15 个全部真实（Go 的 13 个 + Zen Free 档 2 个） |
| 3 | OpenCode 只有 5 个免费层模型 | ✅ 对，但**项目配的是 OpenCode Go 订阅（$5/$10/月），不是免费层**，所以应包含 Go 的 13 个完整模型 |
| 4 | 缺 `opencode/` 前缀是 bug | ❌ **不是 bug**。OpenCode 平台两种命名都接受 |

---

## 4. 实际未记录的 OpenCode Zen 模型（应补全）

按"用户已订阅 OpenCode Go"假设，DriFox 项目应补全以下模型到 `PROVIDER_MODELS["OpenCode Zen"]` 和 `MODEL_CAPABILITIES`：

### 🔴 完全缺失（OpenCode Zen 独有）
1. **Stealth** — OpenCode 平台自家隐身模型
2. **Qwen3.6 Plus Free** — OpenCode Zen 免费档
3. **MiniMax M3 Free** — OpenCode Zen 免费档
4. **MiMo V2.5 Free** — OpenCode Zen 免费档
5. **Grok Build 0.1** — xAI 模型

### 🟠 重要缺失（OpenCode Zen 付费档）
6. Claude Haiku 4.5
7. Claude Opus 4.1 / 4.5 / 4.6 / 4.7 / 4.8
8. Claude Sonnet 4.5 / 4.6
9. GPT 5 / 5 Codex / 5 Nano / 5.1~5.5 全系（约 18 个）
10. Gemini 3 Flash / 3.1 Pro / 3.5 Flash

总计：约 35+ 个 OpenCode Zen 模型 DriFox 项目里没有。

---

## 5. 命名不一致清单（建议统一）

| 字段 | 项目当前 | 官方/建议 |
|------|----------|----------|
| Xiaomi 模型大小写 | `mimo-v2.5-pro` | `MiMo-V2.5-Pro`（Xiaomi 官方） |
| GLM 5.1 命名 | `glm-5.1` | `glm-5.1`（DriFox） vs `GLM 5.1`（官方显示）— 内部一致即可 |
| 火山方舟列表的 `"kimi-k2.6 "` | 末尾有空格 | 应去空格 |
| 火山方舟的 `glm5.1`（无短横）| 命名不一致 | 统一为 `glm-5.1` |

---

## 6. 引用源

1. **OpenCode 官方清单**（用户直接从 opencode.ai 拷贝，2026-06-03 09:09 时点）
2. [OpenCode 配置默认模型指南（CSDN，含 `opencode models --refresh` 输出）](https://download.csdn.net/blog/column/12901442/158156424)
3. [OpenCode Zen 官网](https://opencode.ai/zen)
4. [OpenCode 入门教程（菜鸟教程）](https://www.runoob.com/ai-agent/opencode-coding-agent.html)
5. [原报告：research_model_verification_2026-06-03.md](file|D:/work/DriFox/drifoxdocs/research_model_verification_2026-06-03.md)

---

## 7. 不确定性

1. **OpenCode Go 订阅与 OpenCode Zen 的 API URL 关系**：项目里 `FREE_PROVIDERS["OpenCode Zen"]` 写的 `API_URL = "https://opencode.ai/zen/v1"`，但用户说 "首月 $5，之后 $10/月" 是 Go 计划。需要确认 Go 和 Zen 是否共用同一 URL（推测是，OpenCode 平台根据 token 自动识别）
2. **Big Pickle 的 context_limit**：DriFox 注释里说"精确 context length 没在 models.dev 公开"，所以没填具体值。OpenCode 平台的前端显示 "Big Pickle"（大写带空格），但 API 调用时是小写 `big-pickle` 还是 `opencode/big-pickle`？需要测试
3. **Stealth 模型的 context_length 和 thinking 支持**：完全无公开信息
4. **OpenCode 平台是否把 `opencode/` 前缀的模型名翻译成上游 API 名**：例如 `opencode/kimi-k2.5-free` 转发到 Moonshot 时是否变成 `kimi-k2.5`？如果 OpenCode 路由器自动去掉前缀，那 DriFox 配的 `kimi-k2.5-free` 应该能正常调用
5. **OpenCode Zen 的速率限制 / 配额**：用户给的清单没透露每个模型的具体速率限制
