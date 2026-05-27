"""
通用工具模块 - 提供 Python 探测、API Key 获取、截图功能
支持 macOS 和 Windows 双平台
"""
import os
import sys
import subprocess
import json
import base64
import platform
from pathlib import Path


class PythonFinder:
    """自动探测系统中可用的 Python 解释器"""
    
    @classmethod
    def find_from_path(cls):
        """从 PATH 环境变量中查找 python"""
        import shutil
        
        for name in ['python3', 'python', 'py']:
            path = shutil.which(name)
            if path:
                return Path(path)
        return None
    
    @classmethod
    def find_mac_python(cls):
        """macOS 特定路径"""
        mac_paths = [
            Path('/usr/bin/python3'),
            Path('/usr/local/bin/python3'),
            Path('/opt/homebrew/bin/python3'),
            Path('/opt/local/bin/python3'),
        ]
        for p in mac_paths:
            if p.exists():
                return p
        return None
    
    @classmethod
    def find_win_python(cls):
        """Windows 特定路径"""
        win_paths = [
            Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe",
            Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python311" / "python.exe",
            Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python310" / "python.exe",
            Path("C:/") / "Python312" / "python.exe",
            Path("C:/") / "Python311" / "python.exe",
            Path.home() / "anaconda3" / "python.exe",
            Path.home() / "miniconda3" / "python.exe",
        ]
        for p in win_paths:
            if p.exists():
                return p
        return None
    
    @classmethod
    def verify_python(cls, path):
        """验证 Python 解释器是否可用"""
        try:
            result = subprocess.run(
                [str(path), '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except:
            return False
    
    @classmethod
    def find(cls):
        """综合查找 Python"""
        # 1. 优先使用当前解释器
        if cls.verify_python(Path(sys.executable)):
            return Path(sys.executable)
        
        # 2. 从 PATH 查找
        found = cls.find_from_path()
        if found and cls.verify_python(found):
            return found
        
        # 3. 平台特定查找
        system = platform.system()
        if system == 'Darwin':
            found = cls.find_mac_python()
            if found and cls.verify_python(found):
                return found
        elif system == 'Windows':
            found = cls.find_win_python()
            if found and cls.verify_python(found):
                return found
        
        return None


class APIKeyFinder:
    """MiniMax API Key 查找器"""
    
    @classmethod
    def find_from_env(cls):
        """从环境变量获取"""
        # 尝试多个可能的环境变量名
        for key_name in ['MINIMAX_API_KEY', 'MINIMAX_APIKEY', 'API_KEY']:
            key = os.environ.get(key_name)
            if key:
                return key
        return None
    
    @classmethod
    def find_from_file(cls):
        """从配置文件获取"""
        # 尝试多个可能的配置文件位置
        config_paths = [
            Path.home() / ".minimax" / "api_key",
            Path.home() / ".minimax" / "api_key.txt",
            Path.home() / ".config" / "minimax" / "api_key",
        ]
        
        for config_path in config_paths:
            try:
                if config_path.exists():
                    content = config_path.read_text().strip()
                    if content:
                        return content
            except:
                pass
        return None
    
    @classmethod
    def find(cls):
        """综合查找 API Key"""
        # 1. 环境变量优先
        key = cls.find_from_env()
        if key:
            return key
        
        # 2. 配置文件
        key = cls.find_from_file()
        if key:
            return key
        
        return None


class Screenshot:
    """跨平台截图工具"""
    
    @staticmethod
    def take(output_path="screenshot.png", delay=0):
        """
        截取全屏
        
        Args:
            output_path: 保存路径
            delay: 延迟截图秒数 (macOS)
        
        Returns:
            bool: 是否成功
        """
        system = platform.system()
        
        if not os.path.isabs(output_path):
            output_path = os.path.join(os.getcwd(), output_path)
        
        if system == 'Darwin':
            return Screenshot._take_macos(output_path, delay)
        elif system == 'Windows':
            return Screenshot._take_windows(output_path)
        else:
            print(f"不支持的系统: {system}")
            return False
    
    @staticmethod
    def _take_macos(output_path, delay=0):
        """macOS 截图"""
        print("正在截取屏幕 (macOS)...")
        
        try:
            # macOS 使用 screencapture
            cmd = ['screencapture']
            
            if delay > 0:
                cmd.extend(['-T', str(int(delay))])
            
            cmd.extend(['-x', output_path])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30
            )
            
            if result.returncode == 0 and os.path.exists(output_path):
                size = os.path.getsize(output_path)
                print(f"截图成功! 文件大小: {size/1024/1024:.1f} MB")
                return True
            else:
                error_msg = result.stderr.decode('utf-8') if result.stderr else '未知错误'
                print(f"截图失败: {error_msg}")
                return False
                
        except subprocess.TimeoutExpired:
            print("截图超时")
            return False
        except Exception as e:
            print(f"截图异常: {e}")
            return False
    
    @staticmethod
    def _take_windows(output_path):
        """Windows 截图"""
        import tempfile
        
        ps_script = '''
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
    [DllImport("gdi32.dll")] public static extern int GetDeviceCaps(IntPtr hDC, int index);
}
"@

$hdc = [Win32]::GetDC([IntPtr]::Zero)
$w = [Win32]::GetDeviceCaps($hdc, 117)
$h = [Win32]::GetDeviceCaps($hdc, 118)
[Win32]::ReleaseDC([IntPtr]::Zero, $hdc) | Out-Null

if ($h -gt $w) {
    $temp = $w; $w = $h; $h = $temp
}

$bmp = New-Object System.Drawing.Bitmap($w, $h)
$graf = [System.Drawing.Graphics]::FromImage($bmp)
$graf.CopyFromScreen(0, 0, 0, 0, (New-Object System.Drawing.Size($w, $h)))
$bmp.Save('%OUTPUT%', [System.Drawing.Imaging.ImageFormat]::Png)
$graf.Dispose()
$bmp.Dispose()
'''.replace('%OUTPUT%', output_path.replace('\\', '\\\\').replace('/', '\\\\'))
        
        temp_file = os.path.join(tempfile.gettempdir(), 'shot.ps1')
        
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                content = ps_script.replace('{{', '{').replace('}}', '}')
                f.write(content)
            
            result = subprocess.run(
                ['powershell', '-ExecutionPolicy', 'Bypass', '-File', temp_file],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            
            os.remove(temp_file)
            
            if result.returncode == 0 and os.path.exists(output_path):
                size = os.path.getsize(output_path)
                print(f"截图成功! 文件大小: {size/1024/1024:.1f} MB")
                return True
            else:
                print(f"截图失败: {result.stderr.strip() if result.stderr else '未知错误'}")
                return False
                
        except Exception as e:
            print(f"截图异常: {e}")
            try:
                os.remove(temp_file)
            except:
                pass
            return False


class ConfigError(Exception):
    """配置错误异常"""
    
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def get_python_executable():
    """获取可用的 Python 解释器"""
    found = PythonFinder.find()
    if found:
        return str(found)
    raise ConfigError("未找到可用的 Python 解释器")


def get_api_key():
    """获取 MiniMax API Key"""
    key = APIKeyFinder.find()
    if key:
        return key
    raise ConfigError(
        "未找到 MiniMax API Key\n"
        "请设置环境变量 MINIMAX_API_KEY 或创建配置文件 ~/.minimax/api_key"
    )


if __name__ == "__main__":
    print("=== 诊断信息 ===")
    print(f"平台: {platform.system()}")
    print(f"Python: {sys.executable}")
    
    print("\nPython 查找测试:")
    python_path = PythonFinder.find()
    if python_path:
        print(f"  找到: {python_path}")
    else:
        print("  未找到")
    
    print("\nAPI Key 查找测试:")
    try:
        key = get_api_key()
        if key:
            print(f"  找到: {key[:10]}...")
    except ConfigError as e:
        print(f"  未找到: {e}")
    
    print("\n截图测试:")
    test_path = "/tmp/test_screenshot.png"
    if Screenshot.take(test_path):
        print(f"  成功: {test_path}")
    else:
        print("  失败")
