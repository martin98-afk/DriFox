"""
MiniMax 图片分析脚本
使用 MiniMax 多模态 API 分析图片内容

支持 macOS 和 Windows 双平台

使用方法:
    python analyze_image.py                    # 分析截图
    python analyze_image.py --file image.png   # 分析指定图片
    python analyze_image.py --prompt "你的问题" # 自定义提示词

环境配置:
    Linux/macOS: export MINIMAX_API_KEY=your_key
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
from pathlib import Path

# 添加 common 目录到路径
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from common.utils import get_api_key, ConfigError, Screenshot


def encode_image_to_base64(image_path):
    """
    将图片文件转换为 Base64 编码
    
    Args:
        image_path: 图片文件路径
    
    Returns:
        str: Base64 编码字符串
    """
    with open(image_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def analyze_image(image_path, prompt=None, api_key=None):
    """
    使用 MiniMax API 分析图片
    
    Args:
        image_path: 图片文件路径
        prompt: 分析提示词
        api_key: MiniMax API 密钥
    
    Returns:
        dict: API 响应结果 {"success": bool, "content": str, ...}
    """
    # 默认提示词
    if prompt is None:
        prompt = "请详细描述这张图片的内容，包括所有文字、界面元素、颜色和布局。"

    # 获取 API Key
    if api_key is None:
        try:
            api_key = get_api_key()
        except ConfigError as e:
            return {"success": False, "error": str(e)}

    # 读取图片并编码
    print(f"读取图片: {image_path}")
    try:
        base64_data = encode_image_to_base64(image_path)
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
            
            # 解析响应 - MiniMax 返回格式
            if 'choices' in result and len(result['choices']) > 0:
                content = result['choices'][0]['message']['content']
            elif 'content' in result:
                content = result['content']
            elif 'base_resp' in result and result['base_resp'].get('status_code') == 0:
                # 成功但内容在别处
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


def main():
    parser = argparse.ArgumentParser(
        description="使用 MiniMax AI 分析图片内容",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python analyze_image.py                          # 分析 screenshot.png
  python analyze_image.py --file myimage.png      # 分析指定图片
  python analyze_image.py --prompt "描述图片内容" # 使用自定义提示词
  python analyze_image.py --screenshot             # 截图并分析

环境配置:
  macOS/Linux: export MINIMAX_API_KEY=sk-xxx
  Windows:     set MINIMAX_API_KEY=sk-xxx
  或创建文件:  ~/.minimax/api_key (内容为密钥)
        """
    )

    parser.add_argument(
        '--file', '-f',
        default=None,
        help='图片文件路径'
    )

    parser.add_argument(
        '--prompt', '-p',
        default=None,
        help='分析提示词 (默认: "请详细描述这张图片的内容")'
    )
    
    parser.add_argument(
        '--screenshot', '-s',
        action='store_true',
        help='先截图再分析'
    )

    args = parser.parse_args()

    # 确定图片路径
    if args.screenshot:
        # 截图
        screenshot_path = "/tmp/screenshot.png"
        if not Screenshot.take(screenshot_path):
            sys.exit(1)
        args.file = screenshot_path
    elif args.file is None:
        # 默认尝试 screenshot.png
        if os.path.exists("screenshot.png"):
            args.file = "screenshot.png"
        else:
            print("错误: 请指定图片文件路径或使用 --screenshot 先截图")
            print("用法: python analyze_image.py --help")
            sys.exit(1)

    # 检查文件是否存在
    if not os.path.exists(args.file):
        print(f"错误: 找不到图片文件: {args.file}")
        sys.exit(1)

    # 分析图片
    result = analyze_image(args.file, args.prompt)
    print_result(result)

    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
