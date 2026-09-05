# workflow

受限 Python 脚本编排子智能体（对齐 Claude Code dynamic workflows 体验）。
模型一次性写出编排程序（条件/循环/并行扇出），脚本经钩子派发子智能体；
默认后台运行立即返回，对话流实时卡片可见进度。

## 用法

### 脚本钩子（def 定义，签名严格）

- `agent(prompt, agent=角色, phase=分组, label=标签, model=别名, schema=JSONSchema) -> str|None`
  - 跑一个子智能体到完成，返回最终文本，失败/超时返回 None
  - `model` 需在插件配置 `model_aliases` 注册别名（如 `sonnet=模型ID`），未注册别名降 None 并在日志提示
  - `schema` 传 JSON Schema dict：返回校验通过的 dict（失败自动带错重试 1 次，仍失败降 None）
- `parallel([零参函数])` 并发执行并等全部（屏障）；异常项降 None；不支持嵌套
- `pipeline(items, *stages)` 无屏障流水线，每项独立流过各阶段
- `phase(title, detail=None)` 进度分组，detail 上卡片副标题
- `log(msg)` 进度消息
- 最终结果赋给 `result` 变量

### 工具参数（action）

| action | 参数 | 行为 |
|---|---|---|
| `run`（默认） | script+meta / from_saved | 执行；默认后台立即返回 `{run_id, status}` |
| `save` | save_as + script+meta | 存为可复用 workflow（同名覆盖） |
| `list` | — | 列已存 workflow 与最近 runs |
| `load` | name | 读存档（script 全文 + meta + 默认 args） |
| `status` | run_id | 查任务状态；`html=true` 返回渲染卡片 |
| `resume` | run_id | 从原 run 续跑：指纹命中的 agent 回放结果不真跑 |

- `foreground=true`：同步执行到完成（旧行为）
- `from_saved="名字"`：跑已存 workflow，`args` 覆盖存档默认值

### 存档目录

`<app_data_dir>/workflows/`：`saved/<name>.py`（可复用）+ `runs/<run_id>/`
（`script.py` / `meta.json` / `journal.jsonl` / `status.json` / `result.json`）。

### resume 注意

agent 指纹 = sha256(prompt+角色+model+schema)。prompt 内拼时间戳/随机数会指纹
漂移导致 resume 全量重跑；保持 prompt 确定性。

### 沙箱边界

禁止 import（预置 `json`/`math`/`re`/`statistics`/`datetime`）；无文件/网络能力；
宿主内部变量（`_` 开头）不在脚本命名空间。违反项在脚本预检阶段直接拦截报人话。

## 与其他工具分工

- 一两个委派 → `subagent_para`
- 固定依赖图 → `subagent_dag`
- 条件/循环/数据变换的大规模扇出 → `workflow`

## 配置

设置卡可调：并发上限 / 总数上限 / 单次项数 / 总时长 / 单 agent 等待上限 / 默认角色 /
结果字符上限 / `model_aliases`（别名=模型ID，逗号分隔）/ `default_foreground`（默认前台）/
`card_refresh_ms`（卡片轮询间隔）。
环境变量 → 设置值 → 内置默认，三级链。
