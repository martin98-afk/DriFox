# 可复用控件 — SQLite 读取模式

> 来自 `context-usage-stats` 等项目提炼的 SQLite 数据读取最佳实践。
> 索引和设计原则见 `widgets.md`。

---

## 一、路径查找（dev / 用户环境兜底）

> 插件可能运行在两种环境：
> - **dev 环境**：直接 `python main.py`，DB 在项目根目录 `.drifox/sessions.db`
> - **打包后**：用户在 `~/.drifox/sessions.db`（用户目录）
>
> 必须两种环境都支持，否则打包后插件会找不到数据库。

### 1.1 `_find_db()`

```python
from pathlib import Path
from typing import Optional

# 假设插件位于 plugins/<plugin-name>/ui/cards.py
# 那么 _PROJECT_ROOT 是 plugins/<plugin-name>/ 向上 4 级 → 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEV_DB_PATH = _PROJECT_ROOT / ".drifox" / "sessions.db"
_USER_DB_PATH = Path.home() / ".drifox" / "sessions.db"


def _find_db() -> Optional[Path]:
    """查找 DB 文件路径（开发环境 → 用户目录兜底）

    Returns:
        找到的 DB 路径；都没找到返回 None
    """
    if _DEV_DB_PATH.exists():
        return _DEV_DB_PATH
    if _USER_DB_PATH.exists():
        return _USER_DB_PATH
    return None
```

### 1.2 `_get_db_connection()`

```python
import sqlite3
from typing import Optional

from loguru import logger


def _get_db_connection() -> Optional[sqlite3.Connection]:
    """获取 SQLite 连接（只读模式，超时 3s）

    Returns:
        sqlite3.Connection 或 None（找不到 DB 或连接失败）
    """
    db_path = _find_db()
    if db_path is None:
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        conn.row_factory = sqlite3.Row  # 字典式访问 row["column_name"]
        return conn
    except Exception as e:
        logger.error(f"[<plugin>] 无法打开数据库: {e}")
        return None
```

### 1.3 路径层级计算说明

```
plugins/
└── <plugin-name>/
    └── ui/
        └── cards.py     ← 当前文件

# Path(__file__).resolve()
#   = plugins/<plugin-name>/ui/cards.py

# .parent.parent.parent.parent  (向上 4 级)
#   = . (项目根目录)

# 验证：
#   Path("plugins/my-plugin/ui/cards.py").resolve().parent.parent.parent.parent
#   = Path(".").resolve()
```

如果你的插件在用户目录 `~/.drifox/plugins/<plugin-name>/`，调整向上层级数即可。

---

## 二、N 天窗口查询模板

> 最常用的查询模式：展示"最近 N 天每天的某指标"。
> 核心思想：**先建日期标签 → 初始化全 0 map → 查询后归类 → 按标签顺序输出**。
> 这样保证没有数据的日期也会显示为 0，而不是缺失。

### 2.1 通用模板

```python
from datetime import datetime, timedelta
from typing import Dict, List, Tuple


def _fetch_last_n_days(conn, days: int = 14) -> List[Tuple[str, int]]:
    """通用 N 天窗口查询模板

    Returns:
        [(日期标签 "MM-DD", 该日统计值), ...] 按日期升序（最早 → 最近）
    """
    # 1. 建标签列表（最早 → 最近）
    today = datetime.now()
    date_labels = [
        (today - timedelta(days=i)).strftime("%m-%d")
        for i in range(days - 1, -1, -1)
    ]

    # 2. 初始化 map（全 0）
    daily_map: Dict[str, int] = {dl: 0 for dl in date_labels}

    # 3. 查询数据库并归类
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT DATE(created_at) as day, COUNT(*) as cnt "
        f"FROM your_table "
        f"WHERE created_at >= date('now', '-{days} days') "
        f"GROUP BY DATE(created_at) ORDER BY day"
    )
    for row in cursor.fetchall():
        day_str = row["day"]
        if not day_str:
            continue
        try:
            # 4. 转 "YYYY-MM-DD" → "MM-DD" 标签
            label = datetime.strptime(day_str, "%Y-%m-%d").strftime("%m-%d")
            daily_map[label] = row["cnt"]
        except (ValueError, TypeError):
            continue  # 跳过无法解析的日期

    # 5. 按标签顺序输出（保证图表 X 轴顺序正确）
    return [(dl, daily_map[dl]) for dl in date_labels]
```

### 2.2 多指标并行查询模板

