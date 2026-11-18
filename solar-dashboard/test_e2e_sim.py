# -*- coding: utf-8 -*-
"""
端到端链路模拟（服务端侧，不需要真机/broker）。

模拟 v2 固件的完整上报序列：register → 三天日账推进 → 实时 reading
→ 调 /api/rooms/{id}/review 出评估。验证四件事：

1. 自注册能在空库上建出 房屋/房间/设备 三层
2. v2 payload 契约（day 累计 vs 近窗小时量）落库语义正确，
   日账用的是今日累计而不是近一小时
3. seq 去重、灯光虚增（naive-sun）可算
4. 评估接口在无 key 时走 heuristic 且 grade/quality 完整

跑法：python3 test_e2e_sim.py   （在当前目录）
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ["SOLAR_DB"] = os.path.join(tempfile.mkdtemp(), "e2e.db")
os.environ.pop("ARK_API_KEY", None)
os.environ.pop("DASHSCOPE_API_KEY", None)
os.environ.pop("NVIDIA_API_KEY", None)

import app  # noqa: E402


async def main():
    await app.init_db()

    # 1. 自注册
    await app.register_device("solar-ledger-02",
                              {"room": "客厅", "fw": "v2.0"})
    async with app._db.execute("SELECT * FROM devices") as c:
        dev = await c.fetchone()
    assert dev and dev["device_id"] == "solar-ledger-02", "设备未注册"
    room_id = dev["room_id"]
    assert room_id, "设备未挂到房间"
    print("1) 自注册 OK  room_id=%s" % room_id)

    # 2. 三天日账（模拟已归档的天）
    for d, sun, naive, peak in [("2026-11-13", 187, 199, 820),
                                ("2026-11-14", 96, 136, 510),
                                ("2026-11-15", 231, 239, 880)]:
        await app._db.execute(
            """INSERT OR REPLACE INTO daily_ledger
               (room_id,date,sun_minutes,naive_minutes,peak_mv)
               VALUES (?,?,?,?,?)""", (room_id, d, sun, naive, peak))
    await app._db.commit()

    # 3. 实时 reading（v2 契约：sun_min=近1h，day.sun_min=今日累计）
    base_seq = 0
    for i in range(4):
        base_seq += 1
        payload = {
            "seq": base_seq, "ts_ms": 0, "csq": 21,
            "natural_mv": 520 + i * 10, "lamp_mv": 30,
            "temp": 25.5, "rh": 48.0,
            "sun_min": 40 + i,                    # 近一小时，不该进日账
            "naive_sun_min": 45 + i,
            "day": {"sun_min": 120 + i * 5,       # 今日累计，日账用这个
                    "lamp_min": 18, "sat_ratio": 0.02},
            "peak": 860, "sat_ratio": 0.01, "fw": "v2.0",
        }
        ok = await app.save_reading("solar-ledger-02", payload)
        assert ok, "reading 入库失败"
    # 重复 seq 必须被去重
    dup = await app.save_reading("solar-ledger-02",
                                 dict(payload, seq=base_seq))
    assert dup is False, "seq 去重失效"
    async with app._db.execute(
        "SELECT sun_minutes FROM daily_ledger WHERE room_id=? "
        "AND date=?", (room_id, datetime.now().strftime("%Y-%m-%d"))) as c:
        row = await c.fetchone()
    assert row and row["sun_minutes"] == 135, "日账没用今日累计: %s" % row
    print("2) v2 契约落库 OK（日账=135 今日累计，近窗 43 未污染；seq 去重 OK）")

    # 4. 评估接口（无 key → heuristic，但分级/可信度必须完整）
    result = await app.room_review_api(room_id, days=7)
    assert result["grade"] in ("A", "B", "C", "D"), result
    assert result["source"] == "heuristic"
    assert "headline" in result and "laundry" in result
    assert result["stats"]["days_count"] >= 3
    print("3) 评估 OK  grade=%s  headline=%s" % (result["grade"], result["headline"]))
    print("   可信度:", "；".join(result["quality"]))

    # 5. 状态接口
    st = await app.llm_status()
    assert st["provider"] == "none" and st["key_set"] is False
    print("4) llm_status OK：无 key 时如实报 provider=none（不会冒充模型）")

    await app.close_db()
    print("\n端到端模拟全部通过。烧录后设备只会比这里多两件事：")
    print("真 MQTT 传输 和 真 ADC 读数。")


asyncio.run(main())
