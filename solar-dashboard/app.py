"""日光账本 · 后端服务

职责：
  1. MQTT 订阅设备上报，落库
  2. 提供 REST API 给前端拉取
  3. seq 去重、告警成对、设备离线判定

运行：pip install -r requirements.txt && python app.py
"""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi import Response
from pydantic import BaseModel

import llm_provider
import room_review

# ---------------- 配置 ----------------
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_UP_TOPIC = "/solar_ledger/+/up"       # + 匹配单级
MQTT_CMD_TOPIC = "/solar_ledger/{dev}/cmd"
DB_PATH = os.environ.get("SOLAR_DB", "solar.db")
SCHEMA_PATH = "schema.sql"
STALE_MINUTES = 30                        # 超过则判定设备离线

_db: Optional[aiosqlite.Connection] = None
_seen_seq = {}          # device_id -> {seq: ts}
_latest = {}            # room_id -> 最新读数（内存缓存，DB 为准）

# ============================================================
# 数据库
# ============================================================
async def init_db():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        await _db.executescript(f.read())
    await _db.commit()


async def close_db():
    if _db:
        await _db.close()


async def get_room_by_device(device_id):
    """devices 表没有 name 列（schema 核对过），房间名必须 JOIN rooms 取。
    原 SELECT 写成 devices.name，save_reading 一调就抛 no such column——
    这条入库路径此前从未被真实数据跑过，所以一直没暴露。"""
    async with _db.execute(
        """SELECT d.room_id, r.name FROM devices d
           LEFT JOIN rooms r ON r.id = d.room_id
           WHERE d.device_id = ?""", (device_id,)
    ) as cur:
        row = await cur.fetchone()
    return (row["room_id"], row["name"]) if row else (None, None)


# ============================================================
# MQTT
# ============================================================
mqtt_client: Optional[mqtt.Client] = None
_loop_holder = {}


def on_connect(client, userdata, flags, rc):
    print(f"[MQTT] connected rc={rc}")
    client.subscribe(MQTT_UP_TOPIC, qos=1)


def on_disconnect(client, userdata, rc, *args):
    print(f"[MQTT] disconnected rc={rc}")


def on_message(client, userdata, msg):
    """paho 回调是同步的，不能直接 await。

    把数据丢回主事件循环处理。
    """
    try:
        # paho-mqtt 2.x 的 VERSION2 回调里 topic 是 str、payload 是 bytes；
        # 1.x 时代两个都是 bytes。直接 .decode() 在 2.x 上抛
        # 'str' object has no attribute 'decode'——部署实测踩过。
        raw_topic = msg.topic
        topic = raw_topic if isinstance(raw_topic, str) else raw_topic.decode()
        parts = topic.split("/")
        dev = parts[2] if len(parts) > 2 else "unknown"
        raw_payload = msg.payload
        text = raw_payload if isinstance(raw_payload, str) else raw_payload.decode()
        payload = json.loads(text)
        asyncio.run_coroutine_threadsafe(_handle_up(dev, payload),
                                         _loop_holder["loop"])
    except Exception as e:
        print(f"[MQTT] handle error: {e}")


async def _handle_up(dev, payload):
    t = payload.get("type")
    if t == "register":
        await register_device(dev, payload)
        print(f"[REG] {dev} -> {payload.get('room')}")
    elif t == "reading":
        ok = await save_reading(dev, payload)
        if ok:
            print(f"[INGEST] {dev} natural={payload.get('natural_mv')} "
                  f"lamp={payload.get('lamp_mv')}")
    elif t in ("alert", "recover", "ack"):
        await save_event(dev, payload)
        print(f"[EVENT] {dev} {t}: {payload.get('label')}")
    elif t == "status":
        await update_device_status(dev, payload)
    else:
        await save_event(dev, {"type": "info", "label": f"未知类型 {t}"})


