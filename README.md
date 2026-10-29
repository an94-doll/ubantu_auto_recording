# 海康 USB 相机采集台 · 首版

启动后自动连接相机、恢复画面参数并显示预览。点击一次“开始录制”，再点击“停止并保存”，得到一个 **H.265/MKV 视频**，采集信息汇总到同目录的 **recordings.csv**。

面向 Ubuntu 带屏工控机和 MV-CT032G-104UC01-PRO USB 工业相机。程序通过海康官方 MVS SDK 读取型号、全幅分辨率和最高帧率，不把设计阶段的 2048×1536 / 104.8 fps 写死为设备规格。

## 先用演示相机试操作

在 Ubuntu 上，将项目放到固定目录，例如 `~/slicing`：

```bash
cd ~/slicing
sudo apt update
sudo apt install python3-venv libegl1 libopengl0 libxcb-cursor0 libxkbcommon-x11-0 fonts-noto-cjk
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m slicing_recorder --demo
```

演示模式不需要相机或 MVS，画面和相机型号会明确标注 DEMO。默认保存到 `~/Videos/slicing-demo`，使用单独的 `~/.config/slicing-recorder/demo.json`，不覆盖真实相机设置。

Windows 开发测试也可运行 `python -m slicing_recorder --demo`。从 Windows 复制项目到 Ubuntu 时，不要复制 `.venv`，应在 Ubuntu 重新创建。

## 接入真实相机

