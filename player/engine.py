"""VLC 播放引擎：把 python-vlc 的 ctypes 细节与 UI 层隔离，统一异常类型。

性能设计（重要）
----------------
libvlc 3.0 在结束当前播放时（``stop()`` / ``set_media()`` / ``set_media(None)``）
会**同步等待** vout 与 input 线程退出，实测固定约 5 秒；当视频输出嵌入 Qt
窗口时还可能等待窗口消息循环，演变成界面长时间无响应甚至死锁。

因此本模块把「打开 / 停止 / 释放」这类可能阻塞的调用统一投递到一条专用
后台线程（daemon）串行执行，UI 主线程只保留毫秒级的只读状态查询。
切换期间 ``is_busy()`` 为 True，UI 应跳过状态轮询，避免竞态。

副作用：``open()`` / ``stop()`` 是**异步**的。引擎通过 ``set_callbacks()``
注入的 ``on_opened`` / ``on_error`` 通知 UI；回调运行在后台线程，UI 侧必须
转成 Qt 信号（queued connection）后再操作控件。
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".mp4", ".mkv", ".avi"})


class EngineError(RuntimeError):
    """播放引擎可预期的错误：文件、运行时、VLC 调用。"""


class VlcEngine:
    """对 libvlc_media_player 的薄封装。所有 VLC 调用都有异常捕获。"""

    def __init__(self, video_win_id: int) -> None:
        # 顺序关键：先准备 DLL 搜索路径，再 import vlc
        from . import vlc_runtime

        try:
            self.lib_dir, self.plugins_dir = vlc_runtime.prepare()
        except vlc_runtime.VLCRuntimeNotFound as exc:
            raise EngineError(str(exc)) from exc

        try:
            import vlc
        except Exception as exc:
            raise EngineError(f"python-vlc 导入失败: {exc}") from exc

        self._vlc = vlc
        self._instance = None
        self._player = None
        self._media = None
        self._fps_cache = 0.0
        self._end_cb = None  # 持有事件回调引用，防止被 ctypes GC

        try:
            self._instance = vlc.Instance(
                "--no-video-title-show",   # 不在画面上叠加文件名
                "--no-snapshot-preview",
                "--quiet",
            )
            if self._instance is None:
                raise EngineError(
                    "libvlc 实例创建失败。最常见原因是 libvlc.dll 与 Python "
                    "位数不一致（两者都必须是 64 位）。"
                )
            self._player = self._instance.media_player_new()
            if self._player is None:
                raise EngineError("media_player_new() 返回空对象")
            self._player.set_hwnd(int(video_win_id))
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(f"VLC 初始化失败: {exc}") from exc

        # ---- 后台串行任务队列：所有可能阻塞的 libvlc 调用都走这里 ----
        self._tasks: queue.Queue = queue.Queue()
        self._busy = False
        self._busy_lock = threading.Lock()
        self._closed = False
        self._on_opened = None
        self._on_error = None
        self._worker = threading.Thread(
            target=self._work_loop, name="vlc-engine-worker", daemon=True
        )
        self._worker.start()

    # ---------------- 后台任务调度 ----------------
    def set_callbacks(self, on_opened=None, on_error=None) -> None:
        """注入 UI 通知回调。回调在后台线程执行，UI 侧须转成 Qt 信号。"""
        self._on_opened = on_opened
        self._on_error = on_error

    def is_busy(self) -> bool:
        """是否有后台切换任务正在执行；True 时 UI 应跳过状态轮询。"""
        with self._busy_lock:
            return self._busy

    def _submit(self, fn, *args, label: str = "") -> None:
        if self._closed:
            return
        with self._busy_lock:
            self._busy = True
        self._tasks.put((fn, args, label))

    def _work_loop(self) -> None:
        while True:
            item = self._tasks.get()
            if item is None:                 # 退出哨兵
                self._tasks.task_done()
                break
            fn, args, label = item
            try:
                fn(*args)
                if label == "open" and self._on_opened is not None:
                    self._on_opened()
            except Exception as exc:
                logger.error("VLC 后台任务 %s 失败: %s", label, exc)
                if self._on_error is not None:
                    try:
                        self._on_error(f"{label} 失败: {exc}")
                    except Exception:
                        logger.exception("错误回调本身异常")
            finally:
                with self._busy_lock:
                    self._busy = not self._tasks.empty()
                self._tasks.task_done()

    # ---------------- 媒体加载（异步）----------------
    def open(
        self,
        path: str | Path,
        rate: float = 1.0,
        volume: int = 80,
        muted: bool = False,
    ) -> None:
        """异步打开视频并立即返回。

        文件校验是同步的（可直接抛 EngineError）；真正的 set_media/play 交给
        后台线程，避免 libvlc 结束上一路播放时的约 5 秒等待冻结界面。
        rate / volume / muted 一起在后台设置，避免与主线程产生并发写。
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise EngineError(f"文件不存在或不是普通文件: {file_path}")
        if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise EngineError(
                f"不支持的格式 {file_path.suffix!r}，仅支持 mp4 / mkv / avi"
            )
        self._submit(
            self._do_open,
            str(file_path.resolve()),
            float(rate),
            int(volume),
            bool(muted),
            label="open",
        )

    def _do_open(self, path: str, rate: float, volume: int, muted: bool) -> None:
        media = self._instance.media_new_path(path)
        if media is None:
            raise EngineError("media_new_path() 返回空对象")
        # 不再显式调用 stop()：set_media() 会让 libvlc 自动结束上一路播放。
        # 这一步仍可能阻塞约 5 秒，但发生在后台线程，UI 完全不受影响。
        self._player.set_media(media)
        self._media = media
        self._fps_cache = 0.0
        self._player.set_rate(rate)
        self._player.audio_set_volume(volume)
        self._player.audio_set_mute(muted)
        self._player.play()

    # ---------------- 播放控制 ----------------
    def play(self) -> None:
        try:
            self._player.play()
        except Exception as exc:
            raise EngineError(f"播放失败: {exc}") from exc

    def pause(self) -> None:
        """确保进入暂停态。set_pause(1) 幂等；pause() 是切换，不用它。"""
        try:
            self._player.set_pause(1)
        except Exception as exc:
            raise EngineError(f"暂停失败: {exc}") from exc

    def toggle_pause(self) -> None:
        try:
            self._player.pause()
        except Exception as exc:
            raise EngineError(f"切换播放/暂停失败: {exc}") from exc

    def stop(self) -> None:
        """异步停止：libvlc 的 stop() 会同步等约 5 秒，放到后台线程。"""
        self._submit(self._do_stop, label="stop")

    def _do_stop(self) -> None:
        if self._player is not None:
            self._player.stop()

    # ---------------- 时间 / 进度 ----------------
    def get_time_ms(self) -> int:
        try:
            value = int(self._player.get_time() or 0)
        except Exception:
            return 0
        return max(0, value)

    def get_length_ms(self) -> int:
        try:
            value = int(self._player.get_length() or 0)
        except Exception:
            return 0
        return max(0, value)

    def set_time_ms(self, ms: int) -> None:
        """跳转到指定毫秒；暂停态、播放态都生效（VLC 会 seek 并刷新画面）。"""
        try:
            target = max(0, int(ms))
            length = self.get_length_ms()
            if length > 0:
                target = min(target, length)
            self._player.set_time(target)
        except Exception as exc:
            raise EngineError(f"跳转失败: {exc}") from exc

    def seek_by(self, delta_ms: int) -> None:
        """相对当前时间跳转（播放态方向键快进 / 快退）。

        末尾留一小段余量：直接 set_time(length) 会让 VLC 立刻进入 Ended，
        在开启「自动续播」时按一下快进就会跳到下一个视频，体验很突兀。
        """
        target = max(0, self.get_time_ms() + int(delta_ms))
        length = self.get_length_ms()
        if length > 0:
            tail = min(500, max(0, length // 10))
            target = min(target, max(0, length - tail))
        self.set_time_ms(target)

    # ---------------- 倍速 / 音量 ----------------
    def set_rate(self, rate: float) -> None:
        """VLC 内置时间拉伸，倍速切换时音频自动变速并保持音画同步。"""
        try:
            self._player.set_rate(float(rate))
        except Exception as exc:
            raise EngineError(f"设置倍速失败: {exc}") from exc

    def get_rate(self) -> float:
        try:
            return float(self._player.get_rate() or 1.0)
        except Exception:
            return 1.0

    def set_volume(self, volume: int) -> None:
        try:
            self._player.audio_set_volume(max(0, min(100, int(volume))))
        except Exception as exc:
            raise EngineError(f"设置音量失败: {exc}") from exc

    def get_volume(self) -> int:
        try:
            return int(self._player.audio_get_volume() or 0)
        except Exception:
            return 0

    def set_mute(self, muted: bool) -> None:
        try:
            self._player.audio_set_mute(bool(muted))
        except Exception as exc:
            raise EngineError(f"静音切换失败: {exc}") from exc

    def is_muted(self) -> bool:
        try:
            return bool(self._player.audio_get_mute())
        except Exception:
            return False

    # ---------------- 帧 ----------------
    def next_frame(self) -> None:
        """VLC 唯一的帧步进 API，仅在暂停态有效。"""
        try:
            self._player.next_frame()
        except Exception as exc:
            raise EngineError(f"前进一帧失败: {exc}") from exc

    def get_fps(self) -> float:
        """返回媒体帧率；刚打开时可能暂时取不到，返回 0 表示需要重试。"""
        if self._fps_cache > 0:
            return self._fps_cache
        try:
            value = float(self._player.get_fps() or 0.0)
        except Exception as exc:
            logger.debug("读取 fps 失败: %s", exc)
            return 0.0
        if value > 0:
            self._fps_cache = value
        return value

    # ---------------- 状态 ----------------
    def is_paused(self) -> bool:
        try:
            return self._player.get_state() == self._vlc.State.Paused
        except Exception:
            return False

    def is_playing(self) -> bool:
        try:
            return self._player.get_state() == self._vlc.State.Playing
        except Exception:
            return False

    def is_end_reached(self) -> bool:
        """播放到自然结尾时为 True，用于「播完自动切下一个」。"""
        try:
            return self._player.get_state() == self._vlc.State.Ended
        except Exception:
            return False

    def set_end_reached_callback(self, callback) -> None:
        """注册 libvlc 的 MediaPlayerEndReached 事件，作为轮询之外的双保险。

        **回调运行在 VLC 线程**：libvlc 不可重入，回调里禁止调用任何
        libvlc 函数（包括 is_end_reached）。它只把消息投递回主线程，
        由 UI 层在主线程复查状态后再切换视频。

        注册失败只记日志：UI 的定时轮询仍能兜底，不影响功能。
        """
        self._end_cb = callback  # 必须持有引用，否则 ctypes 回调会被回收
        try:
            manager = self._player.event_manager()
            manager.event_attach(
                self._vlc.EventType.MediaPlayerEndReached, callback
            )
        except Exception as exc:
            logger.warning("注册播放结束事件失败（由轮询兜底）: %s", exc)

    # ---------------- 释放 ----------------
    def release(self) -> None:
        """窗口关闭时调用：异步投递释放任务，立即返回，不阻塞退出。

        worker 是 daemon 线程，不会阻止进程退出；OS 会在进程结束时回收
        尚未完全释放的 VLC 资源。
        """
        if self._closed:
            return
        self._submit(self._do_release, label="release")
        self._closed = True
        self._tasks.put(None)

    def _do_release(self) -> None:
        try:
            if self._player is not None:
                self._player.event_manager().event_detach(
                    self._vlc.EventType.MediaPlayerEndReached
                )
        except Exception as exc:
            logger.debug("注销结束事件异常: %s", exc)
        try:
            if self._player is not None:
                self._player.stop()
                self._player.release()
        except Exception as exc:
            logger.warning("释放 player 异常: %s", exc)
        try:
            if self._instance is not None:
                self._instance.release()
        except Exception as exc:
            logger.warning("释放 instance 异常: %s", exc)