```python
def _fetch_multiple_daily_stats(conn, days: int = 14) -> dict:
    """一次查询获取多个指标的每日数据

    适用：每日会话数 + 每日消息数 这种"按日分组"的场景，
    用一次 SUM/COUNT 同时算出，避免多次查询。
    """
    today = datetime.now()
    date_labels = [(today - timedelta(days=i)).strftime("%m-%d")
                   for i in range(days - 1, -1, -1)]

    sessions_map: Dict[str, int] = {dl: 0 for dl in date_labels}
    messages_map: Dict[str, int] = {dl: 0 for dl in date_labels}

    cursor = conn.cursor()
    cursor.execute(
        f"SELECT DATE(created_at) as day, "
        f"COUNT(*) as cnt, "
        f"COALESCE(SUM(message_count), 0) as msgs "
        f"FROM sessions "
        f"WHERE created_at >= date('now', '-{days} days') "
        f"AND project NOT LIKE '__archived__%' "
        f"GROUP BY DATE(created_at) ORDER BY day"
    )
    for row in cursor.fetchall():
        try:
            label = datetime.strptime(row["day"], "%Y-%m-%d").strftime("%m-%d")
            sessions_map[label] = row["cnt"]
            messages_map[label] = row["msgs"]
        except (ValueError, TypeError):
            continue

    return {
        "daily_sessions": [(dl, sessions_map[dl]) for dl in date_labels],
        "daily_messages": [(dl, messages_map[dl]) for dl in date_labels],
    }
```

### 2.3 为什么不用 `Pandas` / `Arrow`？

| 场景 | 推荐 |
|------|------|
| 14 天窗口、几百条数据 | 直接 SQLite + Python 字典（最简单） |
| 几万条数据 + 复杂聚合 | 考虑在主程序构建期声明 `pandas` |
| 跨表 JOIN + 时间序列 | 同上，`pandas` 更省事 |

> UI 插件通常数据量小，**避免为了简单查询引入 `pandas`**——既增加打包体积又增加冷启动时间。

---

## 三、字段 fallback 模式（新字段缺失时回退估算）

> 常见场景：数据库升级加了 `context_usage` 列，但旧数据该列为 0 / NULL。
> 不能只查新列（旧数据统计为 0），也不能每次都回退（影响性能）。
> 正确做法：**精确值 + 估算值分开查，最后相加**。

### 3.1 标准 fallback 模式

```python
def _fetch_with_fallback(conn) -> int:
    """context_usage 缺失时回退到 messages 估算（兼容旧数据）

    模式：
    1. 先查精确值（context_usage > 0 的记录）
    2. 再查估算值（context_usage = 0 的旧记录，用 messages 估算）
    3. 相加得到完整总量
    """
    cursor = conn.cursor()

    # 1. 精确值（新数据）
    cursor.execute(
        "SELECT COALESCE(SUM(context_usage), 0) as total "
        "FROM sessions WHERE context_usage > 0"
    )
    total = cursor.fetchone()["total"]

    # 2. 估算值（context_usage 缺失的旧记录）
    cursor.execute(
        "SELECT messages FROM sessions "
        "WHERE (context_usage IS NULL OR context_usage = 0) "
        "AND messages IS NOT NULL AND messages != ''"
    )
    for row in cursor.fetchall():
        msg_data = row["messages"]
        if isinstance(msg_data, (str, bytes)):
            # 截断 100k 字符防 OOM（messages JSON 可能很大）
            total += _fast_estimate_tokens(str(msg_data)[:100000])

    return total
```

### 3.2 时间窗口内的 fallback（用于折线图每日统计）

```python
def _fetch_daily_tokens_with_fallback(conn, days: int = 14) -> List[Tuple[str, int]]:
    """14 天窗口 + 精确值/估算值分别查询后合并"""
    today = datetime.now()
    date_labels = [(today - timedelta(days=i)).strftime("%m-%d")
                   for i in range(days - 1, -1, -1)]
    daily_map: Dict[str, int] = {dl: 0 for dl in date_labels}

    cursor = conn.cursor()

    # 1. 精确值（context_usage > 0 的会话）
    cursor.execute(
        f"SELECT DATE(created_at) as day, COALESCE(SUM(context_usage), 0) as total_tokens "
        f"FROM sessions "
        f"WHERE created_at >= date('now', '-{days} days') "
        f"AND context_usage > 0 "
        f"GROUP BY DATE(created_at) ORDER BY day"
    )
    for row in cursor.fetchall():
        try:
            label = datetime.strptime(row["day"], "%Y-%m-%d").strftime("%m-%d")
            daily_map[label] = row["total_tokens"]
        except (ValueError, TypeError):
            continue

    # 2. 估算值（context_usage 缺失的旧会话，逐条加到对应日期）
    cursor.execute(
        f"SELECT DATE(created_at) as day, messages "
        f"FROM sessions "
        f"WHERE created_at >= date('now', '-{days} days') "
        f"AND (context_usage IS NULL OR context_usage = 0) "
        f"AND messages IS NOT NULL AND messages != '' "
        f"ORDER BY created_at DESC"
    )
    for row in cursor.fetchall():
        try:
            label = datetime.strptime(row["day"], "%Y-%m-%d").strftime("%m-%d")
            msg_data = row["messages"]
            if isinstance(msg_data, (str, bytes)):
                daily_map[label] += _fast_estimate_tokens(str(msg_data)[:100000])
        except (ValueError, TypeError):
            continue

    return [(dl, daily_map[dl]) for dl in date_labels]
```

