---
description: UI 插件编码级踩坑记录（症状→原因→修法），来自实战回归
---

# UI 插件踩坑记录（pitfalls）

> 生成代码后的 30 秒自检先行；本文件只收「编码级」踩坑。
> 架构级陷阱速查见 widgets.md §6；新坑按同格式写回本文件。

---

## 生成后 30 秒自检

```
拿到新卡片 → 开程序 → 输 /<card_id>
1. 字体和主界面一致吗？        → 不一致 → 检查 _apply_latest_theme 的字体处理（见下「字体注入」）
2. 按钮文字显示完整吗？         → 不完整 → 检查 button padding 和 fixedSize
3. 主题色跟随主程序变吗？      → 不跟  → 检查 set_context_provider + _apply_latest_theme
4. 刷新/操作不卡 UI 吗？       → 卡UI  → 检查是否用了 QThread（模式见 patterns.md §3.2）
5. 连续快速点击会崩吗？        → 崩溃  → 检查 _is_busy 防重入 + worker cleanup
6. 写剪贴板的图粘贴有反应吗？   → 无反应 → 检查是否用了 setImage（见下）
7. 提示条看得见吗？            → 看不见 → 检查是否用了 InfoBar 而非 QToolTip（见下）
8. 样式真的生效了吗？          → 没生效 → HTML 片段必走无头截图（见下 §28），字符串断言查不出
9. 窄窗口挤一下会坏吗？        → 溢出/粘连 → 动态宽度容器禁写死 px（见下 §23）
```

### 渲染类插件（HTML 片段 / 欢迎 tab / 消息卡片）的额外自检

```
① 拉窄窗口到 ~420px 再看一遍（列宽 flex 均分、标签不粘连）    §23
② 明暗两态各看一遍（色阶方向要一致：都是"越深 = 越多"）      §28
③ 悬停 tooltip 看一遍（HTML 侧禁原生 title）                 §24
④ 数据一直"加载中"？→ 查日志有没有"完成"那行（判 key 判据）  §25
```

> 以上四类坑的共同特征：**结构对、类名对、断言全绿，但显示是错的**。
> 只能靠截图 + 运行时观察发现，详见 §28。

---

## 1. Python 3.14 异常语法

**症状**：卡片加载即 SyntaxError，插件整包失效。

**原因**：Python 3.14 不再支持 Python 2 风格 `except Exception, e:`，多异常必须元组包裹。

**修法**：
```python
# ❌ 不可用（Python 2 语法，3.14 报 SyntaxError）
except OSError, PermissionError:
    pass

# ✅ 正确
except (OSError, PermissionError):
    pass

# ✅ 正确（带异常变量）
except (OSError, PermissionError) as e:
    logger.error(f"操作失败: {e}")
```

## 2. 按钮高度 vs padding 陷阱

**症状**：按钮看起来存在，但文字不显示。

**原因**：`setFixedHeight` 小于 padding 上下之和时内容区被压成 0（32 - 16 - 16 = 0）。

**修法**：
```python
# ❌ 按钮文字不显示
btn.setFixedHeight(32)
btn.setStyleSheet("padding: 16px 0;")

# ✅ 正确
btn.setStyleSheet("padding: 0 10px;")   # 左右 padding，不占用高度
```

**规则**：`fixedHeight` < 40px 的按钮用 `padding: 0 Xpx`；大按钮用 `padding: Ypx 0`。

## 3. QTimer.singleShot 按钮自动恢复模式

**症状**：操作完成后临时成功文案不恢复，或恢复时误覆盖新操作文案。

**原因**：恢复回调无脑 set 默认文案，与防重入状态脱节。

**修法**：
```python
self._btn.setText("✅ 清理完成，释放 234 MB")
QTimer.singleShot(3000, self._reset_btn)  # 3 秒后恢复

def _reset_btn(self):
    if not self._is_busy:  # 防止恢复时正在执行新操作
        self._btn.setText("默认文案")
```

## 4. 字体注入容易漏

**症状**：卡片功能正常，但字体与主程序不一致（最常犯 bug）。

**原因**：`_apply_latest_theme` 里只更新了颜色，漏了字体。

**修法**：颜色与字体必须同时注入，模板见 patterns.md §1.4。

```python
# ❌ 只更新颜色
child.setStyleSheet("color: rgba(255,255,255,0.9);")

# ✅ 用 _make_style 同时注入颜色 + 字体
child.setStyleSheet(_make_style(tc, font_family, font_size))
```

## 5. 异步 worker 生命周期

**症状**：关闭卡片后程序崩溃或线程泄漏。

**原因**：销毁时未停 worker 线程；C++ 对象销毁后残留信号触发 RuntimeError。

