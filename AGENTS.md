# 项目开发规范

AI Agent 操作手册与约束。

---

## 1. 目标与边界

**允许**: 读写顶层/docs/skills等；执行 lint/构建/测试；增改功能修复问题。
**禁止**: 改 CI 配置（除非明确要求）、改 LICENSE、硬编码密钥、大范围重构。
**敏感**: `.github/workflows/*.yml`、`.env*`。

---

## 2. 推荐执行路径

```bash
git pull --rebase origin develop
uv sync --group dev
ruff check . && ruff format --check .    # 代码检查
pytest tests/ -x                         # 测试
# 修改...
pytest tests/ -x                         # 复测
git add -A && git commit -m "feat|fix|docs|chore: scope - summary"
git push origin develop
```

**单测**: `pytest tests/test_xxx.py -v` **打包**: `python build.py`

---

## 3. 项目结构

| 目录 | 职责 |
|---|---|
| `app/core/` | 引擎：backend、chat_session、hook_manager、workers |
| `app/gateway/` | 多平台网关（钉钉/Telegram/Discord/飞书） |
| `app/tools/` | 35+ 工具（读/写/搜索/MCP） |
| `app/widgets/` | UI 组件、设置卡片、像素宠物 |
| `plugins/system/` | 插件：hooks、skills、themes、commands |
| `tests/` | 测试 |

---

## 4. 关键依赖

Python 3.14+、PyQt5、PyQt-Fluent-Widgets、openai、loguru、httpx、mcp、pygls。可选组：gateway、dev、build。

---

## 5. 风格规范

- **格式化**: ruff（行宽120，双引号）
- **导入**: 标准→三方→本地
- **命名**: 代码英文；文件名小写中划线；注释中文
- **设计**: 函数短小单一职责

---

## 6. 提交规范

```
feat|fix|docs|chore|refactor|test: scope - summary
```

**强制同步**: 任何变化必须同步更新文档。不确定用 TODO。

---

## 7. 已修复

- **hook 双重注入** (06-26): `on_hook_finished` 写入双路径导致翻倍。修复：只写格式化版本 + 去重。
- **工具循环卡死** (06-26): V2 重写截断逻辑，保留正确轮次。测试 21 项全过。
- **tool_control_card 不更新** (06-18): 补全信号链、删除重复定义。
- **卡片内部滚轮 race condition** (07-04): 流式输出时 `_suppressScrollEvent=false` 同步解除，但 Chromium scroll 事件异步派发，pending scroll 在 suppress 解除后才被 dispatch，错误地把程序 auto-scroll 标记为"用户主动滚动" → `_userScrolledWithin=true` 累积 → 后续 updateContent 跳过 auto-scroll → 视觉上"卡顶部"。修复：在所有 auto-scroll 入口打 `_autoScrollTime = performance.now()` 时间戳；scroll 事件回调增加 50ms 时间窗检查，识别程序触发的事件并跳过标记。
- **AGENTS.md 偶尔被清空** (07-10): `MemoryManagerCore.save_project_note` 直接 `write_text(content)`，无空内容/纯空白检查 → UI 编辑器被全选删除后 300ms 防抖自动保存（或上游传空 content）会把 AGENTS.md 写成 0 字节。修复：在 `save_project_note` 入口加 `if not (content or "").strip(): return False, log warning`，保护磁盘上已有内容不被空写入覆盖。回归测试：`tests/core/test_memory_manager_empty_protection.py`（7 项用例）。`read_project_notes.hook` 的"0 字节 → 恢复 INITIAL_TEMPLATE"逻辑保留作为纵深防御。
- **工具运行折叠框偶尔不转为完成框并持续累积** (07-12): 运行折叠框由 `_find_latest_assistant_card()` 创建，工具结果由 `self._current_assistant_card` 写入。当二者指向不同卡片（skill 结果 / 子智能体回调 / compact 等会创建新 assistant 卡片的子流程）时，结果写入错误卡片 → 该卡片 `append_tool_result` 的 `querySelector('[data-tool-call-id=...]')` 找不到运行框，走兜底 `data-tool-injected` 追加新完成块；而原卡片 viewer 的 restore 逻辑因 markdown 无该 id 完整块，把"仍在运行"的块复活 → 永久卡在运行中并持续累积（消息内容仍正常更新）。修复（两层防御）：(1) `main_widget.py` 新增 `_tool_card_map`（`tool_call_id→卡片`），`_on_tool_args_updated`/`_on_tool_call_started` 记录归属，`_on_tool_result_received` 用 `target_card = self._tool_card_map.get(tool_call_id) or self._current_assistant_card` 确保结果写入与运行框同一张卡片；(2) `message_card.py` 把 `MessageCard._finished_streaming_ids` 共享给 viewer 的 `_restore_finished_ids`，`_perform_update` 的两个 restore 兜底逻辑若发现某 id 已收到结果但其 DOM 块仍是 `data-streaming="true"`，则强制转为完成态。回归测试：`tests/widgets/test_message_card_tool_streaming.py`。
