"""播放器核心层：VLC 运行时定位、播放引擎、帧网格。

注意：本包在 import 时不会加载 libvlc.dll，真正加载发生在
``VlcEngine.__init__`` 里（先准备运行时目录，再 import vlc）。
"""
__all__ = ["vlc_runtime", "engine", "frames"]
