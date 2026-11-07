# Windows 桌面视频逐帧播放器

基于 **Python + PySide6 + python-vlc** 的本地视频逐帧查看 / 分析工具，仅支持 **Windows 64 位**。所有处理都在本地完成，不访问网络。

仓库：<https://github.com/NF000/video-frame-player>

## 功能

| 能力 | 说明 |
| --- | --- |
| 格式 | mp4 / mkv / avi |
| 播放控制 | 播放·暂停·停止、进度条拖动、音量 / 静音、0.5x / 1x / 2x / 3x 倍速 |
| 逐帧 | **暂停时** `←` / `→` 单帧后退 / 前进，状态栏显示帧号与 fps |
| 快进 | **播放时** `←` / `→` 前后跳转 5 秒，无需先暂停 |
| 文件夹播放 | 选父文件夹后**递归扫描所有层子文件夹**，按「目录层级 + 自然排序」排列（`ep2` 排在 `ep10` 前），自动播第一个、播完自动续播；双击列表或用上/下一个按钮切换 |
| 拖拽 | 拖入视频文件（自动把同目录视频排成列表）或拖入文件夹（递归载入全部视频） |
| 界面 | 单窗口 + 左侧播放列表，窗口可自由缩放，`F` 全屏 / `Esc` 退出 |
| 断点续播 | 自动记住上次的文件夹 / 视频 / 播放位置 / 倍速与音量，下次打开接着看（详见「观看记录」章节） |
| 流畅性 | libvlc 的阻塞调用放在后台串行任务队列，切换视频、载入数百个文件都不会让界面「未响应」 |

## 快捷键

| 按键 | 作用 |
| --- | --- |
| `Space` | 播放 / 暂停 |
| `→` / `←` | 暂停态：单帧前进 / 后退；播放态：快进 / 快退 5 秒 |
| `F` | 切换全屏 |
| `Esc` | 退出全屏 |

## 运行环境

- Windows 64 位；Python 3.12（推荐 conda 环境）
- 依赖见 `requirements.txt`

```bash
conda create -n video-player python=3.12 -y
conda activate video-player
python -m pip install -r requirements.txt
python main.py
```

## VLC 运行时（必读）

`python-vlc` 只是 ctypes 包装，**不自带 `libvlc.dll`**。仓库不含 `vendor/vlc/`（约 143MB 第三方二进制，已被 `.gitignore` 排除），clone 之后需任选一种方式提供运行时：

1. **项目内置（推荐，打包也走这条）**：下载 VLC 官方 **Windows 64 位 zip 便携版**，把解压目录里的 `libvlc.dll`、`libvlccore.dll` 和整个 `plugins/` 放到项目根下的 `vendor/vlc/`。
2. **环境变量**：`set VLC_LIB_DIR=D:\path\to\vlc`，该目录下同样需要 `libvlc.dll`、`libvlccore.dll` 和 `plugins/`。
3. **系统 VLC**：安装 64 位 VLC 桌面版到默认路径。

查找优先级：`VLC_LIB_DIR` → `vendor/vlc/` → 系统 VLC。**Python 与 VLC 必须同为 64 位**，否则加载 DLL 会报 `WinError 193`。

> 可选优化：用 VLC 便携包里的 `vlc-cache-gen.exe` 在 `plugins/` 生成 `plugins.dat` 插件缓存，可明显缩短冷启动时扫描数百个插件 DLL 的时间。

## 打包成 exe

```bat
build\build_exe.bat
```

产物：`dist\VideoFramePlayer\VideoFramePlayer.exe`（onedir 模式）。

