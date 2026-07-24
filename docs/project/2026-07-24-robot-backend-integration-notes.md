# robot 后端仓库对接说明

> 仓库：`D:/Documents/Projects/AGILE/robot/`  
> GitHub：https://github.com/AdventureX-Ciallo/robot  
> 日期：2026-07-24  
> 状态：已本地读取并通过干跑测试

---

## 1. 结论

`robot` 仓库已经可以作为麻将机器人 Demo 的后端基础，主要包含两块：

1. `piper_http_bridge`：PiPER 机械臂 HTTP/TCP 控制桥。
2. `camera_stream`：香橙派相机 MJPEG/WebRTC 直播服务。

当前仓库已经覆盖：

- 机械臂状态查询。
- 机械臂使能/失能。
- 关节控制。
- 末端位姿控制。
- 夹爪控制。
- 停止、复位、归零。
- 相机 MJPEG 直播、snapshot、health、state。
- systemd 部署脚本。
- token 鉴权。
- 本地干跑测试。

因此，麻将项目不需要从零写机械臂后端。下一步应在该仓库上新增“麻将动作编排层”。

---

## 2. 已验证测试

在 Windows 本地干跑：

```bash
python piper_http_bridge\test\client_test.py
python piper_http_bridge\test\sdk_dry_run_test.py
python camera_stream\test\dry_run_test.py
```

结果：

| 测试 | 结果 |
|---|---|
| `client_test.py` | 23 passed / 0 failed |
| `sdk_dry_run_test.py` | 41 passed / 0 failed |
| `camera_stream dry_run_test.py` | 35 passed / 0 failed |

---

## 3. 当前后端能力

### 3.1 PiPER 控制服务

基础地址：

```text
http://<香橙派IP>:8080
```

核心接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/state` | 查询机械臂状态 |
| POST | `/cmd` | 统一命令入口 |

`POST /cmd` 支持的动作：

| action | 说明 |
|---|---|
| `enable` | 使能机械臂 |
| `disable` | 失能机械臂 |
| `joint_ctrl` | 关节角控制 |
| `pose_ctrl` | 末端位姿控制 |
| `gripper` | 夹爪控制 |
| `go_zero` | 归零 |
| `stop` | 停止 |
| `reset` | 复位 |
| `block_arm` | 阻塞机械臂 |
| `set_mode` | 切换模式，后端支持时可用 |

### 3.2 相机流服务

基础地址：

```text
http://<香橙派IP>:8090
```

核心接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 内嵌播放器 |
| GET | `/stream` | MJPEG 直播 |
| GET | `/snapshot` | 单帧 JPEG |
| GET | `/health` | 健康检查 |
| GET | `/state` | 采集状态 |

如果启用 MediaMTX，可提供 WebRTC：

```text
http://<香橙派IP>:8889/cam
```

---

## 4. 与麻将 Demo 的关系

当前 `robot` 仓库提供的是“低层控制能力”，麻将项目还需要在上层补：

```text
麻将动作编排层
  -> 调 piper_http_bridge 的 pose_ctrl / gripper / stop

麻将视觉识别层
  -> 调 HUSKYLENS 2 / camera_stream / GX10 推理

麻将决策层
  -> 输出推荐弃牌 tile_id

麻将状态管理层
  -> 串联识牌、算牌、动作、复检
