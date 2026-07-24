# piper_http_bridge

为 **AgileX Piper 6 轴机械臂** 暴露一个 **HTTP / TCP 控制端口** 的 ROS 包。
面向 **香橙派3B（Orange Pi 3B，ARM64，无头 / headless）** 部署。

它**不直接操作 CAN**，而是桥接官方 `piper_ros` 控制节点
（`piper_ctrl_single_node.py`）已经发布/订阅/提供的 topic 与 service，
在外面套一层任何语言都能调用的 **HTTP/JSON** 与 **TCP/JSON** 接口。

```
客户端(curl/python/任何语言)
        │  HTTP :8080  /  TCP :9090   (JSON)
        ▼
┌───────────────────────┐
│ piper_http_bridge_node │   ← 本包（单位换算 + 限位校验 + 鉴权）
└─────────┬─────────────┘
          │ ROS topic / service
          ▼
┌────────────────────────────┐
│ piper_ctrl_single_node (官方) │  ← 走 SocketCAN
└─────────┬──────────────────┘
          │ CAN 1Mbps (can0)
          ▼
       Piper 机械臂
```

## 单位约定（对客户端）

| 量 | 客户端单位 | 说明 |
|----|-----------|------|
| 关节角 joint_ctrl | **度 (deg)** | 6 个值，内部转成 rad 发给 ROS |
| 末端位置 x,y,z | **毫米 (mm)** | 内部转成 m |
| 末端姿态 roll,pitch,yaw | **度 (deg)** | 内部转成 rad |
| 夹爪 gripper | **毫米 (mm)** | 0~80，内部转成 m |
| 速度 speed | 1~100 (%) | |

关节限位（度，软件侧校验，超出直接拒绝）：J1 ±150，J2 0~180，J3 -170~0，J4 ±100，J5 ±70，J6 ±120。

---

## 一、在香橙派3B 上部署

前提：已经按官方文档装好 `piper_ros`（含 `piper` / `piper_msgs` 包），CAN 已能在 `can0` 上激活。

```bash
# 1. 把本包放进你的 catkin 工作空间 src 下（假设工作空间是 ~/piper_ros）
cd ~/piper_ros/src
# 复制 piper_http_bridge 目录到这里，然后：
cd ~/piper_ros
catkin_make
source devel/setup.bash

# 2. 激活 CAN（1 Mbps）
bash can_activate.sh can0 1000000

# 3. 一键启动（控制节点 + HTTP/TCP 桥）
roslaunch piper_http_bridge piper_http_bridge.launch \
    can_port:=can0 auto_enable:=true http_port:=8080 tcp_port:=9090
```

> 生产上建议加一个 `--token`（launch 参数 `token:=你的密钥`）做 Bearer 鉴权，
> 否则同网段任何人都能控制机械臂。

### 开机自启（无头）

```bash
sudo cp scripts/piper-bridge.service /etc/systemd/system/
# 编辑该文件里的 用户名 / 工作空间路径 / can_activate.sh 路径
sudo systemctl daemon-reload
sudo systemctl enable --now piper-bridge.service
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

**夹爪**（position_mm 0~80；effort 0~5000 即 0~5 N·m）
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

## 四、本地干跑测试（无需真机/无需ROS）

`test/dry_run_test.py` 用 stub 顶替 rospy 与消息类型，在本机（甚至 Windows）
直接验证 HTTP/TCP 服务、限位校验与单位换算逻辑：

```bash
python test/dry_run_test.py
```

> 注意：这只是逻辑自测，不代替真机联调。真机首次测试请先 `auto_enable`
> 并用小 `speed`（如 10）做小幅关节运动，确认工作空间无障碍。