### 3.3 截断长度选择（防 OOM）

| 场景 | 推荐截断长度 | 说明 |
|------|------------|------|
| 单条消息内容 | 4,000 字符 | 单条消息超过 4k 字符极少见 |
| 整条 messages JSON | 100,000 字符 | 会话级别 JSON，保留大部分内容 |
| 整个数据库扫描 | 50,000 字符/行 | 保守估计，避免内存爆炸 |

> 截断后 token 估算会偏低，但作为 fallback 已经够用。

---

## 四、常见陷阱

| 陷阱 | 症状 | 修复 |
|------|------|------|
| 路径层级数错 | 找不到 dev 环境 DB | 打印 `_PROJECT_ROOT` 验证 |
| 打包后 DB 找不到 | 用户环境运行失败 | `_find_db()` 兜底到 `~/.drifox/` |
| `row_factory` 未设置 | `row[0]` 数字索引 | `conn.row_factory = sqlite3.Row` |
| 数据库被锁 | "database is locked" | `timeout=3` + 短事务 |
| 旧数据 NULL | `TypeError: unsupported operand` | `COALESCE(col, 0)` 包一层 |
| messages 太大 OOM | 进程崩溃 | `str(msg_data)[:100000]` 截断 |
| 时区不一致 | `DATE(created_at)` 跨天 | SQLite 默认 UTC，与本地时间差 8h |
| 字符串字段非 str | bytes 类型导致 split 报错 | `isinstance(msg_data, (str, bytes))` 防御 |

### 4.1 时区问题（最隐蔽）

```sql
-- SQLite 的 DATE() 函数默认按 UTC 时区处理 created_at
SELECT DATE(created_at) as day FROM sessions;

-- 如果你的 created_at 是本地时间（北京 UTC+8），
-- 那"晚上 23 点的对话"会被归到第二天的 DATE() 结果里

-- 修复方法（按需）：
-- 1. 写入时统一转 UTC
-- 2. 查询时用 DATETIME() 转换：DATETIME(created_at, 'localtime')
-- 3. 或在 Python 层 datetime.strptime 时调整
```

### 4.2 数据库锁定问题

```python
# 问题：UI 插件在后台线程查询时，主程序同时写入 → "database is locked"

# 解决 1：超时重试
def _get_db_connection_with_retry(max_retries: int = 3) -> Optional[sqlite3.Connection]:
    for i in range(max_retries):
        conn = _get_db_connection()
        if conn is not None:
            return conn
        time.sleep(0.1 * (i + 1))  # 100ms, 200ms, 300ms
    return None

# 解决 2：用 WAL 模式（需要主程序启用）
# conn.execute("PRAGMA journal_mode=WAL")  # ← 不要在 UI 插件里改这个，让主程序决定
```

---

## 五、完整读取函数骨架

```python
def _fetch_all_stats() -> dict:
    """主读取函数：返回卡片需要的全部数据

    返回 dict 是为了：1) 错误处理统一 2) 易于扩展字段 3) 易于 mock 测试
    """
    result = {
        "total_sessions": 0,
        "total_messages": 0,
        "total_tokens": 0,
        "daily_sessions": [],
        "daily_messages": [],
        "daily_tokens": [],
        "error": None,
    }

    conn = _get_db_connection()
    if conn is None:
        result["error"] = "无法连接到数据库"
        return result

    try:
        cursor = conn.cursor()

        # 1. 基础统计
        cursor.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(message_count), 0) as msgs "
            "FROM sessions"
        )
        row = cursor.fetchone()
        result["total_sessions"] = row["cnt"]
        result["total_messages"] = row["msgs"]

        # 2. 14 天窗口数据
        daily = _fetch_multiple_daily_stats(conn, days=14)
        result["daily_sessions"] = daily["daily_sessions"]
        result["daily_messages"] = daily["daily_messages"]

        # 3. Token 总量（带 fallback）
        result["total_tokens"] = _fetch_with_fallback(conn)

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"[<plugin>] 数据读取失败: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return result
```

## 六、何时不用 SQLite 直读，改用主程序 API

| 场景 | 推荐 |
|------|------|
| 读自己的独立 DB | ✅ 直读（如插件专属 `~/.drifox/my-plugin/data.db`） |
| 读主程序 `.drifox/sessions.db` | ⚠️ 直读 + 注意时区/锁问题（context-usage-stats 就是这样做的） |
| 读主程序内存中的对象 | ❌ 违反插件闭包，应该走主程序提供的 API（如果主程序有的话） |
| 读网络资源 | ✅ HTTP 请求，不需要 SQLite |