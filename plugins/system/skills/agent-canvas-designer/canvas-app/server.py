"""
Agent Canvas Designer - 轻量画布服务器
功能：静态文件服务 + 配置读写 API + 智能保存 + 自动备份 + JSON 校验
"""

import http.server
import json
import os
import shutil
import mimetypes
import sys
from urllib.parse import urlparse

PORT = 8081
# 默认 config.json 与 server.py 在同一目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, 'config.json')

# 支持 --config 参数指定配置文件路径
CONFIG_PATH = DEFAULT_CONFIG
if '--config' in sys.argv:
    idx = sys.argv.index('--config')
    if idx + 1 < len(sys.argv):
        CONFIG_PATH = os.path.abspath(sys.argv[idx + 1])

BACKUP_PATH = CONFIG_PATH + '.bak'
DIST_DIR = BASE_DIR


# ====== 配置读写与校验 ======

def read_config(filepath):
    """读取并解析 JSON 配置，失败返回 None"""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def write_config(filepath, data):
    """写入配置，先备份旧文件"""
    # 1. 备份旧配置
    if os.path.exists(filepath):
        old = read_config(filepath)
        if old is not None:
            try:
                shutil.copy2(filepath, BACKUP_PATH)
            except OSError:
                pass  # 备份失败不阻断

    # 2. 写入新配置
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return True


def validate_config_data(data):
    """校验配置数据的结构完整性，返回 (is_valid, errors[])"""
    errors = []

    if not isinstance(data, dict):
        return False, ['根节点不是 JSON 对象']

    # 检查必填字段
    if 'nodes' not in data or not isinstance(data['nodes'], list):
        errors.append('缺少 nodes 数组')
    if 'connections' not in data or not isinstance(data['connections'], list):
        errors.append('缺少 connections 数组')

    if errors:
        return False, errors

    nodes = data['nodes']
    conns = data['connections']

    # 1. 必须有 start 和 end
    types = [n.get('type') for n in nodes]
    if 'start' not in types:
        errors.append('缺少 start 节点')
    if 'end' not in types:
        errors.append('缺少 end 节点')

    # 2. id 唯一性
    ids = [n.get('id') for n in nodes]
    if len(ids) != len(set(ids)):
        errors.append('存在重复的节点 id')

    # 3. 所有非 end 节点都有出边
    source_ids = {c['sourceId'] for c in conns if 'sourceId' in c}
    for n in nodes:
        nid = n['id']
        if n.get('type') != 'end' and nid not in source_ids:
            # start/end 可以有 0 出度（没有下游），但其他节点不行
            if n.get('type') not in ('end',):
                errors.append(f'节点 {nid} ({n.get("type")}) 没有出边')

    # 4. 连接指向的节点都存在
    all_ids = set(ids)
    for c in conns:
        if c.get('sourceId') not in all_ids:
            errors.append(f'连接 sourceId={c.get("sourceId")} 指向不存在的节点')
        if c.get('targetId') not in all_ids:
            errors.append(f'连接 targetId={c.get("targetId")} 指向不存在的节点')

    # 5. config 值全是字符串
    for n in nodes:
        nid = n['id']
        cfg = n.get('config', {})
        for key, val in cfg.items():
            if not isinstance(val, str):
                errors.append(f'节点 {nid} config.{key} 的值不是字符串（"{val}"）')

    return len(errors) == 0, errors


def recover_from_backup():
    """从备份恢复配置"""
    bak = read_config(BACKUP_PATH)
    if bak is not None:
        write_config(CONFIG_PATH, bak)
        return True
    return False


# ====== 智能保存逻辑 ======

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
        old_config = {k: v for k, v in old_node.get('config', {}).items()}
        new_config = {k: v for k, v in new_node.get('config', {}).items()}
        if old_config != new_config:
            return False
        if old_node.get('label') != new_node.get('label'):
            return False
        if old_node.get('validated') != new_node.get('validated'):
            return False

    # 检查连线是否变化
    old_conns = set((c['sourceId'], c['targetId'], c.get('label', ''))
                    for c in old_data.get('connections', []))
    new_conns = set((c['sourceId'], c['targetId'], c.get('label', ''))
                    for c in new_data.get('connections', []))
    if old_conns != new_conns:
        return False

    return True


# ====== HTTP Handler ======

class CanvasHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIST_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/get-state':
            self._send_config()
        elif parsed.path == '/api/config':
            self._send_config()
        elif parsed.path == '/api/validate':
            self._validate_and_respond()
        elif parsed.path == '/api/backup':
            self._send_backup_info()
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path in ('/save-config', '/api/config'):
            self._handle_save()
        elif parsed.path == '/api/validate':
            self._handle_validate_body()
        else:
            self.send_error(404)

    # ------ 配置读取 ------

    def _send_config(self):
        """读取配置并返回，若损坏则尝试恢复"""
        data = read_config(CONFIG_PATH)
        if data is not None:
            self._send_json(data)
            return

        # 尝试从备份恢复
        recovered = recover_from_backup()
        if recovered:
            data = read_config(CONFIG_PATH)
            if data is not None:
                self._send_json(data)
                return

        # 全部失败，返回空配置
        self._send_json(self._empty_config())

    def _empty_config(self):
        return {
            "version": "2.0",
            "flow": "custom",
            "meta": {"title": "未命名流程", "description": ""},
            "nodes": [],
            "connections": []
        }

    # ------ 配置保存 ------

    def _handle_save(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            new_data = json.loads(body)

            # 智能保存：检查是否只是位置变化
            old_data = read_config(CONFIG_PATH)

            if is_position_only_change(old_data, new_data):
                self._send_json({
                    'status': 'ok',
                    'message': 'skipped (position only)',
                    'saved': False
                })
                return

            # 校验配置结构
            valid, errors = validate_config_data(new_data)
            if not valid:
                # 只是警告，不阻断保存（允许画布保存中间状态）
                pass

            # 写入配置（含备份）
            write_config(CONFIG_PATH, new_data)
            self._send_json({
                'status': 'ok',
                'message': 'saved',
                'saved': True,
                'warnings': errors if not valid else []
            })
        except json.JSONDecodeError as e:
            self._send_json({
                'status': 'error',
                'message': f'JSON 解析错误: {e}'
            }, 500)
        except Exception as e:
            self._send_json({
                'status': 'error',
                'message': str(e)
            }, 500)

    # ------ 校验 API ------

    def _validate_and_respond(self):
        """GET 模式：校验当前配置"""
        data = read_config(CONFIG_PATH)
        if data is None:
            self._send_json({
                'valid': False,
                'errors': ['配置损坏，无法解析'],
                'backup_available': os.path.exists(BACKUP_PATH)
            })
            return

        valid, errors = validate_config_data(data)
        self._send_json({
            'valid': valid,
            'errors': errors,
            'node_count': len(data.get('nodes', [])),
            'conn_count': len(data.get('connections', []))
        })

    def _handle_validate_body(self):
        """POST 模式：校验请求体中的配置"""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
            valid, errors = validate_config_data(data)
            self._send_json({
                'valid': valid,
                'errors': errors,
                'node_count': len(data.get('nodes', [])),
                'conn_count': len(data.get('connections', []))
            })
        except json.JSONDecodeError as e:
            self._send_json({
                'valid': False,
                'errors': [f'JSON 语法错误: {e}']
            })

    def _send_backup_info(self):
        """返回备份信息"""
        bak_exists = os.path.exists(BACKUP_PATH)
        bak_info = {}
        if bak_exists:
            bak_info['size'] = os.path.getsize(BACKUP_PATH)
            bak_info['modified'] = os.path.getmtime(BACKUP_PATH)
            bak_data = read_config(BACKUP_PATH)
            if bak_data:
                bak_info['title'] = bak_data.get('meta', {}).get('title', '')
                bak_info['node_count'] = len(bak_data.get('nodes', []))
                bak_info['conn_count'] = len(bak_data.get('connections', []))

        self._send_json({
            'backup_exists': bak_exists,
            'config_exists': os.path.exists(CONFIG_PATH),
            'backup': bak_info,
            'can_recover': bak_exists
        })

    # ------ 工具方法 ------

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
        if args[0] not in ('GET',) or '/get-state' in args[1] or '/api' in args[1]:
            super().log_message(format, *args)


if __name__ == '__main__':
    print(f"🚀 Agent Canvas Designer 服务器启动 → http://localhost:{PORT}")
    print(f"📄 配置路径: {CONFIG_PATH}")
    print(f"📁 备份路径: {BACKUP_PATH}")
    print(f"📁 静态目录: {DIST_DIR}")
    print(f"🔧 使用 --config <路径> 指定自定义配置文件")
    print("🔒 智能保存：拖拽移动不保存，参数修改才保存")
    print("🛡️  自动备份：写入前备份旧配置到 .bak")
    print("✓ 校验 API: GET /api/validate 和 POST /api/validate")
    print("按 Ctrl+C 停止")
    server = http.server.HTTPServer(('0.0.0.0', PORT), CanvasHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 服务器已停止")
        server.server_close()