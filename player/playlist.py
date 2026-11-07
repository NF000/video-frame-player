"""播放列表：递归扫描文件夹（含多层子文件夹）里的本地视频并维护当前项。

排序说明：
    普通字典序会把 ``ep10.mp4`` 排在 ``ep2.mp4`` 前面，逐集/逐段分析时很别扭。
    这里用数字感知的自然排序（natural sort），让 ``ep2 < ep10``，忽略大小写。

    多层目录时按「相对路径逐段」比较，即先按父目录顺序分组、组内再按文件名
    自然排序，这样 ``season1/ep1``、``season1/ep2``、``season1/ep10``、
    ``season1/extras/x``、``season2/ep1`` 会按直觉排列，不会因同名文件
    （多个文件夹里都有 ep1.mp4）而交错。

只处理本地文件，不做任何网络访问；目录不可读时抛出 PlaylistError。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .engine import SUPPORTED_SUFFIXES

logger = logging.getLogger(__name__)

# 把 "ep12 part3.mp4" 拆成 ["ep", "12", " part", "3", ".mp4"]
_NUM_RE = re.compile(r"(\d+)")
# 路径段之间的分隔标记：-1 小于数字段(0)和文本段(1)，保证段边界不被抹平
_SEG_SEP = (-1, "")

# 递归扫描时跳过的目录：隐藏目录、系统回收站、常见缓存/依赖目录
_SKIP_DIR_NAMES = frozenset({
    "$recycle.bin",
    "system volume information",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
})


class PlaylistError(RuntimeError):
    """文件夹扫描 / 列表操作的可预期错误。"""


def _skip_dir(name: str) -> bool:
    """判断是否跳过该子目录（隐藏目录与系统/缓存目录）。"""
    lowered = name.casefold()
    return name.startswith(".") or lowered in _SKIP_DIR_NAMES


def _segment_key(text: str) -> list[tuple[int, object]]:
    """单段文本的自然排序键片段。"""
    result: list[tuple[int, object]] = []
    for part in _NUM_RE.split(text.casefold()):
        if not part:
            continue
        result.append((0, int(part)) if part.isdigit() else (1, part))
    return result


def natural_key(path: Path, root: Path | None = None) -> tuple:
    """自然排序键：数字按数值比较，其余按小写文本比较。

    Args:
        path: 待比较的文件路径。
        root: 提供时按「相对 root 的每一级路径」逐段比较（目录优先分组）；
              不提供时退化为只比较文件名，兼容单文件/多文件拖拽排序。

    每一项统一写成 ``(类型标记, 值)``，避免 int 与 str 直接比较触发 TypeError。
    ``0`` 表示数字段，``1`` 表示文本段，``-1`` 表示路径段分隔符。
    """
    if root is not None:
        try:
            rel = Path(path).relative_to(Path(root))
        except (ValueError, TypeError):
            rel = Path(path.name)
    else:
        rel = Path(path.name)

    key: list[tuple[int, object]] = []
    for i, segment in enumerate(rel.parts):
        if i:
            key.append(_SEG_SEP)
        key.extend(_segment_key(segment))
    return tuple(key)


def scan_folder(
    folder: str | Path,
    suffixes: frozenset[str] = SUPPORTED_SUFFIXES,
    recursive: bool = True,
    errors: list[str] | None = None,
) -> list[Path]:
    """扫描文件夹内的视频文件，返回按相对路径自然排序后的列表。

    Args:
        folder: 目标文件夹（父文件夹）。
        suffixes: 允许的扩展名（小写，含点），默认 mp4 / mkv / avi。
        recursive: 是否递归所有层级的子文件夹，**默认 True**。
        errors: 可选，收集遍历中跳过的不可读目录信息，供 UI 提示。

    Raises:
        PlaylistError: 目录不存在、不是目录或根目录读取失败。

    使用 os.walk 而非 rglob：可以在无权限/坏链接的子目录上只跳过该分支
    （onerror），不会让整次扫描失败；followlinks=False 避免符号链接成环。
    """
    root = Path(folder)
    if not root.is_dir():
        raise PlaylistError(f"不是有效文件夹: {root}")

    # 先验证根目录确实可读，否则 os.walk 会静默返回空列表造成误判
    try:
        with os.scandir(root) as entries:
            next(entries, None)
    except OSError as exc:
        raise PlaylistError(f"文件夹不可读: {exc}") from exc

    found: list[Path] = []

    def _on_walk_error(exc: OSError) -> None:
        logger.warning("跳过不可读目录: %s", exc)
        if errors is not None:
            errors.append(str(exc))

    if recursive:
        for dirpath, dirnames, filenames in os.walk(
            root, onerror=_on_walk_error, followlinks=False
        ):
            # 就地裁剪：跳过隐藏目录/系统目录，不再向下遍历
            dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.lower() in suffixes:
                    found.append(path)
    else:
        try:
            for entry in root.iterdir():
                if entry.is_file() and entry.suffix.lower() in suffixes:
                    found.append(entry)
        except OSError as exc:
            raise PlaylistError(f"扫描文件夹失败: {exc}") from exc

    found.sort(key=lambda p: natural_key(p, root))
    return found


class Playlist:
    """有序播放列表，负责当前序号与前后切换。不碰 VLC，纯数据。"""

    def __init__(self) -> None:
        self._items: list[Path] = []
        self._index: int = -1
        self._root: Path | None = None

    # ---------------- 读取 ----------------
    @property
    def items(self) -> list[Path]:
        return list(self._items)  # 返回副本，防止外部误改内部状态

    @property
    def index(self) -> int:
        return self._index

    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def root(self) -> Path | None:
        """列表的根文件夹，用于显示相对层级路径。"""
        return self._root

    def rel_name(self, path: Path) -> str:
        """返回相对根目录的层级路径；无根或非同一树时退化为文件名。"""
        if self._root is not None:
            try:
                # 统一用正斜杠显示，Windows 上也保持 "season1/ep2.mkv" 的层级观感
                return Path(path).relative_to(self._root).as_posix()
            except (ValueError, TypeError):
                pass
        return Path(path).name

    def is_empty(self) -> bool:
        return not self._items

    def current(self) -> Path | None:
        if 0 <= self._index < len(self._items):
            return self._items[self._index]
        return None

    def has_next(self) -> bool:
        return 0 <= self._index < len(self._items) - 1

    def has_previous(self) -> bool:
        return self._index > 0

    # ---------------- 写入 ----------------
    def load(self, items: list[Path], start: int = 0, root: str | Path | None = None) -> None:
        """载入列表并把当前项设为 start；越界时归零。

        root 传入后，列表 UI 与窗口标题可以显示「子文件夹/文件名」层级。
        """
        self._items = list(items)
        self._root = Path(root) if root is not None else None
        if not self._items:
            self._index = -1
        else:
            self._index = start if 0 <= start < len(self._items) else 0

    def set_index(self, index: int) -> Path | None:
        """跳转到指定序号，成功返回该文件，越界返回 None（不改变状态）。"""
        if 0 <= index < len(self._items):
            self._index = index
            return self._items[index]
        return None

    def next(self) -> Path | None:
        return self.set_index(self._index + 1)

    def previous(self) -> Path | None:
        return self.set_index(self._index - 1)

    def clear(self) -> None:
        self._items = []
        self._index = -1
        self._root = None
