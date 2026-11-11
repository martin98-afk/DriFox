# 样例·图像生成（sample-image）

演示插件：输入 `/sample_image <提示词>`，命令经能力池按 `capability_type=image_generation` 过滤已配置服务商，取首个实例的完整配置后调用 OpenAI 兼容 `/images/generations` 端点，图片 URL（或 base64 长度）以通知回显。

## 支持的厂商形态

OpenAI 兼容端点：`POST {API_URL}/images/generations`，Bearer 鉴权，请求体 `{"model", "prompt"}`。

## 配置方法

1. 设置 → 服务商：新建实例时把「能力类型」选为 `image_generation`，填好 API_URL / API_KEY / 模型名称。
2. 启用本插件，输入 `/sample_image 一只戴墨镜的猫`。
3. 命令在后台线程执行（timeout 60s），结果经 `ui.notify` 通知回显；失败回显状态码 + 响应前 200 字 + 脱敏 base_url/模型名。API_KEY 永不回显、不落日志。

> 演示插件：无重试、无私有协议适配，非生产质量。
