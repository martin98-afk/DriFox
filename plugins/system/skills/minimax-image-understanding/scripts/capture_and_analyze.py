"""
一键截图+分析脚本
自动完成: 截图 → 分析 → 输出结果

支持 macOS 和 Windows 双平台

使用方法:
    python capture_and_analyze.py                          # 截图并分析
    python capture_and_analyze.py --no-screenshot         # 仅分析已有截图
    python capture_and_analyze.py --prompt "自定义问题"   # 自定义分析提示词

环境配置:
    macOS/Linux: export MINIMAX_API_KEY=your_key
    Windows:    set MINIMAX_API_KEY=your_key
    或创建文件: ~/.minimax/api_key
"""
import json
import os
import sys
import base64
import ssl
import urllib.request
import urllib.error
import argparse
import platform
from pathlib import Path

# 添加 common 目录到路径
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from common.utils import get_api_key, ConfigError, Screenshot


def analyze_image(image_path, prompt=None, api_key=None):
    """
    使用 MiniMax API 分析图片
    
    Args:
        image_path: 图片文件路径
        prompt: 分析提示词
        api_key: MiniMax API 密钥
    
    Returns:
        dict: API 响应结果
    """
    # 默认提示词
    if prompt is None:
        prompt = "请详细描述这张截图的内容，包括界面上显示的所有文字、图表、按钮等信息。"

    # 获取 API Key
    if api_key is None:
        try:
            api_key = get_api_key()
        except ConfigError as e:
            return {"success": False, "error": str(e)}

    # 读取图片并编码
    print(f"读取图片: {image_path}")
    try:
        with open(image_path, 'rb') as f:
            base64_data = base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        return {"success": False, "error": f"读取图片失败: {e}"}
    
    print(f"图片 Base64 编码完成，长度: {len(base64_data)} 字符")

    # API 配置 - 使用正确的端点
    api_host = "api.minimax.chat"
    endpoint = "/v1/coding_plan/vlm"
    url = f"https://{api_host}{endpoint}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # 请求 payload - 使用 prompt + image_url 格式
    payload = {
        "prompt": prompt,
        "image_url": f"data:image/png;base64,{base64_data}"
    }

    print("正在调用 MiniMax API...")
    print(f"提示词: {prompt[:50]}..." if len(prompt) > 50 else f"提示词: {prompt}")

    # 发送请求
    data = json.dumps(payload)

    try:
        req = urllib.request.Request(url, data=data.encode('utf-8'), headers=headers, method='POST')
        context = ssl.create_default_context()
        
        with urllib.request.urlopen(req, timeout=120, context=context) as response:
            result = json.loads(response.read().decode('utf-8'))
            
            # 解析响应
            if 'choices' in result and len(result['choices']) > 0:
                content = result['choices'][0]['message']['content']
            elif 'content' in result:
                content = result['content']
            elif 'base_resp' in result and result['base_resp'].get('status_code') == 0:
                content = result.get('content', '') or result.get('response', '')
            else:
                return {"success": True, "raw": result, "content": json.dumps(result, ensure_ascii=False, indent=2)}

            return {"success": True, "content": content}

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        try:
            error_json = json.loads(error_body)
            error_msg = error_json.get('error', {}).get('message', error_body)
        except:
            error_msg = error_body
        return {"success": False, "error": f"HTTP {e.code}", "details": error_msg}
    except Exception as e:
        return {"success": False, "error": str(e)}


def print_result(result):
    """打印分析结果"""
    if result.get("success"):
        print("\n" + "=" * 60)
        print("图片分析结果:")
        print("=" * 60)
        print(result.get("content", ""))
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("分析失败:")
        print("=" * 60)
        print(f"错误: {result.get('error')}")
        if 'details' in result:
            print(f"详情: {result.get('details')}")
        print("=" * 60)


# ============== 主入口 ==============

def main():
    print(f"平台: {platform.system()}")
    print("-" * 40)

    parser = argparse.ArgumentParser(
        description="一键截图+分析屏幕内容",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python capture_and_analyze.py                    # 截图并分析
  python capture_and_analyze.py --no-screenshot    # 分析已有截图
  python capture_and_analyze.py -p "请分析UI布局" # 自定义分析问题
  python capture_and_analyze.py -f myimage.png     # 分析指定图片

环境配置:
  macOS/Linux: export MINIMAX_API_KEY=sk-xxx
  Windows:     set MINIMAX_API_KEY=sk-xxx
  或创建文件:  ~/.minimax/api_key
        """
    )

    parser.add_argument(
        '--no-screenshot', '-n',
        action='store_true',
        help='跳过截图，仅分析已有的截图'
    )

    parser.add_argument(
        '--prompt', '-p',
        default=None,
        help='自定义分析提示词'
    )

    parser.add_argument(
        '--file', '-f',
        default=None,
        help='截图保存路径或图片文件路径 (默认: screenshot.png)'
    )

    args = parser.parse_args()

    # 确定截图/图片路径
    if args.file:
        screenshot_path = args.file
    else:
        screenshot_path = "screenshot.png"

    # 1. 截图 (除非指定跳过)
    if not args.no_screenshot:
        print(f"[1/2] 截图阶段")
        if not Screenshot.take(screenshot_path):
            sys.exit(1)
        print()
    else:
        if not os.path.exists(screenshot_path):
            print(f"错误: 找不到图片文件: {screenshot_path}")
            sys.exit(1)
        print(f"使用已有截图: {screenshot_path}")
        print()

    # 2. 分析
    print(f"[2/2] 分析阶段")
    result = analyze_image(screenshot_path, args.prompt)
    print_result(result)

    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
