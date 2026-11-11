# -*- coding: utf-8 -*-
"""DriFox 内存验证 RPC 驱动（T32）：S1 多会话梯度 + S2 关标签回收，纯 HTTP RPC 零键鼠。

用法（需先有带 DRIFOX_UI_TEST_SERVER 的 DriFox 实例，见 --rpc-url）：
    python scripts/mem_drive_rpc.py --rpc-url 127.0.0.1:27998 --n-sessions 6 --gap 15
    python scripts/mem_drive_rpc.py --n-sessions 3 --do-s2 --stage-file .perf_out/s2_stage.txt

采样三项（T23 口径）：主进程 private / WebEngine 子进程数与总 RSS / 整树 RSS。
切换说明：ui_session new 返回的 session_id 是 8 位截断（server.py _tool_ui_session），
直接拿去 switch 必失败（T23 教训）；正确方式=先 tools/call ui_state 取
available_sessions[].session_id（完整值）再 switch。

结果记录表模板（跑完把 JSONL 各行填入）：
    scenario | stage        | main_private | web_n | web_rss | tree_rss | 判据                          | 通过?
    S1       | baseline     |              | <=3   |         |          | 空实例 ~420-620MB             |
    S1       | new1..newN   |              | <=6   | <800MB  |          | renderer 不随会话线性涨       |
    S2       | before_close |              |       |         |          | 3 标签时的 renderer 基线      |
    S2       | after_close  |              | <=S2基线+1 |    |          | 关完回落基线+1 以内（T16）    |
    S3       | t0/+30/+60/+120 |           |       |         |          | 无 200MB 级阶梯且回落>50MB    |
"""
import argparse, json, time, urllib.request

_CLOSE_BTN = {'class': 'TransparentToolButton'}  # TabItem._close_btn（tab_panel.py:624）
# 无 objectName；hover 才可见（:783 setVisible(False)）；两段式确认（:1072 首点变「确认关闭」）。
# 因此 S2 每个标签需点击 2 次；若 ui_click 因不可见拒绝，改用工具端注册 close-tab 动作（见汇报）。


def rpc(url, name, args=None, timeout=95):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': name, 'arguments': args or {}}}).encode()
    req = urllib.request.Request('http://' + url + '/', data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    txt = ((out.get('result') or {}).get('content') or [{}])[0].get('text', '{}')
    try:
        return json.loads(txt)
    except Exception:
        return txt


def sample(tag, root_pid, out):
    import psutil
    p = psutil.Process(root_pid)
    mi = p.memory_info()
    ch = [c for c in p.children(recursive=True) if 'webengine' in c.name().lower()]
    allch = p.children(recursive=True)
    rec = {'tag': tag,
           'main_private': round(mi.private / 1048576, 1),
           'web_n': len(ch),
           'web_rss': round(sum(c.memory_info().rss for c in ch) / 1048576, 1),
           'tree_rss': round((mi.rss + sum(c.memory_info().rss for c in allch)) / 1048576, 1)}
    out.write(json.dumps(rec, ensure_ascii=False) + chr(10))
    out.flush()
    print(rec, flush=True)
    return rec


def stage(path, name):
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(name + chr(10))


def main():
    ap = argparse.ArgumentParser(description='DriFox 内存验证 RPC 驱动（S1/S2）')
    ap.add_argument('--rpc-url', default='127.0.0.1:27998')
    ap.add_argument('--pid', type=int, required=True, help='DriFox 主进程 pid')
    ap.add_argument('--n-sessions', type=int, default=6)
    ap.add_argument('--gap', type=float, default=15.0, help='每次 new 后静置秒')
    ap.add_argument('--switch-back', action='store_true',
                    help='切回第 1 个会话：经 ui_state 取完整 session_id（new 返回值是 8 位截断不可用）')
    ap.add_argument('--do-s2', action='store_true', help='S2：逐个关闭标签（每标签点 2 次，两段式确认）')
    ap.add_argument('--out', default='.perf_out/mem_drive_rpc.jsonl')
    ap.add_argument('--stage-file', default=None)
    a = ap.parse_args()

    out = open(a.out, 'w', encoding='utf-8')
    stage(a.stage_file, 'baseline_start')
    print('静置 30s 采基线...', flush=True)
    time.sleep(30)
    sample('S1_baseline', a.pid, out)

    for k in range(a.n_sessions):
        rpc(a.rpc_url, 'ui_session', {'action': 'new'})
        time.sleep(a.gap)
        sample('S1_new%d' % (k + 1), a.pid, out)

    if a.switch_back:
        st = rpc(a.rpc_url, 'ui_state')
        sessions = (st.get('available_sessions') if isinstance(st, dict) else None) or []
        if sessions:
            sid = sessions[0]['session_id']  # 完整 id（ui_state 直接透传 store 值）
            r = rpc(a.rpc_url, 'ui_session', {'action': 'switch', 'session_id': sid})
            time.sleep(15)
            sample('S1_back_to_1(ok=%s)' % r.get('ok'), a.pid, out)
        else:
            print('ui_state 无可用会话，switch 跳过', flush=True)
            sample('S1_back_to_1(skipped)', a.pid, out)

    if a.do_s2:
        stage(a.stage_file, 's2_start')
        sample('S2_before_close', a.pid, out)
        for k in range(a.n_sessions):
            # 两段式确认关闭：首点变「确认关闭」，再点真关（tab_panel.py:1072）
            for _ in range(2):
                rpc(a.rpc_url, 'ui_click', {'selector': _CLOSE_BTN, 'index': k}, timeout=30)
                time.sleep(1.5)
            time.sleep(3)
            sample('S2_after_close_%d' % (k + 1), a.pid, out)
        stage(a.stage_file, 's2_done')

    print('完成。输出:', a.out, flush=True)


if __name__ == '__main__':
    main()