"""VLC 运行时定位与加载。

python-vlc 只是 ctypes 包装，包里**不含** libvlc.dll。必须在 ``import vlc``
之前把 DLL 目录和 plugins 目录准备好，否则会抛 FileNotFoundError。

查找优先级：
    1. 环境变量 ``VLC_LIB_DIR``（显式指定）
    2. 项目内置 ``vendor/vlc``（源码运行与 PyInstaller 打包都走这里）
    3. 系统安装的 VLC（Program Files\\VideoLAN\\VLC）

兼容两种运行形态：
    * 源码运行：项目根 = 本文件上两级
    * PyInstaller：onefile 资源在 sys._MEIPASS；onedir 资源在 exe 同级
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REQUIRED_FILES = ("libvlc.dll", "libvlccore.dll")

# os.add_dll_directory 返回的句柄必须保持引用，否则 DLL 搜索路径可能被系统回收
_dll_handles: list[object] = []


class VLCRuntimeNotFound(RuntimeError):
    """找不到可用的 VLC 运行时。"""


def _candidate_roots() -> list[Path]:
    """按优先级列出可能存放 libvlc.dll 的目录。"""
    roots: list[Path] = []

    env_dir = os.environ.get("VLC_LIB_DIR")
    if env_dir:
        roots.append(Path(env_dir))

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass) / "vendor" / "vlc")
            roots.append(Path(meipass))
        exe_dir = Path(sys.executable).resolve().parent
        roots.append(exe_dir / "vendor" / "vlc")
        roots.append(exe_dir)

    project_root = Path(__file__).resolve().parents[1]
    bundled = project_root / "vendor" / "vlc"
    roots.append(bundled)
    # 便携版 zip 解压后可能多一层目录，例如 vendor/vlc/vlc-3.0.24/
    try:
        for child in bundled.iterdir():
            if child.is_dir():
                roots.append(child)
    except OSError:
        pass

    for var, fallback in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        roots.append(Path(os.environ.get(var, fallback)) / "VideoLAN" / "VLC")

    seen: set[str] = set()
    unique: list[Path] = []
    for path in roots:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def locate() -> Path:
    """返回同时包含 libvlc.dll 与 libvlccore.dll 的目录。"""
    for directory in _candidate_roots():
        try:
            if all((directory / name).is_file() for name in REQUIRED_FILES):
                return directory
        except OSError:
            continue
    raise VLCRuntimeNotFound(
        "找不到 VLC 运行时（libvlc.dll / libvlccore.dll）。\n\n"
        "请任选一种方式：\n"
        "  1) 把 VLC 官方 Windows 64 位便携版中的 libvlc.dll、libvlccore.dll\n"
        "     和 plugins 目录放到项目 vendor/vlc/ 下；\n"
        "  2) 设置环境变量 VLC_LIB_DIR 指向该目录；\n"
        "  3) 安装 64 位 VLC 桌面版。\n\n"
        "注意：VLC 与 Python 必须同为 64 位。"
    )


def prepare() -> tuple[Path, Path]:
    """准备运行时环境，返回 (dll 目录, plugins 目录)。

    **必须在 import vlc 之前调用**。python-vlc 各版本读取的环境变量名
    不完全一致，这里多路设置以提高兼容性。
    """
    lib_dir = locate()
    plugins_dir = lib_dir / "plugins"
    if not plugins_dir.is_dir():
        raise VLCRuntimeNotFound(f"VLC 插件目录不存在: {plugins_dir}")

    if hasattr(os, "add_dll_directory"):
        try:
            _dll_handles.append(os.add_dll_directory(str(lib_dir)))
        except OSError as exc:
            logger.warning("add_dll_directory 失败（通常可忽略）: %s", exc)

    lib_file = lib_dir / "libvlc.dll"

    # 先自行尝试加载：python-vlc 在 PYTHON_VLC_LIB_PATH 无效时会直接
    # sys.exit(1) 杀掉进程，这里提前拦截，换成可读的异常。
    import ctypes

    try:
        ctypes.CDLL(str(lib_file))
    except OSError as exc:
        raise VLCRuntimeNotFound(
            f"libvlc.dll 加载失败: {exc}\n"
            "请确认 VLC 与 Python 同为 64 位，且 libvlccore.dll 在同一目录。"
        ) from exc

    os.environ["PATH"] = str(lib_dir) + os.pathsep + os.environ.get("PATH", "")
    # python-vlc 3.0.x：LIB_PATH 必须是 DLL 的**完整文件路径**，
    # MODULE_PATH 必须是 plugins **目录**；传反会导致 sys.exit(1)。
    os.environ["PYTHON_VLC_LIB_PATH"] = str(lib_file)
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(plugins_dir)
    os.environ["VLC_PLUGIN_PATH"] = str(plugins_dir)

    logger.info("VLC 运行时目录: %s", lib_dir)
    logger.info("VLC 插件目录: %s", plugins_dir)
    return lib_dir, plugins_dir
