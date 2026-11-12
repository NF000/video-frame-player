"""观看记录 / 断点续播：会话状态持久化。

设计目标（保持流畅度 + 运行数据不出项目目录）
----------------------------------------------
* **数据落项目目录，不写 C 盘**：状态文件默认写在项目自身目录 —— 源码运行
  时是工程根，PyInstaller onedir 冻结态是 exe 所在的分发文件夹。整个文件夹
  搬到别的盘、别的机器，观看记录跟着走。只有显式设置 ``VFP_DATA_DIR`` 才
  改到别处（用于只读介质 / 权限受限的部署）。
* **旧存档一次性搬迁**：首次启动时若新位置还没有存档、但
  ``%APPDATA%\\VideoFramePlayer\\session.json`` 存在，就把它复制过来，并把
  旧文件改名为 ``session.json.migrated`` 留作备份（不删除、不覆盖新文件）。
  任何失败都只记日志，绝不阻断启动。
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
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

VERSION = 1
_MAX_POSITION_MS = 7 * 24 * 3600 * 1000   # 7 天，防止存档里的异常值
_MAX_INDEX = 1_000_000

APP_NAME = "VideoFramePlayer"
SESSION_FILENAME = "session.json"
DATA_DIR_ENV = "VFP_DATA_DIR"

# 源码态：工程根（player/session.py 的上两级）；冻结态：exe 所在分发文件夹。
# 与 player/vlc_runtime.py 的源码态/冻结态判断、script2video 的 D5 约定一致。
FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_ROOT = Path(__file__).resolve().parents[1]
EXE_DIR = Path(sys.executable).resolve().parent if FROZEN else SOURCE_ROOT


def resolve_data_root(
    *, frozen: bool, exe_dir: Path, root: Path,
    override: str | Path | None = None,
) -> Path:
    """解析可写数据根：默认落在项目目录，绝不落到 C 盘用户目录。

    * 冻结态 → exe 所在文件夹（onedir 分发目录）；
    * 源码态 → 工程根；
    * ``VFP_DATA_DIR`` → 显式覆盖，仅用于只读介质 / 权限受限等特殊部署。
    """
    if override:
        return Path(override).expanduser().resolve()
    return Path(exe_dir if frozen else root).resolve()


def data_root() -> Path:
    """当前进程的数据根；每次读取环境变量，便于测试与运行时调整。"""
    return resolve_data_root(
        frozen=FROZEN,
        exe_dir=EXE_DIR,
        root=SOURCE_ROOT,
        override=os.environ.get(DATA_DIR_ENV),
    )


def default_session_path() -> Path:
    """观看记录默认路径：数据根下的 session.json（项目目录，非 APPDATA）。"""
    return data_root() / SESSION_FILENAME


def legacy_session_path() -> Path:
    """旧版存档位置（只读，用于一次性搬迁）。

    旧版把状态写在 ``%APPDATA%\\VideoFramePlayer\\session.json``；这里仅用于
    **读取**，新版不会再往 C 盘写任何运行数据。
    """
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_NAME / SESSION_FILENAME


def migrate_legacy_session(
    new_path: Path | None = None, legacy: Path | None = None,
) -> bool:
    """把旧 APPDATA 存档一次性搬到项目目录；返回是否写入了新文件。

    规则：
      * 新位置已有存档 → 不覆盖，避免用旧快照盖掉真实进度；
      * 旧文件复制成功后改名为 ``session.json.migrated`` 留作备份，不删除；
      * 新旧路径相同或旧文件不存在 → 什么都不做；
      * 任何异常只记日志，绝不抛给调用方（观看记录不能影响启动）。

    只做字节级搬运，不解析 JSON —— 内容是否合法交给 ``SessionStore.load()``
    的容错逻辑判断。
    """
    dest = Path(new_path) if new_path is not None else default_session_path()
    src = Path(legacy) if legacy is not None else legacy_session_path()
    try:
        if src.resolve() == dest.resolve():
            return False
        if not src.is_file():
            return False
        wrote = False
        if dest.is_file():
            logger.info("观看记录已在新位置，跳过旧存档搬迁: %s", dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(src.read_bytes())   # 原子替换，避免半个 JSON
            os.replace(tmp, dest)
            wrote = True
            logger.info("已把旧观看记录搬迁到项目目录: %s -> %s", src, dest)
        # 归档旧文件：不再让它占据 session.json 这个名字，但保留数据可回溯
        backup = src.with_name(src.name + ".migrated")
        try:
            os.replace(src, backup)
            logger.info("旧存档已归档为 %s（未删除）", backup)
        except OSError as exc:
            logger.debug("旧存档归档失败（不影响使用）: %s", exc)
        return wrote
    except (OSError, ValueError) as exc:
        logger.warning("旧观看记录搬迁失败，按无记录处理: %s", exc)
        return False


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
        if path is None:
            # 使用默认路径时才做一次性搬迁；显式传路径（测试 / 特殊部署）
            # 不应去动用户真实的 APPDATA 存档。
            self.path = default_session_path()
            migrate_legacy_session(self.path)
        else:
            self.path = Path(path)
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
