# -*- coding: utf-8 -*-
"""
余额显示组件 - 支持 DeepSeek 和 SiliconFlow
"""
from typing import Optional

import requests
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget

from app.utils.design_tokens import scale_font_size
from app.utils.utils import get_font_family_css

# 支持余额查询的服务商及其接口
BALANCE_APIS = {
    "DeepSeek": {
        "url": "https://api.deepseek.com/user/balance",
        "balance_key": "total_balance",  # 从 balance_infos[0] 中获取
        "currency": "¥",
    },
    "SiliconFlow (硅基流动)": {
        "url": "https://api.siliconflow.cn/v1/user/info",
        "balance_key": "totalBalance",  # 从 data 对象中获取
        "currency": "¥",
    },
}

# 余额显示的服务商列表
SUPPORTED_PROVIDERS = list(BALANCE_APIS.keys())


class BalanceDisplay(QWidget):
    """余额显示组件"""

    # 信号：当余额更新时发出
    balance_updated = pyqtSignal(float, str)  # balance, currency

    def __init__(self, parent=None):
        super().__init__(parent)
        self._balance = None
        self._currency = "¥"
        self._loading = False
        self._current_provider = ""

        # 布局：图标 + 金额
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignVCenter)

        # 余额图标
        self._icon_label = QLabel("💰", self)
        self._icon_label.setFixedWidth(16)
        self._icon_label.setAlignment(Qt.AlignCenter)

        # 金额标签
        self._balance_label = QLabel("", self)
        self._balance_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._balance_label.setStyleSheet(f"""
            QLabel {{
                color: #5aa9ff;
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
                font-weight: 500;
                background: transparent;
                padding: 0px;
            }}
        """)

        layout.addWidget(self._icon_label)
        layout.addWidget(self._balance_label)

        # 初始隐藏
        self.setVisible(False)
        self.setFixedHeight(20)

        # 定时器用于延迟加载
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.timeout.connect(self._do_fetch_balance)

        self.setToolTip("余额查询")

    def set_provider(self, provider_name: str, api_key: Optional[str] = None, api_url: Optional[str] = None):
        """设置当前服务商并加载余额"""
        # 先停止之前的定时器，避免显示混乱
        self._load_timer.stop()

        # 如果不是支持的服务商，隐藏组件
        if provider_name not in SUPPORTED_PROVIDERS:
            self.setVisible(False)
            self._balance = None
            self._current_provider = ""
            return

        api_key = api_key or ""
        if not api_key:
            self.setVisible(False)
            self._balance = None
            self._current_provider = ""
            self.setToolTip("未配置 API Key")
            return

        # 立即显示加载状态
        self._balance_label.setText("...")
        self._icon_label.setText("⏳")
        self.setVisible(True)

        # 延迟加载，避免频繁切换时多次请求
        self._current_provider = provider_name
        self._provider_name = provider_name
        self._api_key = api_key
        self._load_timer.start(200)  # 200ms 延迟

    def refresh(self):
        """手动刷新余额（对话完成后调用）"""
        if self._current_provider:
            # 先停止之前的请求
            self._load_timer.stop()
            # 立即显示加载状态
            self._balance_label.setText("...")
            self._icon_label.setText("⏳")
            self._load_timer.start(100)  # 快速刷新

    def _do_fetch_balance(self):
        """实际执行余额查询"""
        if not hasattr(self, "_provider_name") or not hasattr(self, "_api_key"):
            return

        provider_name = self._provider_name
        api_key = self._api_key

        config = BALANCE_APIS.get(provider_name)
        if not config:
            self.setVisible(False)
            return

        self._loading = True
        self._balance_label.setText("...")
        self._icon_label.setText("⏳")
        self.setVisible(True)

        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            response = requests.get(config["url"], headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                balance = self._extract_balance(data, config)
                if balance is not None:
                    self._balance = float(balance)
                    self._currency = config["currency"]
                    self._update_display()
                    self.setToolTip(f"{provider_name} 余额")
                    self.balance_updated.emit(self._balance, self._currency)
                else:
                    self.setVisible(False)
                    self.setToolTip("获取余额失败")
            else:
                self.setVisible(False)
                self.setToolTip(f"余额查询失败 (HTTP {response.status_code})")

        except requests.exceptions.Timeout:
            self.setVisible(False)
            self.setToolTip("余额查询超时")
        except requests.exceptions.ConnectionError:
            self.setVisible(False)
            self.setToolTip("连接失败")
        except Exception as e:
            self.setVisible(False)
            self.setToolTip(f"余额查询异常: {str(e)}")
        finally:
            self._loading = False
            self._icon_label.setText("💰")

    def _extract_balance(self, data: dict, config: dict) -> Optional[float]:
        """从响应数据中提取余额"""
        try:
            balance_key = config["balance_key"]

            if balance_key == "total_balance":
                # DeepSeek: {"balance_infos": [{"total_balance": "0.92", ...}]}
                balance_infos = data.get("balance_infos", [])
                if balance_infos and len(balance_infos) > 0:
                    balance_str = balance_infos[0].get("total_balance", "")
                    if balance_str:
                        return float(balance_str)
            else:
                # SiliconFlow: {"data": {"totalBalance": "-0.0079", ...}}
                data_obj = data.get("data", data)
                balance_str = data_obj.get(balance_key, "")
                if balance_str:
                    return float(balance_str)
            return None
        except (ValueError, TypeError, KeyError):
            return None

    def _update_display(self):
        """更新显示"""
        if self._balance is None:
            self.setVisible(False)
            return

        balance = self._balance
        color = "#5aa9ff"  # 默认蓝色

        # 根据余额大小设置颜色
        if balance < 0:
            # 欠费或余额为负 - 红色
            color = "#ff6b6b"
        elif balance < 1:
            # 小余额 - 黄色警告
            color = "#f6c453"
        elif balance < 10:
            # 低于10元 - 橙色
            color = "#ff9f43"
        else:
            # 正常余额 - 蓝色
            color = "#5aa9ff"
        # 统一保留 1 位小数
        display_text = f"{self._currency}{balance:.1f}"

        self._balance_label.setStyleSheet(f"""
            QLabel {{
                color: {color};
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
                font-weight: 500;
                background: transparent;
                padding: 0px;
            }}
        """)
        self._balance_label.setText(display_text)
        self.setVisible(True)

    def clear(self):
        """清除显示"""
        self._balance = None
        self._balance_label.setText("")
        self._current_provider = ""
        self.setVisible(False)
        self.setToolTip("余额查询")
