---
description: 通过 Playwright 辅助抓取 OpenCode Zen/Go 和火山方舟的套餐用量查询配置（cookie / csrf_token 等），避免手动从浏览器 DevTools 复制
type: prompt
argument-hint:
  "[--opencode]": "抓取 OpenCode Zen/Go 的 cookie + server_id + workspace_id"
  "[--volcengine]": "抓取火山方舟的 cookie + csrf_token + x-web-id"
  "[--timeout=N]": "自定义登录等待超时秒数（默认 300，范围 60-900）。可与平台名同时使用"
---

## ⚙️ 行为规范（LLM 提示词正文）

### 1. 参数解析

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/quota-setting` 前缀），按空格拆分为平台列表。

| 取值                               | 行为 |
|----------------------------------|------|
| 空                                | 询问用户"请指定要抓取哪个平台：opencode / volcengine（可同时指定多个）"，等待用户回复后再继续 |
| `--opencode`                     | 仅执行 OpenCode 抓取流程 |
| `--volcengine`                   | 仅执行火山方舟抓取流程 |
| 未知平台名                            | 提示"不支持的平台：{xxx}，目前支持 opencode / volcengine"并停止 |

支持的可选标志（**可与平台名混用**）：

| 标志 | 行为 |
|------|------|
| `--timeout=N` | 自定义登录等待超时秒数，默认 300（5 分钟），范围 60-900 |

参数解析示例：
- `/quota-setting --opencode` → target=opencode，timeout=300
- `/quota-setting --volcengine --timeout=180` → target=volcengine，timeout=180
- `/quota-setting --opencode --volcengine` → 依次抓 opencode 和 volcengine
- `/quota-setting` → 询问用户

### 2. 工具后端探测

**Playwright MCP 不可用时必须立即告知用户**，不要硬撑、不要乱猜。检测顺序：

```
1. 检查 mcp__playwright__browser_navigate 是否可用
   → 可用：进入第 3 步
   → 不可用：直接停下，提示用户「需要启用 Playwright MCP server 才能使用本命令。
              请在 DriFox 的 MCP 配置中添加 Playwright server（参考 plugins/system/.mcp.json），
              或暂时使用手动方式：打开浏览器 DevTools → Network 面板 → 复制请求头」
2. 不要尝试用 web 工具（fetch_web / search_web）替代——本命令的核心动作是「让用户在真实浏览器里手动登录」，
   没有真实浏览器交互就不可能拿到登录后的 httpOnly cookie
3. 工具集确认（只读探测，不需要实际执行）：
   - mcp__playwright__browser_navigate ✓
   - mcp__playwright__browser_snapshot ✓
   - mcp__playwright__browser_evaluate ✓
   - mcp__playwright__browser_wait_for ✓
   - mcp__playwright__browser_network_requests ✓
   - mcp__playwright__browser_network_request ✓
   - mcp__playwright__browser_run_code_unsafe ✓   # 拿 httpOnly cookie 必须（MCP 的 network_request 会过滤 Cookie 字段）
   - mcp__playwright__browser_click ✓              # Go 页 ->「使用量」页 fallback
   - mcp__playwright__browser_console_messages ✓   # 抓不到请求时排查用
   - mcp__playwright__browser_take_screenshot ✓    # 抓不到请求时排查用
```

### 3. 通用前置流程

无论抓哪个平台，开头都先执行：

```
1. mcp__playwright__browser_navigate(url="about:blank")  # 重置浏览器环境（必须单独一次调用，不能与后续目标 URL 并行——否则触发 "Navigation interrupted by another navigation" 错误）
2. 提示用户：「即将打开 Playwright 浏览器，请在弹出的窗口中完成登录。
              登录成功后本工具会自动抓取所需配置，无需您手动复制。
              登录过程有 5 分钟超时限制。」
3. 调用对应平台的具体流程（见第 4 / 5 节）
4. 全部平台抓取完成后，mcp__playwright__browser_close() 关闭浏览器
```

### 4. OpenCode Zen / Go 抓取流程

**所需字段**：`cookie`、`server_id`、`workspace_id`

**步骤详解**：

```
0. workspace_id 不提前询问用户（登录后才能看到，问了也没用）。
   如果用户消息中已含 `wrk_xxxxxx` 或 `https://opencode.ai/workspace/wrk_xxxxxx/...` 形式的 URL →
   用正则 `[a-z]+://opencode\.ai/workspace/(wrk_[A-Za-z0-9]+)` 或 `\b(wrk_[A-Za-z0-9]+)\b` 提取。
   否则先导航到 opencode.ai 首页，登录后从 URL 自动提取。

