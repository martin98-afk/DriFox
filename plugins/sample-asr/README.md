# 样例·语音识别（sample-asr）

演示插件：输入 `/sample_asr <音频文件路径>`，命令经能力池按 `capability_type=speech_recognition` 过滤已配置服务商，取首个实例的完整配置后，以 multipart 文件上传调用 OpenAI 兼容 `/audio/transcriptions` 端点，识别文本以通知回显。

## 支持的厂商形态

OpenAI 兼容端点：`POST {API_URL}/audio/transcriptions`，Bearer 鉴权，multipart 字段 `file`（音频文件）+ `model`（模型名称）。

## 配置方法

1. 设置 → 服务商：新建实例时把「能力类型」选为 `speech_recognition`，填好 API_URL / API_KEY / 模型名称。
2. 启用本插件，输入 `/sample_asr ~/tmp/hello.mp3`（路径支持 `~` 与引号包裹）。
3. 命令在后台线程执行（timeout 60s），识别文本经 `ui.notify` 通知回显；失败回显状态码 + 响应前 200 字 + 脱敏 base_url/模型名。API_KEY 永不回显、不落日志。

> 演示插件：无重试、不做音频格式转换、无私有协议适配，非生产质量。
