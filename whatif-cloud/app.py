# -*- coding: utf-8 -*-
"""
What If Lab · 距离即权限 —— 云端答案与统计服务

职责：
  1. 按话题生成三档文案，真正调用 NVIDIA NIM（OpenAI 兼容端点）
  2. 接收设备 MQTT 状态上报，落库，供看板与路演后复盘
  3. 记录每次观众访问的最远档位与停留时长

关于"AI"这两个字的诚实约定
------------------------------------------------------------
上一个项目里，看板那块叫「AI 解读」的东西其实是前端字符串拼接。
这次不重犯。规则写死在这里：

  - 每条文案都带 source 字段：nim（模型生成）/ fallback（本地兜底）
  - fallback 不参与演示卖点。若预取失败，设备走的是缓存或内置文案，
    看板会显式标出"当前为兜底文案，非模型生成"
  - 评委问"用的什么模型"，接口 /api/ai/status 直接答得出模型 ID、
    端点、最近一次调用耗时和成功次数

模型 ID 我没有硬编码一个我以为存在的名字。NIM 目录会变，猜错就是
现场 404。启动时调 /v1/models 列出真实可用模型再选，见 verify_models()。

NIM 侧的硬约束（决定了这里的限流写法）：
  - 免费额度按账号发积分，40 RPM 量级，超出直接 429
  - 一个话题只生成一次并缓存，绝不在请求路径里反复打模型
"""

import asyncio
import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import behavior_judge

try:
    import paho.mqtt.client as mqtt
except ImportError:  # 本地无 broker 时也能起服务，只是收不到上报
    mqtt = None

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# LLM 供应商统一走 llm_provider（ark/dashscope/nvidia 三家 OpenAI 兼容，
# 环境变量切换）。NIM_BASE/NIM_KEY/NIM_MODEL 保留同名变量，值由
# provider 解析结果覆盖，行为与旧版一致：未配置时全部走本地兜底。
import llm_provider

PROVIDER, NIM_BASE, NIM_KEY, NIM_MODEL = llm_provider.resolve()

PORT = int(os.environ.get("CLOUD_PORT", "8000"))
DB_PATH = os.environ.get("CLOUD_DB", os.path.join(os.path.dirname(__file__), "whatif.db"))

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_STATE = "/whatif/+/state"

# 话题：设备只报编号，语义全在这里，改话题不用重新烧固件
TOPICS = {
    1: {
        "name": "这个展位现场的人群行为",
        "hint": "你在描述一个放在展会摊位上的装置观察到的人流行为。"
                "它靠超声波测观众离它有多近。",
    },
    2: {
        "name": "这间屋子里的空气与光线适不适合人待着",
        "hint": "你在描述一个室内装置的温湿度和光照读数。"
                "注意展厅的灯是 LED，不是日光，别把它当自然采光分析。",
    },
    3: {
        "name": "凑近我的这些人的性格",
        "hint": "你在根据观众凑近的程度和停留时长，半开玩笑地推测他们的性格。"
                "要有趣，但别说得像真的心理测评。",
    },
}
DEFAULT_TOPIC = 1

# 三档的写作规格。这是这个作品的表达核心：不是"多少字"，
# 而是"信息量的落差要让人一眼看出它在藏着不说"。
TIER_SPEC = {
    0: {"label": "拒绝", "chars": "不超过 20 个字",
        "brief": "观众离得远，装置只表示它看到有人，但不给任何内容。"
                 "要冷淡，带一点傲慢，让人想凑近。"},
    1: {"label": "一句", "chars": "正好一句话，25 到 45 个字",
        "brief": "观众在中距离。只给一个结论，不给理由、不给数据、不给后半句。"
                 "要让读者明显感觉话被截断了，还有东西没说。"},
    2: {"label": "完整", "chars": "80 到 160 个字，两到四句",
        "brief": "观众凑到跟前。给出完整的观察：带上具体数字和一句自己的判断，"
                 "允许有一个不那么好听的结论。"},
}

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

