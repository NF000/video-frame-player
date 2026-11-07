"""VLC 播放引擎：把 python-vlc 的 ctypes 细节与 UI 层隔离，统一异常类型。

性能设计（重要）
----------------
libvlc 3.0 在结束当前播放时（``stop()`` / ``set_media()`` / ``set_media(None)``）
会**同步等待** vout 与 input 线程退出，实测固定约 5 秒；当视频输出嵌入 Qt
窗口时还可能等待窗口消息循环，演变成界面长时间无响应甚至死锁。

因此本模块保证 **UI 主线程永不直接调用 libvlc**：

* 所有 libvlc 调用都在唯一的后台 worker 线程里串行执行，包括
  ``get_time`` / ``get_length`` / ``get_state`` / ``get_fps`` 这类「看起来很快」
  的只读查询，以及暂停、跳转、单帧步进、倍速、音量等控制命令；
* worker 每 ``SAMPLE_INTERVAL_S`` 秒采一份状态快照放进缓存，主线程只读
  缓存副本（加锁复制），状态最多滞后一个采样周期；
* 打开 / 停止 / 释放标记为 heavy，期间 ``is_busy()`` 为 True，UI 跳过刷新；
  轻量控制任务不触发 busy，避免连点被吞。

为什么连只读查询也要挪走：Windows 上 VLC 把 vout 嵌成 Qt 的原生子窗口，
vout 线程渲染 / SetParent 时会等待主线程处理窗口消息；若主线程此刻又
持有 libvlc 的 player / input 锁，就可能形成跨线程互相等待，表现为界面
长时间「未响应」。无头或非前台窗口环境测不出这种死锁，只能靠架构规避。

副作用：引擎所有公开方法都「立即返回」。``open()`` / ``stop()`` /
``release()`` 通过 ``set_callbacks()`` 注入的回调通知 UI，回调运行在后台
线程，UI 侧必须转成 Qt 信号（queued connection）后再操作控件。
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".mp4", ".mkv", ".avi"})
SAMPLE_INTERVAL_S = 0.05   # 后台状态采样间隔；主线程零 libvlc 调用


class EngineError(RuntimeError):
    """播放引擎可预期的错误：文件、运行时、VLC 调用。"""


class VlcEngine:
    """libvlc 的线程安全封装：后台串行执行 + 状态快照缓存。"""

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
        self._end_cb = None          # 外部回调（Qt 信号 emit）
        self._end_cb_impl = None     # 真正注册到 libvlc 的 ctypes 回调，防 GC

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

        # ---- 状态快照：worker 写、主线程读，读写都在锁内且不含 libvlc 调用 ----
        self._state_lock = threading.Lock()
        self._cached = {
            "time_ms": 0,
            "length_ms": 0,
            "state": vlc.State.NothingSpecial,
            "fps": 0.0,
            "rate": 1.0,
            "volume": 80,
            "muted": False,
        }
        self._end_flag = False       # VLC 结束事件置位，避免 50ms 采样漏掉短状态

        # ---- 后台串行任务队列 ----
        self._tasks: queue.Queue = queue.Queue()
        self._heavy_pending = 0
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
        """是否有 heavy 任务（打开/停止/释放）正在执行。"""
        with self._busy_lock:
            return self._busy

    def _cached_value(self, key: str):
        """主线程读取快照；锁内只做字典索引，绝不触碰 libvlc。"""
        with self._state_lock:
            return self._cached[key]

    def _sample_state(self) -> None:
        """worker 线程：采集一份完整状态。异常一律忽略，保留上一份快照。"""
        if self._closed or self._player is None:
            return
        player = self._player
        try:
            snap = {
                "time_ms": max(0, int(player.get_time() or 0)),
                "length_ms": max(0, int(player.get_length() or 0)),
                "state": player.get_state(),
                "fps": float(player.get_fps() or 0.0),
                "rate": float(player.get_rate() or 1.0),
                "volume": int(player.audio_get_volume() or 0),
                "muted": bool(player.audio_get_mute()),
            }
        except Exception as exc:
            logger.debug("状态采样失败: %s", exc)
            return
        with self._state_lock:
            self._cached = snap

    def _submit(self, fn, *args, label: str = "", heavy: bool = False) -> None:
        """把一次 libvlc 调用投递到 worker；立即返回。"""
        if self._closed:
            return
        if heavy:
            with self._busy_lock:
                self._heavy_pending += 1
                self._busy = True
        self._tasks.put((fn, args, label, heavy))

    def _work_loop(self) -> None:
        """唯一的 libvlc 调用线程：有任务就执行，空闲就采样状态。"""
        while True:
            try:
                item = self._tasks.get(timeout=SAMPLE_INTERVAL_S)
            except queue.Empty:
                self._sample_state()
                continue
            if item is None:                 # 退出哨兵
                self._tasks.task_done()
                break
            fn, args, label, heavy = item
            try:
                fn(*args)
                self._sample_state()         # 任务后立即刷新，减少 UI 滞后
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
                if heavy:
                    with self._busy_lock:
                        self._heavy_pending = max(0, self._heavy_pending - 1)
                        self._busy = self._heavy_pending > 0
                self._tasks.task_done()

    # ---------------- 媒体加载（异步，heavy）----------------
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
        rate / volume / muted 一起在后台设置，避免并发写。
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
            heavy=True,
        )

    def _do_open(self, path: str, rate: float, volume: int, muted: bool) -> None:
        with self._state_lock:
            self._cached["fps"] = 0.0
            self._cached["time_ms"] = 0
            self._cached["length_ms"] = 0
            self._end_flag = False
        media = self._instance.media_new_path(path)
        if media is None:
            raise EngineError("media_new_path() 返回空对象")
        # 不再显式调用 stop()：set_media() 会让 libvlc 自动结束上一路播放。
        # 这一步仍可能阻塞约 5 秒，但发生在后台线程，UI 完全不受影响。
        self._player.set_media(media)
        self._media = media
        self._player.set_rate(rate)
        self._player.audio_set_volume(volume)
        self._player.audio_set_mute(muted)
        self._player.play()

    # ---------------- 播放控制（异步，轻量）----------------
    def play(self) -> None:
        self._submit(self._do_play, label="play")

    def _do_play(self) -> None:
        try:
            self._player.play()
        except Exception as exc:
            raise EngineError(f"播放失败: {exc}") from exc

    def pause(self) -> None:
        """确保进入暂停态。set_pause(1) 幂等；pause() 是切换，不用它。"""
        self._submit(self._do_pause, label="pause")

    def _do_pause(self) -> None:
        try:
            self._player.set_pause(1)
        except Exception as exc:
            raise EngineError(f"暂停失败: {exc}") from exc

    def toggle_pause(self) -> None:
        self._submit(self._do_toggle_pause, label="toggle-pause")

    def _do_toggle_pause(self) -> None:
        try:
            self._player.pause()
        except Exception as exc:
            raise EngineError(f"切换播放/暂停失败: {exc}") from exc

    def stop(self) -> None:
        """异步停止：libvlc 的 stop() 会同步等约 5 秒，放到后台线程。"""
        self._submit(self._do_stop, label="stop", heavy=True)

    def _do_stop(self) -> None:
        if self._player is not None:
            self._player.stop()

    # ---------------- 时间 / 进度 ----------------
    def get_time_ms(self) -> int:
        """读缓存快照，不调用 libvlc。"""
        return int(self._cached_value("time_ms"))

    def get_length_ms(self) -> int:
        return int(self._cached_value("length_ms"))

    def set_time_ms(self, ms: int) -> None:
        """异步跳转到指定毫秒；暂停态、播放态都生效。"""
        self._submit(self._do_set_time, max(0, int(ms)), label="seek")

    def _do_set_time(self, ms: int) -> None:
        try:
            target = max(0, int(ms))
            length = int(self._player.get_length() or 0)
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
        self._submit(self._do_seek_by, int(delta_ms), label="seek-by")

    def _do_seek_by(self, delta_ms: int) -> None:
        target = max(0, int(self._player.get_time() or 0) + int(delta_ms))
        length = int(self._player.get_length() or 0)
        if length > 0:
            tail = min(500, max(0, length // 10))
            target = min(target, max(0, length - tail))
        self._do_set_time(target)

    # ---------------- 倍速 / 音量 ----------------
    def set_rate(self, rate: float) -> None:
        """VLC 内置时间拉伸，倍速切换时音频自动变速并保持音画同步。"""
        self._submit(self._do_set_rate, float(rate), label="rate")

    def _do_set_rate(self, rate: float) -> None:
        try:
            self._player.set_rate(float(rate))
        except Exception as exc:
            raise EngineError(f"设置倍速失败: {exc}") from exc

    def get_rate(self) -> float:
        return float(self._cached_value("rate"))

    def set_volume(self, volume: int) -> None:
        self._submit(self._do_set_volume, int(volume), label="volume")

    def _do_set_volume(self, volume: int) -> None:
        try:
            self._player.audio_set_volume(max(0, min(100, int(volume))))
        except Exception as exc:
            raise EngineError(f"设置音量失败: {exc}") from exc

    def get_volume(self) -> int:
        return int(self._cached_value("volume"))

    def set_mute(self, muted: bool) -> None:
        self._submit(self._do_set_mute, bool(muted), label="mute")

    def _do_set_mute(self, muted: bool) -> None:
        try:
            self._player.audio_set_mute(bool(muted))
        except Exception as exc:
            raise EngineError(f"静音切换失败: {exc}") from exc

    def is_muted(self) -> bool:
        return bool(self._cached_value("muted"))

    # ---------------- 帧 ----------------
    def next_frame(self) -> None:
        """VLC 唯一的帧步进 API，仅在暂停态有效；同样投递后台。"""
        self._submit(self._do_next_frame, label="next-frame")

    def _do_next_frame(self) -> None:
        try:
            self._player.next_frame()
        except Exception as exc:
            raise EngineError(f"前进一帧失败: {exc}") from exc

    def get_fps(self) -> float:
        """返回采样到的媒体帧率；刚打开时可能是 0，UI 会定时重试。"""
        return float(self._cached_value("fps"))

    # ---------------- 状态 ----------------
    def is_paused(self) -> bool:
        return self._cached_value("state") == self._vlc.State.Paused

    def is_playing(self) -> bool:
        return self._cached_value("state") == self._vlc.State.Playing

    def is_end_reached(self) -> bool:
        """播放自然结束：事件标志优先，避免 50ms 采样漏掉短暂的 Ended。"""
        with self._state_lock:
            if self._end_flag:
                return True
            return self._cached["state"] == self._vlc.State.Ended

    def set_end_reached_callback(self, callback) -> None:
        """注册 libvlc 的 MediaPlayerEndReached 事件，作为轮询之外的双保险。

        **回调运行在 VLC 线程**：libvlc 不可重入，回调里禁止调用任何
        libvlc 函数。内部回调只置一个标志再转发给外部 Qt 信号，两者都
        不碰 libvlc。注册本身也放在后台线程，保持主线程零 libvlc 调用。
        """
        self._submit(
            self._do_register_end_callback, callback, label="register-end-cb"
        )

    def _do_register_end_callback(self, callback) -> None:
        self._end_cb = callback

        def _internal(*_args) -> None:
            with self._state_lock:
                self._end_flag = True
            if callback is not None:
                callback(*_args)

        self._end_cb_impl = _internal   # 必须持有引用，否则 ctypes 回调会被回收
        try:
            manager = self._player.event_manager()
            manager.event_attach(
                self._vlc.EventType.MediaPlayerEndReached, _internal
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
        self._submit(self._do_release, label="release", heavy=True)
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