```

---

## 5. 建议新增麻将动作接口

建议不要让前端直接调用 `pose_ctrl` 和 `gripper` 编排复杂动作，而是在后端新增高级动作。

### 5.1 推荐动作

| 高级动作 | 说明 |
|---|---|
| `home` | 回安全位 |
| `calibrate_table` | 桌面/槽位标定 |
| `move_to_tile` | 移动到目标牌上方 |
| `push_tile` | 推倒/推出目标牌 |
| `grasp_tile` | 夹取目标牌 |
| `place_to_discard` | 放到弃牌区 |
| `discard_tile` | 完整出牌动作，内部包含移动、夹取/推牌、放置、撤离 |
| `recover` | 失败后撤离并复位 |
| `emergency_stop` | 急停 |

### 5.2 `discard_tile` 请求示例

```json
{
  "action": "discard_tile",
  "mode": "grasp",
  "tile_id": "tile_05",
  "tile_label": "3万",
  "target_position": {
    "x_mm": 220,
    "y_mm": 120,
    "z_mm": 0,
    "theta_deg": 0
  },
  "discard_position": {
    "x_mm": 320,
    "y_mm": 180,
    "z_mm": 0
  },
  "speed": 20,
  "confirm_required": true
}
```

### 5.3 返回示例

```json
{
  "ok": true,
  "action": "discard_tile",
  "status": "completed",
  "steps": [
    "move_above_tile",
    "open_gripper",
    "approach",
    "close_gripper",
    "lift",
    "move_to_discard",
    "release",
    "retreat"
  ],
  "duration_ms": 6200
}
```

---

## 6. 夹取并打出动作建议

虽然第一版验收仍是“识牌、算牌、出牌”，但可以尝试夹取并打出。

推荐先写成状态机：

```text
IDLE
  -> MOVE_ABOVE_TILE
  -> OPEN_GRIPPER
  -> APPROACH_TILE
  -> CLOSE_GRIPPER
  -> LIFT_TILE
  -> MOVE_TO_DISCARD_AREA
  -> RELEASE_TILE
  -> RETREAT
  -> VERIFY
  -> DONE / FAILED
```

失败恢复：

```text
FAILED
  -> OPEN_GRIPPER
  -> RETREAT
  -> HOME
  -> WAIT_FOR_MANUAL_CONFIRM
```

MVP 阶段可以先实现两种模式：

| mode | 说明 |
|---|---|
| `push` | 推倒/推出，稳定优先 |
| `grasp` | 夹取并打出，演示真实感优先 |

---

## 7. 前端对接建议

前端控制台建议接入这些服务：

| 前端模块 | 后端来源 |
|---|---|
| 机械臂状态 | `GET :8080/state` |
| 急停 | `POST :8080/cmd {"action":"stop"}` |
| 回零 | `POST :8080/cmd {"action":"go_zero"}` |
| 夹爪控制 | `POST :8080/cmd {"action":"gripper"}` |
| 相机画面 | `GET :8090/stream` 或 WebRTC |
| 单帧采样 | `GET :8090/snapshot` |
| 高级出牌 | 后续新增 `POST /mahjong/action` |

建议前端不要直接暴露复杂的 `pose_ctrl` 参数给演示操作者。研发调试页可以暴露，演示模式只保留：

```text
开始识别
计算出牌
执行出牌
暂停
急停
```

---

## 8. 与 HUSKYLENS 2 的衔接

`camera_stream` 当前面向普通 V4L2 相机。HUSKYLENS 2 更可能通过 UART/I2C 输出识别结果，不一定以 `/dev/video0` 形式提供原始视频。

建议新增一个独立服务：

```text
huskylens2_vision_bridge
  - 读取 HUSKYLENS 2 blocks
  - 转换为统一 JSON
  - 提供 GET /vision/state
  - 提供 GET /vision/tiles
  - 可选提供 WebSocket 推送
```

统一输出格式参见：

```text
docs/project/2026-07-24-huskylens2-vision-integration-notes.md
```

---

## 9. 当前缺口

| 缺口 | 建议 |
|---|---|
| 没有麻将高级动作接口 | 在 `piper_http_bridge` 上层新增动作编排 |
| 没有 HUSKYLENS 2 数据读取服务 | 新增 `huskylens2_vision_bridge` |
| 没有牌桌/相机/机械臂标定模块 | 新增 `calibration` 配置与工具 |
| 没有弃牌决策服务 | 新增 `mahjong_decision` 或由电脑端提供 |
| 没有统一 Demo API | 新增 `POST /demo/run_once` |
| 没有动作复检 | 接视觉结果判断是否出牌成功 |

---

## 10. 下一步建议

1. 等后端同学确认 `robot` 仓库是否继续作为主后端。
2. 在 `robot` 仓库新增麻将动作编排模块。
3. 在项目文档中补一份正式 API 设计。
4. 等机械臂同学给出夹取动作程序后，再把 `grasp_tile` 状态机接入。
5. 等 HUSKYLENS 2 真机测试后，确定视觉数据字段。

