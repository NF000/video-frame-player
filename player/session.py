"""观看记录 / 断点续播：会话状态持久化。

设计目标（保持流畅度）
----------------------
* 状态文件只有几百字节，放在 ``%APPDATA%\\VideoFramePlayer\\session.json``，
  不污染项目目录，PyInstaller 打包后同样可写。
* 播放期间所有写盘都在**后台 daemon 线程**完成，主线程只投递快照；连续投递
  会合并，只保留最新一份，避免频繁 I/O 影响播放。
* 写盘用「临时文件 + os.replace」原子替换，中途崩溃不会留下半个 JSON。
* 退出时 ``flush()`` 同步写一次最终快照（持锁 <1ms），不等待后台队列，
  关窗不会被拖慢。
* 读取只在启动时做一次，任何损坏（坏 JSON、缺字段、旧版本）都静默降级为
  「没有记录」，绝不影响启动。

记录的是**上次会话**（文件夹、当前视频、播放位置、倍速/音量/静音、退出时
是否正在播放），不是每个视频各自的历史进度：逐帧分析工具里重新打开某一集
通常意味着想从头分析，自动 seek 反而会干扰。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

VERSION = 1
_MAX_POSITION_MS = 7 * 24 * 3600 * 1000   # 7 天，防止存档里的异常值
_MAX_INDEX = 1_000_000


def default_session_path() -> Path:
    """Windows 用户级数据目录：可写、不随程序目录变化。"""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "VideoFramePlayer" / "session.json"


@dataclass
class SessionState:
    """一次会话的可恢复状态。所有字段都做类型/范围收敛，手改存档也不会崩。"""

    version: int = VERSION
    saved_at: str = ""
    folder: str = ""
    current_path: str = ""
    index: int = 0
    position_ms: int = 0
    duration_ms: int = 0
    rate: float = 1.0
    volume: int = 80
    muted: bool = False
    was_playing: bool = False
    auto_resume: bool = True

    @classmethod
    def from_dict(cls, data: object) -> SessionState | None:
        """容错解析：版本不符或结构异常时返回 None（当作没有记录）。"""
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return None

        def _str(key: str, default: str = "") -> str:
            value = data.get(key, default)
            return value if isinstance(value, str) else default

        def _int(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(data.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        def _float(key: str, default: float, low: float, high: float) -> float:
            try:
                value = float(data.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        return cls(
            version=VERSION,
            saved_at=_str("saved_at"),
            folder=_str("folder"),
            current_path=_str("current_path"),
            index=_int("index", 0, 0, _MAX_INDEX),
            position_ms=_int("position_ms", 0, 0, _MAX_POSITION_MS),
            duration_ms=_int("duration_ms", 0, 0, _MAX_POSITION_MS),
            rate=_float("rate", 1.0, 0.25, 4.0),
            volume=_int("volume", 80, 0, 100),
            muted=bool(data.get("muted", False)),
            was_playing=bool(data.get("was_playing", False)),
            auto_resume=bool(data.get("auto_resume", True)),
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "saved_at": self.saved_at,
            "folder": self.folder,
            "current_path": self.current_path,
            "index": self.index,
            "position_ms": self.position_ms,
            "duration_ms": self.duration_ms,
            "rate": self.rate,
            "volume": self.volume,
            "muted": self.muted,
            "was_playing": self.was_playing,
            "auto_resume": self.auto_resume,
        }


class SessionStore:
    """会话状态的读 / 异步合并写 / 退出同步落盘。

    每个快照带单调递增的序号：退出时 flush() 会拿到更大的序号，后台线程里
    尚未写完的旧快照会自动失效，避免「旧位置覆盖退出时的最终位置」。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_session_path()
        self._queue: queue.Queue[tuple[int, SessionState] | None] = queue.Queue()
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._written_seq = 0
        self._write_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._work_loop, name="session-writer", daemon=True
        )
        self._worker.start()

    def load(self) -> SessionState | None:
        """启动时读一次；失败一律当作「没有记录」，不抛异常。"""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            logger.debug("读取观看记录失败（按无记录处理）: %s", exc)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("观看记录损坏，已忽略: %s", exc)
            return None
        return SessionState.from_dict(data)

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _drop_pending(self) -> int:
        """丢弃队列中尚未写出的旧快照（合并写）。"""
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            dropped += 1
        return dropped

    def save(self, state: SessionState) -> None:
        """投递一份快照；主线程只做队列操作，不碰磁盘。"""
        seq = self._next_seq()
        dropped = self._drop_pending()
        if dropped:
            logger.debug("合并观看记录快照 %d 份", dropped)
        state.saved_at = datetime.now().isoformat(timespec="seconds")
        self._queue.put((seq, state))

    def flush(self, state: SessionState) -> None:
        """退出时同步写最终快照：持锁写 <1ms，不等待后台队列。

        序号比所有待写快照都大，后台线程随后拿到的旧快照会被 _write 跳过。
        """
        seq = self._next_seq()
        self._drop_pending()
        state.saved_at = datetime.now().isoformat(timespec="seconds")
        self._write(seq, state)

    def clear(self) -> None:
        """清除观看记录（供「重置观看记录」入口 / 手动清理使用）。"""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清除观看记录失败: %s", exc)

    def _write(self, seq: int, state: SessionState) -> None:
        with self._write_lock:
            if seq < self._written_seq:
                return   # 已有更新的快照落盘，丢弃过期写入
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            self._written_seq = seq

    def _work_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                seq, state = item
                try:
                    self._write(seq, state)
                except Exception as exc:
                    logger.warning("后台保存观看记录失败: %s", exc)
            finally:
                self._queue.task_done()
