"""
截图脚本 - 跨平台屏幕截图工具
支持 macOS 和 Windows
"""
import sys
import os
from pathlib import Path

# 添加 common 目录到路径
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from common.utils import Screenshot


def main():
    """命令行截图工具"""
    import argparse
    
    parser = argparse.ArgumentParser(description="截取屏幕截图")
    parser.add_argument(
        'output',
        nargs='?',
        default='screenshot.png',
        help='截图保存路径 (默认: screenshot.png)'
    )
    parser.add_argument(
        '--delay', '-d',
        type=float,
        default=0,
        help='延迟截图秒数 (macOS)'
    )
    
    args = parser.parse_args()
    
    success = Screenshot.take(args.output, args.delay)
    
    if success:
        print(f"截图已保存: {args.output}")
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
