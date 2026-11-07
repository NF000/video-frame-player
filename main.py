"""Windows 桌面视频逐帧播放器 - 程序入口。

运行：
    conda activate video-player
    python main.py

VLC 运行时由 player/vlc_runtime.py 在引擎初始化时自动定位，
优先级：项目 vendor/vlc/ > 环境变量 VLC_LIB_DIR > 系统安装的 VLC。
"""
from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Windows 桌面视频播放器")
        window = MainWindow()
        window.show()
        # 先让 Qt 把窗口真正绘制出来：VLC 初始化（加载 360 个插件）期间
        # 至少要让用户看到界面，而不是一片空白的"未响应"窗口
        app.processEvents()
        # 必须在 show() 之后初始化引擎：此时视频 widget 的原生窗口句柄才有效
        window.init_engine()
        return app.exec()
    except Exception as exc:
        logging.exception("程序启动失败")
        print(f"程序启动失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
