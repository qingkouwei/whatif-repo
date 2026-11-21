-- 日光账本 · 数据库设计
-- SQLite（单机够用；多设备高并发时换 PostgreSQL，DDL 基本兼容）

PRAGMA journal_mode = WAL;      -- 读写并发
PRAGMA foreign_keys = ON;

-- ============================================================
-- 房屋：物理空间的第一层
-- ============================================================
CREATE TABLE IF NOT EXISTS houses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,              -- 「阳光里 3 栋 1204」
    address     TEXT,
    area        REAL,                          -- 建筑面积 ㎡
    orientation TEXT,                          -- 整体朝向
    floor       INTEGER,
    built_year  INTEGER,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ============================================================
-- 房间：房屋下的第二层
-- ============================================================
CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    house_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,              -- 客厅 / 主卧 / 书房
    orientation TEXT,                          -- 房间窗户朝向
    floor       INTEGER,                       -- 在几楼
    area        REAL,
    has_sensor  INTEGER NOT NULL DEFAULT 1,    -- 是否装了采集设备
    device_id   TEXT,                          -- 对应的设备编号
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (house_id) REFERENCES houses(id) ON DELETE CASCADE,
    UNIQUE (house_id, name)
);

CREATE INDEX IF NOT EXISTS idx_rooms_house ON rooms(house_id);

-- ============================================================
-- 设备：物理采集终端
-- ============================================================
CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT    NOT NULL UNIQUE,      -- solar-ledger-01
    room_id       INTEGER,
    model         TEXT,                        -- QuecDuino-EC800
    firmware      TEXT,
    status        TEXT    NOT NULL DEFAULT 'offline',  -- online / offline
    csq           INTEGER,                      -- 最近一次信号强度
    last_seen_at  TEXT,                        -- 最后上报时间（判离线用）
    installed_at  TEXT,
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE SET NULL
);

-- ============================================================
-- 采样数据：时序主体
-- 关键：natural 与 lamp 必须分开存，而不是只存总照度
-- ============================================================
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    ts          TEXT    NOT NULL,              -- 采样时间 ISO8601
    ts_ms       INTEGER NOT NULL,              -- 设备端 ticks_ms，便于排序
    natural_mv  INTEGER NOT NULL DEFAULT 0,    -- 自然光（mV，已扣分压）
    lamp_mv     INTEGER NOT NULL DEFAULT 0,    -- 人工照明（mV）
    temp_c      REAL,                          -- 温度 ℃
    rh          REAL,                          -- 湿度 %
    lux         INTEGER,                       -- 相对亮度（非物理照度）
    sun_min     INTEGER NOT NULL DEFAULT 0,    -- 截至该时刻的累计有效日照
    in_sun      INTEGER NOT NULL DEFAULT 0,    -- 该点是否处于有效日照中
    raw         INTEGER NOT NULL,              -- ADC 原始读数，供复查
    inst_mv     INTEGER,                       -- 设备瞬时读数(mV/lux)：账本不用，调试台快线用
    seq         INTEGER,                       -- 设备端序号，用于去重
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE
);

-- 按房间+时间查询是最高频操作
CREATE INDEX IF NOT EXISTS idx_readings_room_ts ON readings(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

-- ============================================================
-- 日账：按天聚合，等效于房间的「采光结算单」
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id       INTEGER NOT NULL,
    date          TEXT    NOT NULL,            -- YYYY-MM-DD
    sun_minutes   INTEGER NOT NULL DEFAULT 0,  -- 净有效日照（已剔除灯光）
    naive_minutes INTEGER NOT NULL DEFAULT 0,  -- 若不剔除灯光会算出的值
    peak_mv       INTEGER NOT NULL DEFAULT 0,
    score         INTEGER NOT NULL DEFAULT 0,
    grade         TEXT,                        -- A / B+ / C / D
    weather       TEXT,
    settled       INTEGER NOT NULL DEFAULT 0,  -- 是否已结算
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
    UNIQUE (room_id, date)                    -- 同一天重复上报则覆盖
);

CREATE INDEX IF NOT EXISTS idx_ledger_room_date ON daily_ledger(room_id, date);

-- ============================================================
-- 事件：告警与恢复，成对出现
-- ============================================================
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER,
    ts          TEXT    NOT NULL,
    type        TEXT    NOT NULL,              -- alert / recover / ack
    kind        TEXT,                          -- soil_dry / too_bright ...
    label       TEXT,                          -- 中文描述
    detail      TEXT,
    acked       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_room_ts ON events(room_id, ts);

-- ============================================================
-- 视图：给前端用的宽表，省得每次 JOIN
-- ============================================================
CREATE VIEW IF NOT EXISTS v_room_latest AS
SELECT
    r.id            AS room_id,
    r.name          AS room_name,
    r.orientation,
    r.house_id,
    h.name          AS house_name,
    d.device_id,
    d.status        AS device_status,
    d.last_seen_at,
    d.csq,
    l.date,
    l.sun_minutes,
    l.peak_mv,
    l.score,
    l.grade,
    l.naive_minutes,
    -- 灯光虚增量：这是数据质量的直接体现
    COALESCE(l.naive_minutes - l.sun_minutes, 0) AS lamp_inflation,
    (SELECT COUNT(*) FROM events e
      WHERE e.room_id = r.id AND e.type = 'alert' AND e.acked = 0
    ) AS open_alerts
FROM rooms r
JOIN houses h ON h.id = r.house_id
LEFT JOIN devices d ON d.room_id = r.id
LEFT JOIN daily_ledger l
       ON l.room_id = r.id
      AND l.date = (SELECT MAX(date) FROM daily_ledger WHERE room_id = r.id);

-- ============================================================
-- 设备离线判定：超过 3 个采样周期未上报视为离线
-- ============================================================
CREATE VIEW IF NOT EXISTS v_device_health AS
SELECT
    d.device_id,
    d.room_id,
    d.status,
    d.last_seen_at,
    d.csq,
    CAST((julianday('now', 'localtime') - julianday(d.last_seen_at)) * 24 * 60
         AS INTEGER) AS minutes_since_last,
    CASE
        WHEN d.last_seen_at IS NULL THEN 'never_seen'
        WHEN (julianday('now','localtime') - julianday(d.last_seen_at)) * 24 * 60 > 30
            THEN 'stale'
        ELSE 'live'
    END AS freshness
FROM devices d;