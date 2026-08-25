"""长时间运行性能压测。

与 `tests/perf/` 静态源码分析测试不同，本子目录为运行时压测：
- 直接构造 ChatBackend / SessionManager / PluginManager 等真实组件
- 每分钟采样 RSS / QObject 总数 / tracemalloc 顶部分配点
- 输出 CSV/JSON + 内存曲线图 + Markdown 报告
- pytest `-m perf_long` 触发；环境变量 `LONGRUN_FULL=1` 切换全量模式（≥5万次操作）
"""
