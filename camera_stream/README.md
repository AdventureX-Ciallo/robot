# camera_stream

为 **接入香橙派（Orange Pi 3B，ARM64，无头 / headless）的相机** 暴露一个 **HTTP 直播源**。
纯 Python 3 标准库实现，**不依赖 ROS、不依赖 OpenCV**；采集由系统 `ffmpeg`/`ffprobe`
命令行完成，向任意数量的浏览器客户端以 **MJPEG**（`multipart/x-mixed-replace`）广播，
浏览器用普通 `<img>` 标签即可原生播放。

```
浏览器 / VLC / 任何 <img>
        │  HTTP :8090  /stream  (MJPEG)
        ▼
┌──────────────────────────┐
│ camera_stream_server.py   │  ← 本包(采集 + 广播 + 内嵌网页 + 鉴权)
│  后台采集线程(ffmpeg)      │     单进程采集一次,多客户端共享同一份帧
│  看门狗(掉线自动重启采集)   │     相机抖动/拔插后自动恢复,无需重启进程
└─────────┬────────────────┘
          │ ffmpeg -f v4l2 ... -c:v mjpeg -f mpjpeg -
          ▼
       V4L2 相机 (/dev/video0)
```

**内置健壮性**：
- 单进程采集，N 个浏览器共享同一份帧（不会一个客户端开一个相机）。
- 采集管道挂掉后看门狗自动重启（指数退避 1s→2s→…→30s），相机拔插/抖动自愈。
- 首帧到达时自动用 `ffprobe` 记录真实码流信息，`GET /state` 可查。

---

## 一、克隆即跑（香橙派3B，一条命令）

前提：香橙派3B 有 `python3`、能 `git clone`、相机已插上（USB 或 CSI，呈现为 V4L2 设备）。

```bash
git clone <本仓库地址> robot
./robot/camera_stream/install.sh
```

`install.sh` 是**幂等**的，会自动依次完成：
1. 装依赖（`ffmpeg`、`v4l-utils`）
2. 检测相机设备并列出其支持的分辨率/格式
3. 安装并启动 systemd 服务（开机自启）

跑完直播即上线：
```bash
# 浏览器直接打开看画面
http://<香橙派IP>:8090/
# 或命令行验证
curl http://<香橙派IP>:8090/health
curl http://<香橙派IP>:8090/snapshot -o frame.jpg
```

常用选项：
```bash
./robot/camera_stream/install.sh \
    --device /dev/video0 \       # V4L2 设备（默认 /dev/video0）
    --view 720p \                # 分辨率预设(qvga/vga/720p/1080p)，覆盖 --width/--height
    --fps 30 \
    --quality 5 \                # JPEG 质量 2(最好)~31(最差)，默认 5
    --token 你的密钥 \            # Bearer 鉴权（建议）
    --port 8090
```

> 相机画面属敏感信息，开放网络下**建议加 `--token`**。设了 token 后，内嵌网页会自动
> 在 `<img>` 与 `fetch` 的 URL 上带 `?token=`。

### 日常运维

```bash
./robot/camera_stream/update.sh       # 改完代码后：重启服务
./robot/camera_stream/uninstall.sh    # 卸载：停服务 + 删 systemd unit
sudo systemctl status camera-stream   # 服务状态
journalctl -u camera-stream -f        # 实时日志
```

### 手动启动（不装 systemd 时）

```bash
./robot/camera_stream/install.sh --no-service --token 你的密钥
python3 ./robot/camera_stream/scripts/camera_stream_server.py \
    --device /dev/video0 --port 8090 --view 720p --token 你的密钥
```

---

## 二、HTTP API

基础地址 `http://<香橙派IP>:8090`。若设了 token，所有请求加请求头
`Authorization: Bearer <token>`（内嵌网页用 `?token=`）。

| 端点 | 说明 |
|------|------|
| `GET /` | 内嵌网页播放器（直接在浏览器看直播） |
| `GET /stream` | **MJPEG 直播源**，可作 `<img src>` 或喂给 VLC/obs |
| `GET /snapshot` | 单帧 JPEG |
| `GET /health` | `{"ok":true,"alive":true}` |
| `GET /state` | 采集统计 + `ffprobe` 探测到的真实码流信息（JSON） |

别名：`/mjpeg`、`/stream.mjpeg` → `/stream`；`/snapshot.jpg` → `/snapshot`；
以上端点也都有 `/api/...` 前缀版本。

### 在网页里嵌入直播
```html
<img src="http://192.168.1.100:8090/stream">
```

### 用 VLC 播放
```
媒体 → 打开网络串流 → http://192.168.1.100:8090/stream
```

### 抓一帧
```bash
curl http://192.168.1.100:8090/snapshot -o frame.jpg
```

### 批量采集 YOLO 训练图片
如果要从机械臂/牌桌当前相机视角采集 200~300 张训练图，可用内置脚本循环调用
`/snapshot`：

```bash
python ./robot/camera_stream/scripts/capture_snapshots.py \
    --base-url http://192.168.1.100:8090 \
    --out ./datasets/mahjong_raw/images \
    --count 300 \
    --interval 0.5
```

如果服务启用了 token：