_db: sqlite3.Connection = None
_cache: dict = {}           # (topic_id, day) -> {"answers":..., "source":...}
_ai_stats = {"calls": 0, "ok": 0, "fail": 0, "last_ms": None,
             "last_error": None, "model": None, "available": []}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS states (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT NOT NULL,
  ts TEXT NOT NULL,
  seq INTEGER,
  tier INTEGER,
  dist_cm REAL,
  k_divisor REAL,
  lux_mv INTEGER,
  cache_cov INTEGER,
  csq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_states_ts ON states(ts);
CREATE INDEX IF NOT EXISTS idx_states_tier ON states(tier);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT NOT NULL,
  ts TEXT NOT NULL,
  max_tier INTEGER NOT NULL,
  dwell_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
  topic_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  tier INTEGER NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (topic_id, day, tier)
);
"""


def init_db():
    global _db
    _db = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    _db.executescript(SCHEMA)
    _db.commit()


# ---------------------------------------------------------------------------
# NIM 调用
# ---------------------------------------------------------------------------

async def verify_models(client: httpx.AsyncClient):
    """拉取真实模型列表。猜模型 ID 是最容易在现场翻车的事。"""
    if not NIM_KEY:
        _ai_stats["last_error"] = "未设置 LLM key（ARK/DASHSCOPE/NVIDIA 任一）"
        return []
    try:
        r = await client.get(f"{NIM_BASE}/models",
                             headers={"Authorization": "Bearer %s" % NIM_KEY},
                             timeout=15)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        _ai_stats["available"] = ids
        return ids
    except Exception as e:
        _ai_stats["last_error"] = "模型列表获取失败: %s" % e
        return []


def pick_model(available):
    """从可用列表里按优先级挑一个对话模型。三家统一走 llm_provider。"""
    if NIM_MODEL and (not available or NIM_MODEL in available):
        return NIM_MODEL
    return llm_provider.auto_pick(available) or (available[0] if available else None)


async def nim_chat(client, model, messages, max_tokens=700, temperature=0.85):
    """一次 NIM chat completion，返回文本；失败抛异常。"""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.95,
    }
    t0 = time.monotonic()
    _ai_stats["calls"] += 1
    try:
        r = await client.post(f"{NIM_BASE}/chat/completions",
                              headers={"Authorization": "Bearer %s" % NIM_KEY,
                                       "Content-Type": "application/json",
                                       "Accept": "application/json"},
                              json=payload, timeout=45)
        if r.status_code == 429:
            raise RuntimeError("NIM 限流 429（免费额度 40 RPM 量级，别在请求路径里反复调用）")
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"].strip()
        _ai_stats["ok"] += 1
        _ai_stats["last_ms"] = int((time.monotonic() - t0) * 1000)
        return text
    except Exception as e:
        _ai_stats["fail"] += 1
        _ai_stats["last_error"] = "%s: %s" % (type(e).__name__, e)
        _ai_stats["last_ms"] = int((time.monotonic() - t0) * 1000)
        raise


def _clean(text, tier):
    """去掉模型爱加的前缀、引号和 markdown。三档文案是要直接上屏的。"""
    t = text.strip().strip('"').strip("'").strip()
    t = re.sub(r"^\**", "", t)
    t = re.sub(r"^(答|回答|观察|装置|文案)\s*[：:]\s*", "", t)
    if tier == 1:
        # 中档必须是"一句"。多出来的句子砍掉——这个截断本身是设计，不是 bug。
        parts = re.split(r"(?<=[。！？!?])\s*", t)
        t = parts[0] if parts else t
    return t[:400]


# 兜底文案：NIM 不可用时用。它的定位是"让演示不至于开天窗"，
# 不是"假装是 AI 写的"，所以 source 一定标 fallback，看板一定显示。
FALLBACK = {
    1: {
        0: "我看到有人，但你现在还不够近。",
        1: "这地方今天人不少，空气已经开始闷了。",
        2: "你们凑近我的时候，这个展位的二氧化碳浓度大概在往上走。"
           "我读到最近一次是 38 厘米，停留十九秒。"
           "这个距离上没人真的需要信息，你们都只是想看看我会说什么。",
    },
    2: {
        0: "屋里没人靠近，我懒得说话。",
        1: "这儿的光是灯给的，不是太阳给的。",
        2: "我现在读到的光照是分压后的电压，不是照度，别把这两个混起来。"
           "温度和湿度都在人能忍的范围内，但你们的呼吸正在改变它。"
           "如果这是你家客厅，我的建议是开窗，不是买传感器。",
    },
    3: {
        0: "站那么远，我看不出你是谁。",
        1: "敢退半步又凑回来的人，通常是自己也不太信自己。",
        2: "凑到三十厘米以内的人，一般先笑一下再看屏幕。"
           "你们刚才的反应我数了：多数人退开的时候比凑近的时候快。"
           "这说明你们不是被我吸引的，是被'可能错过什么'吸引的。"
           "这个区别很重要，它决定了你们会不会回来第二次。",
    },
}


def observe():
    """当天真实运行数据，注入生成提示词。

    这是「AI 融合硬件」和「AI 写了三段话贴在硬件上」的区别所在：
    模型看到的不是编好的数字，是这台设备今天真实接待过谁、
    被凑到多近、在这个亮度温度下正在读到什么。

    数据不足时如实告诉模型"今天还没有数据"，让它承认自己是新开机，
    而不是让它对着空数字瞎编一个"凑近的人很多"。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    obs = {"day": today}
    try:
        # closest 在 states 表不在 sessions 表——这里曾经写错列名，
        # 被 except 吞掉后表现为"observed 永远是空的"，查了一轮才定位。
        cur = _db.execute(
            "SELECT COUNT(*) visits, SUM(max_tier=2) close_visits, "
            "ROUND(AVG(dwell_ms)/1000,1) avg_dwell_s "
            "FROM sessions WHERE ts LIKE ?", (today + "%",))
        r = cur.fetchone()
        obs["visits"] = r["visits"] or 0
        obs["close_visits"] = r["close_visits"] or 0
        obs["avg_dwell_s"] = r["avg_dwell_s"]
        cur = _db.execute(
            "SELECT MIN(dist_cm) closest FROM states WHERE ts LIKE ?",
            (today + "%",))
        obs["closest_cm"] = cur.fetchone()["closest"]
        cur = _db.execute(
            "SELECT dist_cm, k_divisor, lux_mv FROM states "
            "ORDER BY id DESC LIMIT 1")
        last = cur.fetchone()
        if last:
            obs["current_dist_cm"] = last["dist_cm"]
            obs["lux_mv"] = last["lux_mv"]
            if last["k_divisor"]:
                # 反推环境温度：k = 20000/(331.3+0.606T)
                obs["temp_c"] = round(
                    (20000.0 / last["k_divisor"] - 331.3) / 0.606, 1)
    except Exception as e:
        print("[observe] 统计读取失败（不阻断生成）：%s" % e)
    return obs