**修法**：完整线程清理模式见 patterns.md §3.2（quit→wait(500)→置 None，重写 deleteLater）。

## 6. 剪贴板写图必须用 setImage，禁用 setPixmap

**症状**：提示已复制，同进程粘贴却无反应；外部工具粘贴正常。

**原因**：`setPixmap()` 后剪贴板所有权在本进程，同进程粘贴时 `mimeData().imageData()` 返回 QPixmap，主程序输入框的 `isinstance(img, QImage)` 检查静默跳过。跨进程读 CF_DIB 自动转 QImage，故外部工具不受影响。

**修法**：
```python
QApplication.clipboard().setImage(pixmap.toImage())   # ✅
QApplication.clipboard().setPixmap(pixmap)            # ❌ DriFox 内粘贴无反应
```

详见 templates-entries.md §9.3（主程序 2026-09 已双类型兼容，插件侧仍以 QImage 为准）。

## 7. 用户提示统一 InfoBar，QToolTip 不可靠

**症状**：`QToolTip.showText` 调用了但提示不显示。

**原因**：主程序源码明确注释「绕开 QToolTip 样式问题」，DriFox 内 QToolTip 不可靠。

**修法**：提示统一走 `qfluentwidgets.InfoBar`：

```python
from qfluentwidgets import InfoBar, InfoBarPosition
InfoBar.success("标题", "内容", parent=main_widget,
                position=InfoBarPosition.BOTTOM, duration=2500)
```

模板与兜底写法见 templates-entries.md §9.4。

## 8. SQLite 并发读与路径兜底

**症状**：卡片读库报 database is locked，或打包后读不到库文件。

**原因**：直接连主会话库且无超时；硬编码开发机路径。

**修法**：路径兜底 + N 天窗口查询模板见 widgets-sqlite.md §一/§二。

> ⚠️ **路径定位的坑看 §26**：不要用 `Path(__file__).resolve().parent.parent...`
> 上溯找数据文件（插件安装位置不固定，会踩到同名空壳库且**完全静默**）。
> §26 给出「自包含约定路径 + 有效性校验」的写法。

## 9. QPlainTextEdit 高度自适应：document().size() 是行数不是像素

**症状**：多行输入框写了 textChanged → setFixedHeight 自适应，但输入多行后框高永远不变（或高得离谱）。

**原因**：QPlainTextEdit 下 `document().size().height()` 和 `documentLayout().documentSize().height()` 返回的都是**行数**，不是像素。10 行文本 doc_h=10，按像素用永远算出小高度。

**修法**：行数 × lineSpacing + 垂直 chrome（QSS padding×2 + border×2 + documentMargin×2 + 余量）；无换行的长段落按 viewport 宽度估算软折行：

```python
fm = edit.fontMetrics()
avail = max(40, edit.viewport().width() - 16)
lines = 0
blk = edit.document().firstBlock()
while blk.isValid():
    w = fm.horizontalAdvance(blk.text())
    lines += max(1, int(w / avail) + (1 if w % avail else 0))
    blk = blk.next()
target = max(36, min(max(1, lines) * fm.lineSpacing() + 24, 160))
```

验证：用 D:\work\DriFox\.venv 的 python（带完整 PyQt5）写 QTimer 序列脚本离线实测，见 plugin-creator troubleshooting.md「热重载」条。

## 10. ExpandSettingCard 覆写 _adjustViewSize 后展开收不回

**症状**：qfluentwidgets ExpandSettingCard 为消除展开大空白覆写 `_adjustViewSize` 把 `spaceWidget.setFixedHeight(0)`，结果展开后点折叠没反应。

**原因**：原版折叠动画靠 spaceWidget 占位提供滚动余量（scrollbar value 从 0 动画到 maximum）。占位归零后 maximum=0，动画失效，`setFixedHeight` 停在展开高度。

**修法**：连 `setExpand` 一起覆写，绕开滚动动画直接切高度：

```python
def setExpand(self, isExpand: bool):
    if self.isExpand == isExpand:
        return
    self.isExpand = isExpand
    self.setProperty("isExpand", isExpand)
    self.setStyle(QApplication.style())
    self.card.expandButton.setExpand(isExpand)
    self._adjustViewSize()

def _adjustViewSize(self):
    h = self.viewLayout.sizeHint().height()
    self.spaceWidget.setFixedHeight(0)
    self.setFixedHeight(self.card.height() + h if self.isExpand else self.card.height())
```

## 11. retheme 重涂陷阱：选择器类型不匹配静默失效

**症状**：改了控件类型（如 QLineEdit → QPlainTextEdit）后主题色/字体从不生效，报错也没有。

**原因**：`_apply_latest_theme/_retheme` 的 QSS 写死了旧控件类型选择器（`QLineEdit { ... }`），选择器不匹配时整段样式静默忽略。

