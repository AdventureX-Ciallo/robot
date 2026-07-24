# π0.5 / openpi 调研说明

> 主题：评估 Physical Intelligence 的 π0.5 是否可直接用于 AGILE Mahjong Robot  
> 日期：2026-07-24  
> 结论：可研究、可做后续 VLA 路线，但不建议作为第一版主链路

---

## 1. 结论

π0.5 是 Physical Intelligence 发布的视觉-语言-动作模型（VLA, Vision-Language-Action），目标是让机器人根据图像和语言指令输出动作。`openpi` 仓库已经开源了 π0、π0-FAST、π0.5 相关模型和代码，并提供预训练/微调 checkpoint。

但它**不能直接替代我们第一版的 YOLO + 牌效 + 动作模板**。

原因：

1. 它输出的是机器人动作，不是麻将牌类别和 bbox。
2. 已有专家 checkpoint 主要面向 DROID、ALOHA、LIBERO 等平台，不是 PiPER。
3. 要接 PiPER，需要做观测格式、动作空间、归一化、相机输入和控制接口适配。
4. 如果要稳定完成“夹取麻将牌并打出”，仍然需要我们采 PiPER 示教数据并做 fine-tuning 或动作模板。

推荐定位：

```text
第一版：YOLO 识牌 + 牌效算法 + PiPER 动作模板
后续研究：openpi / π0.5 做 VLA 自动操作实验
```

---

## 2. π0.5 是什么

公开资料显示：

- π0 是 Physical Intelligence 的通用机器人基础策略模型。
- π0.5 在 π0 基础上增强了 open-world generalization。
- π0.5 使用多机器人数据、高层子任务预测、语言指令、网页数据等异构数据进行训练。
- 论文展示了移动机械臂在新厨房/卧室中完成长程整理任务。

它更接近：

```text
给机器人一句话任务
  -> 模型看图
  -> 输出动作序列
```

而不是：

```text
识别麻将牌类型
  -> 算出该打哪张
```

---

## 3. openpi 仓库能力

官方 `openpi` README 显示：

- 包含 π0、π0-FAST、π0.5 三类模型。
- 提供 base model checkpoints。
- 提供 fine-tuned checkpoints，例如 `pi05_droid`、`pi05_libero`。
- 支持推理、fine-tuning、policy server。
- 提供远程推理方式。

硬件需求方面，官方 README 写到：

| 模式 | 显存需求示例 |
|---|---|
| Inference | > 8 GB，示例 RTX 4090 |
| LoRA Fine-Tuning | > 22.5 GB，示例 RTX 4090 |
| Full Fine-Tuning | > 70 GB，示例 A100/H100 |

因此，GX10 如果显存/统一内存足够，可以用于研究和推理实验；但这不是香橙派或二哈视觉 2 能直接跑的东西。

---

## 4. 能不能直接用？

### 4.1 不能直接用于“识牌”

π0.5 不是目标检测模型。它不会像 YOLO 一样稳定输出：

```json
{
  "label": "3万",
  "bbox": [x, y, w, h]
}
```

所以识牌仍然建议用 YOLO。

### 4.2 不能直接用于 PiPER 控制

openpi 现成 fine-tuned checkpoint 不是为 PiPER 训练的。直接接 PiPER 需要解决：

- PiPER 的 joint/action space。
- 相机观测字段。
- 夹爪动作字段。
- 坐标系。
- 动作频率。
- 安全限制。
- 失败恢复。

官方 README 也提醒：模型原本面向他们自己的机器人，适配其他平台未必成功。

### 4.3 可以作为后续研究路线

它可以用于：

- 研究 VLA 如何接机械臂。
- 用示教数据 fine-tune。
- 做高层动作选择。
- 作为 LM/VLA 组的实验方向。

---

## 5. 如果要试，怎么试？

建议不要直接上真机。

### 5.1 第一步：离线推理

在 GX10/PC 上跑 openpi 的 dummy inference。

目标：

```text
确认模型能下载
确认环境能跑
确认 policy server 能起来
```

### 5.2 第二步：看输入输出格式

理解它需要什么 observation：

```text
camera image
robot state
language prompt
```

以及输出什么 action：

```text
action_chunk
```

### 5.3 第三步：不要直接接真机，先接动作模板

让 VLA/LM 输出高层技能：

```json
{
  "skill": "grasp_and_discard",
  "target": "3万"
}
```

底层仍然由我们自己的 `grasp_and_discard()` 状态机执行。

### 5.4 第四步：如果有价值，再采示教数据 fine-tune

使用 PiPER 示教数据转 LeRobot dataset，再尝试 fine-tune。

---

## 6. 对本项目的建议

| 问题 | 建议 |
|---|---|
| 第一版识牌 | 继续 YOLO |
| 第一版算牌 | 继续牌效算法 |
| 第一版夹取/出牌 | 继续动作模板/状态机 |
| LM/VLA 实验 | 可以让同学试 openpi / π0.5 |
| 是否直接替代主链路 | 不建议 |
| 是否需要示教数据 | 如果要 fine-tune，需要 |

---

## 7. 参考资料

- Physical Intelligence π0.5 Blog：https://www.physicalintelligence.company/blog/pi05
- π0.5 论文：https://arxiv.org/abs/2504.16054
- openpi GitHub：https://github.com/Physical-Intelligence/openpi
- π0 Blog：https://www.physicalintelligence.company/blog/pi0