def _obs_text(obs):
    """把观测 dict 转成给模型看的中文事实清单。缺项就明说缺。"""
    f = []
    if obs.get("visits"):
        f.append("今天已经接待过 %d 人次，其中 %d 人凑到了最近档，"
                 "平均停留 %s 秒，被凑得最近的一次是 %s 厘米。" % (
                     obs["visits"], obs["close_visits"],
                     obs.get("avg_dwell_s", "?"), obs.get("closest_cm", "?")))
    else:
        f.append("今天还没有任何人访问过你——你是刚被开机放到这儿的新东西。")
    if obs.get("current_dist_cm") is not None:
        f.append("此刻你正读到一个 %s 厘米外的物体。" % obs["current_dist_cm"])
    if obs.get("temp_c") is not None:
        f.append("当前环境温度约 %s℃（由超声除数反推）。" % obs["temp_c"])
    if obs.get("lux_mv") is not None:
        f.append("光敏读数 %s mV（分压后电压，不是照度）。" % obs["lux_mv"])
    return "\n".join(f)


async def generate_topic(topic_id):
    """生成某话题的三档文案。一次生成三档，只打模型一次。

    为什么一次生成而不是三次调用：免费额度按积分算，三次调用除了慢
    没有额外好处，而且分开生成会让三档之间失去连贯（同一件事说了三遍）。
    一次生成还能让模型自己控制三档的信息量落差。
    """
    topic = TOPICS.get(topic_id, TOPICS[DEFAULT_TOPIC])
    day = datetime.now().strftime("%Y-%m-%d")
    key = (topic_id, day)

    async with _lock:
        if key in _cache:
            return _cache[key]

        model = _ai_stats["model"]
        answers, source = None, "fallback"
        obs = observe()

        if NIM_KEY and model:
            spec = "\n".join(
                "  档 %d（%s）：%s。要求：%s" % (
                    t, s["label"], s["brief"], s["chars"])
                for t, s in sorted(TIER_SPEC.items()))
            prompt = [
                {"role": "system",
                 "content": "你是一台装在小盒子里的硬件装置，只有一只超声波传感器、"
                            "一只温湿度计和一只光敏电阻，还有一块小屏幕。"
                            "你说话克制、具体，偶尔刻薄。不要解释你是谁，不要客套。"
                            "你的输出会被直接显示在屏幕上，所以不要加引号、不要加前缀、"
                            "不要用 Markdown 语法。"},
                {"role": "user",
                 "content": "装置正在观察的对象是：%s。\n%s\n\n"
                            "以下是你此刻真实观测到的运行数据，档 2 必须引用其中"
                            "至少两个数字，数字要照原样用，不许四舍五入不许编造：\n"
                            "%s\n\n"
                            "如果数据显示今天没人访问过你，就坦白说你是刚开机的，"
                            "不许假装见过很多人。\n\n"
                            "按下面的三档分别写一句话，档与档之间必须是同一个话题的"
                            "信息量递进，不是三句互不相干的话：\n%s\n\n"
                            "严格按 JSON 输出，不要任何多余文字：\n"
                            '{"0": "...", "1": "...", "2": "..."}'
                            % (topic["name"], topic["hint"],
                               _obs_text(obs), spec)},
            ]
            try:
                async with httpx.AsyncClient() as client:
                    raw = await nim_chat(client, model, prompt,
                                         max_tokens=700, temperature=0.85)
                parsed = json.loads(_strip_fence(raw))
                if all(str(t) in parsed for t in TIER_SPEC):
                    answers = {t: _clean(str(parsed[str(t)]), t) for t in TIER_SPEC}
                    source = "nim"
            except Exception as e:
                print("[NIM] 生成失败，退回兜底文案：%s" % e)

        if answers is None:
            answers = FALLBACK.get(topic_id, FALLBACK[DEFAULT_TOPIC])
            source = "fallback"

        entry = {"topic_id": topic_id, "topic": topic["name"],
                 "answers": {str(t): answers[t] for t in sorted(TIER_SPEC)},
                 "source": source, "model": model if source == "nim" else None,
                 # 生成那一刻喂给模型的真实数据快照。路演讲评时用它解释
                 # "AI 当时看到了什么"——它和当前实时数据往往不同，
                 # 这个不同恰恰证明数据是真的在流动。
                 "generated_observed": obs,
                 "generated_at": datetime.now().isoformat(timespec="seconds")}
        _cache[key] = entry
        _persist_answers(entry)
        return entry


