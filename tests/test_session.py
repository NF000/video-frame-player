"""观看记录 / 断点续播回归测试。

运行：
    conda activate video-player
    python -m unittest discover -s tests -v

不依赖真实 VLC：FakeEngine 替身模拟「后台打开完成」回调，在 offscreen Qt
平台下完整走一遍 启动恢复 → 后台重建列表 → on_opened → 断点 seek → 关窗落盘。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from player.session import SessionState, SessionStore, VERSION  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def _wait_for(predicate, timeout: float = 5.0, step: float = 0.01) -> bool:
    """轮询等待异步流程完成，期间驱动 Qt 事件循环（若 QApplication 存在）。"""
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(step)
    return False


class FakeEngine:
    """记录调用、不碰 libvlc 的引擎替身。"""

    def __init__(self, win_id: int) -> None:
        self.lib_dir = "fake-vlc"
        self.opened_paths: list[tuple] = []
        self.seek_calls: list[int] = []
        self.pause_calls = 0
        self.playing = True
        self._time_ms = 0
        self._on_opened = None
        self._on_error = None

    # ---- 引擎初始化会调用 ----
    def set_callbacks(self, on_opened=None, on_error=None) -> None:
        self._on_opened = on_opened
        self._on_error = on_error

    def set_end_reached_callback(self, callback) -> None:
        pass

    def set_volume(self, value: int) -> None:
        pass

    def set_rate(self, rate: float) -> None:
        pass

    def set_mute(self, muted: bool) -> None:
        pass

    # ---- 状态查询 ----
    def is_busy(self) -> bool:
        return False

    def is_playing(self) -> bool:
        return self.playing

    def is_paused(self) -> bool:
        return not self.playing

    def is_end_reached(self) -> bool:
        return False

    def get_fps(self) -> float:
        return 0.0

    def get_time_ms(self) -> int:
        return self._time_ms

    def get_length_ms(self) -> int:
        return 120000

    def get_volume(self) -> int:
        return 80

    # ---- 播放控制 ----
    def open(self, path, rate=1.0, volume=80, muted=False) -> None:
        self.opened_paths.append((str(path), rate, volume, muted))
        self._time_ms = 0
        if self._on_opened is not None:
            # 模拟后台线程完成：Qt 信号把通知排回主线程
            QTimer.singleShot(20, self._on_opened)

    def set_time_ms(self, ms: int) -> None:
        self.seek_calls.append(int(ms))
        self._time_ms = int(ms)

    def pause(self) -> None:
        self.pause_calls += 1
        self.playing = False

    def toggle_pause(self) -> None:
        self.playing = not self.playing

    def seek_by(self, delta_ms: int) -> None:
        pass

    def next_frame(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def release(self) -> None:
        pass


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / "session.json"
        self.store = SessionStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_flush_is_synchronous_and_atomic(self) -> None:
        state = SessionState(
            folder="D:/videos", current_path="D:/videos/a.mp4",
            position_ms=12345, index=2, rate=2.0, volume=55,
        )
        self.store.flush(state)
        self.assertTrue(self.path.is_file())
        self.assertFalse(
            self.path.with_name(self.path.name + ".tmp").exists(),
            "临时文件应已被 os.replace 消耗",
        )
        loaded = self.store.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.position_ms, 12345)
        self.assertEqual(loaded.rate, 2.0)
        self.assertTrue(loaded.saved_at)

        raw = self.path.read_bytes()
        self.assertEqual(raw.count(bytes([0])), 0, "存档必须是 UTF-8 文本")
        raw.decode("utf-8")

    def test_save_is_async_and_merges_to_latest(self) -> None:
        started = time.perf_counter()
        for i in range(5):
            self.store.save(SessionState(position_ms=i * 1000))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.05, "主线程不应等待磁盘 I/O")

        ok = _wait_for(
            lambda: (self.store.load() or SessionState()).position_ms == 4000,
            timeout=3.0,
        )
        self.assertTrue(ok, "后台线程应写出最后一份快照")

    def test_flush_beats_pending_async_write(self) -> None:
        """退出时的最终快照不能被后台尚未写完的旧快照覆盖。"""
        for _ in range(20):
            self.store.save(SessionState(position_ms=10))
        self.store.flush(SessionState(position_ms=99000))
        time.sleep(0.15)   # 给后台线程充分机会去写过期快照
        loaded = self.store.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.position_ms, 99000)

    def test_corrupted_or_old_file_is_ignored(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.store.load())

        self.path.write_text(json.dumps({"version": VERSION + 1}), encoding="utf-8")
        self.assertIsNone(self.store.load())

        self.path.write_text(
            json.dumps({
                "version": VERSION,
                "position_ms": "oops", "volume": 9999, "index": -3,
                "rate": 999.0, "current_path": 42,
            }),
            encoding="utf-8",
        )
        state = self.store.load()
        self.assertIsNotNone(state)
        self.assertEqual(state.position_ms, 0)
        self.assertEqual(state.volume, 100)
        self.assertEqual(state.index, 0)
        self.assertEqual(state.rate, 4.0)
        self.assertEqual(state.current_path, "")

    def test_clear(self) -> None:
        self.store.flush(SessionState(current_path="x.mp4"))
        self.store.clear()
        self.assertIsNone(self.store.load())


class RestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_restore_folder_position_and_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            paths: list[Path] = []
            for rel in ("s1/ep1.mp4", "s1/ep2.mp4", "s2/ep10.mp4"):
                path = folder / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake")   # FakeEngine 不会真的解码
                paths.append(path)

            store = SessionStore(folder / "session.json")
            target = paths[1]
            store.flush(SessionState(
                folder=str(folder), current_path=str(target), index=1,
                position_ms=45000, duration_ms=120000,
                rate=2.0, volume=55, muted=True, was_playing=False,
            ))

            fake = FakeEngine(0)
            with patch("ui.main_window.VlcEngine", return_value=fake), \
                    patch("ui.main_window.SessionStore", return_value=store):
                window = MainWindow()
                window.show()
                window.init_engine()

                ok = _wait_for(
                    lambda: bool(fake.seek_calls)
                    and window._pending_seek_ms is None,
                    timeout=6.0,
                )
                self.assertTrue(ok, "断点恢复 seek 未完成")

                self.assertEqual(window._playlist.size, 3)
                self.assertEqual(window._playlist.index, 1)
                self.assertEqual(fake.opened_paths[0][0], str(target))
                self.assertEqual(fake.seek_calls[-1], 45000)
                self.assertEqual(fake.pause_calls, 1, "退出时是暂停态，恢复后应保持暂停")
                self.assertEqual(window._rate_box.currentIndex(), 2)  # 2x
                self.assertEqual(window._last_volume, 55)
                self.assertTrue(window._muted)
                self.assertIn("已恢复上次观看", window.statusBar().currentMessage())

                window.close()
                saved = store.load()
                self.assertIsNotNone(saved)
                self.assertEqual(saved.current_path, str(target))
                self.assertEqual(saved.folder, str(folder))
                self.assertEqual(saved.position_ms, 45000)
                self.assertEqual(saved.rate, 2.0)
                self.assertFalse(saved.was_playing)

    def test_user_action_cancels_pending_restore(self) -> None:
        """恢复扫描尚未回来时用户手动打开视频，恢复结果必须被丢弃。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "a.mp4"
            video.write_bytes(b"fake")
            store = SessionStore(folder / "session.json")
            store.flush(SessionState(
                folder=str(folder), current_path=str(video),
                position_ms=30000, was_playing=True,
            ))
            fake = FakeEngine(0)
            with patch("ui.main_window.VlcEngine", return_value=fake), \
                    patch("ui.main_window.SessionStore", return_value=store):
                window = MainWindow()
                window.show()
                window.init_engine()
                window._cancel_restore()
                ok = _wait_for(lambda: True, timeout=1.2)  # 让定时器全部跑完
                self.assertTrue(ok)
                self.assertEqual(window._playlist.size, 0)
                self.assertEqual(fake.opened_paths, [], "不应自动打开上次的视频")
                window.close()

    def test_auto_resume_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "a.mp4"
            video.write_bytes(b"fake")
            store = SessionStore(folder / "session.json")
            store.flush(SessionState(
                current_path=str(video), position_ms=30000, auto_resume=False,
            ))
            fake = FakeEngine(0)
            with patch("ui.main_window.VlcEngine", return_value=fake), \
                    patch("ui.main_window.SessionStore", return_value=store):
                window = MainWindow()
                window.show()
                window.init_engine()
                _wait_for(lambda: True, timeout=1.2)
                self.assertEqual(fake.opened_paths, [])
                window.close()


    def test_restore_playing_state_and_near_end_position(self) -> None:
        """上次正在播放且停在片尾附近：恢复后继续播放，不 seek 到片尾触发自动续播。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "a.mp4"
            video.write_bytes(b"fake")
            store = SessionStore(folder / "session.json")
            store.flush(SessionState(
                current_path=str(video), position_ms=119000,
                duration_ms=120000, was_playing=True,
            ))
            fake = FakeEngine(0)
            with patch("ui.main_window.VlcEngine", return_value=fake), \
                    patch("ui.main_window.SessionStore", return_value=store):
                window = MainWindow()
                window.show()
                window.init_engine()
                ok = _wait_for(
                    lambda: bool(window._restore_label)
                    and window._pending_seek_ms is None,
                    timeout=6.0,
                )
                self.assertTrue(ok, "恢复流程未走完")
                self.assertEqual(fake.seek_calls, [], "片尾断点不应再 seek")
                self.assertEqual(fake.pause_calls, 0, "退出时在播放，恢复后不应暂停")
                self.assertTrue(fake.is_playing())
                window.close()

    def test_restore_single_file_without_folder(self) -> None:
        """单文件模式（没有父文件夹）也能恢复断点。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "a.mp4"
            video.write_bytes(b"fake")
            store = SessionStore(folder / "session.json")
            store.flush(SessionState(
                current_path=str(video), position_ms=30000, was_playing=False,
            ))
            fake = FakeEngine(0)
            with patch("ui.main_window.VlcEngine", return_value=fake), \
                    patch("ui.main_window.SessionStore", return_value=store):
                window = MainWindow()
                window.show()
                window.init_engine()
                ok = _wait_for(
                    lambda: bool(fake.seek_calls)
                    and window._pending_seek_ms is None,
                    timeout=6.0,
                )
                self.assertTrue(ok)
                self.assertEqual(window._playlist.size, 0)
                self.assertEqual(fake.seek_calls[-1], 30000)
                self.assertEqual(fake.pause_calls, 1)
                window.close()
                saved = store.load()
                self.assertIsNotNone(saved)
                self.assertEqual(saved.folder, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
