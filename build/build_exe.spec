# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir 模式）。

坑点速记（完整说明见交付说明 ④）：
  1. 必须 64 位 Python + 64 位 libvlc.dll，位数不一致会 WinError 193
  2. plugins/ 必须整体作为目录带上，libvlc 靠它加载解码器
  3. 强烈建议 onedir：onefile 解压到临时目录会破坏 plugins 的相对查找路径
  4. console=False 时看不到异常，调试期可临时改成 True
"""
import os
from pathlib import Path

ROOT = Path(SPECPATH).parent.resolve()   # spec 位于 <项目根>/build/
# 打包时复制哪份 VLC 运行时：环境变量 VLC_LIB_DIR 优先，否则用项目内置 vendor/vlc
# （与 player/vlc_runtime.py 的查找规则保持一致）
VLC = Path(os.environ.get("VLC_LIB_DIR") or str(ROOT / "vendor" / "vlc"))

# VLC 运行时：两个 DLL 放根目录，plugins 整体作为目录
# 运行时 vlc_runtime.locate() 会在 sys._MEIPASS / exe 同级找到它们
datas = [
    (str(VLC / "libvlc.dll"), "."),
    (str(VLC / "libvlccore.dll"), "."),
    (str(VLC / "plugins"), "plugins"),
]
binaries = []
hiddenimports = ["vlc"]

# conda 环境的 Python 把部分 C 扩展依赖放在 Library\bin（不在 PATH 里），
# PyInstaller 分析时解析不到，需显式带上；否则打包后 _ctypes / _lzma / _bz2
# 会加载失败（python-vlc 走 ctypes，ffi-8.dll 尤其关键）。
import sys as _sys

_env_bin = Path(_sys.executable).resolve().parent / "Library" / "bin"
for _name in ("ffi-8.dll", "libbz2.dll", "liblzma.dll"):
    _dll = _env_bin / _name
    if _dll.is_file():
        binaries.append((str(_dll), "."))

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 本项目只用 QtWidgets；排除未用的大模块可显著减小体积与打包时间
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia", "PySide6.Qt3DCore",
        "tkinter", "matplotlib", "numpy", "PIL",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoFramePlayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX 压缩 Qt / VLC 的 DLL 容易损坏，保持 False
    console=False,   # GUI 程序；调试期改 True 可看到 VLC 报错
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="VideoFramePlayer",
)
