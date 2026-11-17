# What If Lab · 参赛作品部署仓库

两个作品共用一台云服务器、一个 LLM key：

| 服务 | 目录 | 端口 | 说明 |
|---|---|---|---|
| 同桌 | `whatif-cloud/` | 8000 | 行为判定 / 点评生成 / 现场看板 |
| AI房评师 | `solar-dashboard/` | 8001 | 采光采集存储 / 模型评估报告 |
| MQTT broker | eclipse-mosquitto 镜像 | 1883 | 设备上报入口 |

设备端固件不在本仓库部署职责内：`device-solar/`（采光 v2，烧 /usr）、
`device-proximity/`（同桌原型，烧 /usr）。

## 部署（火山引擎 Ubuntu，也适用 Lightsail/任意 Docker 主机）

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
git clone <本仓库> whatif && cd whatif

# 填 key：火山方舟 / 阿里 DashScope / NVIDIA NIM 任填其一（多个都填则按
# LLM_PROVIDER 或 ARK>DASHSCOPE>NVIDIA 顺序取）
cp .env.example .env && vim .env

docker compose up -d
docker compose ps          # 三个都应 running

# 验证
curl -s localhost:8000/api/llm/status | python3 -m json.tool | head -5
curl -s localhost:8001/api/overview
```

云安全组放行：1883、8000、8001（22 已开）。

## 切换模型供应商

改 `.env` 后 `docker compose up -d` 重建即可，代码零改动：

```
ARK_API_KEY=xxx                    # 火山方舟（当前）
DASHSCOPE_API_KEY=xxx              # 阿里
NVIDIA_API_KEY=nvapi-xxx           # NVIDIA NIM（申请下来后切这个）
LLM_PROVIDER=nvidia                # 同时有 key 时用它指定；不填按顺序取
LLM_MODEL=                         # 留空=启动时拉 /models 现场选，防猜错 ID
```

模型不可用时全部接口自动退回本地统计/兜底文案，输出带 `source` 字段
如实标注（nim / heuristic），看板同样显示——演示不中断，也不冒充。

## 诚实边界

- MQTT 匿名、看板无鉴权：黑客松演示取舍，README 与本表均如实声明
- 光敏数据在换 BH1750 前是相对亮度（mV），评估文案不出现 lux 绝对值