def _strip_fence(s):
    s = re.sub(r"^```[a-z]*\s*", "", s.strip())
    s = re.sub(r"```\s*$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    return m.group(0) if m else s


def _persist_answers(entry):
    for t, txt in entry["answers"].items():
        _db.execute(
            "INSERT OR REPLACE INTO answers "
            "(topic_id,day,tier,text,source,model,created_at) VALUES (?,?,?,?,?,?,?)",
            (entry["topic_id"], datetime.now().strftime("%Y-%m-%d"), int(t),
             txt, entry["source"], entry["model"], entry["generated_at"]))
    _db.commit()


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def on_message(client, userdata, msg):
    try:
        p = json.loads(msg.payload.decode())
    except Exception:
        return
    dev = msg.topic.split("/")[2] if len(msg.topic.split("/")) > 2 else "?"
    if msg.topic.endswith("/state"):
        _db.execute(
            "INSERT INTO states(device,ts,seq,tier,dist_cm,k_divisor,lux_mv,cache_cov,csq)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (dev, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), p.get("seq"),
             p.get("tier"), p.get("dist_cm"), p.get("k"), p.get("lux_mv"),
             p.get("cache"), p.get("csq")))
        _db.commit()


def on_session(client, userdata, msg):
    try:
        p = json.loads(msg.payload.decode())
    except Exception:
        return
    dev = msg.topic.split("/")[2]
    _db.execute(
        "INSERT INTO sessions(device,ts,max_tier,dwell_ms) VALUES (?,?,?,?)",
        (dev, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         p.get("max_tier", 0), int(p.get("dwell_ms", 0))))
    _db.commit()


