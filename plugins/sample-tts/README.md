# 样例·语音合成（sample-tts）

演示插件：输入 `/sample_tts <文本>`，命令经能力池按 `capability_type=speech_generation` 过滤已配置服务商，取首个实例的完整配置后调用 OpenAI 兼容 `/audio/speech` 端点，返回的音频字节数以通知回显。

## 支持的厂商形态

OpenAI 兼容端点：`POST {API_URL}/audio/speech`，Bearer 鉴权，请求体 `{"model", "input"}`，成功响应为音频二进制。内置「豆包语音合成」ProviderDef 即为该类型（其真实协议为厂商私有，演示仅验证配置链路）。

## 配置方法

1. 设置 → 服务商：选择预置的「豆包语音合成」或新建实例时把「能力类型」选为 `speech_generation`，填好 API_URL / API_KEY / 模型名称。
2. 启用本插件，输入 `/sample_tts 你好，世界`。
3. 命令在后台线程执行（timeout 60s），结果经 `ui.notify` 通知回显；失败回显状态码 + 响应前 200 字 + 脱敏 base_url/模型名。API_KEY 永不回显、不落日志。

> 演示插件：无重试、不落盘音频、无私有协议适配，非生产质量。