**修法**：改控件类型时同步改 retheme 里的 QSS 选择器。另注意 retheme 循环规则：

- QLabel 带 `keepColor` 属性 → 整个跳过（语义色/字号自管，适合分支名、图例）
- QPushButton 只有带 styleSheet 才被更新（字号替换为 fs-3，下限 11）
- 带 QSS 的控件写死 `font-size` 会覆盖继承的 widget 字体，需跟随系统字体的控件不要写死字号，显式传 `font-family`/`font-size`

## 12. 需要原生 tooltip 时用事件过滤器

> 本条针对 **Qt widget**（卡片按钮等）。**HTML 内容**（欢迎 tab / 消息卡片的
> ```html / ```widget 围栏）的 tooltip 是另一套，见 §24（原生 title 不可控）。

**症状**：`setToolTip("...")` 悬停弹的是主程序自绘气泡，想要系统原生样式做不到。

**原因**：主程序全局 patch 了 `QWidget.setToolTip`，任何 setToolTip 都会被装上自绘 hover filter（QAbstractScrollArea/QLineEdit/QTextEdit 有豁免，但依赖主程序版本）。

**修法**：插件侧装事件过滤器抢占 ToolTip 事件，直接 `QToolTip.showText`（与 #7 不冲突：给用户的操作提示用 InfoBar，控件的悬停说明用本模式）：

```python
class _NativeTooltipFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.ToolTip:
            tip = obj.toolTip()
            if tip:
                QToolTip.showText(event.globalPos(), tip, obj)
            return True
        return False

widget.setToolTip("说明文字")
widget.installEventFilter(_NativeTooltipFilter(widget))
```

## 13. QPlainTextEdit 没有 setText：回调链静默中断

**症状**：提交成功后输入框没清空，且同函数里的后续逻辑（如自动刷新）也没执行，无任何报错。

**原因**：QPlainTextEdit 清空用 `setPlainText("")`；`setText` 只属于 QLineEdit/QLabel。信号回调里抛出的 AttributeError 被吞时，同函数后续语句全部不执行。

**修法**：

```python
edit.setPlainText("")   # ✅ QPlainTextEdit
edit.setText("")        # ❌ QLineEdit/QLabel 的 API，这里 AttributeError
```

**规则**：从单行输入框迁移到多行时全文搜 `.setText(`；信号回调链里的异常要么显式记日志要么别吞。

---

## 14. 工作台插件页拿到的 context 是构造时快照

**症状**：切换项目后插件页仍显示上一个项目的数据（文件树显示旧目录、工作树提示未设置工作目录）；页面显示的路径与 `ctx["project_root"]` 对得上，但就是不变。

**原因**：`WorkbenchPanel._make_page_widget` 只在页面**构造时**推一次 `_build_ui_context()` 的结果。页面把它存成 provider 闭包后一直返回同一份 dict，切项目不会自动更新。

**修法**：

```python
    def __init__(self, parent=None, context=None):
        ...
        # 宿主注入的拉取入口（没有它就拿不到最新 ctx）
        self._host_context_provider = (context or {}).get("context_provider")

    def refresh_data(self) -> None:
        provider = self._host_context_provider
        if callable(provider):
            ctx = provider()          # ✅ 每次拿最新
            self._context = ctx
```

**规则**：插件页的 `context` 只当**初值**用；需要跟随项目 / 工作目录变化时，实现可选协议
`on_project_changed`（宿主切项目时派发），内部走 `refresh_data()` 重取 ctx 并重载数据。
详见 `references/patterns.md §11`。

## 15. config_schema 的 select options 写成 dict → 仓库校验 FAIL

**症状**：插件本地功能正常，`validate_plugins.py` 报
`config_schema.fields.N.options: {...} is not of type 'array'`。

**原因**：主程序契约（`plugin_config.py`）兼容 dict/list/list[dict] 三种写法，
但插件仓库的 JSON Schema 只收 array——运行时兼容 ≠ 校验通过。

**修法**：一律写 `list[dict]`：

```json
"options": [
    {"value": "auto", "label": "自动"},
    {"value": "minimax", "label": "仅 MiniMax"}
]
```

## 16. 宿主打包环境没有 requests → 云端 API 用标准库 multipart

**症状**：插件 import requests 即 ModuleNotFoundError（DriFox 安装版 PyInstaller
`_internal` 未收集 requests/urllib3/httpx）。

**原因**：宿主是 PyInstaller onedir 打包，第三方库只有打包时显式收集的；
插件不能假设宿主有任意网络库。

**修法**：短音频/小文件上传直接用标准库 `urllib.request` 手写 multipart
（边界 + Bearer 头 + `{"text": ...}` 响应，30s 超时）：

