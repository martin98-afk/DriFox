---
description: 插件开发常见问题与解决方案（症状→原因→修法）
---

# 常见问题

> 统一格式：**症状 → 原因 → 修法**。承接自 SKILL.md §8（已瘦身至路由面）。

---

## Manifest 相关

### ❌ validate_plugins.py 报错「name does not match directory」

**症状**：验证报 name 不匹配；或插件装上后不出现在 `/plugin-marketplace`。

**原因**：`plugin.json` 的 `name` 字段与插件目录名不一致。

**修法**：
```jsonc
// 目录：plugins/my-cool-plugin/
// ❌ "name": "myCoolPlugin"
// ✅ "name": "my-cool-plugin"
```

### ❌ JSON Schema 校验失败

**症状**：`validate_plugins.py` 报 schema 校验错误。

**原因**：`plugin.json` 不符合 `schemas/plugin.schema.json`。

**修法**：
- 确认所有必填字段存在（name, description, version, components）
- 确认 `name` 符合 `^[a-z][a-z0-9-]{1,63}$`、`version` 符合 SemVer
- 确认 `components` 至少启用一个

### ❌ 「Components flag enabled but directory not found」

**症状**：验证报组件目录缺失。

**原因**：`components` 某个 flag 为 `true` 但没有对应目录/文件（如 `"commands": true` 却没有 `commands/`）。

**修法**：删除该 flag 或补齐对应目录。

---

## 运行时问题

### ❌ 插件不显示在 /plugin-marketplace

**症状**：装了插件但列表里没有。

**原因**（按概率排序）：
1. `plugin.json` 位置错误 → 应为 `<name>/.drifox-plugin/plugin.json`
2. `name` 与目录名不一致
3. JSON 语法错误
4. 插件放在不被扫描的目录

**修法**：
- 对照目录结构逐项检查
- 用 `python -c "import json; json.load(open('.drifox-plugin/plugin.json'))"` 测 JSON
- 重启 DriFox

### ❌ 插件配置 enabled 但组件全无（工具/UI/hooks 一个都不加载）

**症状**：设置里插件已启用，日志却只有一行拒载记录，所有组件静默消失。

**原因**：manifest `api_version` 高于宿主门槛 `HOST_PLUGIN_API_VERSION`（`app/plugins/version_gate.py`，当前 2）。`VersionGate` 在加载前硬拦，任何组件都不进 registry。

**修法**：
- 日志特征：`[VersionGate] 插件 'x' api_version=3 高于宿主支持的 2，拒载`
- 把 `api_version` 降到 ≤ 宿主当前值；宿主升版前不要抢先声明更高版本
- 仓库级守卫：`tests/core/test_api_version_contract.py` 会扫全部 manifest，超标直接测试失败

### ❌ 工具注册被拒（registry 拒绝加载）

**症状**：`tools/*.py` 写完不生效，日志报注册失败。

**原因**：`registry.register(...)` 未显式声明 `danger` 参数——插件工具必须显式声明（safe/dangerous），未声明直接拒绝注册。

**修法**：
```python
# ❌ registry.register("my_tool", schema, impl=...)            # 缺 danger
# ✅ registry.register("my_tool", schema, impl=_impl, danger="safe")
```


### ❌ 命令不显示 / 不触发

**症状**：`/xxx` 输入后无此命令。

**原因**：frontmatter 缺 `description` 或 `type`；文件名含大写或特殊字符；`components.commands` 未开。

**修法**：对照 `components.md §Commands` 检查。


### ❌ UI 卡片空白 / 不显示

**症状**：卡片命令可输入但无窗口，或直接无命令。

**原因**：`ui/__init__.py` 缺失或无 `register_ui(registry)`；`components.ui` 未开；widget 构造签名不符。

**修法**：先按上述三点自检；仍不行 → **调用 `ui-plugin-creator` 技能**，提供详细症状。

### ❌ 改了代码但热重载没生效 / 无法确认是否生效

**症状**：往 `~/.drifox6/plugins/<name>/` 复制/保存文件后，面板行为还是旧的；或不确定重载是否成功。

**原因**：watchfiles 监控按批次合并事件，复制文件后偶尔不触发；且部分变更（如纯资源文件）不产生日志。

