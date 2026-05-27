"""
Agent Canvas Designer - 轻量画布服务器
功能：静态文件服务 + 配置读写 API + 智能保存
"""

import http.server
import json
import os
import mimetypes
from urllib.parse import urlparse

PORT = 8081
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
DIST_DIR = os.path.dirname(os.path.abspath(__file__))


def is_position_only_change(old_data, new_data):
    """检查变化是否仅仅是位置变化（拖拽移动）"""
    if old_data is None:
        return False
    
    old_nodes = {n['id']: n for n in old_data.get('nodes', [])}
    new_nodes = {n['id']: n for n in new_data.get('nodes', [])}
    
    # 检查是否有节点增删
    if set(old_nodes.keys()) != set(new_nodes.keys()):
        return False
    
    # 检查是否有节点配置变化（除了 x, y）
    for node_id, new_node in new_nodes.items():
        old_node = old_nodes.get(node_id, {})
        # 比较配置（排除 x, y）
        old_config = {k: v for k, v in old_node.get('config', {}).items()}
        new_config = {k: v for k, v in new_node.get('config', {}).items()}
        if old_config != new_config:
            return False
        # 检查 label 是否变化
        if old_node.get('label') != new_node.get('label'):
            return False
    
    # 检查连线是否变化
    old_conns = set((c['sourceId'], c['targetId']) for c in old_data.get('connections', []))
    new_conns = set((c['sourceId'], c['targetId']) for c in new_data.get('connections', []))
    if old_conns != new_conns:
        return False
    
    return True


class CanvasHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIST_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/get-state':
            self._send_json_file(CONFIG_PATH)
        elif parsed.path == '/api/config':
            self._send_json_file(CONFIG_PATH)
        else:
            # Serve static files
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path in ('/save-config', '/api/config'):
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            try:
                new_data = json.loads(body)
                
                # 智能保存：检查是否只是位置变化
                old_data = None
                if os.path.exists(CONFIG_PATH):
                    try:
                        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                            old_data = json.load(f)
                    except:
                        pass
                
                # 如果只是位置变化（拖拽移动），不保存
                if is_position_only_change(old_data, new_data):
                    self._send_json({
                        'status': 'ok', 
                        'message': 'skipped (position only)',
                        'saved': False
                    })
                    return
                
                # 否则保存完整配置
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(new_data, f, ensure_ascii=False, indent=2)
                self._send_json({
                    'status': 'ok', 
                    'message': 'saved',
                    'saved': True
                })
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, 500)
        else:
            self.send_error(404)

    def _send_json_file(self, filepath):
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._send_json(data)
            except Exception as e:
                self._send_json({'error': str(e)}, 500)
        else:
            # Return default empty config
            self._send_json({
                "version": "2.0",
                "flow": "custom",
                "meta": {"title": "未命名流程", "description": ""},
                "nodes": [],
                "connections": []
            })

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, format, *args):
        # Only log important messages
        if args[0] not in ('GET',) or '/get-state' in args[1] or '/api' in args[1]:
            super().log_message(format, *args)


if __name__ == '__main__':
    print(f"🚀 Agent Canvas Designer 服务器启动 → http://localhost:{PORT}")
    print(f"📄 配置路径: {CONFIG_PATH}")
    print(f"📁 静态目录: {DIST_DIR}")
    print("🔒 智能保存：拖拽移动不保存，参数修改才保存")
    print("按 Ctrl+C 停止")
    server = http.server.HTTPServer(('0.0.0.0', PORT), CanvasHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 服务器已停止")
        server.server_close()