async def mqtt_loop():
    if mqtt is None:
        print("[MQTT] paho-mqtt 未安装，跳过上行订阅")
        return
    # paho-mqtt 2.x 要求显式传 CallbackAPIVersion，1.x 不接受该参数。
    # 这里探测签名，两版都能起，避免线上环境因为版本差直接崩。
    try:
        import inspect
        if "CallbackAPIVersion" in inspect.signature(mqtt.Client).parameters:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="whatif-cloud")
        else:
            c = mqtt.Client(client_id="whatif-cloud")
    except Exception:
        c = mqtt.Client(client_id="whatif-cloud")
    c.on_connect = lambda *a: c.subscribe(MQTT_TOPIC_STATE, qos=0)
    c.on_message = on_message
    def _run():
        while True:
            try:
                c.connect(MQTT_HOST, MQTT_PORT, 60)
                c.loop_forever()
            except Exception as e:
                print("[MQTT] 重连：%s" % e)
                time.sleep(5)
    await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with httpx.AsyncClient() as client:
        available = await verify_models(client)
        _ai_stats["model"] = pick_model(available)
    if not NIM_KEY:
        print("[LLM] 未配置任何模型 key（ARK/DASHSCOPE/NVIDIA 三选一），"
              "文案将使用本地兜底（看板会标注非模型生成）")
    else:
        print("[LLM] provider=%s 选用模型：%s（可用 %d 个）" % (
            PROVIDER, _ai_stats["model"], len(available)))
    if mqtt is not None:
        asyncio.create_task(mqtt_loop())
    yield
    if _db:
        _db.close()


