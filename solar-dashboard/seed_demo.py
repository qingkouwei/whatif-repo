"""生成演示数据。

⚠️ 仅用于本地预览看板效果。真实环境由设备上报，不需要跑这个。

用法：python seed_demo.py
"""

import math
import random
import sqlite3
from datetime import datetime, timedelta

random.seed(20261015)

DB = "solar.db"

HOUSES = [
    dict(id=1, name="阳光里 3 栋 1204", address="阳光里小区", area=89.0,
         orientation="南", floor=12, note="两室一厅"),
    dict(id=2, name="阳光里 8 栋 302", address="阳光里小区", area=89.0,
         orientation="北", floor=3, note="两室一厅"),
]

ROOMS = [
    (1, "客厅", "南", 12, "solar-ledger-01", 1.00),
    (1, "主卧", "南", 12, "solar-ledger-02", 0.62),
    (1, "书房", "北", 12, "solar-ledger-03", 0.28),
    (2, "客厅", "北", 3, "solar-ledger-04", 0.34),
    (2, "主卧", "东", 3, "solar-ledger-05", 0.41),
    (2, "书房", "北", 3, None, 0.22),
]

# 与文章实测一致：板子到手三天，三天数据
# 与文章实测一致：板子到手三天，三天数据
# cloud = 全天基础衰减，cloud_hour = 该小时额外遮蔽（模拟云块断续遮挡）
WEATHER = [
    ("多云转晴", 0.28, [9, 10, 14, 15, 16]),  # D1 -> B+ 187 min
    ("阴", 0.62, [9, 10, 11, 12, 14, 15, 16, 17]),  # D2 -> C 96 min
    ("晴", 0.06, [15]),                # D3 -> B+ 231 min
]

THRESHOLD_MV = 460          # 有效日照阈值（mV，分压后）
LAMP_START, LAMP_END = 18, 23   # 晚上开灯时段



# 文章实测值：三天，客厅（南向）为 187/96/231 分钟。
# 其余房间按朝向比例缩放，保证相对关系合理。
def _build_measured():
    m = {}
    for room_id, orient, base in ((1,'南',1.00),(2,'南',0.62),(3,'北',0.28),
                                  (4,'北',0.34),(5,'东',0.41),(6,'北',0.22)):
        # 索引与生成顺序对齐：d=0 -> 最早那天(多云转晴187)
        #生成时 d 从 2 递减到 0，故按倒序映射
        for d, (s_min, p_lux) in enumerate(
                [(187,1240),(96,690),(231,1380)]):
            m[(room_id, d)] = (int(s_min * base), int(p_lux * base))
    return m

MEASURED = _build_measured()

def natural_mv(orient, base, cloud, cloud_hours, h):
    """自然光照强度（mV）。"""
    if h < 6 or h > 18:
        return 0
    peak = {"南": 12.5, "东": 9, "西": 16, "北": 12.5}.get(orient, 12.5)
    if orient == "北":
        v = math.exp(-((h - 12.5) ** 2) / (2 * 5.2 ** 2)) * 0.55
    else:
        v = math.exp(-((h - peak) ** 2) / (2 * 3.4 ** 2))
    if h in cloud_hours:
        v *= 0.12
    v *= (1 - cloud) * base
    return int(min(1180, v * 1180))


def lamp_mv(has_lamp, h):
    if not has_lamp or not (LAMP_START <= h < LAMP_END):
        return 0
    return 145 + (h % 3) * 12


def grade(minutes, peak):
    if minutes >= 300 and peak >= 900:
        return 95, "A"
    if minutes >= 180 and peak >= 650:
        return 82, "B+"
    if minutes >= 90 or peak >= 380:
        return 66, "C"
    return 45, "D"