1. 构造目标 URL：
   - 有 workspace_id → https://opencode.ai/workspace/{workspace_id}/go
   - 无 workspace_id → https://opencode.ai （登录后跳转到 /workspace/{id}/...）
2. mcp__playwright__browser_navigate(url=目标URL)
3. 等待用户完成登录（轮询判定）：
   轮询间隔 5 秒，最长等 timeout 秒。判定条件（满足任一即视为登录完成）：
   (a) URL 包含 /workspace/wrk_ （说明已登录并进入工作台）
   (b) snapshot 中出现 "Coding Plan"、"5h"、"Weekly"、"Monthly" 任一关键词
   (c) URL 不再是 /sign-in 或 /login 或 /auth
   每次轮询调 mcp__playwright__browser_snapshot()（不要等满 timeout 才发现已登录）
4. 提取 workspace_id（如果步骤 0 中未提前提取）：
   - 从 URL 正则提取 /workspace/(wrk_[A-Za-z0-9]+)/
   - 若当前不在 Go 页，导航到 https://opencode.ai/workspace/{workspace_id}/go
5. 刷新 Go 页确保最新状态 + 捕获 `/_server` 请求：
   5.1 先查一次 network_requests 能否捡到漏：
       mcp__playwright__browser_network_requests(static=true)
       如果有 `/_server` → 跳到 7.2 提取公式
   5.2 如果没捡到，需要**带着 listener 重新导航**（listener 必须在 navigation 之前注册，否则漏掉）：
       mcp__playwright__browser_run_code_unsafe(code="""
         async (page) => {
           const url = await new Promise((resolve) => {
             const timer = setTimeout(() => resolve(null), 10000);
             page.on('request', req => {
               if (req.url().includes('/_server')) {
                 clearTimeout(timer);
                 resolve(req.url());
               }
             });
             // listner 就位后才导航
             page.goto('""" + 当前URL + """').then(() => {});
           });
           return url;
         }
       """)
       返回非 null → 从 URL 的 `id` query 参数提取 server_id，跳到步骤 8
       返回 null → 继续步骤 6（cookie 照抓，server_id 走 7.3 人工兜底）
   5.3 无论 5.2 是否抓到，都会重新加载了一次页面，cookie 不受影响

6. 抓取 cookie：
   ⚠️ **关键踩坑**：`browser_network_request(part="request-headers")` 实测只返回 ~8 个非敏感 header
   （sec-ch-ua、referer、user-agent 等），Cookie 字段被 MCP 服务端过滤掉了。
   **必须用** Playwright 原生 `context.cookies()` 绕过过滤。

   mcp__playwright__browser_run_code_unsafe(code="""
     async (page) => {
       const cookies = await page.context().cookies('https://opencode.ai');
       return {
         count: cookies.length,
         str: cookies.map(c => c.name + '=' + c.value).join('; '),
         raw: cookies
       };
     }
   """)
   - str 为空 → 提示「未抓到任何 cookie，请确认已登录」
   - str 长度 < 20 → 走第 7 节「Cookie 长度异常」错误处理
   - 否则 cookie = str，记录 length

7. 提取 server_id（tRPC procedure 哈希，64 字符 SHA256）：
   ⚠️ Go 页通过 HTTP GET `/_server?id=<64字符SHA256>&args=...` 获取套餐用量数据。
   不是 WebSocket/SSE。核心捕获逻辑已在步骤 5 中完成（request listener 拦截 + 重新导航）。
   这里只是提取和兜底：

   7.1 如果步骤 5.2 已返回 URL → 用正则 `[?&]id=([A-Fa-f0-9]{64})` 从 URL 提取 server_id
   7.2 如果步骤 5.1 的 `browser_network_requests` 捡到 `/_server`：
       mcp__playwright__browser_network_request(index=该index, part="request-headers")
       从 URL 的 `id` query 参数提取 server_id
   7.3 **人工兜底**：若以上都拿不到：
       提示用户：「`/_server` GET 请求的 `id` 参数值（64 字符 SHA256 哈希）即为 server_id。
       请在 DriFox 配置中留空此字段，或从浏览器 DevTools → Network 筛选 `/_server` 复制 `id` 参数。」

