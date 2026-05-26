# -*- coding: utf-8 -*-
"""
Drifox 会话查询脚本
用法: py -3.12 query_sessions.py --date YYYY-MM-DD

路径说明:
  脚本位置: skills/session-summary/scripts/query_sessions.py
  数据库位置: .drifox/sessions.db (相对于 Drifox 安装目录)
"""

import sqlite3
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path


def get_drifox_paths():
    """获取 Drifox 相关路径（基于脚本位置）"""
    # 脚本目录: session-summary/scripts
    script_dir = Path(__file__).parent.resolve()
    # Drifox 根目录: ../../../../.. = _internal/app/../.. -> Drifox
    drifox_root = script_dir.parent.parent.parent.parent.parent
    db_path = drifox_root / ".drifox" / "sessions.db"
    return drifox_root, db_path


# 全局路径
DRIFOX_ROOT, DB_PATH = get_drifox_paths()


def query_sessions(date_str):
    """查询指定日期的所有会话"""
    if not DB_PATH.exists():
        print(f"Error: Database not found at {DB_PATH}")
        print(f"Drifox root: {DRIFOX_ROOT}")
        return []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT session_id, title, message_count, project, created_at, messages, updated_at
        FROM sessions
        WHERE created_at LIKE ?
        ORDER BY created_at DESC
    """, (f"{date_str}%",))

    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def format_message_content(msg):
    """格式化消息内容"""
    role = msg.get('role', 'unknown')
    content = msg.get('content', '')

    if isinstance(content, str):
        content = ' '.join(content.split())[:200]
    else:
        content = str(content)[:200]

    return f"[{role}]: {content}"


def summarize_session(session):
    """生成会话摘要"""
    title = session.get('title') or '(无标题)'
    project = session.get('project', '默认项目')
    message_count = session.get('message_count', 0)
    created_at = session.get('created_at', '')
    session_id = session.get('session_id', '')[:32]

    summary = f"""
{'='*60}
会话标题: {title}
项目: {project}
消息数: {message_count}
会话ID: {session_id}...
创建时间: {created_at}
{'='*60}
"""

    messages_json = session.get('messages')
    if messages_json:
        try:
            messages = json.loads(messages_json)
            summary += f"\n消息内容摘要 (共 {len(messages)} 条):\n"
            for i, msg in enumerate(messages[:10], 1):
                summary += f"  [{i}] {format_message_content(msg)}\n"
            if len(messages) > 10:
                summary += f"  ... 还有 {len(messages) - 10} 条消息\n"
        except json.JSONDecodeError:
            summary += "\n消息内容 (JSON解析失败):\n"
            summary += f"  {messages_json[:500]}...\n"
    else:
        summary += "\n(无消息内容)\n"

    return summary


def print_daily_summary(date_str, sessions):
    """打印每日总结"""
    print(f"\n{'#'*60}")
    print(f"# Drifox 会话记录 - {date_str}")
    print(f"# 共找到 {len(sessions)} 条会话")
    print(f"# Drifox 目录: {DRIFOX_ROOT}")
    print(f"# 数据库: {DB_PATH}")
    print(f"# 查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}\n")

    for i, session in enumerate(sessions, 1):
        print(f"\n[{i}] " + summarize_session(session))
        print()


def main():
    parser = argparse.ArgumentParser(description='查询 Drifox 会话记录')
    parser.add_argument('--date', '-d',
                        help='查询日期 (格式: YYYY-MM-DD，默认昨天)',
                        default=None)
    parser.add_argument('--list', '-l',
                        help='只列出会话概要，不显示消息内容',
                        action='store_true')
    parser.add_argument('--session', '-s',
                        help='查询指定会话ID',
                        default=None)

    args = parser.parse_args()

    # 确定查询日期
    if args.date:
        date_str = args.date
    else:
        yesterday = datetime.now() - timedelta(days=1)
        date_str = yesterday.strftime('%Y-%m-%d')

    if args.session:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT session_id, title, message_count, project, created_at, messages
            FROM sessions WHERE session_id = ?
        """, (args.session,))
        row = cursor.fetchone()
        conn.close()

        if row:
            session = dict(row)
            print(summarize_session(session))
        else:
            print(f"未找到会话: {args.session}")
    else:
        sessions = query_sessions(date_str)
        print_daily_summary(date_str, sessions)


if __name__ == '__main__':
    main()