def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    with open("schema.sql", encoding="utf-8") as f:
        c.executescript(f.read())

    now = datetime.now()

    for h in HOUSES:
        c.execute("""INSERT OR REPLACE INTO houses
                     (id,name,address,area,orientation,floor,note)
                     VALUES (?,?,?,?,?,?,?)""",
                  (h["id"], h["name"], h["address"], h["area"],
                   h["orientation"], h["floor"], h["note"]))

    for house_id, name, orient, floor, dev, base in ROOMS:
        c.execute("""INSERT INTO rooms (house_id,name,orientation,floor,has_sensor,device_id)
                     VALUES (?,?,?,?,?,?)""",
                  (house_id, name, orient, floor, 1 if dev else 0, dev))
        room_id = c.lastrowid
        if dev:
            c.execute("""INSERT INTO devices
                         (device_id,room_id,model,status,csq,last_seen_at)
                         VALUES (?,?,?,?,?,?)""",
                      (dev, room_id, "QuecDuino-EC800", "online",
                       random.randint(12, 25), now.strftime("%Y-%m-%d %H:%M:%S")))

        # 生成 3 天数据，每 10 分钟一个点
        for d in range(0, 3):
            date = (now - timedelta(days=2 - d)).date()
            wname, cloud, chours = WEATHER[d]
            has_lamp = dev is not None

            seq, sun_min, naive_min, peak = 0, 0, 0, 0
            naive_min = 0

            for h in range(24):
                for q in range(6):
                    nat = max(0, natural_mv(orient, base, cloud, chours, h)
                              + random.randint(-4, 4))
                    lamp = lamp_mv(has_lamp, h) + random.randint(-2, 2)
                    total = nat + lamp
                    ts = datetime(date.year, date.month, date.day, h, q * 10)

                    # 累计（每 6 点 = 1 小时，计 10 分钟）
                    if nat >= THRESHOLD_MV:
                        sun_min += 10
                        peak = max(peak, nat)
                    if total >= THRESHOLD_MV:
                        naive_min += 10

                    c.execute("""INSERT INTO readings
                        (room_id,ts,ts_ms,natural_mv,lamp_mv,temp_c,rh,lux,
                         sun_min,in_sun,raw,seq)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (room_id, ts.strftime("%Y-%m-%d %H:%M:%S"),
                               int(ts.timestamp() * 1000),
                               nat, lamp, round(22 + nat / 160, 1),
                               round(50 + nat / 300, 1), nat,
                               sun_min, 1 if nat >= THRESHOLD_MV else 0,
                               total, seq))
                    seq += 1

            # 日账以「文章实测值」为准，而非由曲线累计反推。
            # 原因：曲线累计受阈值与模拟参数影响会漂移，实测值才是权威。
            # 参数：DATE_SUN / DATE_PEAK 按房间给定的三日实测值。
            base_sun, base_peak = MEASURED.get((room_id, d), (sun_min, peak))
            score, g = grade(base_sun, base_peak)
            c.execute("""INSERT OR REPLACE INTO daily_ledger
                        (room_id,date,sun_minutes,naive_minutes,peak_mv,
                         score,grade,weather,settled)
                        VALUES (?,?,?,?,?,?,?,?,1)""",
                      (room_id, date.strftime("%Y-%m-%d"),
                       base_sun, naive_min, base_peak, score, g, wname))

            # 告警事件（成对：alert + recover）
            if nat >= 900 and q == 2:
                c.execute("""INSERT INTO events
                             (room_id,ts,type,kind,label,detail,acked)
                             VALUES (?,?,?,?,?,?,0)""",
                          (room_id, ts.strftime("%Y-%m-%d %H:%M:%S"),
                           "alert", "too_bright", "光照过强（暴晒）",
                           f"{nat} mV"))
                c.execute("""INSERT INTO events
                             (room_id,ts,type,kind,label,detail,acked)
                             VALUES (?,?,?,?,?,?,1)""",
                          (room_id,
                           (ts + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                           "recover", "too_bright", "光照过强（暴晒）",
                           "云层遮蔽，已恢复"))

    conn.commit()
    c.execute("SELECT COUNT(*) FROM readings")
    n = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM rooms")
    r = c.fetchone()[0]
    print(f"完成：{len(HOUSES)} 套房 / {r} 个房间 / {n} 条采样")
    conn.close()


if __name__ == "__main__":
    main()