# piper_http_bridge

为 **AgileX Piper 6 轴机械臂** 暴露一个 **HTTP / TCP 控制端口**。面向 **香橙派3B（Orange Pi 3B，ARM64，无头 / headless）** 部署。

提供**两种后端**，对外暴露的 HTTP/JSON 与 TCP/JSON 接口完全一致：

| 后端 | 是否依赖 ROS | 接口完整度 | 适用 |
|------|------------|-----------|------|
| **`sdk`（默认/推荐）** | ❌ 不需要 | ✅ 全（stop/reset/go_zero/gripper 都有） | 香橙派3B，最省内存，最快 |
| `ros` | ✅ 桥接官方 noetic 节点 | ✅ 全 | 还需要 MoveIt/RViz/ROS 生态时 |

SDK 直连架构（默认）：

```
客户端(curl/python/任何语言)
        │  HTTP :8080  /  TCP :9090   (JSON)
        ▼
┌──────────────────────────┐
│ piper_sdk_server.py       │  ← 本包(限位校验 + 单位换算 + 鉴权)
│  + server_common.py       │     启动时自动激活 CAN + 自动使能机械臂
│  + CAN 看门狗(自动恢复)    │     检测 BUS-OFF 自动 down/up 恢复并重连
└─────────┬────────────────┘
          │ piper_sdk (python-can, SocketCAN)
          ▼
       CAN 1Mbps (can0)  →  Piper 机械臂
```

**内置健壮性**：
- 启动自动 `ip link set can0 up type can bitrate 1000000`（`--no-can-init` 关闭）
- 启动自动使能机械臂（`--no-auto-enable` 关闭）
- **CAN 看门狗**：gs_usb 适配器有已知「发送卡死」bug（发送几次后 TX buffer 泄漏、
  进 BUS-OFF)，看门狗每 2s 检测一次，发现 BUS-OFF 就自动 down/up 接口并重连 SDK、
  重新使能（`--no-watchdog` 关闭，`--watchdog-interval` 调周期）。免去人工拔插适配器。

ROS 桥架构（可选 `--backend ros`）：`客户端 → piper_http_bridge_node.py → ROS topic/service → 官方 piper_ctrl_single_node → CAN`。

## 单位约定（对客户端）

| 量 | 客户端单位 | 说明 |
|----|-----------|------|
| 关节角 joint_ctrl | **度 (deg)** | 6 个值，内部转成 rad 发给 ROS |
| 末端位置 x,y,z | **毫米 (mm)** | 内部转成 m |
| 末端姿态 roll,pitch,yaw | **度 (deg)** | 内部转成 rad |
| 夹爪 gripper | **毫米 (mm)** | 0~100（±0.5），内部转成 m |
| 速度 speed | 1~100 (%) | |

关节限位（度，软件侧校验，超出直接拒绝）：J1 ±154，J2 0~195，J3 -175~0，J4 ±102，J5 ±75，J6 ±170。
关节最大速度（度/秒）：J1 180，J2 195，J3 180，J4 225，J5 225，J6 225。

---

## 一、克隆即跑（香橙派3B，一条命令）

前提：香橙派3B 有 `python3` + `pip`、能 `git clone`、USB-CAN 已插上。**默认 SDK 直连，无需 ROS。**

```bash
git clone <本仓库地址> robot
./robot/piper_http_bridge/install.sh --token 你的密钥
```

`install.sh` 是**幂等**的，会自动依次完成：
1. 装依赖（`can-utils`、`ethtool`、`piper_sdk`、`python-can`）
2. SDK 后端无需编译；ROS 后端才会克隆编译官方 `piper_ros`
3. 激活 CAN 接口（默认 `can0` @ 1 Mbps）
4. 安装并启动 systemd 服务（开机自启，含 CAN 拉起）

跑完控制端口即上线：
```bash
curl http://<香橙派IP>:8080/state
curl -X POST http://<香橙派IP>:8080/cmd -H "Authorization: Bearer 你的密钥" -d '{"action":"enable"}'
```

常用选项：
```bash
./robot/piper_http_bridge/install.sh \
    --backend sdk \            # 默认 sdk；改 ros 走 ROS 桥
    --token 你的密钥 \          # Bearer 鉴权（强烈建议）
    --can can0 \               # CAN 接口名（默认 can0）
    --speed 30 \               # 默认速度 %（首次建议调低）
    --auto-enable \            # 启动即使能机械臂
    --http-port 8080 --tcp-port 9090
```

> 生产上**务必加 `--token`**，否则同网段任何人都能控制机械臂。

### 日常运维

```bash
./robot/piper_http_bridge/update.sh       # 改完代码后：同步 + (ros则重编译) + 重启服务
./robot/piper_http_bridge/uninstall.sh    # 卸载：停服务 + 删包
sudo systemctl status piper-bridge        # 服务状态
journalctl -u piper-bridge -f             # 实时日志
```

### 手动启动（不装 systemd 时）

SDK 后端：
```bash
./robot/piper_http_bridge/install.sh --no-service --token 你的密钥
sudo ip link set can0 up type can bitrate 1000000   # 若 CAN 未激活
python3 ./robot/piper_http_bridge/scripts/piper_sdk_server.py --can can0 --token 你的密钥 --speed 30
```

