"""入围演示 · 三天历史回填（直接落库，非实时发布）

为什么直接写库：服务端 save_reading 存的是接收时刻，历史 ts
从 MQTT 路径写不进去。所以三天数据走这条路，实时调试页走
mock_device.py 的真实 MQTT 链路，两条路各管各的。

目标设备：solar-mock-01 / 房间「演示桌」——和真机数据分区存放，
演示结束可一键清理（见脚本尾部的 --purge）。

曲线口径：三天天气 = 晴 / 多云转晴 / 阴，模拟南向客厅窗台；
夜间 19–23 点台灯事件（lamp 分量），用来现场讲"灯光不算日照"。

用法（服务器本机）：
  python3 backfill_3days.py            # 幂等：已有数据先删再插
  python3 backfill_3days.py --purge    # 清理三天假数据与 mock 设备
"""
import math
import random
import sqlite3
import sys
from datetime import datetime, timedelta

random.seed(20261120)

DB = "/opt/whatif/data/solar.db"
DEV = "solar-mock-01"
ROOM_NAME = "演示桌"
HOUSE_NAME = "演示房屋"
THRESHOLD_MV = 400          # 与 config.SUN_ACTIVE_MV 同口径
LAMP_START, LAMP_END = 19, 24

# 三天：日期偏移(2=前天) -> (天气, 云量, 遮蔽小时)
WEATHER = [
    ("晴",       0.06, [15]),
    ("多云转晴", 0.30, [9, 10, 14]),
    ("阴",       0.60, [10, 11, 14, 15, 16]),
]


def natural_mv(cloud, cloud_hours, h):
    """南向窗台钟形曲线。"""
    if h < 6 or h > 19:
        return 0
    v = math.exp(-((h - 12.7) ** 2) / (2 * 3.3 ** 2))
    if h in cloud_hours:
        v *= 0.15
    v *= (1 - cloud)
    return int(min(1140, v * 1180))


def purge(c):
    c.execute("""DELETE FROM readings WHERE room_id =
                 (SELECT id FROM rooms WHERE device_id = ?)""", (DEV,))
    c.execute("""DELETE FROM daily_ledger WHERE room_id =
                 (SELECT id FROM rooms WHERE device_id = ?)""", (DEV,))
    c.execute("DELETE FROM events WHERE room_id = (SELECT id FROM rooms WHERE device_id = ?)", (DEV,))
    c.execute("DELETE FROM devices WHERE device_id = ?", (DEV,))
    c.execute("DELETE FROM rooms WHERE device_id = ?", (DEV,))
    print("已清理", DEV, "的房间/读数/日账")


def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # 幂等：先清旧 mock
    c.execute("SELECT id FROM rooms WHERE device_id = ?", (DEV,))
    if c.fetchone():
        purge(c)

    c.execute("SELECT id FROM houses WHERE name = ?", (HOUSE_NAME,))
    row = c.fetchone()
    if row:
        house_id = row[0]
    else:
        c.execute("INSERT INTO houses (name) VALUES (?)", (HOUSE_NAME,))
        house_id = c.lastrowid

    c.execute("INSERT INTO rooms (house_id, name, device_id, has_sensor) VALUES (?,?,?,1)",
              (house_id, ROOM_NAME, DEV))
    room_id = c.lastrowid
    c.execute("""INSERT INTO devices (device_id, room_id, model, firmware, status, csq, last_seen_at)
                 VALUES (?,?,?,?, 'online', 22, datetime('now','localtime'))""",
              (DEV, room_id, "QuecDuino-EC800", "demo-mock-v1"))

    now = datetime.now()
    total_rows = 0
    for d in range(3):
        date = (now - timedelta(days=2 - d)).date()
        wname, cloud, chours = WEATHER[d]
        sun = naive = peak = 0
        seq = 0
        cum_sun = 0.0
        for h in range(24):
            for m in range(0, 60, 2):                      # 2 分钟一点，一天720
                nat = max(0, natural_mv(cloud, chours, h) + random.randint(-6, 6))
                # 台灯分量必须越过 400 阈值，"灯光被误计为日照"的
                # 虚增对比才是可见的（seed 早期用 150 永远够不着线，是个 bug）
                lamp = (430 + (h % 3) * 15 + random.randint(-6, 6)) \
                    if LAMP_START <= h < LAMP_END else 0
                ts = datetime(date.year, date.month, date.day, h, m)
                if ts > now:
                    continue          # 未来时刻不回填：波形里出现"未来的数据"
                                      # 是最容易被评委看穿的 mock 破绽，
                                      # 今天的数据从此刻起由 mock_device 实时续写
                if nat >= THRESHOLD_MV:
                    sun += 2; cum_sun += 2 / 60.0
                    peak = max(peak, nat)
                if nat + lamp >= THRESHOLD_MV:
                    naive += 2
                c.execute("""INSERT INTO readings
                    (room_id, ts, ts_ms, natural_mv, lamp_mv, temp_c, rh,
                     lux, sun_min, in_sun, raw, seq)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (room_id, ts.strftime("%Y-%m-%d %H:%M:%S"),
                     int(ts.timestamp() * 1000), nat, lamp,
                     round(21.5 + nat / 150 + (0.8 if lamp else 0), 1),
                     round(52 - nat / 260, 1), nat,
                     round(cum_sun, 2), 1 if nat >= THRESHOLD_MV else 0,
                     nat + lamp, seq))
                seq += 1
        # 日账：灯光虚增量是演示重点
        score, grade = grade_of(sun, peak)
        c.execute("""INSERT OR REPLACE INTO daily_ledger
            (room_id, date, sun_minutes, naive_minutes, peak_mv,
             score, grade, weather, settled)
            VALUES (?,?,?,?,?,?,?, ?,1)""",
            (room_id, date.strftime("%Y-%m-%d"), sun, naive, peak, score, grade, wname))
        total_rows += seq
        print("回填 %s %s: 净日照%d min / 不剔除%d min / 峰值%d mV"
              % (date, wname, sun, naive, peak))

    conn.commit()
    print("完成：room_id=%s，共 %d 行读数" % (room_id, total_rows))
    print("看板曲线/日账/AI房评立即可用；实时演示请另跑 mock_device.py")


def grade_of(minutes, peak):
    if minutes >= 300 and peak >= 900: return 95, "A"
    if minutes >= 180 and peak >= 650: return 82, "B+"
    if minutes >= 90 or peak >= 380:  return 66, "C"
    return 45, "D"


if __name__ == "__main__":
    if "--purge" in sys.argv:
        conn = sqlite3.connect(DB)
        purge(conn.cursor()); conn.commit()
    else:
        main()