**修法**：
```powershell
# 1) 强制触发：touch 文件 mtime
(Get-Item "$env:USERPROFILE\.drifox6\plugins\<name>\ui\*.py").LastWriteTime = Get-Date
# 2) 查日志确认（出现即成功）
#    [UIPluginRegistry] Loaded UI components for plugin: <name>
#    [PluginHost] Plugin [<name>] reloaded via kernel: ... ui=True ...
Get-Content "$env:USERPROFILE\.drifox6\logs\all.log" | Select-String "<name>" | Select-String "reloaded|Loaded UI"
```
UI 逻辑离线实测（不依赖 DriFox 运行时）：`D:\work\DriFox\.venv\Scripts\python.exe` 带完整 PySide6 + qfluentwidgets，可写 QTimer 序列脚本验证 QSS/高度/动画等 widget 行为（base 环境的 PySide6 缺 QtCore，不可用）。

### ❌ 插件内修改被升级覆盖

**症状**：改了 `plugins/` 下 system-* 系列内置插件，DriFox 更新后改动丢失或行为异常。

**原因**：system-* 是 DriFox 内置插件，不应手动修改。

**修法**：把需要的代码 fork 成自己的插件放到 `~/.drifox6/plugins/<your-plugin>/`（用户插件根），基于它开发；system 插件保持原样。

### ❌ 改了代码但 version 没动

**症状**：市场/用户侧更新后拿到的还是旧版；CI 报版本不一致。

**原因**：每次修改后未更新 `plugin.json` 的 `version`。

**修法**：**每次修改后都更新 `version`**（SemVer：破坏性变更升 major / 新功能升 minor / 修复升 patch），再跑 generate。

### ❌ LSP 列表为空 / .lsp.json 不生效

**症状**：插件带 `.lsp.json`，设置 → LSP 列表为空；`logs/lsp.log` 报
`解析 xxx 配置失败: 'list' object has no attribute 'get'`。

**原因**：`.lsp.json` 写成了 `{"servers": [...]}`（旧模板格式）。宿主解析器
（`lsp_config.load_lsp_configs`）遍历顶层 dict，没有 `servers` 包装层，
对 list 调 `.get` 即炸，注册失败且无明确指引。

**修法**：顶层直接写 `{服务器名: {command, args, extensionToLanguage, installHint}}`，
字段全集见 [components/lsp.md](components/lsp.md)。另注意 Windows 下 npm
语言服务器命令是 `.cmd` shim，进程直启找不到，`command` 需用 `node.exe`
绝对路径直跑 JS 入口。

---

## 发布问题

### ❌ PR 的 CI 失败

**症状**：提交 PR 后 GitHub Actions 红。

**原因**：`validate_plugins.py` 未通过或 `marketplace.json` 不一致。

**修法**（在 drifox-plugins 仓库 clone 中执行）：
```bash
python tools/validate_plugins.py
python tools/generate_marketplace.py
git add marketplace.json
git commit -m "chore: update marketplace.json"
```

### ❌ PR 被要求修改

**症状**：maintainer 或 bot 留下修改意见。

**常见原因**：`description` 过长（>200 字）；缺少 `README.md`；`version` 不合理；缺少 `license` 字段。

---

## 其他


### ❌ 不知道从何开始

**修法**：复制 [plugins/example-plugin/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin) 或本技能 `examples/` 目录下的最小骨架作为起点。

### ❌ 不知道该用哪种组件

**修法**：看 SKILL.md「触发与第一动作」的触发词表分流。

### ❌ 需要 UI 插件

**修法**：调用 `ui-plugin-creator` 技能，本技能不处理 UI 开发细节。

### ❌ 组件专属问题

**修法**：各组件文档自带「排障」小节——见 [components/](components/) 目录对应文件。

### ❌ qfluentwidgets ComboBox 存了数据但 currentData() 恒为 None

**症状**：`combo.addItem(text, key)` 显示正常，保存时 `currentData()` 取到 None（如 cron-tasks 选了 gateway 会话仍报"无可用会话"）。

**原因**：qfluentwidgets `ComboBox.addItem` 签名是 `(text, icon=None, userData=None)`——第二位置参数是 icon，不是 userData。

**修法**：
```python
# ❌ combo.addItem("显示文本", "feishu:ou_xxx")        # key 被当 icon
# ✅ combo.addItem("显示文本", userData="feishu:ou_xxx")
```

### ❌ 任务执行成功/失败但 UI 状态卡死（重开卡片才刷新）

**症状**：浮动卡列表里任务行停在"运行中"，完成后不更新；关开卡片恢复。

**原因**：插件热重载清 sys.modules 后，旧卡片实例持有旧 controller 单例，`jobs_changed` 等推送信号断在新旧实例之间。

