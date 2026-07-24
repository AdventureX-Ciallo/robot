# HUSKYLENS 2 视觉模块对接调研与集成说明

> 用途：给视觉/后端/香橙派同学对接二哈视觉 2  
> 日期：2026-07-24  
> 状态：初步调研，待真机验证

---

## 1. 结论

HUSKYLENS 2 可以作为第一版视觉输入方案尝试。官方文档显示它支持自训练模型部署，并支持通过 Mind+ 无代码训练、Mind+ 本地转换、Python 训练 YOLO 后部署三种路线。

对本项目最关键的点：

1. **支持自训练模型部署**：官方文档说明可训练并部署自定义视觉识别模型。
2. **支持 YOLOv8n / YOLO11n 路线**：官方本地转换说明提到 Mind+ 训练的 `yolov8n` 和 `yolo11n` 可转换部署。
3. **通信支持 I2C / UART**：官方 Arduino 示例说明默认是 I2C，如果使用 UART，需要在 HUSKYLENS 2 设置里手动切换协议。
4. **输出结构适合项目使用**：可读取识别框 block，包含目标 ID、中心坐标、宽高等信息。

---

## 2. 推荐系统连接

```text
HUSKYLENS 2
  - 运行麻将 YOLO 识牌模型
  - 输出每张牌的 class_id / bbox / confidence

香橙派
  - 通过 I2C 或 UART 读取识别结果
  - 维护当前手牌状态
  - 调用决策模块
  - 生成目标牌动作请求

电脑端 / GX10
  - 训练模型
  - 管理数据集
  - 显示前端控制台
  - 必要时作为备用推理端
```

第一版建议优先用 **UART 115200** 或官方库支持最稳定的模式。若 UART 调试遇到问题，再切 I2C。官方资料显示 HUSKYLENS 2 默认 I2C，因此使用 UART 时必须在设备设置中切换协议。

---

## 3. 训练与部署路线

### 3.1 快速路线：Mind+ 无代码训练

适合快速验证二哈视觉 2 能不能识别麻将牌。

流程：

1. 用真实麻将牌采集图片。
2. 在 Mind+ 中做 Object Detection 数据集。
3. 训练模型。
4. 点击部署到 HUSKYLENS 2。
5. 在设备屏幕上验证识别效果。

优点：快。  
缺点：可控性较弱，后续模型管理不够工程化。

### 3.2 工程路线：YOLO 数据集 + Python 训练 + 部署

适合正式项目。

数据集格式：

```text
dataset/
  images/
    train/
    val/
  labels/
    train/
    val/
  data.yaml
```

训练建议：

```bash
yolo detect train model=yolov8n.pt data=data.yaml imgsz=640 epochs=100
```

部署步骤：

1. 训练得到 YOLOv8n / YOLO11n 模型。
2. 导出 ONNX。
3. 使用官方模型转换/打包工具。
4. 将生成的模型 ZIP 拷贝到 HUSKYLENS 2 的 `\storage\installation_package`。
5. 在 HUSKYLENS 2 上进入 Model Installation，选择 Local Installation。

---

## 4. 读取数据格式

官方 Arduino 库的核心调用方式为：

```cpp
huskylens.request();
while (huskylens.available()) {
  HUSKYLENSResult result = huskylens.read();
}
```

官方库说明中，`request()` 用于请求所有 blocks 和 arrows；`requestBlocks()` 用于只请求 blocks；`read()` 返回 `HUSKYLENSResult`。

对本项目，重点只需要 blocks。一个麻将牌识别结果建议统一转成：

```json
{
  "id": 1,
  "class_id": 12,
  "label": "3万",
  "x_center": 320,
  "y_center": 180,
  "width": 42,
  "height": 58,
  "confidence": 0.94,
  "source": "huskylens2",
  "timestamp": 1780000000
}
```

注意：官方基础库常见 block 字段包括目标 ID、中心 x/y、宽高；是否能直接读到 confidence，需要真机和当前模型接口确认。如果无法读 confidence，后端可先用 `confidence: null`，或从设备侧其他接口扩展。

---

## 5. 香橙派侧建议接口

建议在香橙派侧写一个视觉采集服务，把 HUSKYLENS 2 原始结果转成统一 JSON。

### 5.1 视觉服务输出

```json
{
  "frame_id": 1024,
  "device": "huskylens2",
  "tiles": [
    {
      "tile_id": "tile_001",
      "class_id": 12,
      "label": "3万",
      "bbox": {
        "x_center": 320,
        "y_center": 180,
        "width": 42,
        "height": 58
      },
      "table_position": null,
      "confidence": null
    }
  ]
}
```

### 5.2 后端处理流程

```text
读取 HUSKYLENS 2 blocks
  -> class_id 映射为麻将牌 label
  -> bbox 映射为图像坐标
  -> 通过标定转换为桌面坐标
  -> 输出当前手牌列表
```

---

## 6. 类别编码建议

第一版 34 类基础牌：

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

后端内部使用英文短码，前端显示中文。

---

## 7. 第一轮真机验证任务

### 7.1 视觉验证目标

1. HUSKYLENS 2 能否部署自训练模型。
2. 香橙派能否读取 blocks。
3. 是否能同时识别 13-14 张手牌。
4. 识别结果是否包含类别 ID 和位置。
5. 延迟是否满足演示。

### 7.2 测试集

先不要直接做 34 类。建议：

| 阶段 | 类别数 | 图片数 | 目标 |
|---|---:|---:|---|
| Test A | 5 类 | 200 张 | 验证训练/部署/读取链路 |
| Test B | 10 类 | 500 张 | 验证多类识别稳定性 |
| Test C | 34 类 | 1500+ 张 | 验证完整手牌识别 |

### 7.3 通过标准

第一轮通过标准：

- 能稳定读出每张牌的 ID 与 bbox。
- 固定光照下 10 类识别准确率达到 90% 以上。
- 单帧 13-14 张牌漏检不超过 2 张。
- 数据能被后端转成统一 JSON。

---

## 8. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 34 类细粒度识别难 | 误识别相似牌 | 先分阶段训练；增加近景数据；统一光照 |
| 多张牌同时识别漏检 | 手牌状态错误 | 使用固定牌架，增加牌间距 |
| 置信度字段无法直接读取 | 前端无法展示置信度 | 先展示 ID/bbox；后续从接口扩展 |
| UART/I2C 调试不稳定 | 香橙派读取失败 | 两种协议都预留；优先用官方库 |
| 模型转换失败 | 无法部署到二哈视觉 2 | 保留 GX10/PC 推理作为备选 |

---

## 9. 参考资料

- HUSKYLENS 2 自训练模型部署官方文档：https://wiki.dfrobot.com/sen0638/docs/22604
- HUSKYLENS 2 Arduino 示例官方文档：https://wiki.dfrobot.com/sen0638/docs/22636
- HUSKYLENS Arduino 库 API：https://github.com/HuskyLens/HUSKYLENSArduino
- PyHuskyLens Python 库：https://github.com/AntonsMindstorms/pyhuskylens
- DFRobot HUSKYLENS 2 产品页：https://www.dfrobot.com/product-2995.html