```python
def transcribe_wav(url: str, model: str, api_key: str, wav_path: str) -> str:
    body = _build_multipart({"model": model}, Path(wav_path).read_bytes())
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={_boundary}",
    }, method="POST")
    with urllib.request.urlopen(request, timeout=30) as resp:
        return str(json.loads(resp.read().decode()).get("text") or "").strip()
```

大依赖（PIL、numpy 等）走 `_vendor/` 打包（见 templates-plugins.md §五 / testing-vendor.md）。
实战参照：`drifox-plugins2/plugins/voice-input/ui/cloud_recognizer.py`。

## 17. 「插件改了怎么没生效」→ 先分清生效通道

**症状**：改了插件/主程序代码，界面行为照旧。

**原因**：两类代码生效通道不同：
- **插件文件**：源码即所得，但要求目标目录同步 + Python 不命中旧字节码；
- **主程序 UI**（app/ 或设置页控件）：安装版是 PyInstaller pyc，源码改动
  **必须 dev 运行或重新打包**才生效，直接改源码对安装版永远无效。

**修法**：
1. 插件：把仓库目录整个复制到 `~/.drifox/plugins/<name>/`（先删旧目录再复制，
   并递归删 `__pycache__`），重启/热重载生效；
2. 主程序：`python main.py` 跑 dev 验证，或走打包发版流程；
3. 排查时先确认用户跑的是哪个（安装版 vs dev）再看「为什么没生效」。
4. **数据目录分裂**（2026-09-15 实测）：dev 版 app_data_dir 是仓库内 `.drifox/`，
   插件实际加载自 `<仓库>/.drifox/plugins/`；安装版才是 `~/.drifox/plugins/`。
   改了 home 目录的插件对 dev 版永远无效——先确认用户实例加载的是哪份。
5. **UI 组件不参与热重载**：改插件 UI 后 watchfiles 日志出现
   `plugin reloaded: ... ui=False`，输入按钮/浮动卡仍挂旧回调，必须完全重启 DriFox。
---
## 18. 取主程序模型配置：禁直读 app.config，走 services["get_provider_config"]
**症状**：插件发起 LLM 调用报 `401 log in fail: Please carry the API secret key
in the 'Authorization' field (1004)`，但同一服务商在主程序对话里正常。
**原因**：主程序 `SecretMode` 非 `none` 时，`~/.drifox/app.config` 里
`LLM.SavedProviders[*].API_KEY` 是密文（`password` 模式 `enc:v2:…`）或空串
（`keyring` 模式密钥已入系统凭证库）。插件 `json.load` 该文件拿到的是密文/空值，
当 Bearer token 发出必然 401。只有主程序内存态持有解密后的明文。
**修法**：一律走宿主公开服务（输入按钮 context 自 0.6.2 起含 `services` 键）：
```python
# ✅ 正确：宿主读内存态已解锁明文，并叠加模型默认参数
services = context.get("services") or {}
get_cfg = services.get("get_provider_config")
cfg = get_cfg("MiniMax", "MiniMax-M2.7")   # provider/model 均可传空串=当前
client = build_openai_client(api_key=cfg["API_KEY"], base_url=cfg["API_URL"])

# ❌ 错误：直读磁盘（任何密钥模式下都拿不到可用 key）
data = json.load(open(os.path.expanduser("~/.drifox/app.config"), encoding="utf-8"))
key = data["LLM"]["SavedProviders"]["xxx"]["API_KEY"]   # enc:v2:… 或 ""
```
- 判据用「**配置非空**」而非「key 非空」：免鉴权服务商（OpenCode 免费模型等）
  API_KEY 本为空，`build_openai_client` 会剥掉 Authorization 头；
- 显式指名的服务商解析不到 → 返回 `{}`，插件应报「未找到模型配置」，
  **不得静默串到当前会话模型**（用户以为在用 A 实际扣的是 B 的额度）；
- 磁盘文件仍可读**非密钥字段**（`provider_name` / `模型列表`）渲染下拉选项；
- 实战参照：`drifox-plugins2/plugins/prompt-enhancer/ui/__init__.py`（v0.3.0）
  与 `plugins/git-panel/ui/llm_config.py`（v2.0.1），回归测试
  `drifox-plugins2/tests/test_provider_key_channel.py`。
