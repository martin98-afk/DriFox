# 样例·对话（sample-chat）

演示插件：输入 `/sample_chat <消息文本>`，命令经能力池按 `capability_type=chat` 过滤已配置服务商，取首个实例的完整配置（API_URL / API_KEY / 模型名称）后调用 OpenAI 兼容 `/chat/completions` 端点，模型回复以通知回显。

## 支持的厂商形态

OpenAI 兼容端点：`POST {API_URL}/chat/completions`，Bearer 鉴权。API_URL 建议填到 `/v1` 一级（如 `https://api.deepseek.com/v1`）。

## 配置方法

1. 设置 → 服务商：任意普通对话服务商实例（默认能力类型即 `chat`），填好 API_URL / API_KEY / 模型名称。
2. 启用本插件，输入 `/sample_chat 你好`。
3. 命令在后台线程执行（timeout 60s），结果经 `ui.notify` 通知回显；失败回显状态码 + 响应前 200 字 + 脱敏 base_url/模型名。API_KEY 永不回显、不落日志。

> 演示插件：无重试、无流式、无私有协议适配，非生产质量。
