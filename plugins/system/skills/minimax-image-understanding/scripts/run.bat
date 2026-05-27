"""
智能启动器 - 自动解决 Python 环境问题
用户只需运行这个脚本，无需关心 Python 路径配置
"""
import os
import sys
import subprocess
from pathlib import Path

def find_python():
    """智能查找可用的 Python 解释器"""
    # 方法1: 尝试 py launcher (Windows 推荐)
    try:
        result = subprocess.run(
            ['py', '-3.12', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return 'py', '-3.12'
    except:
        pass
    
    # 方法2: 尝试 py launcher 找任意 Python 3
    for version in ['-3.11', '-3.10', '-3.9', '-3']:
        try:
            result = subprocess.run(
                ['py', version, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return 'py', version
        except:
            pass
    
    # 方法3: 直接尝试 python
    for cmd in ['python', 'python3']:
        try:
            result = subprocess.run(
                [cmd, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return cmd, None
        except:
            pass
    
    return None, None


def main():
    # 切换到脚本所在目录
    script_dir = Path(__file__).parent.resolve()
    os.chdir(script_dir)
    
    print("飘狐 DriFox - 智能截图分析")
    print("=" * 40)
    
    # 1. 找 Python
    print("\n[1/3] 检测 Python 环境...")
    python_cmd, version_arg = find_python()
    
    if python_cmd is None:
        print("错误: 未找到可用的 Python 解释器")
        print("请安装 Python 3.x: https://www.python.org/downloads/")
        input("\n按 Enter 键退出...")
        sys.exit(1)
    
    # 构建 Python 命令
    if version_arg:
        python_full = f"{python_cmd} {version_arg}"
        python_args = [python_cmd, version_arg, "-u", str(script_dir / "capture_and_analyze.py")]
    else:
        python_full = python_cmd
        python_args = [python_cmd, "-u", str(script_dir / "capture_and_analyze.py")]
    
    print(f"  使用 Python: {python_full}")
    
    # 2. 检查 API Key
    print("\n[2/3] 检查 API Key 配置...")
    api_key = os.environ.get('MINIMAX_API_KEY')
    
    if not api_key:
        # 尝试从配置文件读取
        config_file = Path.home() / ".minimax" / "api_key"
        if config_file.exists():
            api_key = config_file.read_text().strip()
    
    if not api_key:
        print("  警告: 未设置 MINIMAX_API_KEY 环境变量")
        user_key = input("\n请输入 MiniMax API Key (或直接回车退出): ").strip()
        if not user_key:
            print("取消操作")
            sys.exit(0)
        api_key = user_key
    
    print(f"  API Key: {api_key[:10]}...")
    
    # 3. 设置环境并运行
    print("\n[3/3] 执行截图分析...")
    os.environ['MINIMAX_API_KEY'] = api_key
    
    try:
        result = subprocess.run(
            python_args,
            env=os.environ.copy()
        )
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        print("\n操作已取消")
        sys.exit(0)


if __name__ == "__main__":
    main()