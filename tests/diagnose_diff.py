"""
实时诊断：检查差异统计按钮的数据库查询
使用方法：在 DriFoxx 运行中执行此脚本
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.store.session_store import SessionStore
from app.utils.file_operation_recorder import FileOperationRecorder


def diagnose():
    # 连接到相同的数据库
    store = SessionStore.get_instance()
    
    # 获取所有会话
    sessions = store.get_all_sessions()
    print(f"共有 {len(sessions)} 个会话")
    
    for s in sessions[-3:]:  # 最近3个会话
        sid = s.session_id if hasattr(s, 'session_id') else s.get('session_id')
        print(f"\n=== 会话 {sid} ===")
        
        # 获取此会话的所有文件操作
        recorder = FileOperationRecorder(store)
        ops = recorder.get_all_operations_for_session(sid)
        print(f"  文件操作总数: {len(ops)}")
        
        # 按 call_id 分组
        by_call_id = {}
        for op in ops:
            cid = op.get("call_id", "unknown")
            if cid not in by_call_id:
                by_call_id[cid] = []
            by_call_id[cid].append(op)
        
        print(f"  唯一 call_id 数: {len(by_call_id)}")
        for cid, ops_list in list(by_call_id.items())[:5]:
            print(f"    {cid}: {len(ops_list)} 个操作, files: {[o.get('file_path','?').split('/')[-1] for o in ops_list]}")
        
        if len(by_call_id) > 5:
            print(f"    ... 还有 {len(by_call_id)-5} 个 call_id")
        
        # 检查是否有 call_id 为空的记录
        empty_cid = [op for op in ops if not op.get("call_id")]
        if empty_cid:
            print(f"  ⚠️ 发现 {len(empty_cid)} 条 call_id 为空的记录!")
        
        # 获取消息中的 tool_call_id
        if hasattr(s, 'messages'):
            messages = s.messages
        else:
            messages = s.get('messages', [])
        
        from app.core.message_content import consolidate_messages, get_user_round_ranges
        canonical = consolidate_messages(messages)
        
        # 从消息中提取 tool_call_ids
        msg_call_ids = set()
        for msg in canonical:
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and tc.get("id"):
                    msg_call_ids.add(tc["id"])
            if msg.get("tool_call_id"):
                msg_call_ids.add(msg["tool_call_id"])
        
        print(f"  消息中的唯一 call_id 数: {len(msg_call_ids)}")
        
        # 检查匹配
        db_call_ids = set(by_call_id.keys())
        matched = msg_call_ids & db_call_ids
        missing = msg_call_ids - db_call_ids
        
        print(f"  匹配: {len(matched)}, 未匹配: {len(missing)}")
        if missing:
            print(f"  ⚠️ 以下 call_id 在消息中存在但数据库找不到:")
            for cid in list(missing)[:10]:
                print(f"    - {cid}")
            if len(missing) > 10:
                print(f"      ... 还有 {len(missing)-10} 个")


if __name__ == "__main__":
    diagnose()