ROS 后端：
```bash
./robot/piper_http_bridge/install.sh --backend ros --no-service
source ~/piper_ros_ws/devel/setup.bash
roslaunch piper_http_bridge piper_http_bridge.launch can_port:=can0 auto_enable:=true
```

---

## 二、HTTP API

基础地址 `http://<香橙派IP>:8080`。若设了 token，所有请求加请求头
`Authorization: Bearer <token>`（TCP 则在每条 JSON 里加 `"token":"<token>"`）。

### 查询状态  `GET /state`
```bash
curl http://192.168.1.100:8080/state
```
返回当前关节角(度)、末端位姿(mm/度)、夹爪(mm)、机械臂状态码、是否使能。

### 健康检查  `GET /health`

### 统一命令入口  `POST /cmd`
body 是一个 JSON，必须含 `action` 字段。

**使能 / 失能**
```bash
curl -X POST http://192.168.1.100:8080/cmd -d '{"action":"enable"}'
curl -X POST http://192.168.1.100:8080/cmd -d '{"action":"disable"}'
```

**关节运动**（单位：度；speed 1~100；可选 gripper_mm）
```bash
curl -X POST http://192.168.1.100:8080/cmd -d '{
  "action":"joint_ctrl",
  "joints":[0, 30, -30, 0, 20, 0],
  "speed":30,
  "gripper_mm":40
}'
```

**末端位姿运动**（x,y,z 毫米；roll,pitch,yaw 度）
```bash
curl -X POST http://192.168.1.100:8080/cmd -d '{
  "action":"pose_ctrl",
  "x":200, "y":0, "z":200,
  "roll":0, "pitch":90, "yaw":0
}'
```

**夹爪**（position_mm 0~100；effort 0~5000 即 0~5 N·m）
```bash
curl -X POST http://192.168.1.100:8080/cmd -d '{"action":"gripper","position_mm":0,"effort":1000}'
```

**归零 / 停止 / 复位 / 阻塞**
```bash
curl -X POST http://192.168.1.100:8080/cmd -d '{"action":"go_zero","is_mit_mode":false}'
curl -X POST http://192.168.1.100:8080/cmd -d '{"action":"stop"}'    # 恒定阻尼落下
curl -X POST http://192.168.1.100:8080/cmd -d '{"action":"reset"}'   # 立即掉电落下
curl -X POST http://192.168.1.100:8080/cmd -d '{"action":"block_arm","block":true}'
```

成功返回 `{"ok":true, ...}`；参数错误返回 HTTP 400 + `{"ok":false,"error":"..."}`。

---

## 三、TCP API（换行分隔 JSON）

端口 `9090`。每发一行 JSON（必须以 `\n` 结尾），返回一行 JSON 结果。字段与 HTTP `POST /cmd` 完全相同。

```bash
# 用 nc 测试
echo '{"action":"state"}' | nc 192.168.1.100 9090
echo '{"action":"joint_ctrl","joints":[0,30,-30,0,20,0],"speed":30}' | nc 192.168.1.100 9090
```

Python 客户端示例：
```python
import socket, json
s = socket.create_connection(("192.168.1.100", 9090))
f = s.makefile("rw")
def cmd(**kw):
    f.write(json.dumps(kw) + "\n"); f.flush()
    return json.loads(f.readline())
print(cmd(action="enable"))
print(cmd(action="joint_ctrl", joints=[0,30,-30,0,20,0], speed=30))
print(cmd(action="state"))
```

---

## 四、Python 客户端（上位机直接调用）

`client/piper_client.py` 是**纯标准库**客户端（无需 pip 安装），同时支持 HTTP 与 TCP：

```python
from piper_client import PiperClient
arm = PiperClient("192.168.1.100", token="你的密钥")   # use_tcp=True 走 TCP
arm.enable()
arm.joint_ctrl([0, 30, -30, 0, 20, 0], speed=10)      # 度
arm.pose_ctrl(200, 0, 150, 0, 90, 0)                  # mm / 度
arm.gripper(40)                                       # mm
print(arm.state())
arm.go_zero()
arm.disable()
```

命令行用法：
```bash
python client/piper_client.py --host 192.168.1.100 --token 你的密钥 --state
python client/piper_client.py --host 192.168.1.100 --token 你的密钥 --joints 0,30,-30,0,20,0 --speed 10
python client/piper_client.py --host 192.168.1.100 --token 你的密钥 --demo   # 完整流程演示
```

## 五、本地干跑测试（无需真机/无需ROS/无需CAN）

三套测试都用桩顶替底层依赖，在本机（甚至 Windows）直接验证 HTTP/TCP 服务、
限位校验与单位换算：

```bash
python test/sdk_dry_run_test.py   # SDK 直连路径（25 项）
python test/dry_run_test.py       # ROS 桥接路径（32 项）
python test/client_test.py        # 客户端 HTTP+TCP 往返（23 项）
```

> 这只是逻辑自测，不代替真机联调。真机首次测试请用 `--speed 10` 做小幅关节运动，
> 确认工作空间无障碍。
