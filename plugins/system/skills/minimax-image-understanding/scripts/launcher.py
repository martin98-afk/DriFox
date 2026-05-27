"""
截图分析一键启动器
自动检测环境并执行截图分析
"""
import os
import sys
import subprocess
from pathlib import Path

# 添加 common 目录到路径
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from common.utils import get_python_executable, get_api_key, ConfigError, Screenshot


def find_script(script_name):
    """查找脚本路径"""
    scripts = {
        'capture': SCRIPT_DIR / 'capture_and_analyze.py',
        'analyze': SCRIPT_DIR / 'analyze_image.py',
        'screenshot': SCRIPT_DIR / 'take_screenshot.py',
    }
    return scripts.get(script_name)


def check_environment():
    """检查运行环境"""
    print("=" * 50)
    print("  飘狐 DriFox - 截图分析")
    print("=" * 50)
    print()

    errors = []

    # 检查 Python
    print("[1/4] 检测 Python 环境...")
    try:
        python_path = get_python_executable()
        print(f"  ✓ 找到: {python_path}")
    except ConfigError as e:
        print(f"  ✗ 错误: {e}")
        errors.append("Python")

    # 检查 API Key
    print()
    print("[2/4] 检查 API Key...")
    try:
        api_key = get_api_key()
        # 显示密钥前缀
        prefix = api_key[:10] if len(api_key) > 10 else api_key
        print(f"  ✓ 找到: {prefix}...")
    except ConfigError as e:
        print(f"  ✗ 错误: {e}")
        print("  请设置环境变量 MINIMAX_API_KEY")
        errors.append("API Key")

    # 检查截图权限 (macOS)
    print()
    print("[3/4] 检查截图权限...")
    try:
        test_path = "/tmp/permission_test.png"
        if Screenshot.take(test_path):
            print("  ✓ 截图功能正常")
            if os.path.exists(test_path):
                os.remove(test_path)
        else:
            print("  ✗ 截图功能异常")
            errors.append("截图")
    except Exception as e:
        print(f"  ✗ 错误: {e}")
        errors.append("截图")

    # 检查脚本
    print()
    print("[4/4] 检查脚本文件...")
    script = find_script('capture')
    if script and script.exists():
        print(f"  ✓ 找到: {script.name}")
    else:
        print(f"  ✗ 未找到: capture_and_analyze.py")
        errors.append("脚本")

    print()
    print("=" * 50)

    return errors


def main():
    """主入口"""
    errors = check_environment()

    if errors:
        print()
        print("环境检查发现问题，请先解决上述错误")
        print()
        print("快速配置:")
        print("  macOS/Linux: export MINIMAX_API_KEY=你的密钥")
        print("  Windows:      set MINIMAX_API_KEY=你的密钥")
        print()
        print("按回车键退出...")
        input()
        return 1

    print()
    print("所有检查通过! 正在启动截图分析...")
    print()

    # 执行截图分析
    script = find_script('capture')
    if script:
        try:
            # 使用当前 Python 执行
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(SCRIPT_DIR)
            )
            return result.returncode
        except Exception as e:
            print(f"执行失败: {e}")
            return 1

    return 0


if __name__ == "__main__":
    # 如果带参数，直接执行
    if len(sys.argv) > 1:
        script_name = sys.argv[1]
        script = find_script(script_name)
        if script and script.exists():
            result = subprocess.run([sys.executable, str(script)])
            sys.exit(result.returncode)
        else:
            print(f"未知脚本: {script_name}")
            sys.exit(1)
    else:
        sys.exit(main())