**分发时必须把整个 `VideoFramePlayer` 文件夹一起给出**（含 `_internal\` 与 `plugins\`），只发单个 exe 会导致 VLC 找不到解码插件。

打包用哪个 Python，按以下优先级自动选择，脚本里不再写死任何本机路径：

1. 第一个命令行参数：`build_exe.bat "D:\your\env\python.exe"`
2. 环境变量：`set PYTHON_EXE=D:\your\env\python.exe`
3. 当前已激活 conda 环境：`%CONDA_PREFIX%\python.exe`
4. PATH 中的 `python`

`build_exe.spec` 同样会读取 `VLC_LIB_DIR` 决定打包时复制哪份 VLC 运行时；未设置时取 `vendor/vlc/`。

### 打包坑点

- 64 位 Python 必须配 64 位 `libvlc.dll` / `libvlccore.dll`，位数不一致直接 `WinError 193`。
- `plugins/` 必须**整体**作为目录带上；用 onedir 而非 onefile——onefile 解压到临时目录会破坏插件的相对查找路径。
- `PYTHON_VLC_LIB_PATH` 必须是 **`libvlc.dll` 的完整文件路径**（不是目录），`PYTHON_VLC_MODULE_PATH` / `VLC_PLUGIN_PATH` 必须指向 `plugins/` 目录；传反会让 python-vlc 直接 `sys.exit(1)`。
- conda 环境的 `ffi-8.dll` / `libbz2.dll` / `liblzma.dll` 放在 `Library\bin`，PyInstaller 分析不到，spec 里已显式带上（`python-vlc` 走 ctypes，`ffi-8.dll` 尤其关键）。
- `console=False` 时看不到任何异常，调试期可临时改成 `True`；`upx=False` 保持不动，UPX 压缩 Qt / VLC 的 DLL 容易损坏。

## 目录结构

```
main.py                     程序入口
player/vlc_runtime.py       VLC 运行时定位与加载（必须在 import vlc 之前）
player/engine.py            异步 VLC 引擎：后台串行任务队列 + Qt 信号回主线程
player/frames.py            帧网格与单帧步进
player/playlist.py          递归扫描、自然排序、播放列表
ui/main_window.py           主窗口、控件、快捷键、拖拽、自动续播
ui/video_widget.py          提供原生窗口句柄给 libvlc 渲染
build/build_exe.spec        PyInstaller 配置（onedir + VLC datas）
build/build_exe.bat         一键打包
requirements.txt            pip 依赖
vendor/vlc/                 （不入库）VLC 便携运行时
```

> `dist/`、`output/`、`build/*/`、`.tmp/`、`vendor/vlc/` 均不入库，规则见 `.gitignore`。

## 观看记录 / 断点续播

关掉软件后，下次打开会自动回到上次的视频和播放位置，不用重新找。

- **记录内容**：当前文件夹（递归播放列表）、正在播放的视频、播放位置、倍速、音量、静音状态，以及退出时是否正在播放。
- **恢复行为**：启动后自动重建播放列表、定位到上次那一集并跳到断点。退出时正在播放 → 恢复后继续播放；退出时是暂停 → 恢复后停在断点，按空格继续。状态栏会提示「已恢复上次观看」。
- **失效容错**：视频被移动/删除、文件夹不可读、记录损坏或版本不符，都只是跳过恢复，不弹框、不影响启动。
- **不跳到片尾**：上次停在结尾 2 秒内时从头播，避免一打开就触发「播完自动续播」跳到下一集。
- **状态文件**：`%APPDATA%\VideoFramePlayer\session.json`（UTF-8，几百字节）。删除该文件即可清除记录；把里面的 `auto_resume` 改成 `false` 可临时关闭自动恢复。

### 为什么不影响流畅度

| 措施 | 说明 |
| --- | --- |
| 异步写盘 | 播放期间的状态写入全在后台 daemon 线程，主线程只投递快照，磁盘 I/O 为零 |
| 5 秒节流 + 合并 | 每 5 秒最多写一次；位置变化不足 1 秒且无其它改动就跳过；连续快照只保留最新一份 |
| 原子替换 + 序号 | 临时文件 + `os.replace`，崩溃不留半个 JSON；带单调序号，退出时的最终快照不会被后台旧快照覆盖 |
| 延迟恢复 | 窗口显示、引擎就绪后再读记录（延迟 400ms），首帧不读盘、不扫目录 |
| 后台重建列表 | 恢复时的文件夹递归遍历在后台线程，结果经 Qt 信号回主线程，几百个视频也不冻结界面 |
| 回调内 seek | 等 VLC 解析出时长后在 `on_opened` 回调里 seek（有限次重试），不新增同步阻塞的 libvlc 调用 |
| 退出不等待 | 关窗时同步写一次最终快照（<1ms），不等后台队列 |

> 记录的是「上次会话」，不是每个视频各自的历史进度：逐帧分析时重新打开某一集通常是想从头看，自动跳转反而会干扰。

## 回归测试

```bash
conda activate video-player
python -m unittest discover -s tests -v
```

`tests/test_session.py` 用假引擎替身在 offscreen Qt 平台下跑完整恢复链路，覆盖存档容错、异步合并写、退出竞态、文件夹/单文件恢复、暂停与播放状态、片尾断点、用户操作打断恢复、关闭自动恢复等场景，不需要真实 VLC 或视频文件。
