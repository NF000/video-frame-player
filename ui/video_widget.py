"""承载 libvlc 视频输出的原生 QWidget。

Windows 上通过 ``libvlc_media_player_set_hwnd`` 把画面嵌入这个 widget，
widget 尺寸变化由 Qt 布局驱动，VLC 会自动按新尺寸渲染（画面自适应窗口）。
"""
from __future__ import annotations

from PySide6.QtWidgets import QSizePolicy, QWidget


class VideoWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 黑底避免无视频时露出窗口背景色
        self.setStyleSheet("background-color: #000000;")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setMinimumSize(320, 180)

    def win_id(self) -> int:
        """返回原生窗口句柄（HWND 的整数值）。"""
        return int(self.winId())