**修法**：卡片自驱动兜底——可见期间低频轮询运行态（翻转才全量刷新，避免闪烁），不依赖任何信号链；showEvent 时重新 `bind_card(get_instance())`。参考 `drifox-plugins2` 仓库 cron-tasks 的 `_poll_tick`。

### ❌ 后台线程跑 EngineSession.turn 挂死后永远"运行中"

**症状**：定时任务/无人值守调用偶发卡死，turn 内部 timeout 参数到点也不收尾（无落盘/无通知/串行锁不放）。

**原因**：底层流式读取可能无限期阻塞（无读超时），且 daemon 线程场景 turn 内部超时自救链路可能失效。

**修法**：插件执行器自建硬看门狗——QThread 主体轮询时自查运行时长，超时+缓冲仍无结果则强制 `session.cancel()` 并按 timeout 收尾。参考 cron-tasks `executor.py` 的 `hard_deadline`。根因（流式无读超时）需主程序侧修。

### ❌ 服务商/工具图标显示为占位图形（FluentIcon 兜底）

**症状**：`providers/icons/<icon>.svg` 文件存在，编辑卡/列表里却显示默认占位图标（四个方块拼图）；字母回退也不出现。

**原因**：SVG 文件 XML 语法非法（手写/生成时 DOCTYPE 带双 URL、标签未闭合等）。Qt 的 QSvgRenderer 解析失败是**静默的**——`QIcon(path).isNull()` 为 True 后回退 qrc 兜底图标，无任何日志。

**修法**：先用标准 XML 解析器对比已知正常的内置图标验证良构性（秒级定位，别先查加载链路）：
```bash
py -3 -c "import xml.etree.ElementTree as ET; ET.parse(r'providers/icons/Grok.svg'); print('OK')"
```
手写 SVG 的 DOCTYPE 必须是单 DTD URL：`<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">`。注意主程序 `provider_icons.py` 的图标 LRU 缓存键不含文件内容——修复文件后必须重启 DriFox 才生效。

### ❌ 插件网络请求在打包版失败（URLError 连接超时），同机开发环境正常

**症状**：纯标准库 `urllib.request.urlopen` 请求外部 API，PyInstaller 打包版里稳定超时失败（形态=直连被墙），同机系统 Python 跑同一段代码正常；系统代理（注册表 ProxyEnable=1）明明开着。

**原因**：打包版运行时（Py3.14）的 urllib 默认 opener 未吃到注册表系统代理，直连被墙。环境差异在打包运行时，不在代码逻辑。

**修法**：插件网络层显式构造代理 opener，两级兜底：
```python
def _build_opener():
    proxies = dict(urllib.request.getproxies() or {})  # 环境变量 + 系统代理
    if not proxies.get("https") and not proxies.get("http"):
        import winreg  # 打包环境 getproxies 可能读不到注册表，直读兜底
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if enable and server:
            addr = server if "://" in server else f"http://{server}"
            proxies = {"http": addr, "https": addr}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
```
配套纪律：URLError 异常信息必须带 `.reason`（只打类名会把"代理没生效"误判成"网络不通"）；`HTTPError` 的 JSON body 要单独解析（OAuth2 端点用 400 状态码承载 authorization_pending 等业务状态）。参考 grok-oauth 的 `_grok_auth._build_opener`。

### ❌ OAuth 类服务商「获取模型列表」按钮不显示

**症状**：服务商声明了 `login_hook`（登录按钮正常），但编辑卡「模型管理」行没有「获取模型列表」按钮；`_fetch_models` 函数明明写了。

**原因**：按钮显隐判据是 `capabilities.models_hook`（见 `provider_edit_card._sync_login_btn`）——照抄 anthropic-oauth 模板时漏抄了 `"models_hook": _fetch_models` 这一项。登录按钮可见（login_hook 在）反而掩盖了问题：`has_models_hook or not has_login` 中后半为 False，判定完全取决于漏掉的那个键。

**修法**：capabilities 补 `"models_hook": _fetch_models`。新写 providers 插件后用 headless 实测整张卡（不依赖 DriFox 运行时）：
```python
# QT_QPA_PLATFORM=offscreen + sys.path 指 DriFox 根，系统 python 可跑
from app.widgets.cards.settings.provider_edit_card import ProviderEditCard
card = ProviderEditCard(provider_name="", provider_info={}, is_new=True, preset_provider="Grok OAuth")
card.show(); app.processEvents()
assert card.fetchBtn.isVisible()
```
headless 下 registry 需先手动注册 ProviderDef（模拟 loader 的 icon_dir 注入），ast_guard/qfluentwidgets 在系统 python 环境均可用。
