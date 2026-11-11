# 样例·视频生成（sample-video）

演示插件：输入 `/sample_video <提示词>`，命令经能力池按 `capability_type=video_generation` 过滤已配置服务商，取首个实例的完整配置后调用 OpenAI 兼容 `/videos/generations` 端点，响应原文以通知回显。

## 支持的厂商形态

OpenAI 兼容端点：`POST {API_URL}/videos/generations`，Bearer 鉴权，请求体 `{"model", "prompt"}`。

## 配置方法

1. 设置 → 服务商：新建实例时把「能力类型」选为 `video_generation`，填好 API_URL / API_KEY / 模型名称。
2. 启用本插件，输入 `/sample_video 城市夜景延时摄影`。
3. 命令在后台线程执行（timeout 60s），结果经 `ui.notify` 通知回显。

> 视频生成协议各家差异大（异步任务 id、轮询、私有字段等），演示以「配置链路通」为主：成功回响应原文；失败回状态码 + 响应前 200 字 + 脱敏 base_url/模型名，并注明「该厂商可能需私有协议」。API_KEY 永不回显、不落日志。无重试、无任务轮询，非生产质量。