async def register_device(dev, payload):
    """设备自注册：把 device_id 挂到指定房屋下的同名房间。

    房间不存在则建房（house 固定用「演示房屋」）。幂等：重复注册
    只更新 device_id 指向，不产生新行。这样烧录侧改 DEVICE_ID/ROOM_NAME
    重开机就能自动迁移，不用进服务器改库。
    """
    room_name = payload.get("room") or "默认房间"
    async with _db.execute(
        """INSERT INTO houses (name) SELECT ? WHERE NOT EXISTS
           (SELECT 1 FROM houses WHERE name = ?)""",
        ("演示房屋", "演示房屋")) as _c:
        pass
    await _db.commit()
    async with _db.execute(
        "SELECT id FROM houses WHERE name = ?", ("演示房屋",)) as cur:
        house = await cur.fetchone()
    async with _db.execute(
        "SELECT id FROM rooms WHERE house_id = ? AND name = ?",
        (house["id"], room_name)) as cur:
        room = await cur.fetchone()
    if room is None:
        async with _db.execute(
            "INSERT INTO rooms (house_id, name, device_id) VALUES (?,?,?)",
            (house["id"], room_name, dev)) as cur:
            room_id = cur.lastrowid
    else:
        room_id = room["id"]
        # 房间 device_id 被手动置空 = 维护者主动解绑（暂停入库/清脏数据）。
        # 自注册只在新建房间时绑定；重绑需人工填回，防止板子重启把
        # 隔离中的脏数据通道悄悄接回去。
        if room["device_id"] is not None:
            await _db.execute(
                "UPDATE rooms SET device_id = ?, has_sensor = 1 WHERE id = ?",
                (dev, room_id))
    await _db.execute(
        """INSERT INTO devices (device_id, room_id, model, firmware, status)
           SELECT ?, ?, 'QuecDuino-EC800', ?, 'online' WHERE NOT EXISTS
           (SELECT 1 FROM devices WHERE device_id = ?)""",
        (dev, room_id, payload.get("fw"), dev))
    await _db.execute(
        "UPDATE devices SET firmware=?, model='QuecDuino-EC800' WHERE device_id=?",
        (payload.get("fw"), dev))
    await _db.commit()