## 19. 卡片弹出独立窗口后「样式全丢（按钮黑块）+ 数据空白」
**症状**：浮动卡右键「弹出」为独立窗口后，窗口能开能拖，但内容页样式全丢
（按钮渲染成黑色方块）、数据空白（如 Git 面板显示不出变更/搁置/分支）。
**原因**：卡片类插件的模板惯例是 **`__init__` 只建骨架，数据加载与主题应用都收敛在
`show_card()`**（浮动卡激活时由宿主调用）；主题样式同理（`_apply_latest_theme` →
`_retheme` 由 show_card 触发）。弹窗宿主早期既不调用 `show_card()`，注入的上下文
又不是完整 provider（`card_info.context_provider` 多数卡片为 None，
拿不到 `project_root`）→ 双失败。
**修法**（主程序侧 2026-09-17 已修，插件侧要点）：
- 内容页按约定实现 `set_context_provider` / `show_card` / `refresh_theme` 三接口
  （模板见 `templates-window.md` §15.3）；
- **别在 `__init__` 里做重加载**：数据/样式统一放 `show_card()`，
  两种宿主（浮动卡 / 独立窗）才能行为一致；
- 老主程序上若弹窗内容不刷新，检查宿主版本是否走激活路径
  （`PluginWindow._build_content` 调 `show_card()`）。

## 20. FramelessWindow 自建窗口：内容把标题栏盖住 → 拖不动、点不到按钮
**症状**：基于 `qframelesswindow.FramelessWindow` 自建窗口时，内容页铺满整个窗口，
标题栏被压在下面——窗口拖不动、最小化/关闭点不到。
**原因**：`FramelessWindow` 的 `titleBar` 是**浮动子控件**（不进布局，靠
`resizeEvent` 定位在顶部 `(0, 0, w, titleBar.height())`）；内容页后创建 z-order 更高，
且根布局 margins=0 时内容从 y=0 起铺。
**修法**：
```python
lay = QVBoxLayout(self)
lay.setContentsMargins(0, self.titleBar.height(), 0, 0)  # 顶部留出标题栏高度
lay.addWidget(content)
self.titleBar.raise_()   # content 晚于 titleBar 创建，必须再置顶一次
```
（参考实现：`app/widgets/plugin_window.py`；TabManagerWindow 同款在
`main_layout.setContentsMargins(0, self.titleBar.height(), 0, 0)`。）
---

## 21. CSS 模板用双花括号 + `.replace()` 填充 → 整份样式表静默失效

**症状**：HTML 结构完全正确、类名全对，但**所有样式都不生效**（布局塌陷、
字号不对、颜色丢失），页面退回浏览器默认样式。**无任何报错**。

**原因**：CSS 模板写成 f-string 的双花括号转义 `.wrap {{ ... }}`，
却用 `.replace("{color}", value)` 填充——双花括号从未被折叠：

```python
_CSS = ".tu-wrap {{ font-family: inherit; }}"   # 双花括号
_CSS.replace("{color}", "#333")                  # 只替换占位符，{{ 仍是 {{
# 结果：.tu-wrap {{ font-family: inherit; }}  ← 整条规则语法错误，浏览器丢弃
```

**为什么测试查不出来**：字符串断言型测试（`assert "tu-grid" in html`）
照样全绿——HTML 里确实有 `tu-grid`，只是 CSS 没生效。**只有截图能发现**。

**修法**：单花括号占位 + `.replace()` 填充（`.format()` 会与 CSS 自身大括号冲突）：

```python
_CSS = ".tu-wrap { font-family: inherit; color: {text}; }"

def _css(p: dict) -> str:
    out = _CSS
    for k, v in (("text", p["text"]), ("border", p["border"])):
        out = out.replace("{" + k + "}", str(v))
    return out

# 回归测试（务必加）
def test_css_has_single_brace(render_mod):
    css = render_mod._css(True)
    assert "{{" not in css and "}}" not in css, "CSS 用了双花括号，整份样式表会失效"
    assert "{text}" not in css, "占位符未填充"
```

> 详见 `checklist.md`「无头截图核对」——字符串断言查不出 CSS 失效。

---

## 22. Chromium 83 禁用特性（Qt 5.15.2 的 WebEngine）

**症状**：本机 Chrome 预览一切正常，宿主 QWebEngineView 里布局错乱 / 横向溢出。

**原因**：Qt 5.15.2 内置 **Chromium 83**（2020 年版本），比当下浏览器落后数代。
宿主已知约束见 `app/widgets/card_render_core.py`（mermaid 10 需 polyfill 即因缺
`structuredClone`）。

**禁用清单与替代**：

| 不可用 | 最低版本 | 替代方案 |
|--------|---------|---------|
| flex `gap` | 84 | 用 `margin`（`.col { margin-right: 2px }`） |
| `aspect-ratio` | 84 | `width:100%; padding-bottom:100%` 撑正方形 |
| `:has()` | 105 | JS 加类 |
| CSS 嵌套 | 112 | 展平写 |
| `min()` / `max()` | 79 / 75 | 可用但混用易踩，尺寸统一 px |
| `::-webkit-tooltip` | — | **不存在**，原生 tooltip 无法定制（见第 24 条） |

