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

---

## 三、分辨率预设（`--view`）

`qvga`(320×240)、`vga`(640×480)、`svga`(800×600)、`720p`/`hd`(1280×720)、
`1080p`/`fhd`(1920×1080)，也可直接写 `640x480` 这种形式。`--view` 优先于
`--width/--height`。

相机实际支持哪些分辨率/帧率，可用：
```bash
v4l2-ctl --device=/dev/video0 --list-formats-ext
```
（install.sh 检测到设备时也会自动打印。）

---

## 四、本地干跑测试（无需真机/无需相机/无需ffmpeg）

测试用桩顶替 ffmpeg/ffprobe 子进程，在本机（甚至 Windows）直接验证
MJPEG 分帧、帧广播、快照、网页与各 HTTP 端点：

```bash
python test/dry_run_test.py    # -> 退出码 0 表示通过
```

> 这只是逻辑自测，不代替真机联调。真机请确认 `v4l2-ctl --list-devices`
> 能看到相机，再访问 `/snapshot` 应返回一张 JPEG。
