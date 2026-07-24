# 麻将牌 YOLO 识别模型训练 SOP

> 负责人：视觉模型训练  
> 用途：训练麻将牌识别模型，供 HUSKYLENS 2 / GX10 / 香橙派侧推理使用  
> 日期：2026-07-24

---

## 1. 训练目标

第一阶段模型只解决一件事：

```text
识别每张麻将牌的牌型和图像位置
```

输出：

```json
{
  "label": "3万",
  "class_id": 2,
  "bbox": [x_center, y_center, width, height],
  "confidence": 0.94
}
```

不要把“算牌策略”放进 YOLO。YOLO 只负责看牌，弃牌决策由牌效算法负责。

---

## 2. 类别规划

### 2.1 第一轮不要直接上 34 类

推荐分三轮：

| 阶段 | 类别数 | 目标 |
|---|---:|---|
| A | 5 类 | 跑通训练/部署链路 |
| B | 10 类 | 验证相似牌识别能力 |
| C | 34 类 | 完整基础麻将牌识别 |

### 2.2 34 类编码

内部英文短码：

```text
1m 2m 3m 4m 5m 6m 7m 8m 9m
1p 2p 3p 4p 5p 6p 7p 8p 9p
1s 2s 3s 4s 5s 6s 7s 8s 9s
east south west north white green red
```

中文显示：

```text
1万 2万 ... 9万
1筒 2筒 ... 9筒
1条 2条 ... 9条
东 南 西 北 白 发 中
```

建议 `data.yaml` 里先用英文短码，前端再做中文映射。

---

## 3. 数据采集

### 3.1 第一轮采集目标

第一轮先采：

| 阶段 | 图片数 | 说明 |
|---|---:|---|
| A | 200 张 | 5 类测试集 |
| B | 500 张 | 10 类扩展 |
| C | 1500-3000 张 | 34 类完整训练 |

注意：一张图里有 10-14 张牌，所以 1500 张图可以提供大量实例。

### 3.2 必拍场景

每类牌都要覆盖：

- 正常光照。
- 偏暗光照。
- 轻微反光。
- 轻微旋转。
- 牌间距大。
- 牌间距小。
- 固定牌架。
- 平铺桌面。
- 相机固定俯视。
- 现场演示距离和角度。

### 3.3 不要只拍“干净图”

必须拍真实失败场景：

- 手遮挡边缘。
- 机械臂经过后产生阴影。
- 牌面反光。
- 同一类牌在画面不同位置。
- 背景有桌布、牌架、夹爪、手臂。

---

## 4. 标注规范

### 4.1 标注工具

推荐：

1. CVAT
2. Label Studio
3. Roboflow
4. LabelImg

### 4.2 标注规则

- 每张可见麻将牌都画 bbox。
- bbox 框住整张牌，不只框牌面字符。
- 类别标注为牌型，不是“mahjong_tile”。
- 遮挡超过 50% 的牌可以不标。
- 反光但可人工识别的牌要标。
- 模糊到人也看不清的牌不要标。

### 4.3 数据划分

```text
train: 80%
val: 15%
test: 5%
```

不要把同一组连拍照片全放进 train，否则验证集会虚高。

---

## 5. 数据集结构

Ultralytics YOLO 推荐结构：

```text
mahjong_yolo/
  images/
    train/
    val/
    test/
  labels/
    train/
    val/
    test/
  data.yaml
```

`data.yaml` 示例：

```yaml
path: D:/Documents/Projects/AGILE/datasets/mahjong_yolo
train: images/train
val: images/val
test: images/test

names:
  0: 1m
  1: 2m
  2: 3m
  3: 4m
  4: 5m
```

34 类时继续补全 `names`。

---

## 6. 训练环境

### 6.1 安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install ultralytics
```

验证：

```bash
yolo checks
```

### 6.2 第一轮训练命令

先用轻量模型：

```bash
yolo detect train model=yolo11n.pt data=data.yaml imgsz=640 epochs=100 batch=-1 device=0
```

如果 `yolo11n.pt` 不适配 HUSKYLENS 2 转换流程，就切：

```bash
yolo detect train model=yolov8n.pt data=data.yaml imgsz=640 epochs=100 batch=-1 device=0
```

### 6.3 训练策略

第一轮：

```text
model: yolo11n / yolov8n
imgsz: 640
epochs: 100
batch: auto
```

如果误识别很多：

```text
imgsz: 960
epochs: 150
model: yolo11s / yolov8s
```

---

## 7. 验证与评估

训练后验证：

```bash
yolo detect val model=runs/detect/train/weights/best.pt data=data.yaml imgsz=640
```

预测测试图：

```bash
yolo detect predict model=runs/detect/train/weights/best.pt source=sample_images imgsz=640 save=True
```

第一阶段通过标准：

| 指标 | 阶段 A/B | 阶段 C |
|---|---:|---:|
| 单牌识别准确率 | > 90% | > 95% |
| 目标牌漏检率 | < 10% | < 5% |
| 13-14 张手牌漏检 | 不超过 2 张 | 不超过 1 张 |
| 现场推理延迟 | 可接受 | 可接受 |

实际 Demo 最重要的是：

```text
AI 推荐的那张牌不能识别错
```

所以要单独统计“目标牌识别成功率”。

---

## 8. 导出与部署

### 8.1 导出 ONNX

```bash
yolo export model=runs/detect/train/weights/best.pt format=onnx imgsz=640
```

### 8.2 部署到 HUSKYLENS 2

按官方路线：

1. 使用 Mind+ 或 Python 训练模型。
2. 转换为 HUSKYLENS 2 支持的部署包。
3. 将模型包放入设备 `\storage\installation_package`。
4. 在设备上选择 Local Installation。
5. 用真实手牌场景验证。

注意：HUSKYLENS 2 是否支持当前 YOLO 版本和导出格式，要以实际转换工具为准。

### 8.3 备用部署

如果 HUSKYLENS 2 部署不顺：

```text
GX10 本地推理
  -> 后端输出识别 JSON
  -> 香橙派/机械臂执行
```

---

## 9. 每天训练记录

建议每次训练记录：

```text
日期：
模型：
类别数：
图片数：
实例数：
训练命令：
mAP：
主要误识别：
下一步改进：
```

示例：

```text
2026-07-24
model=yolo11n
classes=10
images=520
epochs=100
主要问题：3万/5万误识别，反光图漏检
下一步：补拍万字牌近景和反光场景
```

---

## 10. 给训练负责人的执行清单

今天先做：

1. 确定第一轮 5 类牌。
2. 拍 200 张真实场景图。
3. 用 CVAT/Label Studio 标注。
4. 导出 YOLO 格式。
5. 写 `data.yaml`。
6. 训练 `yolo11n` 或 `yolov8n`。
7. 保存 `best.pt`、训练曲线、预测结果图。
8. 给后端/前端一份类别 ID 映射表。

---

## 11. 参考资料

- Ultralytics Train 文档：https://docs.ultralytics.com/modes/train/
- Ultralytics Dataset 文档：https://docs.ultralytics.com/datasets/detect/
- Ultralytics CLI 文档：https://docs.ultralytics.com/usage/cli/
- HUSKYLENS 2 自训练模型部署文档：https://wiki.dfrobot.com/sen0638/docs/22604