8. 组装结果 dict：{"cookie": "...", "server_id": "...", "workspace_id": "..."}
9. 进入第 6 节输出格式
```

**OpenCode 字段填充提示**（让用户知道对应到 DriFox 哪个配置项）：

| 抓取字段 | DriFox 服务商配置字段 |
|---------|---------------------|
| `cookie` | `套餐用量查询 → Cookie` |
| `server_id` | `套餐用量查询 → Server ID` |
| `workspace_id` | `套餐用量查询 → Workspace ID` |

### 5. 火山方舟抓取流程

**所需字段**：`cookie`、`csrf_token`、`x_web_id`（后两者可选但强烈建议）

**步骤详解**：

```
1. mcp__playwright__browser_navigate(url="https://console.volcengine.com/ark/region:ark+cn-beijing/openManagement")
2. 等待用户完成登录（火山方舟登录可能含手机验证码，**给足时间**）：
   轮询间隔 5 秒，最长等 timeout 秒。判定条件（满足任一即视为登录完成）：
   (a) URL 包含 /ark/region:ark+cn-beijing/openManagement
   (b) snapshot 中出现 "Coding Plan"、"套餐"、"用量" 任一关键词
   (c) snapshot 中出现火山引擎主导航元素（"费用中心"、"工单"等任意控制台常见元素）
3. 登录后页面会自动加载 coding plan 用量数据。等待 3 秒确保请求发出：
   mcp__playwright__browser_wait_for(time=3)
4. 抓取 cookie（同 OpenCode 流程的步骤 6，必须从网络请求拿）：
   4.1 mcp__playwright__browser_network_requests(static=false)
       找第一个 url 包含 GetCodingPlanUsage 的请求，记下 index
   4.2 mcp__playwright__browser_network_request(index=该index, part="request-headers")
       提取 Cookie 字段（完整值）
5. 抓取 csrf_token：
   5.1 方案 A（推荐，从第 4 步同一次请求拿）：
       在第 4.2 返回的 headers 中找 x-csrf-token 字段
   5.2 方案 B（兜底，从页面 / localStorage 拿）：
       mcp__playwright__browser_evaluate(function="() => document.querySelector('meta[name=\"csrf-token\"]')?.content || ''")
       若为空再试：
       mcp__playwright__browser_evaluate(function="() => localStorage.getItem('csrf_token') || sessionStorage.getItem('csrf_token') || ''")
6. 抓取 x_web_id（可选但建议）：
   6.1 方案 A：从第 4.2 返回的 headers 中找 x-web-id 字段
   6.2 方案 B：
       mcp__playwright__browser_evaluate(function="() => localStorage.getItem('x_web_id') || localStorage.getItem('web_id') || ''")
   6.3 若都拿不到 → 标为 (未抓到，跳过此字段不影响用量查询)
7. 组装结果 dict：{"cookie": "...", "csrf_token": "...", "x_web_id": "..."}
8. 进入第 6 节输出格式
```

**火山方舟字段填充提示**：

| 抓取字段 | DriFox 服务商配置字段 |
|---------|---------------------|
| `cookie` | `套餐用量查询 → Cookie` |
| `csrf_token` | `套餐用量查询 → CSRF Token` |
| `x_web_id` | `套餐用量查询 → X-Web-ID`（可选） |

### 6. 输出格式

每个平台抓取完成后，**用 Markdown 表格展示**，整段放在一个 ```` ```text ```` 代码块里方便用户一次性全选复制。

**OpenCode 输出模板**：

````markdown
## ✅ OpenCode Zen / Go 配置已抓取

> 请将以下三个值复制到 DriFox 服务商编辑面板的「套餐用量查询（可选）」区域：

```text
Server ID:    c7389bd0e731f80f49593e5ee53835475f4e28594dd6bd83eb229bab753498cd   (64 字符 procedure 哈希)
Workspace ID: wrk_01KS487R6RE5G1G1W0VZW4NTPY
Cookie:       auth=Fe26.2**...（完整 Cookie 字符串，保留原始分号分隔）
```

| 字段 | 抓取结果 |
|------|---------|
| `server_id` | `c7389bd0e731f80f49593e5ee53835475f4e28594dd6bd83eb229bab753498cd` (64 字符 procedure 哈希) |
| `workspace_id` | `wrk_01KS487R6RE5G1G1W0VZW4NTPY` |
| `cookie` | 已抓取（**长度 N 字符**，httpOnly, sameSite=Lax, 域 opencode.ai） |

⚠️ **请妥善保管 Cookie，避免泄露**。Cookie 有效期通常为数小时到数天，过期后用 `/quota-setting opencode` 重新抓取。
````

**火山方舟输出模板**：

````markdown
## ✅ 火山方舟配置已抓取

> 请将以下值复制到 DriFox 服务商编辑面板的「套餐用量查询（可选）」区域：

