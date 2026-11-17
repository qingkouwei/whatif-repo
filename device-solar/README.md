# 采光采集固件 v2 · Windows 烧录与验证

对着 `QuecDuino真机调试速查.md` 写的，那条文档里踩过的坑这里全部沿用结论。

## 这次改了什么（为什么"之前的光照有点问题"）

| 问题 | 病根 | v2 的处理 |
|---|---|---|
| 强光读数不涨 | ADC 超量程削平，分压缺失或比例不对 | 分压比进 config，饱和顶值由 calib.py 实测，不再用理论 1200 |
| 白天曲线顶部平了 | 量程外差异全丢，之前没有量化指标 | 每秒记饱和计数，近一小时饱和率 >20% 上报并打日志 |
| 日账数字可疑 | 桶内按秒计数、字段名却叫 *_min | 内部统一按秒，出口换算分钟，注释写明 |
| 自然光/灯光混一起 | 分离只在云端做，原始数据没拆 | 设备端双分量分类，natural_mv / lamp_mv 分开上报（看板契约字段） |
| 极性到底朝哪 | 官方注释与套件板实测相反，两处说法冲突 | 不猜。calib.py 三段测试现场定，LIGHT_INVERT 按结论改 |

## 文件清单（烧之前核对）

```
firmware-solar/
├── main.py          入口，必须放 /usr/main.py（只有这个名字自启）
├── config.py        上板前要先改：MQTT_HOST / 分压电阻 / 饱和顶值
├── calib.py         标定脚本，单独执行，不进自启
└── lib/
    ├── light.py     采集与双分量分类
    ├── queue.py     断网离线队列（NDJSON 追加写）
    ├── uplink.py    MQTT 上报 + 下行指令 + 补报
    └── aht20.py     温湿度（busy 位容错）
```

## Windows 烧录步骤

1. QFlash 烧固件（若板子已刷过 QuecPython 可跳过；判据：REPL 里 `ATI` 返回带 `_OCPU_QPY`）。
2. QPYcom 连 REPL。**端口按 COM 号认，别按名字找**——烧完出现的"USB 串行设备 (COMx)"就是 REPL 口，名字回落成通用名是正常的；`Quectel QDLoader Port` 消失也正常。
3. 文件页把上面七个文件按目录结构传到 `/usr`（lib 下的传 `/usr/lib/`）。**上传不等于运行**，只有 `/usr/main.py` 会开机自启。
4. 先单独跑标定（下一步），再重启进主程序。

## 上板验证顺序（约 20 分钟）

### 第 1 步 跑 calib.py（决定两个关键 config 值）

QPYcom 文件页右键 calib.py「执行」。按它屏幕提示做三段各 30 秒：
正常光静置 → 完全捂死 → 手电/对窗强光。跑完直接给结论：

- **极性**：光强读数高 → `LIGHT_INVERT = False`；光强读数低 → `True`。
  注意官方套件文档和文章续2的实测说法相反，以你板子这次的输出为准。
- **SATURATION_MV**：填强光段顶值减 2% 余量。若强光读数仍 >1200，
  说明分压不够，先换电阻（R1 加大）重跑标定，别急着往下。
- 分压电阻总值 ≥100k（官方硬件手册要求），100k/100k 是默认推荐。

改 `config.py`：`SATURATION_MV`、`LIGHT_INVERT`，重传到 `/usr`。

### 第 2 步 校时

主循环启动会检查时间。REPL 里看打印，或者手动：

```python
import utime; print(utime.localtime())     # 年份应 ≥2025
import ntptime; ntptime.settime()          # 不对就强制同步一次
```

时间不对，"白天 7-18 点"的昼夜分会整体错位，这是日账可信度的前提。

### 第 3 步 填云端地址再启主程序

`config.py` 填 `MQTT_HOST`（公网地址或 EMQX 公共 broker `broker.emqx.io` 先试链路）。
重启方式：QPYcom 断连重连，或 REPL `Ctrl+A → Ctrl+D → Ctrl+B` 后
`import main`。每 5 分钟应打一条：

```
[11-16 13:05:00] 上报 nat=520 lamp=0 sun1h=18.0min 饱和=0% 积压=0
```

### 第 4 步 三个现场测试

| 动作 | 预期 |
|---|---|
| 手电慢慢靠近光敏 | 读数连续上升不卡顶（说明不再削平）；冲顶后 REPL 应报饱和警告 |
| 房间开灯再关灯 | lamp 分量出现一段、natural 不受污染 |
| 拔 MQTT 网线/填错 host 等 10 分钟 | 积压计数上涨；改对地址后自动补报，看板一次收到 backfill 数据 |

### 第 5 步 看板对接

`dashboard/app.py` 的 `MQTT_UP_TOPIC = /solar_ledger/+/up` 与设备 topic
格式一致，payload 字段（natural_mv / lamp_mv / sun_min / naive_sun_min /
temp / rh / seq / csq）已对齐 `save_reading()` 的取数逻辑，
设备 ID 需在看板 devices 表登记到对应房间。本地起服务验证：

```bash
cd dashboard && python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

（需本机 mosquitto 或把 app.py 的 MQTT_HOST 指向公共 broker。）

## 已知没做的

- 未上真机。所有代码只过了语法编译和逻辑设计，第一次插板以 calib.py 的
  实测结论为准，它可能推翻上面任何一条预期——这就是把它排在第 1 步的原因。
- 温湿度接不上不影响主线，payload 里 temp/rh 会是 null。
- 灯光分类用的时间窗+爬升率是启发式，展厅/台灯场景够用，
  日落前后自然光突变可能被误判成灯事件，看板看到 lamp 在傍晚有尖峰就是它。

## 换 BH1750 数字光照传感器（要"精确照度数值"才需要）

KY-018 的极限：光敏电阻强非线性、光谱响应偏蓝、个体差异 ±20%，
标定只能修一个点修不了整条曲线，永远只能给"相对亮度"。
BH1750（十几块，I2C 接口）内置标定，直出 lux：1~65535 lx、
分辨率 1lx、典型精度 ±20%。室内采光评估的性价比答案。

驱动已写好：`lib/bh1750.py`，接口与 light.py 对齐（sample/snapshot
契约不变），main.py 切换只改构造那一段：

```python
# 原：from lib.light import LightSensor, cfg_divider
#     light = LightSensor(adc, config)
# 改：from machine import I2C
#     from lib.bh1750 import BH1750Sensor
#     light = BH1750Sensor(I2C(I2C.I2C0, I2C.STANDARD_MODE), config)
```

阈值换 config 里的 lux 组（不填用代码内缺省）：
SUN_ACTIVE_LUX=200、LAMP_RISE_LUX=300、LAMP_NIGHT_ON_LUX=50。
AHT20 与 BH1750 共用 I2C0，地址不同（0x38/0x23）不冲突。

数据口径：payload 的 natural_mv/lamp_mv 两列在 BH1750 模式下装的是
lux 不是 mV——看板展示要在设备登记处注明单位，或者干脆让 payload
带 "unit": "lux" 字段（未做，接看板时定）。
满量程 65535：室内一般到不了，真恒定在 65535 就是超量程，
和当年 KY-018 削平同类，判读方法相同。
想再往上进 accuracy：买一支几十块的照度计，同点对比两三个亮度档，
给 BH1750 做线性修正——这才够到"和官方文档/规范对得上"的门槛。