**可用**：`flex` 全部属性、`grid`、`@media`、`::after` + `attr()`、
`transform`、CSS 变量、`position: sticky`。

```python
# ❌ Chromium 83 下 gap 无效（元素贴边）
.tu-week { display: flex; gap: 2px; }

# ✅ 用 margin
.tu-week { display: flex; }
.tu-cell { margin-right: 2px; margin-bottom: 2px; }
```

---

## 23. 欢迎 tab 宽度是动态的 → 热力图/网格不能写死 px

**症状**：宽窗口下热力图右侧留一大片空白；窄窗口下横向溢出、出现滚动条。

**原因**：欢迎卡片宽度由主程序按 `parent_width - 20` 动态计算（下限 320px，
`MessageCard.sync_width`），渲染期拿不到最终宽度，且窗口可随时 resize。

**修法**：列宽用 flex 均分，格子尺寸全部走百分比。

```html
<!-- ❌ 写死 12px：窄窗口 53 列 = 636px 溢出 -->
<div class="tu-week" style="width:12px">

<!-- ✅ 均分容器，任意宽度都不溢出 -->
<style>
.tu-grid { display: flex; }
.tu-week { flex: 1 1 0; min-width: 0; margin-right: 2px; }  /* margin 非 gap */
.tu-cell { display: block; width: 100%; padding-bottom: 100%; }  /* 正方形 */
</style>
```

**月/日标签的窄屏处理**：标签宽度固定、容器宽度运行时才知道，只能靠媒体查询近似
（Chromium 83 支持 `@media`）：

```html
<span class="tu-month">10<span class="tu-month-suffix">月</span></span>
<style>
@media (max-width: 560px) { .tu-month-suffix { display: none; } }  /* 窄屏只留数字 */
</style>
```

---

## 24. 原生 `title` tooltip 样式不可控 → 改 CSS `::after` + `attr()`

**症状**：悬停提示在浅色主题下是**黑底 + 系统默认大字号**，与卡片风格割裂；
且明暗跟随操作系统而非 Qt 主题。

**原因**：`title` 属性由 **OS 绘制**，插件无法控制其任何样式
（Chromium 83 无 `::-webkit-tooltip` 可覆盖，见第 22 条）。
`prefers-color-scheme` 跟随 OS 而非 `theme_manager`，Qt 暗色 + OS 亮色时不生效。

**修法**：`data-tip` + `::after`：

```python
# 配色与 context-stats 的 echarts tooltip 同源，两插件观感统一
TIP_BG_DARK = "rgba(30,34,42,0.92)"
TIP_BG_LIGHT = "rgba(255,255,255,0.96)"
```

```html
<span class="tu-cell" data-tip="2026-10-21 · 总 120.7K · 69 次请求"></span>
<style>
.tu-cell { position: relative; }                    /* 伪元素定位参照，必须 relative */
.tu-cell:hover::after {
  content: attr(data-tip);
  position: absolute; bottom: 130%; left: 50%; transform: translateX(-50%);
  background: {tooltip_bg}; border: 1px solid {tooltip_border}; color: {text};
  font-size: 11px; white-space: nowrap; padding: 4px 7px; border-radius: 5px;
  pointer-events: none;                              /* 不拦截点击 */
}
/* 右边缘列翻到左侧，否则被容器裁掉 */
.tu-week:nth-last-child(-n+3) .tu-cell:hover::after { left: auto; right: 0; transform: none; }
</style>
```

> ⚠️ 验证陷阱：原生 `title` 在 OS 层绘制，**无头截图抓不到**；CSS `::after` 可以。
> 但强制显示 hover 态时，必须从插件真实 CSS 里 `:hover` → `.force` **全量替换**
> （只写 `content` 的话 `background`/`border` 全丢，因为伪元素样式不继承）。

---

## 25. 缓存 key 写读共用易变值 → 永远判未命中（无限重算）

**症状**：欢迎 tab 永远停在「加载中」，数据永不出现；日志有"任务启动"无"任务完成"。

**原因**：`get_cached()` 读取时**重算** key，而 key 含 `db mtime_ns`；
采集完成时写入的 key 是**那一刻**的 mtime。采集期间有写入（正常对话必然产生），
读时 mtime 已变 → 两次 key 不等 → 判定未命中 → 每次重起线程 → 采集完 mtime 又变。
**自相矛盾，永不收敛**。

```python
# ❌ 写与读共用易变 key
def build_cache_key() -> tuple:
    return (db_path.stat().st_mtime_ns, date.today().isoformat())

def get_cached():
    if _cache is None or _cache_key != build_cache_key():   # ← 每次重算
        return None
```

**修法**：读路径只按 TTL 判；db 变化交给单独的 stale 检查。

