---
description: 通过 Playwright 辅助抓取火山方舟的套餐用量查询配置（cookie / csrf_token 等），避免手动从浏览器 DevTools 复制
（OpenCode Go 已改用官方 Bearer 接口，用服务商 API Key 即可查询，无需抓取）
type: prompt
argument-hint:
  "[--volcengine]": "抓取火山方舟的 cookie + csrf_token + x-web-id"
  "[--timeout=]": "自定义登录等待超时秒数（默认 300，范围 60-900）"
prompt_sections:
  --volcengine: "volcengine"
---

## ⚙️ 行为规范（LLM 提示词正文）

### 1. 参数解析

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/quota-setting` 前缀），按空格拆分为平台列表。

| 取值 | 行为 |
|------|------|
| 空 | 询问用户"请指定要抓取哪个平台：volcengine"，等待用户回复后再继续 |
| `--volcengine` | 执行火山方舟抓取流程 |
| 未知平台名 | 提示"不支持的平台：{xxx}，目前支持 volcengine"并停止 |

支持的可选标志：

| 标志 | 行为 |
|------|------|
| `--timeout=` | 自定义登录等待超时秒数，默认 300（5 分钟），范围 60-900 |

参数解析示例：
- `/quota-setting --volcengine` → target=volcengine，timeout=300
- `/quota-setting --volcengine --timeout=180` → target=volcengine，timeout=180
- `/quota-setting` → 询问用户

> 注：OpenCode Go 用量查询已改走官方接口（Bearer API Key），无需抓取配置。

### 2. 工具后端探测

**Playwright MCP 不可用时必须立即告知用户**，不要硬撑、不要乱猜。检测顺序：

```
1. 检查 mcp__playwright__browser_navigate 是否可用
   → 可用：进入第 3 步
   → 不可用：直接停下，提示用户「需要启用 Playwright MCP server 才能使用本命令。
              请在 DriFox 的 MCP 配置中添加 Playwright server（参考 plugins/system-mcp/.mcp.json），
              或暂时使用手动方式：打开浏览器 DevTools → Network 面板 → 复制请求头」
2. 不要尝试用 web 工具（fetch_web / search_web）替代——本命令的核心动作是「让用户在真实浏览器里手动登录」
3. 工具集确认（只读探测）：
   - mcp__playwright__browser_navigate ✓   - mcp__playwright__browser_snapshot ✓
   - mcp__playwright__browser_evaluate ✓   - mcp__playwright__browser_wait_for ✓
   - mcp__playwright__browser_network_requests ✓   - mcp__playwright__browser_network_request ✓
   - mcp__playwright__browser_run_code_unsafe ✓   - mcp__playwright__browser_click ✓
   - mcp__playwright__browser_console_messages ✓   - mcp__playwright__browser_take_screenshot ✓
```

### 3. 通用前置流程

无论抓哪个平台，开头都先执行：

```
1. mcp__playwright__browser_navigate(url="about:blank")
2. 提示用户：「即将打开 Playwright 浏览器，请在弹出的窗口中完成登录。
              登录成功后本工具会自动抓取所需配置，无需您手动复制。
              登录过程有 5 分钟超时限制。」
3. 调用对应平台的具体流程
4. 抓取完成后，mcp__playwright__browser_close() 关闭浏览器
```

### 6. 输出格式规范

每个平台抓取完成后，**用 Markdown 表格展示**，整段放在 ```` ```text ```` 代码块里方便用户一次性全选复制。

### 7. 错误处理

| 错误场景 | 应对 |
|---------|------|
| 工具集缺失 | 第 2 节已处理：明确提示用户启用 Playwright MCP |
| 等待登录超时 | 提示：「登录超时。请重试命令，或检查浏览器是否被其他窗口挡住。如果您已登录成功但工具没识别到，可以告诉我您当前看到的页面标题，我会重新判定」 |
| 网络请求列表为空 | 调 `browser_take_screenshot` 截图，提示：「可能需要检查网络或刷新页面」 |
| 找不到目标 API 请求 | 退而求其次：拿任意一个同源请求的 Cookie |
| httpOnly cookie 拿不到 | 用 `browser_run_code_unsafe` + `page.context().cookies()` 拿全量 |
| 用户中途关闭浏览器 | 检测到后续调用失败时停止，提示：「浏览器已被关闭」 |
| 抓到的 cookie 长度异常（< 20 字符） | **不要输出**。提示：「Cookie 长度异常，可能未抓到完整登录态」 |

### 8. 边界

**会做**：
- 主动探测 Playwright MCP 可用性
- 用户手动登录（账号密码、扫码、2FA 都支持）
- 自动从登录后的网络请求中提取 httpOnly cookie
- 清晰标注每个字段对应 DriFox 的哪个配置项

**不会做**：
- 存储结果到任何文件/数据库/配置
- 自动写入剪贴板
- 替用户自动登录
- 抓取 cookie 之外的敏感信息
- 没有真实浏览器交互时用其他工具替代

<!-- section:volcengine -->
### 5. 火山方舟抓取流程

**所需字段**：`cookie`、`csrf_token`、`x_web_id`（后两者可选但强烈建议）

**步骤详解**：

```
1. mcp__playwright__browser_navigate(url="https://console.volcengine.com/ark/region:ark+cn-beijing/openManagement")
2. 等待登录（轮询 5 秒，最长 timeout 秒）：URL 含 /ark/region:ark+cn-beijing/openManagement 或 snapshot 出现相关关键词
3. 等待 3 秒确保请求发出
4. 抓取 cookie：browser_network_requests 找 GetCodingPlanUsage 请求，提取 Cookie 字段
5. 抓取 csrf_token：从同一请求的 headers 中找 x-csrf-token，兜底用 browser_evaluate 从页面获取
6. 抓取 x_web_id：从请求 headers 或 localStorage 获取
7. 组装结果：{"cookie": "...", "csrf_token": "...", "x_web_id": "..."}
```

**字段填充提示**：

| 抓取字段 | DriFox 服务商配置字段 |
|---------|---------------------|
| `cookie` | `套餐用量查询 → Cookie` |
| `csrf_token` | `套餐用量查询 → CSRF Token` |
| `x_web_id` | `套餐用量查询 → X-Web-ID`（可选） |

**输出模板**：

````markdown
## ✅ 火山方舟配置已抓取

```text
Cookie:     <完整 Cookie 字符串>
CSRF Token: <x-csrf-token 的值>
X-Web-ID:   <x-web-id 的值>
```
| 字段 | 结果 | 必需 |
|------|------|------|
| cookie | 已抓取 | ✅ |
| csrf_token | 已抓取 | ✅ |
| x_web_id | 未抓到（跳过） | ⚪ |
⚠️ 火山方舟登录态过期较快（约 2-24 小时），如用量查询失败请重新抓取。
````
<!-- end -->
