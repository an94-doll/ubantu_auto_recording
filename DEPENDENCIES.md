# 依赖列表

## Python 环境

建议 Ubuntu 22.04/24.04 x86_64、Python 3.10–3.12，使用项目独立虚拟环境。现场 Ubuntu 版本尚待确认；ARM/Jetson 不属于本次部署验证范围。

| 依赖 | 范围 | 用途 |
|---|---|---|
| Python | 3.10+ | 应用与线程控制 |
| NumPy | `>=1.26,<3` | 连续图像缓冲与合成测试 |
| PySide6 | `>=6.7,<7` | Qt 桌面界面、单实例锁 |
| PyAV (`av`) | `>=16,<20` | 调用 FFmpeg 库，H.265 编码和 MKV 封装 |

安装命令：`python -m pip install -r requirements.txt`。测试使用标准库 `unittest`，无需 pytest、OpenCV 或 ROS。PyAV 二进制 wheel 自带其 FFmpeg 库；应用本身不调用系统 `ffmpeg` 命令。

本轮开发验证版本：Python 3.12.14、NumPy 2.5.4、PySide6 6.12.0、PyAV 19.0.2（Windows x86_64）。版本范围方便 Ubuntu 安装，目标机仍需按 README 验收。

## Ubuntu 桌面库

```bash
sudo apt install python3-venv libegl1 libopengl0 libxcb-cursor0 libxkbcommon-x11-0 fonts-noto-cjk
```

需要已登录的图形桌面和可写的采集目录。精简系统如仍报告 xcb 依赖缺失，应根据错误补齐对应系统库；普通 Ubuntu 桌面已包含大部分基础 X11/Wayland 依赖。

## 海康相机组件

- 与 Ubuntu/CPU 架构匹配的官方 Linux MVS 安装包、USB 驱动/权限规则、`libMvCameraControl.so`。
- 同一安装包中的 **MvImport** Python 声明目录，包含 `MvCameraControl_class.py` 及配套头文件转换模块。
- USB 3.x 直连、足够的控制器带宽和稳定供电。先在 MVS 客户端验证出图，再交给本程序独占设备。

MVS 不通过 `pip install` 安装，也未随项目分发。厂商 Python 声明与动态库必须使用匹配版本；安装和授权条款按[海康下载中心](https://www.hikrobotics.com/cn/machinevision/service/download)的实际包说明执行。

## H.265 编码硬件

- **软件 `libx265`**：无需 GPU，但高分辨率/高帧率下可能不能实时处理。
- **`hevc_nvenc`**：需要支持 HEVC 编码的 NVIDIA GPU、驱动及相应 PyAV/FFmpeg 构建。
- **`hevc_qsv` / `hevc_vaapi`**：需要支持 HEVC 编码的 Intel/其他兼容 GPU、媒体驱动和相应 PyAV/FFmpeg 构建；VAAPI 还需要可访问的渲染设备。

程序会实际探测，不仅检查编码器名称是否存在。硬件支持不能由 Python 包安装成功推断。若 PyAV wheel 没有目标硬编，可按 [PyAV 官方安装说明](https://pyav.basswood.io/docs/stable/overview/installation.html)使用带目标能力的 FFmpeg 构建 PyAV；不要仅安装另一个系统 FFmpeg 就认为应用已切换后端。

可选安装 `ffmpeg`，用于现场独立解码检查：

```bash
sudo apt install ffmpeg
ffprobe -v error -count_frames -select_streams v:0 \
  -show_entries stream=codec_name,width,height,nb_read_frames:format=duration,size 采集视频.mkv
ffmpeg -v error -i 采集视频.mkv -f null -
```
