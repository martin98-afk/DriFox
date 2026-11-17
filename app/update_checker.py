import glob
import os
import platform
import plistlib
import subprocess
import tempfile
import threading
import time
import weakref

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QProgressDialog, QWidget
from qfluentwidgets import (
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    PrimaryPushButton,
)

from app.utils import update_proxy
from app.utils.config import Settings
from app.utils.utils import AsyncUpdateChecker, DownloadThread


class UpdateChecker(QWidget):
    """优化后的更新检查器：保留 InfoBar 交互，适配 Inno Setup EXE"""
    finished = pyqtSignal(object)  # 供外部监听检查结果
    error = pyqtSignal(str)  # 供外部监听错误
    # 安装线程 → 主线程信号（N2 后台安装链）：安装线程禁碰任何 Qt UI API
    _install_finished = pyqtSignal()  # 安装完成，请求主线程退出
    _install_failed = pyqtSignal(str)  # 安装失败，回主线程弹错误条
    # 退出兜底 join 安装线程的超时（秒），短超时兜底消除半成品安装风险
    _INSTALL_JOIN_TIMEOUT = 10
    _instance = None
    _initialized = False

    def __new__(cls, parent=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, parent=None):
        super().__init__(parent)
        if self._initialized:
            # 使用 weakref 存储 parent，避免循环引用导致 C++ 对象被删除
            self._parent_ref = weakref.ref(parent) if parent else None
            return
        self._initialized = True
        self._parent_ref = weakref.ref(parent) if parent else None
        self.cfg = Settings.get_instance()

        # --- 修复报错：初始化 AsyncUpdateChecker 依赖的属性 ---
        self.platform = self.cfg.patch_platform.value
        self.repo = self.cfg.github_repo
        self.token = self.cfg.github_token.value
        self.current_version = self.cfg.current_version
        # -----------------------------------------------

        self.progress_dialog = None
        self.download_thread = None
        self.installer_path = None
        # N2 后台安装链状态（macOS 安装线程句柄与「已启动」标志）
        self._install_thread = None
        self._installer_started = False

    @classmethod
    def get_instance(cls, parent=None):
        """获取单例实例"""
        # 检查旧实例是否仍然有效（C++ 对象未被删除）
        if cls._instance is not None:
            # 通过尝试获取 parent 来判断实例是否有效
            try:
                if cls._instance.parent() is not None or cls._instance._parent_ref is not None:
                    pass  # 实例有效，可以复用
            except Exception:
                # 实例已失效，重置为 None
                cls._instance = None

        if cls._instance is None:
            cls._instance = cls(parent)
        elif parent is not None:
            cls._instance._parent_ref = weakref.ref(parent)

        return cls._instance

    def parent_widget(self):
        """安全获取父窗口"""
        if self._parent_ref is not None:
            parent = self._parent_ref()
            if parent is not None:
                return parent
        return super().parent()

    def check_update(self, silent=False):
        """检查更新入口

        Args:
            silent: 静默模式。True 表示自动检查（启动时），不弹"已是最新"提示；
                     False 表示手动检查（用户点击），已最新时也弹 InfoBar 告知用户。
        """
        self._silent_check = silent
        # 安全检查：确保 parent 仍然有效（C++ 对象未删除）
        parent = self.parent_widget()
        if parent is None:
            return

        self.async_checker = AsyncUpdateChecker(self)
        self.async_checker.allow_beta = self.cfg.allow_beta_update.value
        self.async_checker.check_finished.connect(self._on_check_finished)
        self.async_checker.error.connect(
            lambda msg: self.create_errorbar("检查更新失败", msg)
        )
        self.async_checker.start()


    def _on_check_finished(self, latest_release):
        """异步请求完成回调"""
        # 通知外部监听者
        self.finished.emit(latest_release)
        if latest_release:
            latest_version = latest_release.get("tag_name", "").lstrip("v")
            if self._compare_versions(latest_version, self.current_version) > 0:
                self._show_update_infobar(latest_release)
            elif not self._silent_check:
                # 手动触发且已是最新 → 弹 InfoBar 告知用户
                parent = self.parent_widget() or self
                InfoBar.success(
                    title="已是最新版本",
                    content=f"当前版本 {self.current_version} 已是最新",
                    position=InfoBarPosition.BOTTOM,
                    duration=3000,
                    parent=parent,
                ).show()

    def _show_update_infobar(self, latest_release):
        """保持原有的 InfoBar 交互方式，并增加查看详情按钮"""
        latest_version = latest_release.get("tag_name", "未知")
        html_url = latest_release.get("html_url", "").strip()  # 获取发布页面URL

        info_bar = InfoBar(
            icon=InfoBarIcon.INFORMATION,
            title=f"发现新版本 {latest_version}",
            content="",
            orient=Qt.Vertical,
            isClosable=True,
            position=InfoBarPosition.BOTTOM,
            duration=-1,
            parent=self.parent_widget() or self,
        )

        # 创建按钮容器（水平布局）
        from PyQt5.QtCore import QUrl
        from PyQt5.QtGui import QDesktopServices
        from qfluentwidgets import PushButton  # 普通按钮用于次要操作

        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(8)
        button_layout.addStretch(1)  # 左侧弹性空间，使按钮右对齐

        # 立即更新按钮（主操作）
        update_button = PrimaryPushButton("立即更新")
        update_button.setFixedWidth(80)
        update_button.clicked.connect(
            lambda: self._on_update_confirmed(latest_release, info_bar)
        )

        # 查看详情按钮（次要操作）
        view_button = PushButton("查看详情")
        view_button.setFixedWidth(80)
        if html_url:
            view_button.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(html_url))
            )
        else:
            view_button.setEnabled(False)  # 无有效URL时禁用按钮

        # 添加按钮到容器
        button_layout.addWidget(update_button)
        button_layout.addWidget(view_button)

        # 将按钮容器添加到InfoBar布局
        info_bar.widgetLayout.addWidget(button_container, 0, Qt.AlignRight)

        info_bar.show()

    def _on_update_confirmed(self, latest_release, info_bar):
        """用户点击 InfoBar 上的按钮后触发"""
        info_bar.close()
        self._start_download(latest_release)

    # Release 资产命名约定（见 .github/workflows/release.yml 的 variant matrix）：
    #   PyQt5 版 → 无后缀 Drifox-Windows-Setup-v0.6.0.exe（历史文件名，存量旧版靠它更新）
    #   PySide6 版 → 带 -pyside6 后缀
    # 同一 Release 挂两套包，必须按 build_variant 过滤；跨绑定覆盖安装会让 _internal 里
    # Qt5 / Qt6 动态库混杂，启动即崩。
    _PYSIDE6_MARK = "pyside6"

    def _pick_asset(self, assets, system):
        """按当前平台与构建变体挑选安装包。

        变体无命中时退回本平台第一个包：历史 Release 只有无后缀的 PyQt5 包，
        PySide6 版在旧 Release 上找不到对应资产时不该让更新链彻底断掉。
        """
        if system not in ("darwin", "windows"):
            return None
        ext = ".dmg" if system == "darwin" else ".exe"
        want_pyside = getattr(self.cfg, "build_variant", "pyqt5") == self._PYSIDE6_MARK
        fallback = None
        for asset in assets:
            name = asset["name"].lower()
            if not name.endswith(ext):
                continue
            if fallback is None:
                fallback = asset
            if want_pyside == (self._PYSIDE6_MARK in name):
                return asset
        return fallback

    def _start_download(self, latest_release):
        """下载并安装新版本。"""
        assets = latest_release.get("assets", []) or []

        # --- 全量更新路径 ---
        update_url = None
        exe_name = f"Update_{latest_release['tag_name']}.exe"

        # 获取当前系统平台
        system = platform.system().lower()

        # 按平台 + 构建变体定位安装文件（assets 顺序不可信：republish 重传会打乱）
        asset = self._pick_asset(assets, system)
        if asset:
            update_url = asset["browser_download_url"]
            exe_name = asset["name"]

        if not update_url:
            self.create_errorbar("未找到安装程序", "请前往 Release 手动下载")
            return

        # 加速前缀只作用于下载链路（检查更新已直连完成，此处拿到的是原始资产 URL）
        # exe_name 保持原始名，不受前缀影响：改写过的是请求 URL，落盘名要干净
        download_url = update_proxy.rewrite_url(update_url)

        # 下载到临时目录
        self.installer_path = os.path.join(tempfile.gettempdir(), exe_name)

        # 显示下载进度对话框
        self.progress_dialog = QProgressDialog(
            "正在下载新版本...", "取消", 0, 100, self.parent_widget() or self
        )
        self.progress_dialog.setWindowTitle("软件更新")
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.canceled.connect(self._cancel_download)

        self.download_thread = DownloadThread(
            download_url, self.installer_path, self.token
        )
        self.download_thread.progress_signal.connect(self.progress_dialog.setValue)
        self.download_thread.finished_signal.connect(self._handle_download_finished)
        self.download_thread.error_signal.connect(self._handle_download_error)
        self.download_thread.start()

    # ------------------------------------------------------------------
    # 全量更新
    # ------------------------------------------------------------------

    def _handle_download_finished(self):
        """下载完成：直接进入安装（不再弹窗确认）"""
        if self.progress_dialog:
            # 后续进度由安装器自己的进度条接管，这里只留一句状态提示后随主程序退出
            self.progress_dialog.setLabelText("正在安装更新，完成后将自动启动…")
            self.progress_dialog.setValue(100)
            self.progress_dialog.repaint()

        if platform.system().lower() == "darwin":
            # N2 性能优化：macOS 安装链含多个同步 subprocess（hdiutil/ditto 全量拷贝
            # .app 可达数十秒），移入后台线程，主线程零阻塞只管进度条文案。
            # Windows 保持同步路径（Popen + DETACHED_PROCESS 毫秒级返回，无线程必要）
            self._start_macos_install()
        else:
            self._run_installer()

    def _start_macos_install(self):
        """启动 macOS 后台安装线程（主线程立即返回，UI 保持响应）

        双实例容忍依据（评审必改④）：单实例锁默认关闭（app/utils/config.py:844
        EnableSingleInstance 默认 False），装完 quit 与旧进程退出的共存窗口拉长
        无害；SQLite WAL + config 三方合并（commit c5df538cd）兜底数据一致。
        """
        if self._install_thread is not None:
            # 防重复启动：PyQt 信号 connect 同一 bound method 不去重，重复 start 会双 emit
            return
        self._install_finished.connect(self._on_install_finished)
        self._install_failed.connect(self._on_install_failed)
        QApplication.instance().aboutToQuit.connect(self._on_about_to_quit_guard)
        self._installer_started = True
        self._install_thread = threading.Thread(
            target=self._macos_install_worker, name="macos-installer", daemon=False
        )
        self._install_thread.start()

    def _macos_install_worker(self):
        """安装线程主体：只碰 subprocess/glob/plistlib/os/time，禁任何 Qt UI API"""
        try:
            self._run_macos_upgrade()
            self._install_finished.emit()
        except Exception as e:
            # 评审必改①：错误不在线程内碰 create_errorbar（Qt UI），信号回主线程弹
            self._install_failed.emit(str(e))

    def _on_install_finished(self):
        """安装完成（主线程槽）：关进度条，请求退出事件循环"""
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        QApplication.quit()

    def _on_install_failed(self, msg):
        """安装失败（主线程槽）：关进度条弹错误条，应用继续运行（与改前等价）"""
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        self.create_errorbar("启动失败", msg)

    def _on_about_to_quit_guard(self):
        """事件循环退出兜底（评审必改②）：join 安装线程后仍 os._exit(0)

        不裸删 os._exit：维持「退出尽量快」旧语义，规避 QtWebEngine teardown
        挂起与双实例窗口拉长；join 短超时兜底消除「硬杀时安装线程还在 ditto
        拷贝半路留半成品 .app」的风险（超时后仍退出，快语义优先）。
        """
        if not self._installer_started:
            return
        thread = self._install_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._INSTALL_JOIN_TIMEOUT)
        os._exit(0)

    def _run_installer(self):
        """启动安装（Windows 半静默、macOS 自动升级）"""
        system = platform.system().lower()
        try:
            if system == "darwin":
                self._run_macos_upgrade()
            else:
                self._run_windows_installer()

            # 立即关闭主程序
            QApplication.quit()
            os._exit(0)
        except Exception as e:
            self.create_errorbar("启动失败", str(e))

    def _run_windows_installer(self):
        """Windows：一条进度条装完，装完由安装器自动拉起新版本

        参数与 `dist/installer.iss` 配套：
        - `/SILENT`：只显示进度条窗口，跳过向导页与「完成」页，全程零点击、进度可见
          （`/VERYSILENT` 连窗口都没有，用户看不到安装进度，故不用）
        - `/CLOSEAPPLICATIONS`：旧进程若未退干净，由安装器直接结束，避免 [InstallDelete]
          清空 {app} 时因文件占用失败（需 iss 里 `CloseApplications=yes`，已配）
        - 不加 `/SUPPRESSMSGBOXES`：安装出错时保留错误提示，否则静默失败用户无从得知
        - 自动启动不在这里做：iss 的 [Run] 有一条 `skipifnotsilent` 条目，只在静默模式下
          执行，且带 `runasoriginaluser`，不会让新进程继承安装时的管理员权限
        """
        args = [
            self.installer_path,
            "/SILENT",
            "/NORESTART",
            "/SP-",
            "/CLOSEAPPLICATIONS",
            "/LOG=" + os.path.join(tempfile.gettempdir(), "Drifox-Setup.log"),
        ]
        # args 已经是列表，shell=True 多余，去除消除 CWE-78 风险
        subprocess.Popen(
            args,
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS,
        )

    def _run_macos_upgrade(self):
        """macOS 自动升级：挂载DMG → 拷贝 .app 到 /Applications → 启动新版本"""
        # 1. 挂载 DMG（输出 plist 格式以便解析挂载点）
        # 注意：不能加 -quiet，否则 stdout 会被关闭导致 -plist 输出为空
        result = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-plist", self.installer_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"DMG 挂载失败: {result.stderr}")
        if not result.stdout.strip():
            raise RuntimeError("hdiutil 未输出挂载信息（stdout 为空）")

        # 2. 解析 plist 获取挂载点
        plist_data = plistlib.loads(result.stdout.encode())
        mount_point = None
        for entity in plist_data.get("system-entities", []):
            mount_point = entity.get("mount-point")
            if mount_point:
                break

        if not mount_point:
            raise RuntimeError("无法找到 DMG 挂载点")

        # 3. 在挂载卷中找到 .app 并拷贝到 /Applications
        app_bundles = glob.glob(os.path.join(mount_point, "*.app"))
        if not app_bundles:
            raise RuntimeError(f"DMG 挂载卷中未找到 .app: {mount_point}")

        src_app = app_bundles[0]
        dst_app = os.path.join("/Applications", os.path.basename(src_app))

        # 删除旧版本（如果存在）
        if os.path.exists(dst_app):
            subprocess.run(["rm", "-rf", dst_app], check=True)

        # 使用 ditto 拷贝（保留代码签名、资源分支等 macOS 元数据）
        subprocess.run(["ditto", src_app, dst_app], check=True)

        # 去除 quarantine（隔离）属性，避免 Gatekeeper 阻止自动启动
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", dst_app],
            check=False,
            capture_output=True,
        )

        # 4. 卸载 DMG
        subprocess.run(["hdiutil", "detach", "-quiet", mount_point], capture_output=True)

        # 5. 清理临时 DMG 文件
        try:
            os.remove(self.installer_path)
        except OSError:
            pass

        # 6. 启动新版本：使用 open -a 确保可靠启动，无视 quarantine
        subprocess.Popen(["open", "-a", dst_app])
        # 给 LaunchServices 一点时间完成应用启动请求
        time.sleep(0.5)

    def _cancel_download(self):
        if self.download_thread:
            self.download_thread.is_canceled = True
            self.download_thread = None

    def _handle_download_error(self, error_msg):
        if self.progress_dialog:
            self.progress_dialog.close()
        self.create_errorbar("下载失败", error_msg)

    def create_errorbar(self, title, content):
        InfoBar.error(
            title,
            content,
            position=InfoBarPosition.BOTTOM,
            duration=2000,
            parent=self.parent_widget() or self,
        )

    def _compare_versions(self, v1, v2):
        """
        改进的版本比对：支持 v0.3.5 > v0.3.5-beta
        规则：如果数字部分相同，有后缀的（预发布版）小于无后缀的（正式版）
        """
        import re

        def split_version(v):
            # 提取前面的数字部分和后面的后缀部分
            # 如 "0.3.5-beta" -> ([0, 3, 5], "-beta")
            match = re.match(r"^v?([\d.]+)(.*)", v.strip().lower())
            if not match:
                return [], ""
            nums = [int(x) for x in match.group(1).split(".") if x]
            suffix = match.group(2)
            return nums, suffix

        try:
            p1_nums, p1_suffix = split_version(v1)
            p2_nums, p2_suffix = split_version(v2)

            # 1. 首先比较数字部分 [0, 3, 5]
            if p1_nums != p2_nums:
                return (p1_nums > p2_nums) - (p1_nums < p2_nums)

            # 2. 如果数字相同，检查后缀
            # 原则：无后缀 > 有后缀 (正式版 > 预览版)
            if not p1_suffix and p2_suffix:
                return 1
            if p1_suffix and not p2_suffix:
                return -1

            # 3. 如果都有后缀，按字母序比较 (如 beta < rc)
            return (p1_suffix > p2_suffix) - (p1_suffix < p2_suffix)

        except Exception:
            # 后备方案：纯字符串比较
            return (v1 > v2) - (v1 < v2)