1. 从[海康机器人下载中心](https://www.hikrobotics.com/cn/machinevision/service/download)安装与系统架构匹配的 **Linux MVS**，包含驱动、运行库和 Python 示例。
2. 相机接到 USB 3.x 口。先在 MVS 客户端确认可见、能连续出图，然后关闭 MVS 客户端，释放独占连接。
3. 在项目目录启动，填写自己的序列号和实际 MvImport 路径：

```bash
bash scripts/run.sh \
  --serial 你的相机序列号 \
  --sdk-path /opt/MVS/Samples/64/Python/MvImport
```

若本机 MVS 的示例位于 `/opt/MVS/Samples/Python/MvImport`，改成该路径。只有一台 USB 相机时可省略 `--serial`；多台时必须指定。成功连接后保存序列号和 SDK 路径，下次只需：

```bash
bash scripts/run.sh
```

SDK 安装在其他位置时，使用 `MVS_ROOT=/实际/MVS目录 bash scripts/run.sh`。启动脚本显式设置 MVS 动态库环境，桌面自启动也不依赖 `.bashrc`。`--sdk-path` 指向 Python 声明目录，不是动态库目录。

完整依赖和编码硬件说明见 [DEPENDENCIES.md](DEPENDENCIES.md)。

## 日常采集

1. 等待“预览中 · 可以录制”。相机未上电时每 3 秒重试连接。
2. 调曝光、增益和白平衡，点击“应用并保存”。程序写入相机后读回生效值，再保存。仅显示设备支持的节点与范围。
3. 选择保存目录，填写采集名称。编码器默认“自动”，质量值默认 23。
4. 点击“开始录制”。编码器探测完成后才进入“正在录制”。
5. 点击“停止并保存”，等待“保存完成”后再关机或移除存储设备。关闭窗口也会先收尾。

“恢复上次设置”会撤销未应用的本地编辑，并重新应用已保存的参数。自动曝光/增益/白平衡开启时，相应手动值不可编辑。为便于批次比较，建议调好画面后使用手动固定参数。

程序固定采用全幅、8 位 Bayer（设备支持时）和设备帧率节点的最大值；同时恢复支持的 Binning/Decimation 节点，不继承旧的缩采样。预览默认刷新 25 fps，录制独立处理所有接收帧。取消“适应窗口”可按原始比例滚动查看。

界面区分“设置上限”“设备回读”和实测“实际采集 / fps”。曝光过长、USB 带宽不足或主机处理不足都可能让实际帧率低于上限；程序不会为了追帧率自动缩短曝光、缩小画面或补帧。长曝光影响出图时，也可能触发连续 3 秒无图像的超时保护。

## 登录桌面后自动打开

先确认手动运行正常，再在 Ubuntu 项目目录执行：

```bash
source .venv/bin/activate
python scripts/install_autostart.py --dry-run  # 查看将生成的启动项
python scripts/install_autostart.py
```

它写入当前账户的 `~/.config/autostart/slicing-recorder.desktop`。下次登录桌面会打开界面、连接相机并恢复参数，**不会自动开始录制**。移动项目目录后需要重新执行安装脚本。

非默认 MVS 目录或配置文件：

```bash
python scripts/install_autostart.py --mvs-root /实际/MVS目录 --config /绝对路径/config.json
```

移除自启动：`python scripts/install_autostart.py --remove`。脚本不修改系统服务或账户自动登录；若现场要求按电源键后直接出现页面，需另外在 Ubuntu 为采集账户配置自动登录。

## 视频与 CSV

```text
采集目录/
├── recordings.csv
├── 20261029_143000_123456_实验001.mkv
└── 20261029_150000_654321_实验002.mkv
```

一次开始到停止只生成一个视频，不分段，不生成每次的 JSON 或逐帧表。全局应用配置仍用 JSON 保存，默认位置为 `~/.config/slicing-recorder/config.json`；配置示例见 [config.example.json](config.example.json)，使用前修改保存目录。

CSV 为 UTF-8 BOM，便于 Excel 打开；表头使用固定英文名。每次录制建立一行，停止收尾后更新同一行：

| 字段 | 含义 |
|---|---|
| `recording_id`, `video_file` | 该次采集的唯一标识、相对视频文件名 |
| `requested_at` | 点击开始后建立任务的时间，包含编码器准备前的等待 |
| `start_time`, `end_time` | 首个接收并进入录制队列的帧的主机时间、停止接收录制帧的主机时间；含毫秒及本机时区，结束时间不包含编码收尾 |
| `camera_model`, `camera_serial` | 设备读取的型号、序列号 |
| `width`, `height`, `pixel_format` | 实际全幅宽高、相机源像素格式，如 BayerGR8 |
| `target_fps`, `camera_fps_readback` | 全幅帧率设置上限、开始录制时设备结果帧率读回值；无结果节点时后者为帧率设置值 |
| `exposure_us`, `exposure_auto` | 开始录制边界读取的曝光微秒值、自动模式 |
| `gain_db`, `gain_auto` | 同一边界的增益、自动模式 |
| `white_balance` | 自动模式和可读取的 R/G/B 原生比例，存放在一个 CSV 单元格内 |
| `encoder`, `quality`, `output_pixel_format` | 实际 H.265 编码器、质量值、输出 8 位 YUV420 采样格式 |
| `timestamp_source` | `camera_device` 或 `host_monotonic` |
| `received_frames`, `encoded_frames` | 录制期间收到的帧、成功交给编码器处理的帧；文件成功收尾后才显示保存完成 |
| `dropped_frames` | 可检测的相机帧号缺口，加上已收到但未进入/完成编码的帧数；不等同于传感器所有潜在丢帧 |
| `file_size_bytes` | 视频关闭后的实际字节数 |
| `status`, `error` | 结束状态、错误原因 |

自动模式可能在录制中改变曝光、增益和白平衡，CSV 参数是**开始边界的快照**，不是整段视频的恒定值。需要可比较的固定设置时，请关闭自动模式。

`normal` 表示正常停止并收尾；其他状态包括 `camera_disconnected`、`disk_low`、`disk_error`、`frame_loss`、`encoding_error`、`metadata_error`、`application_exit`、`empty`、`internal_error`。异常断电或强制终止可能留下 `recording`、空结束时间或空文件大小，应视为未正常完成，不能仅凭文件存在认定成功。

视频 PTS 使用可读取频率的相机时钟；无法确定设备 tick 单位时，明确使用主机高分辨率接收时钟。缺帧间隔不会按固定帧号压缩，MKV 时间粒度通常为毫秒。主机接收时间不等于传感器曝光时刻。

## 编码与异常处理

- “自动”实际试编码全尺寸帧，依次尝试 NVIDIA NVENC、Intel QSV、VAAPI，最后软件 `libx265`。采用的软件/硬件编码器和探测原因显示在状态栏；显式指定编码器失败时不回退。
- 始终保持 H.265，不切换 H.264，不降低分辨率/帧率。质量值越小通常质量越高、文件越大；不同编码器的同一个数字不代表相同画质。输出经过 Bayer→RGB→YUV420 和有损编码，不是传感器原始像素存档。
- 默认保留 2 GB 磁盘空间，每秒检查；空间不足时停止接收录制帧并尝试收尾。该余量不能替代磁盘容量规划或保障断电恢复。
- 默认队列 32 帧，容量固定；2048×1536 RGB24 的队列像素约占 302 MB，另有 SDK、预览和编码器内存。队列满或检测到相机帧号缺口会报警并结束本次采集，不静默丢帧。
- 断线后自动重连只恢复预览，不自动续录。已有 CSV 表头不匹配时拒绝录制，不改写原数据。

队列大小、磁盘保留量和预览帧率可在程序关闭后编辑全局配置的 `queue_frames`、`min_free_gb`、`preview_fps`。扩大队列只能容纳短时波动，不能解决持续编码过慢。

## 测试与工控机验收

自动化测试（无相机）：

```bash
source .venv/bin/activate
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

编码吞吐基准（下列尺寸/帧率只是设计预算，应替换为实际设备读回值）：

```bash
python scripts/benchmark.py --width 2048 --height 1536 --fps 104.8 \
  --frames 300 --samples 3 --codec auto --output benchmark-output
```

基准记录硬件/软件环境、三次吞吐与波动、实际编码器、文件大小，逐个解码校验帧数和时间跨度。它测量合成 RGB 到编码写盘，不包含 USB、Bayer 转换或真实场景压缩难度；首版测试记录见 [VALIDATION.md](VALIDATION.md)。

部署到工控机后，请完成三项现场验收：

1. 重启、登录桌面，确认自动打开、连接指定相机并恢复已保存参数。
2. 按实际最长采集时长录制：核对全幅分辨率、实际帧率、接收/编码计数一致、丢帧为零、队列无持续增长；停止后完整解码视频并比较时间跨度。
3. 用测试采集验证拔线重连、磁盘保留阈值和退出收尾，确认 CSV 的异常状态与实际行为一致。

若最高规格下软件编码持续跟不上，应先验证硬件编码和存储/USB 链路，不能把扩大队列或降低设置当作满规格通过。

## 常见问题

- **找不到 MvImport**：用 `--sdk-path` 指定本机官方 Python 声明目录；显式路径错误会直接报告，不会换用别的 SDK。
- **相机打不开 / 无权限**：关闭 MVS 和其他采集进程，确认 MVS 驱动/USB 权限已按安装包说明配置；不要用 sudo 运行整套 GUI 来掩盖权限问题。
- **Qt xcb 插件错误**：先核对 [DEPENDENCIES.md](DEPENDENCIES.md) 的桌面库和显示会话；离屏测试用 offscreen，现场预览必须使用正常桌面会话。
- **没有 H.265 硬件编码**：PyAV 构建、GPU/驱动及设备权限都要支持；系统 `ffmpeg` 支持某编码器不代表当前 PyAV 同样支持。界面会给出探测失败原因，选择软件后仍需测吞吐。
- **CSV 无法更新**：停止用 Excel 等软件占用该文件，检查目录写权限、空间。若最终 CSV 更新失败，界面报错，原行可能保留未完成状态；不要将其当正常记录。

## 实现位置

`camera.py`：官方 SDK 与演示后端；`controller.py`：相机线程和状态；`recording.py`：有界队列与编码线程；`encoder.py`：PyAV H.265/MKV；`metadata.py`：CSV；`ui.py`：Qt 界面。采集线程不等待预览刷新，编码线程独占编码器。

接口依据：[海康参数设置说明](https://www.hikrobotics.com/cn2/source/vision/video/2021/6/25/20210625082558512.pdf)、[PyAV 时间戳示例](https://github.com/PyAV-Org/PyAV/blob/master/examples/numpy/generate_video_with_pts.py)、[PyAV 编码容器 API](https://pyav.basswood.io/docs/stable/api/container.html)、[桌面启动项 Exec 规范](https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html)。实际 SDK 结构声明来自本机 MVS 安装，不在项目中重写或分发。