```python
def get_cached(max_age: float = 60.0) -> Optional[dict]:
    with _lock:
        if _cache is None:
            return None
        if _cache.get("error") and time.monotonic() - _cache_ts >= _ERROR_TTL:
            return None                       # 错误短 TTL，允许自动重采
        if time.monotonic() - _cache_ts >= max_age:
            return None
        return _cache

def _is_stale() -> bool:
    """db 在采集完成后又变了 → 数据不全，需重采"""
    with _lock:
        return _cache is not None and _cache_key != build_cache_key()
```

**规则**：**易变值（mtime / 计数器 / 外部状态）只能参与"是否需要重算"的判断，
绝不能参与"缓存是否有效"的判断**。后者用 TTL / 版本号（写入时冻结的）。

**诊断捷径**：宿主日志 `.drifox/logs/all.log` 里对比"启动"与"完成"两行——
有前者无后者 = 回调链路断了；两者都有但 UI 不更新 = key 判据问题。

---

## 26. 插件定位数据文件：不要靠 `__file__` 上溯，会踩到同名空壳

**症状**：面板永远「暂无数据」，**日志无任何报错**（查询合法，只是结果为空）。

**原因**：按「插件文件向上 N 级 = 项目根」的直觉定位数据目录，但插件的
**安装位置不固定**，上溯必然落到意料之外的目录：

| 运行环境 | 插件实际位置 | 上溯落到 |
|---------|-------------|---------|
| 开发（仓库内） | `<repo>/plugins/x/ui/` | 仓库根 ✅ |
| 用户安装 | `~/.drifox/plugins/x/ui/` | `~/.drifox/` ❌ |
| PyInstaller | `<解包目录>/plugins/x/ui/` | 随解包路径漂移 ❌ |

实测事故：`plugins/token-usage/ui/data.py` 上溯 5 级落到 `D:/work/.drifox/sessions.db`
—— 一个**结构完整但 0 行的空壳库**，抢占了真实库 `~/.drifox/sessions.db`。
查询能执行、结果为空，因此完全静默。

**修法**：自包含地按约定路径找 + **校验候选有效性** + 宿主路径作可选增强。

```python
import os
import sqlite3
from pathlib import Path
from typing import Optional

# 约定路径（自包含：不依赖宿主内部实现）
_CANDIDATES = [
    Path(".drifox") / "data.db",                          # 开发：cwd（与宿主一致）
    Path.home() / ".drifox" / "data.db",                   # 打包后：~/.drifox
    Path.home() / "Library/Application Support/App/.drifox/data.db",  # macOS
]

_found: Optional[Path] = None


def _host_data_dir() -> Optional[Path]:
    """宿主数据目录：可选增强，拿不到就用约定路径继续

    ⚠️ 宿主内部模块（app/ 下），仅作增强，绝不能是硬依赖 ——
    宿主重构 / 插件跑在裸解释器里都必须能降级。见 §27。
    """
    env_dir = os.environ.get("DRIFOX_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    try:
        from app.utils.utils import get_app_data_dir   # 宿主内部实现

        return Path(get_app_data_dir())
    except Exception:
        return None


def _valid(path: Path) -> bool:
    """候选有效性：结构完整且非空壳（同名空壳库是最隐蔽的坑）"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            return row is not None and conn.execute("SELECT COUNT(1) FROM sessions").fetchone()[0] > 0
        finally:
            conn.close()
    except Exception:
        return False


def _find_db() -> Optional[Path]:
    """按可信度找数据文件：宿主目录（增强） → 约定路径（自包含）"""
    global _found
    if _found is not None and _found.exists():
        return _found

    candidates = []
    host_dir = _host_data_dir()
    if host_dir is not None:
        candidates.append(host_dir / "data.db")
    candidates.extend(_CANDIDATES)

    existing = [p for p in candidates if p.exists()]
    for p in existing:                       # 有效性校验挡掉空壳库
        if _valid(p):
            _found = p
            return p
    _found = existing[0] if existing else None
    return _found
```

**规则**：
1. 定位失败要**有日志**（`_found is None` 时 warn 一次），别让「没找到」和「库是空的」同症状
2. 候选库**必须验有效性**，空壳库与真实库同名时前者会静默抢占
3. 数据目录与宿主实现解耦：约定路径兜底，宿主路径只是快捷方式

---

## 27. 自包含优先：宿主内部模块只能作「可选增强」

**症状**：插件在某宿主版本上突然整块失效（`ImportError` 传播到 UI 加载），
或某个功能静默降级没人发现。

**原因**：插件把宿主**内部实现**（`app/` 下任何模块）当成硬依赖 ——
它没有稳定性承诺，宿主重构即失效；插件也可能被单独 import 测试（无宿主）。