app = FastAPI(title="What If Lab · 距离即权限", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/topics")
async def list_topics():
    return [{"id": k, "name": v["name"]} for k, v in sorted(TOPICS.items())]


@app.get("/api/answers")
async def get_answers(topic_id: int = DEFAULT_TOPIC, device: str = ""):
    """设备启动时调一次，拿全三档文案。

    响应里两个观测字段要分清：
      observed          —— 本次请求时刻的最新运行数据（看板显示用）
      generated_observed —— 文案生成那一刻的快照（解释"AI 当时看到了什么"）
    路演讲评时这两个数经常不一样：上午生成的文案说"还没人来"，
    下午看板显示 80 人次——这不是 bug，恰恰证明了数据是真的在流动。
    """
    if topic_id not in TOPICS:
        raise HTTPException(404, "话题不存在")
    entry = await generate_topic(topic_id)
    out = dict(entry)
    out["observed"] = observe()      # 实时刷新，供看板显示
    return out


@app.get("/api/ai/status")
async def ai_status():
    """评委问"用的什么模型"时，这个接口就是答案。"""
    return {
        "provider": PROVIDER,
        "endpoint": NIM_BASE,
        "model": _ai_stats["model"],
        "api_key_set": bool(NIM_KEY),
        "calls": _ai_stats["calls"], "ok": _ai_stats["ok"], "fail": _ai_stats["fail"],
        "last_ms": _ai_stats["last_ms"], "last_error": _ai_stats["last_error"],
        "available_models": _ai_stats["available"][:30],
    }


@app.post("/api/answers/regenerate")
async def regenerate(topic_id: int = DEFAULT_TOPIC):
    """路演现场换话题时重新生成。清掉当天缓存再走一次模型。"""
    _cache.pop((topic_id, datetime.now().strftime("%Y-%m-%d")), None)
    return await generate_topic(topic_id)


@app.get("/api/stats")
async def stats():
    """路演后复盘用的核心数据：多少人凑到了近档、平均停留多久。"""
    cur = _db.execute("SELECT COUNT(*) c FROM states")
    total = cur.fetchone()["c"]
    cur = _db.execute("SELECT tier, COUNT(*) c FROM states GROUP BY tier")
    tiers = {r["tier"]: r["c"] for r in cur.fetchall()}
    cur = _db.execute(
        "SELECT COUNT(*) visits, AVG(dwell_ms) avg_ms, "
        "SUM(CASE WHEN max_tier=2 THEN 1 ELSE 0 END) close_visits FROM sessions")
    s = cur.fetchone()
    cur = _db.execute("SELECT MIN(dist_cm) m FROM states WHERE tier=2")
    closest = cur.fetchone()["m"]
    return {
        "samples": total,
        "tier_seconds": {str(k): v for k, v in tiers.items()},
        "visits": s["visits"] or 0,
        "close_visits": s["close_visits"] or 0,
        "avg_dwell_ms": round(s["avg_ms"] or 0),
        "closest_cm": closest,
    }


@app.get("/api/timeline")
async def timeline(limit: int = 400):
    cur = _db.execute(
        "SELECT ts, tier, dist_cm, lux_mv, csq FROM states ORDER BY id DESC LIMIT ?",
        (min(limit, 2000),))
    return [dict(r) for r in cur.fetchall()]


@app.get("/api/sessions")
async def sessions(limit: int = 200):
    cur = _db.execute(
        "SELECT ts, max_tier, dwell_ms FROM sessions ORDER BY id DESC LIMIT ?",
        (min(limit, 1000),))
    return [dict(r) for r in cur.fetchall()]


class JudgePayload(BaseModel):
    """设备在观众进入中档时 POST 这一份，云端判行为 + 决定说什么。"""
    trace: list = []          # [{"min": cm, "spread": cm}, ...] 每秒一项
    dwell_s: int = 0          # 这次访问在近档已停留秒数
    env: dict = {}            # {"temp_c":..,"lux_mv":..}


@app.post("/api/judge")
async def judge(payload: JudgePayload):
    """读观众的距离轨迹，判断行为并生成近档该说的话。

    这是方案 A 里 AI 真正参与决策的入口：模型看到的是这段轨迹，
    不是模板变量。返回里 source 区分 nim / heuristic，看板如实显示，
    兜底文案绝不冒充模型输出。
    """
    st = await stats()
    async with httpx.AsyncClient() as client:
        result = await behavior_judge.judge(
            client, NIM_BASE, NIM_KEY, _ai_stats["model"],
            {"trace": payload.trace, "dwell_s": payload.dwell_s,
             "env": payload.env, "stats": st},
            timeout=12)
    result["model"] = _ai_stats["model"] if result.get("source") == "nim" else None
    return result


static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    p = os.path.join(static_dir, "index.html")
    if os.path.exists(p):
        return FileResponse(p)
    return {"service": "whatif-cloud", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