```bash
python ./robot/camera_stream/scripts/capture_snapshots.py \
    --base-url http://192.168.1.100:8090 \
    --out ./datasets/mahjong_raw/images \
    --count 300 \
    --interval 0.5 \
    --token 你的密钥
```

如果摄像头直接插在电脑上，也可以用 OpenCV 从本机摄像头采集：

```bash
python ./robot/camera_stream/scripts/capture_local_camera.py --list

python ./robot/camera_stream/scripts/capture_local_camera.py \
    --camera 0 \
    --out ./datasets/mahjong_raw/images \
    --count 300 \
    --fps 1 \
    --preview
```

---

## 三、低延迟直播（WebRTC，亚秒级）

MJPEG 胜在零依赖、是个浏览器就能放，但端到端延迟通常在数百毫秒~1s。
要**开车级的低延迟**，用 **MediaMTX + WebRTC**：摄像头出 H.264 →
`camera_stream_server` 把它推给本机 MediaMTX（RTSP）→ MediaMTX 以 WebRTC
发给浏览器，**亚秒级**，且摄像头原生 H.264 直通时 ARM 上几乎零 CPU（零转码）。

```
浏览器(WebRTC <video>)                MediaMTX            camera_stream_server
   ▲  WebRTC :8889 /cam   ┌──────────────┐   RTSP :8554   (ffmpeg 推 H.264)
   └──────────────────────┤  mediamtx     │◄──────────────  --rtsp-url ...
                          └──────────────┘                  (MJPEG 仍照常出 /stream /snapshot)
```

**前提**：摄像头能出 H.264（`v4l2-ctl --device=/dev/video0 --list-formats-ext` 里有 `H264`）。

一条命令装好（自动装 MediaMTX + 配 WebRTC）：

```bash
./robot/camera_stream/install.sh \
    --device /dev/video0 \
    --input-format h264 \        # 摄像头直连 H.264 -> 零转码直通(最优)
    --webrtc \                   # 装 MediaMTX 并推送 H.264 到 rtsp://127.0.0.1:8554/cam
    --view 720p --fps 30 \
    --token 你的密钥
```

跑完：
- **WebRTC 直播**：`http://<香橙派IP>:8889/cam`（亚秒级）
- MJPEG 兜底仍在：`http://<香橙派IP>:8090/stream`
- 摄像头自带网页右下角会出现「⚡ 低延迟 WebRTC」直达链接。

常用选项：
```bash
--webrtc                  # 启用 WebRTC(默认推 rtsp://127.0.0.1:8554/cam,并自动装 MediaMTX)
--rtsp-url URL            # 自定义 MediaMTX 推送目标(隐含 --webrtc)
--rtsp-transport tcp|udp  # 推送传输(默认 tcp)
--mediamtx                # 只(重)装 MediaMTX 网关
--input-format h264       # 摄像头直连 H.264 直通;不给则 ffmpeg 软编 x264(ultrafast/zerolatency)
--h264-enc-arg ARG        # 自定义 H.264 编码器(可重复),如接硬编:
                          #   --h264-enc-arg -c:v --h264-enc-arg h264_v4l2m2m
```

> 只有 MJPEG/YUYV 摄像头：省略 `--input-format h264`，ffmpeg 会用软编
> `libx264`(ultrafast + zerolatency) 转出 H.264 给 WebRTC；720p 在 RK3566 上可行，
> 1080p 偏吃力，建议 `--view 720p` 或接硬编（`--h264-enc-arg`）。

### 在上位机控制面板里看 WebRTC
面板同时支持 MJPEG 与 WebRTC；配了 WebRTC 就优先用它、断开自动回退 MJPEG：
```bash
./robot/piper_http_bridge/host_controller/install.sh \
    --endpoint http://<机械臂>:8080 --token SECRET \
    --camera http://<香橙派IP>:8090/stream.mjpeg \
    --camera-webrtc http://<香橙派IP>:8889/cam
```

### MediaMTX 运维
```bash
sudo systemctl status mediamtx     # 网关状态
journalctl -u mediamtx -f          # 网关日志
# 手动重装/改路径: ./robot/camera_stream/install_mediamtx.sh --path cam
```

---

## 四、分辨率预设（`--view`）

`qvga`(320×240)、`vga`(640×480)、`svga`(800×600)、`720p`/`hd`(1280×720)、
`1080p`/`fhd`(1920×1080)，也可直接写 `640x480` 这种形式。`--view` 优先于
`--width/--height`。

相机实际支持哪些分辨率/帧率，可用：
```bash
v4l2-ctl --device=/dev/video0 --list-formats-ext
```
（install.sh 检测到设备时也会自动打印。）

---

## 五、本地干跑测试（无需真机/无需相机/无需ffmpeg）

测试用桩顶替 ffmpeg/ffprobe 子进程，在本机（甚至 Windows）直接验证
MJPEG 分帧、帧广播、快照、网页与各 HTTP 端点：

```bash
python test/dry_run_test.py    # -> 退出码 0 表示通过
```

> 这只是逻辑自测，不代替真机联调。真机请确认 `v4l2-ctl --list-devices`
> 能看到相机，再访问 `/snapshot` 应返回一张 JPEG。
