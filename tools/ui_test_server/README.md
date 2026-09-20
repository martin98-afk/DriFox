# ui-test-server

DriFox UI 测试服务（MCP 风格 HTTP JSON-RPC，MVP）。仅开发态使用，**打包产物不包含**。

## 启动

设置环境变量后正常启动 DriFox（dev 源码）：

```bat
set DRIFOX_UI_TEST_SERVER=127.0.0.1:8765
.venv\Scripts\python.exe main.py
```

- 仅监听 127.0.0.1；端口被占自动 +1 重试 3 次（实际端口以日志 `[ui-test-server] listening at` 为准）
- 启动即置 ARM 总闸（tools.ui_driver.setArmed(True)）
- 打包版：lazy import 失败 → 一条 warning 跳过，无副作用

## 工具清单（7 个）

| 工具 | 说明 |
|---|---|
| ui_inspect | 控件查询：mode=find 精查（命中可点按钮下发一次性 confirm_token，5 分钟有效）/ mode=tree 浅树 |
| ui_state | 状态快照：会话/批次/已渲染卡/可见窗口/WebView 池/配额/懒队列 |
| ui_click | 点击控件。**两步确认**：必须携带 ui_inspect 领取的 confirm_token；危险关键词（删除/移除/清空/撤回/解散/delete/remove/clear）直接拒 |
| ui_type | 向控件注入键盘文本（selector 定位 + text） |
| ui_scroll | 滚动到指定值（selector 或 target_object_name 定位，缺省主窗口） |
| ui_wait | 等待 seconds 秒，或 until_object_name 出现 |
| ui_screenshot | grab 截图存 PNG 临时文件，返回路径与尺寸 |

## curl 示例

```bat
:: 健康检查
curl http://127.0.0.1:8765/

:: 工具清单
curl -X POST http://127.0.0.1:8765/ -H "Content-Type: application/json" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"

:: 浏览控件树
curl -X POST http://127.0.0.1:8765/ -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"ui_inspect\",\"arguments\":{\"mode\":\"tree\",\"depth\":2}}}"

:: 精查可点按钮（领 token）
curl -X POST http://127.0.0.1:8765/ -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"ui_inspect\",\"arguments\":{\"mode\":\"find\",\"selector\":{\"cls\":\"QPushButton\"}}}}"

:: 点击（带 token）
curl -X POST http://127.0.0.1:8765/ -d "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"ui_click\",\"arguments\":{\"objectName\":\"send_btn\",\"confirm_token\":\"tok_000001_ab12cd34\"}}}"

:: 截图
curl -X POST http://127.0.0.1:8765/ -d "{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/call\",\"params\":{\"name\":\"ui_screenshot\",\"arguments\":{}}}"

:: 干净退出主程序
curl -X POST http://127.0.0.1:8765/shutdown
```

## MCP client 接入（当前：HTTP 直连）

任何 MCP client 只需把 stdio 传输替换为 HTTP POST（JSON-RPC 2.0 同构）：
`initialize` → `tools/list` → `tools/call`。stdio 桥（把本服务封装为 MCP stdio
server 供 Claude Desktop / DriFox 助手直连）二批再做。

## 边界

- 打包产物无 `tools/` 目录，本服务不会进产物（main.py lazy import 失败即跳过）
- 仅监听 127.0.0.1，不暴露局域网
- 运行期安全依赖 ARM 总闸；令牌一次性、5 分钟过期、与 object_name 绑定
