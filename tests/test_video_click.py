"""点击画面切换播放/暂停 的回归测试。

运行：
    conda activate video-player
    python -m unittest discover -s tests -v

验证三条点击通道（Qt 鼠标事件 / WM_PARENTNOTIFY / 轮询兜底）的去重与边界，
以及主窗口是否把 clicked 信号接到了播放控制。测试全程不依赖真实 VLC。
"""
from __future__ import annotations

import ctypes
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from ui.video_widget import (  # noqa: E402
    CLICK_DEBOUNCE_S,
    VideoWidget,
    _IS_WINDOWS,
)


class VideoClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        # 测试期间不让真实鼠标状态干扰；需要测轮询的用例自行覆盖
        cls._no_button = patch.object(
            VideoWidget, "_left_button_down", return_value=False
        )
        cls._no_button.start()
        cls.win = QMainWindow()
        cls.win.resize(500, 400)
        cls.win.show()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._no_button.stop()
        cls.win.close()

    def setUp(self) -> None:
        self.widget = VideoWidget(self.win)
        self.win.setCentralWidget(self.widget)
        self.widget.show()
        self.widget._poll_timer.stop()   # 手动驱动，避免定时器干扰
        self.widget._last_click_at = 0.0
        self.widget._poll_pressed = False
        self.count = 0
        self.widget.clicked.connect(self._on_clicked)

    def tearDown(self) -> None:
        self.widget._poll_timer.stop()

    def _on_clicked(self) -> None:
        self.count += 1

    # ---- 通道 1：Qt 鼠标事件 ----
    def test_left_click_emits_once(self) -> None:
        QTest.mouseClick(self.widget, Qt.MouseButton.LeftButton)
        self.assertEqual(self.count, 1)

    def test_right_click_is_ignored(self) -> None:
        QTest.mouseClick(self.widget, Qt.MouseButton.RightButton)
        self.assertEqual(self.count, 0)

    def test_debounce_merges_multi_channel_reports(self) -> None:
        """同一次点击被多条通道上报时，250ms 内只算一次。"""
        with patch("ui.video_widget.time.monotonic") as clock:
            clock.return_value = 100.0
            self.widget._report_click()
            self.assertEqual(self.count, 1)
            clock.return_value = 100.0 + CLICK_DEBOUNCE_S * 0.5
            self.widget._report_click()
            self.assertEqual(self.count, 1, "短时间内的重复上报应被丢弃")
            clock.return_value = 100.0 + CLICK_DEBOUNCE_S + 0.01
            self.widget._report_click()
            self.assertEqual(self.count, 2, "超过去重窗口后应能再次点击")

    # ---- 通道 2：WM_PARENTNOTIFY 原生消息 ----
    @unittest.skipUnless(_IS_WINDOWS, "WM_PARENTNOTIFY 仅 Windows 有效")
    def test_parent_notify_from_vlc_child_window(self) -> None:
        from ui.video_widget import WM_LBUTTONDOWN, WM_PARENTNOTIFY, _MSG

        msg = _MSG()
        msg.message = WM_PARENTNOTIFY
        msg.wParam = WM_LBUTTONDOWN
        msg.lParam = (20 & 0xFFFF) | ((20 & 0xFFFF) << 16)  # x=20, y=20
        self.widget.nativeEvent(
            b"windows_generic_MSG", ctypes.addressof(msg)
        )
        self.assertEqual(self.count, 1, "VLC 子窗口的点击应转发为 clicked")

    @unittest.skipUnless(_IS_WINDOWS, "WM_PARENTNOTIFY 仅 Windows 有效")
    def test_parent_notify_ignores_other_messages(self) -> None:
        from ui.video_widget import _MSG

        msg = _MSG()
        msg.message = 0x0200   # WM_MOUSEMOVE
        msg.wParam = 0
        msg.lParam = 0
        self.widget.nativeEvent(
            b"windows_generic_MSG", ctypes.addressof(msg)
        )
        self.assertEqual(self.count, 0)

    # ---- 通道 3：轮询兜底 ----
    def test_poll_triggers_only_on_press_edge_inside_widget(self) -> None:
        with patch.object(VideoWidget, "_left_button_down") as down, \
                patch.object(VideoWidget, "_cursor_in_widget") as inside, \
                patch.object(VideoWidget, "_window_is_active",
                             return_value=True):
            down.return_value = False
            inside.return_value = True
            self.widget._poll_click()
            self.assertEqual(self.count, 0, "未按下不应触发")

            down.return_value = True
            self.widget._poll_click()
            self.assertEqual(self.count, 1, "按下边沿应触发一次")
            self.widget._poll_click()
            self.assertEqual(self.count, 1, "按住不放不应重复触发")

            down.return_value = False
            self.widget._poll_click()   # 松开复位
            down.return_value = True
            inside.return_value = False
            self.widget._poll_click()
            self.assertEqual(self.count, 1, "光标在控制栏上不应触发")

            down.return_value = False
            self.widget._poll_click()
            # 模拟已经过去 >250ms，否则会被去重窗口合并掉
            self.widget._last_click_at = 0.0
            down.return_value = True
            inside.return_value = True
            self.widget._poll_click()
            self.assertEqual(self.count, 2, "再次按下应能再次切换")

    def test_poll_ignores_inactive_window(self) -> None:
        with patch.object(VideoWidget, "_left_button_down", return_value=True), \
                patch.object(VideoWidget, "_cursor_in_widget",
                             return_value=True), \
                patch.object(VideoWidget, "_window_is_active",
                             return_value=False):
            self.widget._poll_click()
            self.assertEqual(self.count, 0)


class MainWindowClickWiringTests(unittest.TestCase):
    """主窗口必须把 clicked 接到播放/暂停，且 busy 时忽略。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls._no_button = patch.object(
            VideoWidget, "_left_button_down", return_value=False
        )
        cls._no_button.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._no_button.stop()

    def test_clicked_signal_toggles_playback(self) -> None:
        try:
            from tests.test_session import FakeEngine
        except ImportError:  # unittest discover -s tests 时的导入路径
            from test_session import FakeEngine
        from ui.main_window import MainWindow

        fake = FakeEngine(0)
        with patch("ui.main_window.VlcEngine", return_value=fake), \
                patch("ui.main_window.SessionStore"):
            window = MainWindow()
            window.show()
            window.init_engine()
            window._video._poll_timer.stop()
            self.assertTrue(fake.playing)
            window._video.clicked.emit()
            self.assertFalse(fake.playing, "点击画面应暂停")
            window._video.clicked.emit()
            self.assertTrue(fake.playing, "再点一次应继续播放")

            fake.is_busy = lambda: True
            window._video.clicked.emit()
            self.assertTrue(fake.playing, "后台换片时应忽略点击")
            window.close()

    def test_hint_mentions_click(self) -> None:
        from ui.main_window import HINT
        self.assertIn("点击画面", HINT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