async def save_reading(dev, payload):
    """落库并做 seq 去重。

    去重放在服务端是刻意的选择：设备端存储紧张，做不了
    "哪条已成功送达"的精确管理；服务端空间无限，兜底更可靠。
    """
    room_id, _ = await get_room_by_device(dev)
    if room_id is None:
        print(f"[WARN] 未知设备 {dev}，数据丢弃")
        return False

    seq = payload.get("seq")
    if seq is not None:
        seen = _seen_seq.setdefault(dev, {})
        now = time.time()
        if seq in seen:
            return False                        # 重复投递，丢弃
        for k in [k for k, v in seen.items() if now - v > 600]:
            seen.pop(k, None)
        seen[seq] = now

    natural = payload.get("natural_mv", 0)
    lamp = payload.get("lamp_mv", 0)
    total = natural + lamp

    # 有效日照判定：只看自然光，且超过阈值
    sun_min = payload.get("sun_min", 0)
    threshold = payload.get("threshold_mv", 400)
    in_sun = 1 if natural >= threshold else 0

    # 若不剔除灯光会算成多少 —— 用于量化"数据质量"
    naive_min = payload.get("naive_sun_min", sun_min)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 设备时钟漂移只告警、不改变入库：ts 恒用服务器接收时刻，
    # 曲线时间正确性与设备时钟彻底解耦。重烧固件(UTC_OFFSET_H=8)后
    # 这行偏移应归零；持续 -8h ≈ 板子还在跑旧固件。
    _dev_ts = payload.get("ts")
    if isinstance(_dev_ts, (int, float)) and _dev_ts > 1700000000:
        _skew = _dev_ts - time.time()
        if abs(_skew) > 600:
            print(f"[CLOCK] {dev} 设备时钟偏差 {_skew/3600:+.1f}h（不影响入库 ts）")

    await _db.execute(
        """INSERT INTO readings
           (room_id, ts, ts_ms, natural_mv, lamp_mv, temp_c, rh,
            lux, sun_min, in_sun, raw, seq)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (room_id, ts, payload.get("ts_ms", 0), natural, lamp,
         payload.get("temp"), payload.get("rh"), payload.get("lux"),
         sun_min, in_sun, payload.get("raw", natural + lamp), seq))
    await _db.commit()

    # 顺带更新日账的累计值。
    # 日账语义 = 当天累计：v2 固件的 day.sun_min 才是今日累计，
    # 顶层 sun_min 只是近一小时的量，直接覆盖会把日账算小一个数量级。
    # 旧版固件没有 day 字段，退回顶层值保持兼容。
    today = payload.get("day") or {}
    day_sun = today.get("sun_min", sun_min)
    day_naive = today.get("naive_sun_min", naive_min)
    date = ts[:10]
    await _db.execute(
        """INSERT INTO daily_ledger
           (room_id, date, sun_minutes, naive_minutes, peak_mv)
           VALUES (?,?,?,?,?)
           ON CONFLICT(room_id, date) DO UPDATE SET
             sun_minutes   = excluded.sun_minutes,
             naive_minutes = excluded.naive_minutes,
             peak_mv       = MAX(peak_mv, excluded.peak_mv)""",
        (room_id, date, day_sun, day_naive, payload.get("lux", 0)))
    await _db.commit()

    await _db.execute(
        "UPDATE devices SET status='online', csq=?, last_seen_at=? WHERE device_id=?",
        (payload.get("csq"), ts, dev))
    await _db.commit()

    _latest[room_id] = payload
    return True


async def save_event(dev, payload):
    room_id, _ = await get_room_by_device(dev)
    if room_id is None:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await _db.execute(
        """INSERT INTO events (room_id, ts, type, kind, label, detail, acked)
           VALUES (?,?,?,?,?,?,?)""",
        (room_id, ts, payload.get("type"), payload.get("kind"),
         payload.get("label"), str(payload.get("detail", "")),
         1 if payload.get("type") == "ack" else 0))
    await _db.commit()


async def update_device_status(dev, payload):
    await _db.execute(
        "UPDATE devices SET status='online', csq=?, last_seen_at=? WHERE device_id=?",
        (payload.get("csq"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), dev))
    await _db.commit()


async def mqtt_loop():
    global mqtt_client
    _loop_holder["loop"] = asyncio.get_running_loop()

    mqtt_client = mqtt.Client(client_id="solar-server")
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message

    def _run():
        while True:
            try:
                mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
                mqtt_client.loop_forever()
            except Exception as e:
                print(f"[MQTT] reconnect after error: {e}")
                time.sleep(5)

    await asyncio.to_thread(_run)


# ============================================================
# 生命周期
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(mqtt_loop())
    yield
    task.cancel()
    await close_db()


app = FastAPI(title="日光账本 · 采光评估系统", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ============================================================
# API
# ============================================================
@app.get("/api/houses")
async def list_houses():
    """房屋列表。"""
    async with _db.execute(
        """SELECT h.*,
                  (SELECT COUNT(*) FROM rooms r WHERE r.house_id = h.id) AS room_count
           FROM houses h ORDER BY h.id""" ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/houses/{house_id}/rooms")
async def list_rooms(house_id: int):
    """某房屋下的房间 + 最新账本状态（含灯光虚增量）。"""
    async with _db.execute(
        "SELECT * FROM v_room_latest WHERE house_id = ? ORDER BY room_name", (house_id,)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        raise HTTPException(404, "房屋不存在或无房间")
    return [dict(r) for r in rows]


@app.get("/api/rooms/{room_id}/curve")
async def room_curve(room_id: int, hours: int = Query(24, ge=1, le=24 * 30)):
    """单房间的时间曲线。

    返回 natural 与 lamp 两个分量，前端分别画成两条线。
    """
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    async with _db.execute(
        """SELECT ts, natural_mv, lamp_mv, temp_c, rh, in_sun, sun_min
           FROM readings WHERE room_id = ? AND ts >= ?
           ORDER BY ts ASC""", (room_id, since)) as cur:
        rows = await cur.fetchall()

    return {
        "room_id": room_id,
        "count": len(rows),
        "series": [dict(r) for r in rows],
    }


@app.get("/api/rooms/{room_id}/ledger")
async def room_ledger(room_id: int, days: int = Query(7, ge=1, le=90)):
    """房间近 N 日账本。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with _db.execute(
        """SELECT date, sun_minutes, naive_minutes, peak_mv, weather
           FROM daily_ledger WHERE room_id = ? AND date >= ?
           ORDER BY date ASC""", (room_id, since)) as cur:
        rows = await cur.fetchall()

    # 服务端补算评分与灯光虚增，前端不用再算一遍
    out = []
    for r in rows:
        d = dict(r)
        s, g = grade(d["sun_minutes"], d["peak_mv"])
        d["score"] = s
        d["grade"] = g
        d["lamp_inflation"] = d["naive_minutes"] - d["sun_minutes"]
        out.append(d)
    return {"room_id": room_id, "days": out}


def grade(minutes, peak_mv):
    """采光评分：双档判定。

    只看时长或只看峰值都会失真——阴天峰值高但时长短，
    晴天散射峰值一般但时长够长。
    """
    if minutes >= 300 and peak_mv >= 900:
        return 95, "A"
    if minutes >= 180 and peak_mv >= 650:
        return 82, "B+"
    if minutes >= 90 or peak_mv >= 380:
        return 66, "C"
    return 45, "D"


@app.get("/api/rooms/{room_id}/events")
async def room_events(room_id: int, limit: int = Query(50, ge=1, le=200)):
    async with _db.execute(
        """SELECT ts, type, kind, label, detail, acked
           FROM events WHERE room_id = ? ORDER BY id DESC LIMIT ?""",
        (room_id, limit)) as cur:
        return [dict(r) for r in await cur.fetchall()]


@app.get("/api/devices/health")
async def devices_health():
    """设备健康度。看板用它提示"数据可能是旧的"。"""
    async with _db.execute("SELECT * FROM v_device_health") as cur:
        return [dict(r) for r in await cur.fetchall()]


@app.get("/api/devices/{dev_id}/live")
async def device_live(dev_id: str, minutes: int = Query(20, ge=1, le=1440)):
    """按设备号的实时短窗序列（调试页用）。

    与 room_curve 的区别：按 device 查（真机/mock 都行，不必先建房间视图），
    且返回带分量与饱和度，供前端画滚动波形 + 握/照手势对比。
    raw 一并返回：真机的 natural_mv 是滚动窗口聚合值（慢、抗抖），
    raw 才是最新瞬时读数——演示"波形在动"要看 raw。
    """
    since = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    async with _db.execute(
        """SELECT r.ts, r.natural_mv, r.lamp_mv, r.temp_c, r.rh, r.in_sun, r.seq,
                  r.raw, r.lux
           FROM readings r JOIN rooms m ON m.id = r.room_id
           WHERE m.device_id = ? AND r.ts >= ? ORDER BY r.id ASC""",
        (dev_id, since)) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    async with _db.execute(
        """SELECT d.status, d.csq, d.last_seen_at, d.firmware
           FROM devices d WHERE d.device_id = ?""", (dev_id,)) as cur:
        meta = await cur.fetchone()
    if meta is None:
        # 设备查不到时把在用的设备号吐回来——调试页 URL 里打错一个字符
        # （solar-mock-0 少了个 1）页面只会说"等待数据"，最难自查
        async with _db.execute(
            """SELECT device_id, status, last_seen_at FROM devices
               ORDER BY last_seen_at DESC LIMIT 8""") as cur:
            known = [dict(r) for r in await cur.fetchall()]
    else:
        known = None
    return {"dev": dev_id, "meta": dict(meta) if meta else None,
            "known_devices": known, "series": rows}


class CmdRequest(BaseModel):
    dev: str
    action: str
    config: Optional[dict] = None


@app.post("/api/cmd")
async def send_cmd(req: CmdRequest):
    """下发指令到设备。"""
    if mqtt_client is None:
        raise HTTPException(503, "MQTT 未连接")
    topic = MQTT_CMD_TOPIC.format(dev=req.dev)
    body = {"action": req.action}
    if req.config:
        body["config"] = req.config
    mqtt_client.publish(topic, json.dumps(body), qos=1)
    return {"ok": True, "topic": topic, "payload": body}


@app.get("/api/overview")
async def overview():
    """全房屋对比。用于看板顶部的横向条。"""
    async with _db.execute(
        """SELECT h.id AS house_id, h.name AS house_name,
                  r.id AS room_id, r.name AS room_name, r.orientation,
                  l.sun_minutes, l.peak_mv, l.score, l.grade,
                  COALESCE(l.naive_minutes - l.sun_minutes, 0) AS lamp_inflation
           FROM rooms r
           JOIN houses h ON h.id = r.house_id
           LEFT JOIN daily_ledger l
                  ON l.room_id = r.id
                 AND l.date = (SELECT MAX(date) FROM daily_ledger WHERE room_id = r.id)
           WHERE l.date IS NOT NULL
           ORDER BY h.id, l.sun_minutes DESC""" ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ============================================================
# AI房评师：模型评估接口（项目二云端入口）
# ============================================================

PROVIDER, LLM_BASE, LLM_KEY, LLM_MODEL = llm_provider.resolve()
_llm_model_resolved = None       # 首次调用时拉 /models 现场选，不硬编码型号
_llm_stats = {"calls": 0, "ok": 0, "fail": 0, "last_ms": None, "last_error": None}


async def _resolve_model():
    """模型 ID 懒解析：留空则拉可用列表现场选。猜 ID 是最容易翻车的事。"""
    global _llm_model_resolved
    if _llm_model_resolved is not None:
        return _llm_model_resolved
    if not LLM_KEY:
        _llm_model_resolved = ""
        return ""
    models = await llm_provider.list_models(LLM_BASE, LLM_KEY)
    _llm_model_resolved = LLM_MODEL or llm_provider.auto_pick(models)
    print("[LLM] provider=%s model=%s" % (PROVIDER, _llm_model_resolved or "(无可用)"))
    return _llm_model_resolved


async def _room_days(room_id: int, days: int):
    """从 daily_ledger 取近 N 天日账，转成 room_review 的输入格式。
    peak_mv 列名是历史遗留：v2 固件上报的是 lux 口径（config 换传感器
    字段时看板单位标注跟着换），这里原样透传，不做换算不做造假。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with _db.execute(
        """SELECT date, sun_minutes, naive_minutes, peak_mv
           FROM daily_ledger WHERE room_id = ? AND date >= ?
           ORDER BY date DESC LIMIT ?""", (room_id, since, days)) as cur:
        rows = await cur.fetchall()
    out = []
    for r in reversed(rows):
        sun = r["sun_minutes"] or 0
        lamp = max(0, (r["naive_minutes"] or 0) - sun)
        out.append({
            "date": r["date"], "sun_min": sun, "peak_mv": r["peak_mv"],
            "lamp_min": lamp,
            "saturated": (r["peak_mv"] or 0) >= 1150,
        })
    return out


class RoomInfo(BaseModel):
    orientation: str = "未填"
    floor: str = "未填"
    window_floor_ratio: str = "未填"
    obstruction: str = "未填"


@app.get("/api/rooms/{room_id}/review")
async def room_review_api(room_id: int, days: int = Query(3, ge=1, le=30)):
    """生成一个房间的光照评估（模型分析 + 可审计的本地分级）。

    口径：设备端只测光照时序，orientation/floor 等结构信息来自房源
    录入（默认值"未填"会原样进提示词，模型被要求区分"测到的"与"登记的"）。
    分级由本地规则算，模型失败退回本地统计——两种情况输出都带 source。
    """
    async with _db.execute(
        "SELECT id, name, orientation, floor FROM rooms WHERE id = ?",
        (room_id,)) as cur:
        room_row = await cur.fetchone()
    if not room_row:
        raise HTTPException(404, "房间不存在")

    day_rows = await _room_days(room_id, days)
    if not day_rows:
        raise HTTPException(400, "该房间暂无日账数据，先让设备采几天")

    room = {
        "name": room_row["name"],
        "orientation": room_row["orientation"] or "未填",
        "floor": room_row["floor"] if room_row["floor"] is not None else "未填",
    }
    model = await _resolve_model()
    t0 = time.time()
    async with httpx.AsyncClient() as client:
        result = await room_review.review(
            client, LLM_BASE, LLM_KEY, model, day_rows, room)
    _llm_stats["calls"] += 1
    if result.get("source") == "nim":
        _llm_stats["ok"] += 1
    else:
        _llm_stats["fail"] += 1
        _llm_stats["last_error"] = result.get("note")
    _llm_stats["last_ms"] = int((time.time() - t0) * 1000)
    result["room_id"] = room_id
    result["provider"] = PROVIDER if result.get("source") == "nim" else None
    return result


@app.get("/api/llm/status")
async def llm_status():
    return {"provider": PROVIDER, "endpoint": LLM_BASE,
            "key_set": bool(LLM_KEY), "model": _llm_model_resolved,
            **_llm_stats}


# 静态前端
app.mount("/", StaticFiles(directory="static", html=True), name="static")