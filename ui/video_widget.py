"""承载 libvlc 视频输出的原生 QWidget，并处理「点击画面切换播放/暂停」。

Windows 上通过 ``libvlc_media_player_set_hwnd`` 把画面嵌入这个 widget，
widget 尺寸变化由 Qt 布局驱动，VLC 会自动按新尺寸渲染（画面自适应窗口）。

点击画面切换播放 / 暂停
------------------------
libvlc 在 Windows 上会把视频输出创建成该 widget 的**原生子窗口**，鼠标消息
可能被 vout 窗口截获，Qt 的 ``mousePressEvent`` 收不到。因此用三条互补通道，
统一经 ``_report_click()`` 去重后只发一次 ``clicked`` 信号：

1. ``mousePressEvent`` —— Qt 能收到时最自然、零延迟；
2. ``WM_PARENTNOTIFY`` —— 子窗口被点击时 Windows 会通知父窗口（即本 widget）；
3. 40ms 轻量轮询 ``GetAsyncKeyState`` —— 兜底，保证嵌入窗口吞事件时仍然生效，
   只在「左键按下的边沿 + 光标位于画面内 + 顶层窗口处于活动状态」时触发。

轮询只是一次 Win32 API 调用，不碰 libvlc、不做布局计算，对播放流畅度无影响。
"""
from __future__ import annotations

import ctypes
import sys
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QMouseEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

_IS_WINDOWS = sys.platform == "win32"

# 点击去重窗口：三条通道可能在同一次点击里都触发，只认第一次
CLICK_DEBOUNCE_S = 0.25
POLL_INTERVAL_MS = 40

if _IS_WINDOWS:
    # 自己定义 MSG，避免 ctypes.wintypes 在 Win64 下 WPARAM/LPARAM 宽度不一致
    _ULONG_PTR = (
        ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8
        else ctypes.c_ulong
    )
    _LONG_PTR = (
        ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8
        else ctypes.c_long
    )

    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", _ULONG_PTR),
            ("lParam", _LONG_PTR),
            ("time", ctypes.c_ulong),
            ("pt", ctypes.c_long * 2),
        ]

    WM_PARENTNOTIFY = 0x0210
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONDBLCLK = 0x0203
    VK_LBUTTON = 0x01
    _NATIVE_EVENT_TYPES = (b"windows_generic_MSG", b"windows_dispatcher_MSG")
else:  # pragma: no cover - 本项目仅面向 Windows，保留可读性
    _MSG = None
    WM_PARENTNOTIFY = WM_LBUTTONDOWN = WM_LBUTTONDBLCLK = VK_LBUTTON = 0
    _NATIVE_EVENT_TYPES = ()


class VideoWidget(QWidget):
    """视频输出容器；左键单击画面会发出 ``clicked``。"""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 黑底避免无视频时露出窗口背景色
        self.setStyleSheet("background-color: #000000;")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setMinimumSize(320, 180)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 显式拥有原生 HWND：VLC 嵌入与 WM_PARENTNOTIFY 都依赖它
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)

        self._last_click_at = 0.0
        self._poll_pressed = False

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_click)
        if _IS_WINDOWS:
            self._poll_timer.start()

    def win_id(self) -> int:
        """返回原生窗口句柄（HWND 的整数值）。"""
        return int(self.winId())

    # ---------------- 点击检测 ----------------
    def _report_click(self) -> None:
        """三条通道共用：短时间内重复上报只算一次点击。"""
        now = time.monotonic()
        if now - self._last_click_at < CLICK_DEBOUNCE_S:
            return
        self._last_click_at = now
        self.clicked.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._report_click()
        super().mousePressEvent(event)

    def nativeEvent(self, eventType, message):  # noqa: N802
        """VLC 的原生子窗口被点击时，父窗口会收到 WM_PARENTNOTIFY。

        这个钩子会被 Qt 用于**每一条** Windows 消息，VLC 播放时消息量很大，
        因此这里必须极轻：先做类型/消息号短路，绝大多数消息只做一两次
        整数比较就返回，不构造 ctypes 对象、不查 QWidget 几何、不抛异常。
        任何异常都静默跳过，绝不因为兜底逻辑影响视频输出。
        """
        if not (_IS_WINDOWS and eventType in _NATIVE_EVENT_TYPES):
            return False, 0
        # PySide6 把 MSG* 作为 int 传入；个别版本传其它指针包装对象，
        # 直接跳过（由轮询兜底），避免每条消息都构造一次 Python 异常。
        if not isinstance(message, int):
            return False, 0
        try:
            msg = ctypes.cast(message, ctypes.POINTER(_MSG)).contents
            if msg.message != WM_PARENTNOTIFY:
                return False, 0
            if (int(msg.wParam) & 0xFFFF) not in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK):
                return False, 0
            raw = int(msg.lParam)
            x = ctypes.c_short(raw & 0xFFFF).value
            y = ctypes.c_short((raw >> 16) & 0xFFFF).value
            # 纯数值比较：rect().contains(QPoint(...)) 会在原生消息分发期间
            # 触发额外的 QWidget 几何查询，这里没必要。
            if 0 <= x < self.width() and 0 <= y < self.height():
                self._report_click()
        except Exception:
            pass
        # 返回 false 表示不消费原生消息，Qt 仍按默认流程分发
        return False, 0

    def _window_is_active(self) -> bool:
        top = self.window()
        return top is not None and top.isActiveWindow()

    @staticmethod
    def _left_button_down() -> bool:
        """Win32 全局左键状态；拆成方法便于在测试里替换。"""
        if not _IS_WINDOWS:
            return False
        try:
            return bool(
                ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000
            )
        except Exception:
            return False

    def _cursor_in_widget(self) -> bool:
        return self.rect().contains(self.mapFromGlobal(QCursor.pos()))

    def _poll_click(self) -> None:
        """兜底通道：轮询左键状态 + 光标位置，按下边沿触发一次。"""
        if not _IS_WINDOWS or not self.isVisible() or not self.isEnabled():
            return
        if not self._left_button_down():
            self._poll_pressed = False
            return
        if self._poll_pressed:
            return   # 按住不放不重复触发
        self._poll_pressed = True
        top = self._window_is_active()
        if not top:
            return
        if self._cursor_in_widget():
            self._report_click()

    def stop_click_poll(self) -> None:
        """关窗时停掉轮询定时器（QWidget 销毁时本就会停，显式更稳妥）。"""
        self._poll_timer.stop()