```text
Cookie:     acw_tc=...; _ga=...; _mb_open_token=...（完整 Cookie 字符串）
CSRF Token: x-csrf-token 的值
X-Web-ID:   x-web-id 的值（如未抓到可留空）
```

| 字段 | 抓取结果 | 是否必需 |
|------|---------|---------|
| `cookie` | 已抓取（**长度 N 字符**） | ✅ 必需 |
| `csrf_token` | 已抓取 / 未抓到 | ✅ 必需 |
| `x_web_id` | 已抓取 / 未抓到 | ⚪ 可选 |

⚠️ **请妥善保管 Cookie 和 CSRF Token**。火山方舟的登录态过期较快（约 2-24 小时），如用量查询失败请重新抓取。
````

**多平台抓取**时，用 `---` 分隔多个平台结果：

```markdown
[平台 A 结果]

---

[平台 B 结果]
```

### 7. 错误处理

**每种错误都必须明确告诉用户「该怎么做」，不能只说「失败」**。

| 错误场景 | 应对 |
|---------|------|
| 工具集缺失 | 第 2 节已处理：明确提示用户启用 Playwright MCP |
| 等待登录超时（timeout 秒内未登录成功） | 提示：「登录超时。请重试命令，或检查浏览器是否被其他窗口挡住。**如果您已经登录成功但工具没识别到**，可以告诉我您当前看到的页面标题，我会重新判定登录状态」 |
| 网络请求列表为空（页面加载失败） | 调 `browser_take_screenshot` 截图，给用户看「页面实际显示了什么」，并提示：「可能需要检查网络或刷新页面」 |
| 找不到目标 API 请求（OpenCode 找不到 _server / 火山方舟找不到 GetCodingPlanUsage） | 退而求其次：拿任意一个**同源**请求的 Cookie。提示：「未抓到目标 API 请求，已使用同源其他请求的 Cookie 替代，可能短期有效」 |
| httpOnly cookie 拿不到（document.cookie 返回空） | **不要**用 `browser_network_request` 拿 Cookie（实测它会过滤掉 Cookie 字段）。改用 `browser_run_code_unsafe` + `page.context().cookies('https://opencode.ai')` 拿全量 |
| 抓不到 `/_server` 请求（Go 页 HTTP GET 时机微妙易漏掉） | 走第 4 节 7.1 请求拦截法重试（`browser_run_code_unsafe` 监听 request 事件）。仍失败则提示手动从 DevTools 复制 URL 的 `id` 参数 |
| `X-Server-Id` 缺失或拿到非 64 字符值 | 这是 procedure 标识，**永远应该是 64 字符 SHA256 哈希**。若不是说明拿错了请求，丢弃此 server_id |
| 某个字段（如 x_web_id）抓不到且为可选 | 输出中标 `(未抓到，跳过此字段不影响用量查询)`，不要让用户误以为抓取失败 |
| 用户中途关闭浏览器 | 检测到 `browser_evaluate` 或后续调用失败时停止，提示：「浏览器已被关闭，本次抓取取消」 |
| 抓到的 cookie 长度明显异常（< 20 字符） | **不要输出**。提示：「抓到的 Cookie 长度异常（仅 N 字符），可能未抓到完整登录态，建议重试」 |

### 8. 边界

**会做**：
- 主动探测 Playwright MCP 可用性，不可用时明确告知
- 用户在浏览器里手动完成登录（账号密码、扫码、2FA 都支持）
- 自动从登录后的网络请求中提取 httpOnly cookie（document.cookie 拿不到的部分）
- 清晰标注每个字段对应 DriFox 的哪个配置项
- 多平台一次性抓取
- 识别 Go 页的 `/_server` 是 HTTP GET 请求（非 WebSocket），通过 request 事件拦截自动捕获
- 自动处理 Playwright MCP 过滤 Cookie 字段的问题，通过 `run_code_unsafe` + `context.cookies()` 绕过
- 自动从用户消息中提取 workspace_id（无需追问），前提是消息里含 `wrk_xxx` 或完整 URL
- 当 server_id 无法自动捕获时，给出手动填写指引

**不会做**：
- 存储抓取结果到任何文件 / 数据库 / 配置（**用户要求不写入**）
- 自动写入剪贴板（避免误覆盖用户已复制内容；让用户手动从代码块复制更可控）
- 替用户自动登录（账号密码不在本工具范围内）
- 抓取 cookie 之外的敏感信息（密码、手机号等）
- 在没有真实浏览器交互的情况下尝试用其他工具替代
