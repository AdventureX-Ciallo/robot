# PiPER 夹取/出牌示教数据采集 SOP

> 用途：采集机械臂夹取并打出麻将牌的数据，用于动作模板、遥操复现、模仿学习或后续 VLA/LM 控制实验  
> 日期：2026-07-24

---

## 1. 是否必须采集示教数据？

取决于第一版控制路线。

| 路线 | 是否需要示教数据 | 说明 |
|---|---|---|
| 固定关节路点 / 规则动作 | 不强制 | 只需人工标定几个稳定点位 |
| 笛卡尔脚本动作 | 不强制 | 需要桌面坐标和路径模板 |
| 模仿学习 | 必须 | 需要成功轨迹作为训练数据 |
| VLA / LM 自动遥操 | 强烈建议 | LM 不适合直接输出连续控制，最好调用示教抽象出的技能 |
| 强化学习 | 不一定，但成本高 | 不推荐 MVP 使用 |

MVP 推荐：

```text
先采示教数据
但不要一开始就训练端到端模型
先把示教轨迹提炼成动作模板
```

---

## 2. 第一版应采什么数据？

每次示教记录一次完整“夹取并打出”动作。

### 2.1 必须记录

| 数据 | 说明 |
|---|---|
| 时间戳 | 每一帧/每一步动作时间 |
| 6 轴关节角 | joint_1 ~ joint_6 |
| 末端位姿 | x, y, z, roll, pitch, yaw |
| 夹爪开合 | gripper_mm / effort |
| 动作阶段 | approach / grasp / lift / place / release |
| 目标牌 ID | tile_id / label |
| 是否成功 | success / failed |
| 失败原因 | slip / miss / collision / dropped |

### 2.2 建议记录

| 数据 | 说明 |
|---|---|
| 相机截图 | 每个关键阶段保存一张 |
| 目标牌 bbox | 来自视觉识别 |
| 桌面坐标 | 目标牌映射后的桌面坐标 |
| 控制模式 | joint / pose / teach |
| 人工备注 | 哪一步不稳、是否抖动 |

---

## 3. 一条示教轨迹应该包含哪些阶段？

推荐分解为 9 个阶段：

```text
1. home_safe
2. move_above_tile
3. open_gripper
4. approach_tile
5. close_gripper
6. lift_tile
7. move_above_discard_area
8. release_tile
9. retreat_home
```

每个阶段保存一个关键路点。

---

## 4. 数据格式建议

每次示教保存一个 JSONL 或 JSON 文件。

### 4.1 单步记录示例

```json
{
  "timestamp": 1780000000.123,
  "episode_id": "demo_0001",
  "stage": "approach_tile",
  "tile": {
    "tile_id": "tile_05",
    "label": "3万",
    "bbox": [320, 180, 42, 58],
    "table_position_mm": [220, 120, 0]
  },
  "robot": {
    "joints_deg": [0, 35, -42, 0, 28, 0],
    "end_pose": {
      "x": 220,
      "y": 120,
      "z": 35,
      "roll": 0,
      "pitch": 90,
      "yaw": 0
    },
    "gripper_mm": 35
  },
  "control": {
    "mode": "joint",
    "speed": 10
  },
  "result": {
    "success": null,
    "note": ""
  }
}
```

### 4.2 整条示教记录

```json
{
  "episode_id": "demo_0001",
  "task": "grasp_and_discard",
  "tile_label": "3万",
  "success": true,
  "steps": [
    "home_safe",
    "move_above_tile",
    "open_gripper",
    "approach_tile",
    "close_gripper",
    "lift_tile",
    "move_above_discard_area",
    "release_tile",
    "retreat_home"
  ]
}
```

---

## 5. 要采多少条？

### 5.1 用于动作模板

如果只是提炼固定动作模板：

```text
20-30 条成功示教就有价值
```

### 5.2 用于简单模仿学习

如果要训练一个小模型学动作：

```text
100-300 条成功示教
+ 失败案例
+ 不同位置/角度/牌型
```

### 5.3 用于 VLA / LM 自动遥操

如果想让 LM/VLA 泛化控制：

```text
500+ 条更合理
```

但 MVP 不建议直接走这条路线。

---

## 6. 采集策略

第一轮只采固定场景：

| 批次 | 内容 | 数量 |
|---|---|---:|
| A | 同一位置夹取同一张牌 | 10 条 |
| B | 同一位置不同牌型 | 10 条 |
| C | 不同槽位同一动作 | 20 条 |
| D | 失败案例与边界情况 | 10 条 |

总计 50 条就能开始分析动作模板。

---

## 7. 如何使用这些数据？

### 7.1 第一阶段：提炼动作模板

不要一开始训练模型。先把成功示教转成关键路点：

```text
tile_position
  -> pre_grasp_offset
  -> grasp_offset
  -> lift_height
  -> discard_position
```

得到参数化动作：

```text
目标牌坐标 + 固定偏移 = 机械臂夹取轨迹
```

### 7.2 第二阶段：做动作回放

把示教轨迹回放出来：

```text
示教成功轨迹
  -> 平滑
  -> 降速
  -> 回放
  -> 复检
```

### 7.3 第三阶段：用于学习

如果回放稳定，再考虑训练：

```text
视觉状态 + 目标牌坐标 + 当前关节角
  -> 下一步动作 / 关键路点
```

---

## 8. LM/VLA 的正确用法

不要让 LM 直接输出连续 6 轴控制。

推荐让 LM 做高层决策：

```text
LM 选择技能：
  - move_to_tile
  - grasp_tile
  - lift_tile
  - place_to_discard

底层仍由规则轨迹/示教模板执行。
```

也就是说：

```text
LM/VLA 负责“选动作”
动作模板负责“怎么动”
```

---

## 9. MVP 推荐路线

```text
示教 50 条
  -> 提炼关键路点
  -> 写 grasp_and_discard 状态机
  -> 固定槽位回放
  -> 接视觉坐标
  -> 再考虑模仿学习
```

---

## 10. 采集时的注意事项

- 首次全部低速。
- 每条轨迹都记录成功/失败。
- 失败数据不要删，单独标记。
- 同一个动作至少重复 5 次。
- 夹爪闭合力度要记录。
- 不要边录边频繁切控制模式。
- 每次换牌桌/相机高度后重新标定。