**修法**：约定路径/纯 stdlib 自取为主，宿主 import 一律 try/except 降级。

```python
# ❌ 硬依赖：宿主重构或裸解释器环境直接炸
from app.utils.utils import get_app_data_dir
db = Path(get_app_data_dir()) / "sessions.db"

# ✅ 自包含为主 + 宿主为可选增强
def _host_data_dir():
    try:
        from app.utils.utils import get_app_data_dir   # 仅增强
        return Path(get_app_data_dir())
    except Exception:
        return None
# 拿不到就用自己的约定路径继续（见 §26）
```

**判据**：把插件的 `ui/` 目录单独拷出来、删掉所有 `app.*` import，
它应该仍能跑出正确结果（只是可能用默认路径/默认色）。

**例外**：经 `UIPluginRegistry` 注册表拿到的**正式扩展点**
（`registry.get_instance()`、`ctx["is_dark"]`、`design_tokens.scale_font_size`）
可以用——那是宿主承诺的契约；但仍建议 try/except + 缺省值，别让异常冒到渲染层。

---

## 28. 无头截图是唯一能查出 CSS 失效的手段

**症状**：单元测试全绿、HTML 结构正确、类名正确，**但页面上样式完全没生效**。

**原因**：见第 21 条。字符串断言查不出 CSS 是否被浏览器接受。

**修法**：明/暗 × 宽/窄 四种组合各截一张，逐张核对。

```python
# 包成完整 HTML 落盘（欢迎 tab 的片段本身没有 <html>/<style> 外壳）
page = f'<!doctype html><html><head><meta charset="utf-8"><style>' \
       f'body{{margin:0;padding:24px;background:{bg};font-family:"Microsoft YaHei",sans-serif;}}' \
       f'.wrap{{max-width:{width}px;margin:0 auto;}}</style></head>' \
       f'<body><div class="wrap">{body}</div></body></html>'
Path("tmp_shot.html").write_text(page, encoding="utf-8")
```

```bash
chrome --headless --disable-gpu --screenshot=out.png \
       --window-size=780,620 --hide-scrollbars "file:///abs/path/tmp_shot.html"
```

**核对清单**：① 四指标/网格都渲染 ② 明暗配色各自正确 ③ 窄窗口不溢出
④ 月标签不粘连 ⑤ tooltip 悬停态可见。

**交互态（hover/tooltip）的诊断**：页内 JS 把 `getComputedStyle` 写进 DOM 再截图：

```javascript
var cs = getComputedStyle(cell, '::after');
box.textContent = 'content=' + cs.content + ' bg=' + cs.backgroundColor
                 + ' bottom=' + cs.bottom;
```

比反复截图试参数快得多。

---

## 29. 回归测试要验证"能抓住 bug"，不是"能通过"

**症状**：新写的测试全绿，但把 bug 原样注回去，测试**依然全绿**（假阳性）。

**原因**：测试的前置条件没让断言真正生效。例：
测试写 `invalidate(); assert get_cached() is None`，但 `get_cached()` 因
**TTL 已过期**本就返回 None——断言通过与 invalidate 毫无关系。

**修法**：断言前先确认"不改也会失败"的前置态。

```python
def test_invalidate_clears_cache(collector_mod, monkeypatch):
    monkeypatch.setattr(collector_mod, "_cache", {"daily": {}})
    monkeypatch.setattr(collector_mod, "_cache_key", (1, "2026-01-01"))
    monkeypatch.setattr(collector_mod, "_cache_ts", time.monotonic())
    assert collector_mod.get_cached() is not None, "前置：缓存应在 TTL 内命中"
    collector_mod.invalidate()
    assert collector_mod._cache is None          # 查内部变量，不依赖 TTL
```

**验收标准**：注入 N 处回归（每处对应一个真实 bug 或关键约束），
要求**全部精确打红**。脚本模板：

```python
MUTATIONS = [
    ("退回原生 title", 'data-tip="{...}"', 'title="{...}"'),
    ("色阶方向反转", LEVELS_ASC, LEVELS_DESC),
]
for name, old, new in MUTATIONS:
    src = path.read_text(encoding="utf-8")
    assert old in src, f"锚点未命中: {name}"      # 锚点失效要报，别静默跳过
    path.write_text(src.replace(old, new, 1))
    rc, out = run_tests()
    path.write_text(src)                          # 还原
    assert rc != 0, f"❌未被发现: {name}\n{out}"
```

> 断言 `old in src`：锚点没命中说明代码变了，这条变异根本没注入成功，
> 静默跳过会让人误以为"这条变异不影响功能"。

---
> 新坑写回格式：`## N. 标题` + **症状/原因/修法** 三段 + 可运行代码片段。
