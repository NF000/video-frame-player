"""帧网格：VLC 只有 next_frame()，**没有 previous_frame()**。

需求里的「左键后退一帧」只能靠时间网格 seek 模拟：

    t_n = n * (1000 / fps)

这里维护帧序号 n，而不是反复读 get_time() 再减一帧，可以避免 VLC seek
回读误差被逐次累积。

精度边界（libvlc 固有限制，非本实现缺陷）：
    * 恒定帧率 CFR 视频：准确
    * 可变帧率 VFR / 长 GOP 视频：VLC 的 seek 落在关键帧后解码，
      可能落到相邻帧
"""
from __future__ import annotations

import logging

from .engine import VlcEngine

logger = logging.getLogger(__name__)

DEFAULT_FPS = 30.0


class FrameGrid:
    """基于帧序号的时间网格，负责前进/后退一帧与 fps 校准。"""

    def __init__(self, engine: VlcEngine) -> None:
        self._engine = engine
        self._fps = DEFAULT_FPS
        self._index = 0
        self._assumed_fps = True

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def frame_ms(self) -> float:
        return 1000.0 / self._fps

    @property
    def index(self) -> int:
        return self._index

    @property
    def is_assumed_fps(self) -> bool:
        """True 表示还没拿到媒体真实帧率，UI 应定时重试 refresh()。"""
        return self._assumed_fps

    def reset(self) -> None:
        """打开新视频时调用。"""
        self._fps = DEFAULT_FPS
        self._index = 0
        self._assumed_fps = True
        self.refresh()

    def refresh(self) -> None:
        """重新读取真实 fps；取不到时保留兜底值并标记，供 UI 定时重试。"""
        fps = self._engine.get_fps()
        if fps > 0:
            self._fps = fps
            self._assumed_fps = False
            self.sync_from_time()
        else:
            self._assumed_fps = True

    def sync_from_time(self) -> None:
        """拖拽进度条 / 跳转之后，按当前时间重建帧序号。"""
        try:
            current = self._engine.get_time_ms()
        except Exception:
            current = 0
        self._index = max(0, round(current / self.frame_ms))

    def step_forward(self) -> bool:
        """右键：单帧前进。仅暂停态生效；返回是否真的执行了。"""
        if not self._engine.is_paused():
            return False
        try:
            self._engine.next_frame()
        except Exception as exc:
            logger.warning("前进一帧失败: %s", exc)
            return False
        self._index += 1
        return True

    def step_backward(self) -> bool:
        """左键：单帧后退。仅暂停态生效；返回是否真的执行了。"""
        if not self._engine.is_paused() or self._index <= 0:
            return False
        self._index -= 1
        try:
            self._engine.set_time_ms(int(self._index * self.frame_ms))
        except Exception as exc:
            logger.warning("后退一帧失败: %s", exc)
            return False
        return True
