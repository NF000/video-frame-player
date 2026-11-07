"""主窗口：简洁控件布局、快捷键、拖拽打开、播放列表、全屏、定时刷新。

交互约定：
    空格            播放 / 暂停
    方向右键        暂停态：单帧前进；播放态：快进 5 秒
    方向左键        暂停态：单帧后退；播放态：快退 5 秒
    F               切换全屏 / 退出全屏
    Esc             退出全屏
「播放态不触发帧步进」是原始需求的硬约束，所以播放态的方向键改为
**时间跳转**（不调用 next_frame），暂停态仍保持严格的单帧步进。

拖入 mp4 / mkv / avi 或整个文件夹即可打开；文件夹会**递归扫描所有层级的
子文件夹**，按「目录层级 + 文件名」自然排序，自动从第一个开始播，
播完自动切下一个。

所有可交互控件设为 NoFocus，避免方向键被 QSlider / QComboBox 吞掉，
保证窗口级快捷键稳定触发。

观看记录 / 断点续播（保持流畅度）
--------------------------------
* 播放状态每 5 秒节流投递给后台写线程，主线程不做磁盘 I/O；退出时再同步
  原子落盘一次。
* 启动恢复延后到「窗口已显示 + 引擎已就绪」之后；文件夹遍历放后台线程，
  结果通过 Qt 信号回主线程，首帧不读盘、不扫目录。
* seek 只在引擎 on_opened 回调之后做，且等待 VLC 解析出时长（有限次重试），
  不新增任何同步阻塞的 libvlc 调用。
* 用户一旦主动打开内容，就放弃尚未完成的启动恢复，不覆盖用户操作。
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from player.engine import EngineError, SUPPORTED_SUFFIXES, VlcEngine
from player.frames import FrameGrid
from player.playlist import Playlist, PlaylistError, natural_key, scan_folder
from player.session import SessionState, SessionStore
from .video_widget import VideoWidget

logger = logging.getLogger(__name__)

RATE_ITEMS = (("0.5x", 0.5), ("1x", 1.0), ("2x", 2.0), ("3x", 3.0))
PROGRESS_MAX = 1000   # 用千分比做进度条范围，避免依赖总时长整数上限
SEEK_STEP_MS = 5000   # 播放态方向键一次跳转 5 秒
PLAYLIST_WIDTH = 260  # 左侧播放列表固定宽度（需容纳子文件夹层级路径）

# ---- 观看记录相关 ----
SAVE_INTERVAL_MS = 5000        # 播放期间节流写盘间隔
SAVE_POSITION_EPSILON_MS = 1000  # 位置变化不足 1 秒且无其它变更时跳过写盘
RESTORE_DELAY_MS = 400         # 引擎就绪后再延迟恢复，避开启动首帧
SEEK_RETRY_MS = 200            # 等 VLC 解析出时长的重试间隔
MAX_SEEK_RETRIES = 8           # 最多等 1.6 秒，绝不无限等
RESTORE_TAIL_MS = 2000         # 断点太靠近片尾时从头播，避免恢复即自动续播

HINT = (
    "拖入视频或父文件夹（含所有子文件夹）  |  空格 播放/暂停  |  "
    "→/← 播放态 ±5s、暂停态单帧  |  F 全屏"
)


def format_time(ms: int) -> str:
    """毫秒 -> MM:SS 或 HH:MM:SS。"""
    seconds = max(0, int(ms)) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def nearest_rate_index(rate: float) -> int:
    """把存档里的倍速映射到下拉框最接近的一档。"""
    return min(range(len(RATE_ITEMS)), key=lambda i: abs(RATE_ITEMS[i][1] - rate))


class _EndBridge(QObject):
    """把 VLC 线程 / 引擎后台线程的事件转成 Qt 主线程信号。

    libvlc 回调不可重入，不能在其中操作 UI 或调用 libvlc；
    Signal.emit() 从非 GUI 线程发出时，Qt 会以 queued connection
    把槽函数排到 MainWindow 所在的主线程执行。引擎的打开完成 / 失败
    通知也走这里，保证 UI 永远在主线程更新。
    """

    reached = Signal()       # 播放自然结束（VLC 线程 -> 主线程）
    opened = Signal()        # 后台打开媒体完成（后台线程 -> 主线程）
    failed = Signal(str)     # 后台任务失败（后台线程 -> 主线程）


class _ScanBridge(QObject):
    """后台恢复扫描完成后，把 (token, state, items) 送回主线程。"""

    finished = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Windows 桌面视频播放器")
        self.resize(1100, 680)
        self.setMinimumSize(760, 460)
        self.setAcceptDrops(True)

        self._engine: VlcEngine | None = None
        self._grid: FrameGrid | None = None
        self._dragging = False
        self._last_volume = 80
        self._muted = False
        self._current_path: Path | None = None

        self._playlist = Playlist()
        self._expect_end = False   # True 表示当前视频允许「播完自动续播」
        self._advancing = True     # 防止 Ended 状态被连续 tick 重复消费
        self._end_bridge: _EndBridge | None = None
        self._highlighted_row = -1  # 当前加粗的列表行，用于切换时局部刷新

        # ---- 观看记录状态 ----
        self._session = SessionStore()
        self._session_folder = ""        # 仅文件夹模式记录，单文件/多文件拖拽为空
        self._last_position_ms = 0       # 由 _tick 缓存，退出时不额外查询 libvlc
        self._last_duration_ms = 0
        self._last_saved_pos = -1
        self._save_dirty = True
        self._restoring = False          # 恢复流程内部打开视频时不要触发「取消恢复」
        self._restore_cancelled = False
        self._restore_token = 0
        self._scan_bridge: _ScanBridge | None = None
        self._pending_seek_ms: int | None = None
        self._restore_was_playing = False
        self._restore_label = ""
        self._seek_attempts = 0

        self._build_ui()
        self._bind_shortcuts()

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._save_timer = QTimer(self)
        self._save_timer.setInterval(SAVE_INTERVAL_MS)
        self._save_timer.timeout.connect(self._save_tick)
        self._save_timer.start()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        # 左列：播放列表 + 上/下一个按钮（全屏时整体隐藏）
        self._list_panel = QWidget(central)
        list_col = QVBoxLayout(self._list_panel)
        list_col.setContentsMargins(0, 0, 0, 0)
        list_col.setSpacing(4)
        list_col.addWidget(QLabel("播放列表（含子文件夹 · 按路径顺序）"))
        self._list = QListWidget(self._list_panel)
        self._list.setFixedWidth(PLAYLIST_WIDTH)
        self._list.setUniformItemSizes(True)  # 大列表避免逐项计算 sizeHint
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.itemDoubleClicked.connect(self._on_playlist_double_clicked)
        list_col.addWidget(self._list, 1)

        nav = QHBoxLayout()
        self._prev_btn = QPushButton("上一个")
        self._prev_btn.clicked.connect(self._play_previous)
        self._next_btn = QPushButton("下一个")
        self._next_btn.clicked.connect(self._play_next)
        for button in (self._prev_btn, self._next_btn):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            nav.addWidget(button)
        list_col.addLayout(nav)
        outer.addWidget(self._list_panel)

        # 右列：画面 + 两行控制栏
        right = QVBoxLayout()
        right.setSpacing(6)
        self._video = VideoWidget(central)
        right.addWidget(self._video, 1)  # stretch=1：窗口缩放时画面自适应

        # 第一行：进度条 + 时间
        line1 = QHBoxLayout()
        self._progress = QSlider(Qt.Orientation.Horizontal)
        self._progress.setRange(0, PROGRESS_MAX)
        self._progress.setValue(0)
        self._progress.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._progress.sliderPressed.connect(self._on_slider_pressed)
        self._progress.sliderReleased.connect(self._on_slider_released)
        self._progress.valueChanged.connect(self._on_slider_value_changed)
        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setMinimumWidth(130)
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line1.addWidget(self._progress, 1)
        line1.addWidget(self._time_label)
        right.addLayout(line1)

        # 第二行：播放控制 + 帧号 + 倍速 + 音量
        line2 = QHBoxLayout()
        self._open_btn = QPushButton("打开视频…")
        self._open_btn.clicked.connect(self._on_open_clicked)
        self._folder_btn = QPushButton("打开文件夹…")
        self._folder_btn.clicked.connect(self._on_open_folder_clicked)
        self._play_btn = QPushButton("播放 / 暂停")
        self._play_btn.clicked.connect(self._on_toggle_play)
        self._stop_btn = QPushButton("停止")
        self._stop_btn.clicked.connect(self._on_stop)
        for button in (
            self._open_btn, self._folder_btn, self._play_btn, self._stop_btn
        ):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            line2.addWidget(button)

        self._frame_label = QLabel("帧 0  |  -- fps")
        self._frame_label.setMinimumWidth(160)
        line2.addWidget(self._frame_label)
        line2.addStretch(1)

        line2.addWidget(QLabel("倍速"))
        self._rate_box = QComboBox()
        for text, _rate in RATE_ITEMS:
            self._rate_box.addItem(text)
        self._rate_box.setCurrentIndex(1)  # 默认 1x
        self._rate_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._rate_box.currentIndexChanged.connect(self._on_rate_changed)
        line2.addWidget(self._rate_box)

        line2.addSpacing(12)
        line2.addWidget(QLabel("音量"))
        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(self._last_volume)
        self._volume.setFixedWidth(110)
        self._volume.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._volume.valueChanged.connect(self._on_volume_changed)
        line2.addWidget(self._volume)

        self._mute_btn = QPushButton("静音")
        self._mute_btn.setCheckable(True)
        self._mute_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._mute_btn.toggled.connect(self._on_mute_toggled)
        line2.addWidget(self._mute_btn)
        right.addLayout(line2)

        outer.addLayout(right, 1)
        self.statusBar().showMessage("正在初始化播放器…")

    def _bind_shortcuts(self) -> None:
        QShortcut(QKeySequence("Right"), self, self._on_step_forward)
        QShortcut(QKeySequence("Left"), self, self._on_step_backward)
        QShortcut(QKeySequence("F"), self, self._toggle_fullscreen)
        QShortcut(QKeySequence("Space"), self, self._on_toggle_play)
        QShortcut(QKeySequence("Escape"), self, self._exit_fullscreen)

    # ---------------- 引擎 ----------------
    def init_engine(self) -> None:
        """在窗口 show() 之后调用：此时视频 widget 的原生句柄才有效。"""
        try:
            self._engine = VlcEngine(self._video.win_id())
            self._grid = FrameGrid(self._engine)
            self._engine.set_volume(self._last_volume)

            # 引擎后台线程与 VLC 回调线程的三种通知，统一转成 Qt 主线程信号
            self._end_bridge = _EndBridge()
            bridge = self._end_bridge
            bridge.reached.connect(self._on_media_end)
            bridge.opened.connect(self._on_engine_opened)
            bridge.failed.connect(self._on_engine_failed)
            self._engine.set_callbacks(
                on_opened=bridge.opened.emit,
                on_error=bridge.failed.emit,
            )

            def _on_vlc_end(_event) -> None:
                # 只发信号，不碰 libvlc、不碰 UI（回调在 VLC 线程）
                bridge.reached.emit()

            # 双保险：libvlc 结束事件 + 定时轮询，都汇到 _try_auto_advance()
            self._engine.set_end_reached_callback(_on_vlc_end)
            logger.info("VLC 运行时就绪: %s", self._engine.lib_dir)
            self.statusBar().showMessage(HINT)

            # 恢复延后：首帧已经画出来、引擎就绪之后再读记录/扫目录
            QTimer.singleShot(RESTORE_DELAY_MS, self._restore_session)
        except EngineError as exc:
            self._engine = None
            self._grid = None
            logger.error("VLC 运行时错误: %s", exc)
            QMessageBox.critical(self, "VLC 运行时错误", str(exc))
            self.statusBar().showMessage("VLC 不可用，请检查 vendor/vlc/ 目录")
        except Exception as exc:
            self._engine = None
            self._grid = None
            logger.exception("引擎初始化异常")
            QMessageBox.critical(self, "初始化失败", f"未预期的错误: {exc}")

    # ---------------- 打开单个视频 ----------------
    def _on_open_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开本地视频", "", "视频文件 (*.mp4 *.mkv *.avi);;所有文件 (*.*)"
        )
        if not path:
            return
        self._cancel_restore()
        self._playlist.clear()       # 打开独立文件，清掉旧列表
        self._session_folder = ""
        self._refresh_playlist_ui()
        self.open_file(path)

    def open_file(self, path: str, reset_rate: bool = True) -> None:
        """打开视频（异步提交给引擎后台线程，界面立即返回、不卡顿）。

        reset_rate=False 用于播放列表内部切换与断点恢复，保留当前倍速。
        真正依赖新媒体的 fps 校准放到 _on_engine_opened() 里做。
        """
        if self._engine is None or self._grid is None:
            QMessageBox.critical(self, "无法打开", "VLC 运行时未就绪，请检查 vendor/vlc/ 目录。")
            return
        if not self._restoring:
            # 用户主动打开内容：放弃尚未完成的启动恢复
            self._cancel_restore()
        try:
            if reset_rate:
                self._rate_box.setCurrentIndex(1)
            rate = RATE_ITEMS[self._rate_box.currentIndex()][1]
            # 只提交任务：libvlc 结束上一路播放需约 5 秒，绝不能在主线程等
            self._engine.open(
                path, rate=rate, volume=self._last_volume, muted=self._muted
            )
            self._current_path = Path(path)
            self._last_position_ms = 0
            self._last_duration_ms = 0
            self._save_dirty = True
            self._progress.setValue(0)
            self._expect_end = True
            self._advancing = True
            self.setWindowTitle(f"{Path(path).name} - Windows 桌面视频播放器")
            self.statusBar().showMessage(f"正在加载 {Path(path).name} …  |  {HINT}")
        except EngineError as exc:
            logger.error("打开视频失败: %s", exc)
            QMessageBox.critical(self, "打开失败", str(exc))
        except Exception as exc:
            logger.exception("打开视频异常")
            QMessageBox.critical(self, "打开失败", f"未预期的错误: {exc}")

    # ---------------- 打开文件夹 / 播放列表 ----------------
    def _on_open_folder_clicked(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "打开视频文件夹（自动包含所有子文件夹）", ""
        )
        if folder:
            self._cancel_restore()
            self.open_folder(folder)

    def open_folder(self, folder: str, start: int = 0, recursive: bool = True) -> None:
        """递归扫描父文件夹及其所有子文件夹，按路径顺序载入并自动播放。

        排序规则：先按子文件夹路径自然排序分组，组内再按文件名自然排序，
        因此 ``season1/ep2`` 会排在 ``season1/ep10`` 前，``season2`` 整组排最后。
        隐藏目录 / 系统回收站 / 常见缓存目录会自动跳过；个别子目录无权限
        时只跳过该分支，不影响其余结果。
        """
        errors: list[str] = []
        try:
            items = scan_folder(folder, recursive=recursive, errors=errors)
        except PlaylistError as exc:
            logger.error("扫描文件夹失败: %s", exc)
            QMessageBox.warning(self, "打开文件夹失败", str(exc))
            return
        if not items:
            scope = "该文件夹及其子文件夹" if recursive else "该文件夹"
            QMessageBox.information(
                self, "没有视频", f"{scope}里没有 mp4 / mkv / avi：\n{folder}"
            )
            return

        self._playlist.load(items, start, root=folder)
        self._session_folder = folder if recursive else ""
        self._save_dirty = True
        self._refresh_playlist_ui()
        self._play_at(self._playlist.index)

        dir_count = len({p.parent for p in items})
        scope = "递归" if recursive else "当前层"
        skipped = f"  |  跳过 {len(errors)} 个不可读目录" if errors else ""
        self.statusBar().showMessage(
            f"已{scope}载入 {len(items)} 个视频 / {dir_count} 个目录"
            f"（按目录层级 + 文件名自然排序）: {folder}{skipped}",
            8000,
        )

    def load_files(self, files: list[Path], start: int = 0) -> None:
        """把一组拖入的视频按自然顺序组成列表并播放（用于多文件拖拽）。

        跨目录的多文件拖拽会以共同父目录为根，按相对路径排序并显示层级。
        """
        if not files:
            return
        root: Path | None = None
        try:
            common = Path(os.path.commonpath([str(p) for p in files]))
            root = common if common.is_dir() else common.parent
        except (ValueError, OSError):
            root = None  # 跨盘符等情况，退化为按文件名排序
        ordered = sorted(files, key=lambda p: natural_key(p, root))
        self._playlist.load(ordered, start, root=root)
        self._session_folder = ""   # 一组散文件不记录文件夹，避免恢复时拉进整棵树
        self._save_dirty = True
        self._refresh_playlist_ui()
        self._play_at(self._playlist.index)

    def _play_at(self, index: int) -> None:
        """播放列表中指定序号，并同步列表高亮与窗口标题。"""
        path = self._playlist.set_index(index)
        if path is None:
            return
        self._expect_end = True
        self._advancing = True   # 等 state 离开 Ended 后才允许下一次自动续播
        self.open_file(str(path), reset_rate=False)
        self._highlight_playlist()   # 只更新高亮，避免大列表切换时整表重建
        if self._playlist.size:
            # 多层子文件夹下可能有重名文件，标题显示相对层级路径便于分辨
            display = self._playlist.rel_name(path)
            self.setWindowTitle(
                f"[{self._playlist.index + 1}/{self._playlist.size}] {display}"
                " - Windows 桌面视频播放器"
            )

    def _play_next(self, auto: bool = False) -> None:
        """切下一个；auto=True 表示由「播完自动续播」触发。"""
        if not self._playlist.has_next():
            if auto:
                self._expect_end = False
                self.statusBar().showMessage("播放列表已播完", 4000)
            return
        self._playlist.next()
        self._play_at(self._playlist.index)
        if not auto:
            self.statusBar().showMessage("下一个视频", 1500)

    def _play_previous(self) -> None:
        if not self._playlist.has_previous():
            self.statusBar().showMessage("已经是第一个", 1500)
            return
        self._playlist.previous()
        self._play_at(self._playlist.index)
        self.statusBar().showMessage("上一个视频", 1500)

    def _on_playlist_double_clicked(self, item) -> None:
        self._cancel_restore()
        self._play_at(self._list.row(item))

    def _refresh_playlist_ui(self) -> None:
        """重建列表显示并高亮当前项（纯 UI，无 VLC 调用）。

        列表项显示相对根文件夹的层级路径，tooltip 显示完整绝对路径，
        避免不同子文件夹下的同名视频混淆。
        """
        items = self._playlist.items
        # 批量插入 + 暂停绘制/信号：逐条 addItem 在数百项时会反复触发布局计算
        self._list.setUpdatesEnabled(False)
        blocked = self._list.blockSignals(True)
        try:
            self._list.clear()
            self._list.addItems(
                [f"{i + 1}. {self._playlist.rel_name(p)}"
                 for i, p in enumerate(items)]
            )
            for i, path in enumerate(items):
                item = self._list.item(i)
                if item is not None:
                    item.setToolTip(str(path))
        finally:
            self._list.blockSignals(blocked)
            self._list.setUpdatesEnabled(True)
        self._highlighted_row = -1
        self._highlight_playlist()

    def _highlight_playlist(self) -> None:
        """只更新当前项高亮：递归目录里视频很多时，切集不必重建整张列表。"""
        previous = self._highlighted_row
        if 0 <= previous < self._list.count():
            old_item = self._list.item(previous)
            if old_item is not None:
                old_font = old_item.font()
                old_font.setBold(False)
                old_item.setFont(old_font)

        row = self._playlist.index
        if 0 <= row < self._list.count():
            self._list.setCurrentRow(row)
            current = self._list.currentItem()
            if current is not None:
                font = current.font()
                font.setBold(True)
                current.setFont(font)
        self._highlighted_row = row

    # ---------------- 拖拽 ----------------
    def _has_video_url(self, event) -> bool:
        data = event.mimeData()
        if data is None or not data.hasUrls():
            return False
        for url in data.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_dir() or path.suffix.lower() in SUPPORTED_SUFFIXES:
                return True
        return False

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self._has_video_url(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        data = event.mimeData()
        if data is None or not data.hasUrls():
            event.ignore()
            return
        self._cancel_restore()
        local_paths = [Path(u.toLocalFile()) for u in data.urls() if u.isLocalFile()]

        # 拖入文件夹：直接按文件夹模式载入
        for path in local_paths:
            if path.is_dir():
                self.open_folder(str(path))
                event.acceptProposedAction()
                return

        videos = [
            p for p in local_paths
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        ]
        if not videos:
            event.ignore()
            return

        if len(videos) == 1:
            # 拖单个文件：只把同层目录的视频按序组成列表，避免误把整棵目录树拉进来。
            # 要递归整个父文件夹，直接把文件夹拖进窗口或用「打开文件夹…」。
            target = videos[0]
            try:
                items = scan_folder(target.parent, recursive=False)
            except PlaylistError:
                items = [target]
            start = next(
                (
                    i for i, item in enumerate(items)
                    if item.resolve() == target.resolve()
                ),
                0,
            )
            self._playlist.load(items, start, root=target.parent)
            self._session_folder = ""
            self._refresh_playlist_ui()
            self._play_at(start)
            self.statusBar().showMessage(
                f"已载入同目录 {len(items)} 个视频；"
                "拖入父文件夹（或用「打开文件夹…」）可递归载入所有子文件夹",
                6000,
            )
        else:
            self.load_files(videos)
        event.acceptProposedAction()

    # ---------------- 播放控制 ----------------
    def _on_toggle_play(self) -> None:
        # 后台正在换片时忽略播放控制，避免与 worker 线程同时写 libvlc
        if self._engine is None or self._engine.is_busy():
            return
        try:
            self._engine.toggle_pause()
            self._save_dirty = True
        except EngineError as exc:
            self._warn(exc)

    def _on_stop(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.stop()
            self._expect_end = False   # 手动停止不触发自动续播
            self._advancing = True
            self._last_position_ms = 0
            self._save_dirty = True
            self._progress.setValue(0)
            self._time_label.setText("00:00 / 00:00")
            if self._grid is not None:
                self._grid.sync_from_time()
        except Exception as exc:
            self._warn(exc)

    def _on_rate_changed(self, index: int) -> None:
        if self._engine is None or not (0 <= index < len(RATE_ITEMS)):
            return
        try:
            self._engine.set_rate(RATE_ITEMS[index][1])
            self._save_dirty = True
        except EngineError as exc:
            self._warn(exc)

    def _on_volume_changed(self, value: int) -> None:
        self._last_volume = value
        self._save_dirty = True
        if self._engine is None:
            return
        try:
            self._engine.set_volume(value)
        except EngineError as exc:
            self._warn(exc)

    def _on_mute_toggled(self, checked: bool) -> None:
        self._muted = checked
        self._mute_btn.setText("取消静音" if checked else "静音")
        self._save_dirty = True
        if self._engine is None:
            return
        try:
            self._engine.set_mute(checked)
        except EngineError as exc:
            self._warn(exc)

    # ---------------- 方向键：播放态跳秒 / 暂停态步进 ----------------
    def _on_step_forward(self) -> None:
        self._on_seek_or_step(1)

    def _on_step_backward(self) -> None:
        self._on_seek_or_step(-1)

    def _on_seek_or_step(self, direction: int) -> None:
        """direction=+1 前进 / -1 后退。

        暂停态：调用 FrameGrid 做严格的单帧步进（原始需求）。
        播放态：按 SEEK_STEP_MS 做时间跳转，不调用 next_frame()，
                符合「播放态不触发帧步进」的约束，同时解决快进问题。
        """
        if self._grid is None or self._engine is None or self._engine.is_busy():
            return
        try:
            if self._engine.is_paused():
                ok = (
                    self._grid.step_forward() if direction > 0
                    else self._grid.step_backward()
                )
                if ok:
                    self._save_dirty = True
                    self._refresh_frame_info()
                    self.statusBar().showMessage(
                        "单帧前进" if direction > 0 else "单帧后退", 1200
                    )
                elif direction < 0:
                    self.statusBar().showMessage("已到第一帧，无法继续后退", 1500)
                return

            if not self._engine.is_playing():
                self.statusBar().showMessage("视频未在播放，先打开视频", 1800)
                return

            self._engine.seek_by(SEEK_STEP_MS * direction)
            self._grid.sync_from_time()   # 跳转后重建帧序号，避免暂停步进错位
            self._save_dirty = True
            self._refresh_frame_info()
            self.statusBar().showMessage(
                f"{'快进' if direction > 0 else '快退'} "
                f"{SEEK_STEP_MS // 1000} 秒", 1500
            )
        except Exception as exc:
            self._warn(exc)

    # ---------------- 进度条 ----------------
    def _on_slider_pressed(self) -> None:
        self._dragging = True

    def _on_slider_released(self) -> None:
        self._dragging = False
        if self._engine is None or self._engine.is_busy():
            return
        try:
            length = self._engine.get_length_ms()
            if length > 0:
                target = int(self._progress.value() / PROGRESS_MAX * length)
                self._engine.set_time_ms(target)
                self._save_dirty = True
                if self._grid is not None:
                    self._grid.sync_from_time()
        except Exception as exc:
            self._warn(exc)

    def _on_slider_value_changed(self, value: int) -> None:
        """拖拽过程中预览目标时间，不真正 seek。"""
        if not self._dragging or self._engine is None:
            return
        length = self._engine.get_length_ms()
        if length > 0:
            preview = int(value / PROGRESS_MAX * length)
            self._time_label.setText(
                f"{format_time(preview)} / {format_time(length)}"
            )

    # ---------------- 全屏 ----------------
    def _toggle_fullscreen(self) -> None:
        going_full = not self.isFullScreen()
        if going_full:
            self.showFullScreen()
        else:
            self.showNormal()
        self._list_panel.setVisible(not going_full)

    def _exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._list_panel.setVisible(True)

    # ---------------- 观看记录：保存 ----------------
    def _snapshot(self) -> SessionState:
        """构造当前会话快照；saved_at 由 SessionStore 填写。"""
        return SessionState(
            folder=self._session_folder,
            current_path=str(self._current_path) if self._current_path else "",
            index=max(0, self._playlist.index),
            position_ms=max(0, int(self._last_position_ms)),
            duration_ms=max(0, int(self._last_duration_ms)),
            rate=RATE_ITEMS[self._rate_box.currentIndex()][1],
            volume=self._last_volume,
            muted=self._muted,
            was_playing=bool(
                self._engine is not None and self._engine.is_playing()
            ),
        )

    def _save_tick(self) -> None:
        """5 秒节流写盘：位置没明显变化且无其它改动就跳过。"""
        if self._engine is None or self._current_path is None:
            return
        pos = max(0, int(self._last_position_ms))
        if (not self._save_dirty
                and abs(pos - self._last_saved_pos) < SAVE_POSITION_EPSILON_MS):
            return
        try:
            self._session.save(self._snapshot())   # 只投递队列，不碰磁盘
            self._last_saved_pos = pos
            self._save_dirty = False
        except Exception as exc:
            logger.warning("保存观看记录失败: %s", exc)

    def _cancel_restore(self) -> None:
        """用户主动打开内容后，放弃尚未完成的启动恢复。"""
        self._restore_cancelled = True
        self._pending_seek_ms = None

    # ---------------- 观看记录：恢复 ----------------
    def _restore_session(self) -> None:
        """引擎就绪、界面稳定后调用：读记录 → 后台重建列表 → 打开断点。"""
        if self._engine is None or self._restore_cancelled:
            return
        state = self._session.load()
        if state is None or not state.auto_resume or not state.current_path:
            return
        path = Path(state.current_path)
        if not path.is_file():
            self.statusBar().showMessage("上次的视频已不存在，已跳过恢复", 6000)
            return

        self._apply_preferences(state)
        self._restore_token += 1
        token = self._restore_token

        if state.folder and Path(state.folder).is_dir():
            # 目录遍历放后台线程：冷盘 / 网络盘 / 数百个视频都不冻结界面
            bridge = _ScanBridge()
            self._scan_bridge = bridge    # 持有引用，防止 QObject 被 GC
            bridge.finished.connect(self._on_restore_scan_done)
            self.statusBar().showMessage("正在恢复上次的播放列表…", 4000)
            threading.Thread(
                target=self._scan_restore_folder,
                args=(state, token, bridge),
                name="session-restore-scan",
                daemon=True,
            ).start()
        else:
            self._on_restore_scan_done((token, state, None))

    def _apply_preferences(self, state: SessionState) -> None:
        """恢复倍速 / 音量 / 静音；控件信号会同步给引擎，open() 也会带上。"""
        self._rate_box.setCurrentIndex(nearest_rate_index(state.rate))
        self._volume.setValue(state.volume)
        self._mute_btn.setChecked(state.muted)

    def _scan_restore_folder(
        self, state: SessionState, token: int, bridge: _ScanBridge
    ) -> None:
        """后台线程：只做目录遍历，结果用 Qt 信号送回主线程。"""
        try:
            items = scan_folder(state.folder, recursive=True)
        except PlaylistError as exc:
            logger.warning("恢复播放列表扫描失败: %s", exc)
            items = []
        bridge.finished.emit((token, state, items))

    def _on_restore_scan_done(self, payload) -> None:
        """主线程：拿到扫描结果后一次性填充列表并打开断点。"""
        token, state, items = payload
        if (token != self._restore_token or self._restore_cancelled
                or self._engine is None):
            return
        # 文件夹还在但视频都没了：退化为单文件恢复
        if items is not None and not items:
            items = None
        self._begin_restore(state, items)

    def _begin_restore(self, state: SessionState, items: list[Path] | None) -> None:
        path = Path(state.current_path)
        self._restoring = True
        try:
            if items:
                # 优先按绝对路径定位当前集；找不到就用存档序号兜底
                target = os.path.normcase(os.path.abspath(state.current_path))
                index = state.index if 0 <= state.index < len(items) else 0
                for i, item in enumerate(items):
                    if os.path.normcase(os.path.abspath(str(item))) == target:
                        index = i
                        break
                self._playlist.load(items, index, root=state.folder)
                self._session_folder = state.folder
                self._refresh_playlist_ui()
                self._play_at(index)
            else:
                self._session_folder = ""
                self.open_file(str(path), reset_rate=False)
            # open_file 是异步的：on_opened 信号一定在本方法返回之后才到主线程
            self._pending_seek_ms = state.position_ms
            self._restore_was_playing = state.was_playing
            self._restore_label = f"{path.name} @ {format_time(state.position_ms)}"
            self.statusBar().showMessage(
                f"正在恢复上次观看：{self._restore_label} …", 4000
            )
        finally:
            self._restoring = False
        self._save_dirty = True

    def _schedule_seek_retry(self) -> None:
        self._seek_attempts += 1
        QTimer.singleShot(SEEK_RETRY_MS, self._apply_pending_seek)

    def _apply_pending_seek(self) -> None:
        """媒体打开完成后的断点 seek：等 VLC 解析出时长，有限次重试。"""
        if self._pending_seek_ms is None or self._engine is None:
            return
        busy = self._engine.is_busy()
        length = 0 if busy else self._engine.get_length_ms()
        if busy or (length <= 0 and self._seek_attempts < MAX_SEEK_RETRIES):
            self._schedule_seek_retry()   # 不清除 pending，下一拍继续尝试
            return
        try:
            pos = int(self._pending_seek_ms)
            # 已看到片尾就从头播：避免一恢复就触发「播完自动续播」
            if pos > 0 and (length <= 0 or pos < length - RESTORE_TAIL_MS):
                self._engine.set_time_ms(pos)
            if self._grid is not None:
                self._grid.sync_from_time()
            if not self._restore_was_playing:
                self._engine.pause()
        except Exception as exc:
            logger.warning("恢复播放位置失败: %s", exc)
        finally:
            self._pending_seek_ms = None
            self._refresh_frame_info()
            tip = "（空格继续播放）" if not self._restore_was_playing else "（继续播放）"
            self.statusBar().showMessage(
                f"已恢复上次观看：{self._restore_label}{tip}", 8000
            )

    # ---------------- 引擎后台线程通知（已转到 Qt 主线程）----------------
    def _on_engine_opened(self) -> None:
        """新视频在后台加载完成，此时才做依赖新媒体的 fps / 帧序号校准。"""
        if self._grid is not None:
            self._grid.reset()
        self._advancing = True
        if self._pending_seek_ms is not None:
            # VLC 刚 play() 时可能还没解析出时长，延迟一拍再 seek（不阻塞）
            self._seek_attempts = 0
            QTimer.singleShot(SEEK_RETRY_MS, self._apply_pending_seek)
            return
        self._refresh_frame_info()
        if self._current_path is not None:
            self.statusBar().showMessage(
                f"已打开 {self._current_path.name}  |  {HINT}", 3000
            )

    def _on_engine_failed(self, message: str) -> None:
        """后台任务失败：只提示状态栏，不弹模态框打断用户操作。"""
        logger.error("播放引擎后台任务失败: %s", message)
        self._pending_seek_ms = None
        self.statusBar().showMessage(f"操作失败: {message}", 8000)

    # ---------------- 定时刷新 ----------------
    def _try_auto_advance(self) -> None:
        """播完自动切下一个。

        200ms 轮询与 libvlc 结束事件都汇聚到这里：Ended 状态只消费一次，
        等新视频 state 离开 Ended 后才解锁，避免连续 tick 连跳多个视频。
        手动「停止」会把 _expect_end 置 False，因此不会误续播。
        后台正在切换媒体时直接跳过，不与 worker 线程争用 libvlc。
        """
        if self._engine is None or self._engine.is_busy():
            return
        if not self._engine.is_end_reached():
            self._advancing = False
            return
        if self._expect_end and not self._advancing:
            self._advancing = True
            self._play_next(auto=True)

    def _on_media_end(self) -> None:
        """libvlc 结束事件（已由 Qt 转到主线程），与轮询共用同一判定。"""
        try:
            self._try_auto_advance()
        except Exception as exc:
            logger.warning("处理播放结束事件异常: %s", exc)

    def _tick(self) -> None:
        if self._engine is None or self._engine.is_busy():
            # 后台正在打开/停止媒体：跳过本轮全部 libvlc 查询，避免竞态
            return
        try:
            self._try_auto_advance()   # 播完自动切下一个（轮询兜底）

            length = self._engine.get_length_ms()
            current = self._engine.get_time_ms()
            # 缓存给存档用：退出时不必再为「最后 200ms」额外查询 libvlc
            self._last_position_ms = current
            self._last_duration_ms = length
            if not self._dragging:
                if length > 0:
                    self._progress.setValue(int(current / length * PROGRESS_MAX))
                self._time_label.setText(
                    f"{format_time(current)} / {format_time(length)}"
                )
            # 刚打开视频时 VLC 可能还拿不到 fps，持续重试直到读到真实值
            if self._grid is not None and self._grid.is_assumed_fps:
                self._grid.refresh()
            self._refresh_frame_info()
        except Exception as exc:
            logger.debug("刷新状态失败: %s", exc)

    def _refresh_frame_info(self) -> None:
        if self._grid is None:
            return
        flag = " (估)" if self._grid.is_assumed_fps else ""
        self._frame_label.setText(
            f"帧 {self._grid.index}  |  {self._grid.fps:.3f} fps{flag}"
        )

    # ---------------- 通用 ----------------
    def _warn(self, exc: Exception) -> None:
        logger.warning("操作失败: %s", exc)
        self.statusBar().showMessage(f"操作失败: {exc}", 3000)

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._timer.stop()
            self._save_timer.stop()
            if self._engine is not None and not self._engine.is_busy():
                try:
                    self._last_position_ms = self._engine.get_time_ms()
                    self._last_duration_ms = self._engine.get_length_ms()
                except Exception:
                    pass   # 用 _tick 缓存值兜底即可
            if self._current_path is not None:
                # 同步原子写最终快照（<1ms），不等后台队列，关窗不拖慢
                self._session.flush(self._snapshot())
        except Exception as exc:
            logger.warning("保存观看记录失败: %s", exc)
        try:
            if self._engine is not None:
                # release 走引擎后台线程，不会在关窗时卡住 5 秒
                self._engine.release()
        except Exception as exc:
            logger.warning("退出清理异常: %s", exc)
        super().closeEvent(event)
