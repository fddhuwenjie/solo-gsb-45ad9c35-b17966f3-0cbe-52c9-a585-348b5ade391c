"""SQLite 存储层：单连接 + 写锁，确认操作在单个事务内完成。"""
import os
import sqlite3
import threading

DEFAULT_DB_PATH = "battery.db"

MAX_CELLS = 12  # 最多录入 12 节待选电芯

SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    cell_id      TEXT PRIMARY KEY,
    chemistry    TEXT NOT NULL,
    capacity     REAL NOT NULL,
    resistance   REAL NOT NULL,
    test_version TEXT NOT NULL,
    occupied     INTEGER NOT NULL DEFAULT 0,
    group_id     INTEGER
);

CREATE TABLE IF NOT EXISTS groups (
    group_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chemistry        TEXT NOT NULL,
    series_count     INTEGER NOT NULL,
    limited_capacity REAL NOT NULL,
    created_at       TEXT NOT NULL
);

-- 检测快照：确认时写入，之后不可改写
CREATE TABLE IF NOT EXISTS group_cells (
    group_id     INTEGER NOT NULL REFERENCES groups(group_id),
    cell_id      TEXT NOT NULL,
    capacity     REAL NOT NULL,
    resistance   REAL NOT NULL,
    test_version TEXT NOT NULL,
    PRIMARY KEY (group_id, cell_id)
);

CREATE TRIGGER IF NOT EXISTS trg_groups_no_update BEFORE UPDATE ON groups
BEGIN SELECT RAISE(ABORT, 'groups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_groups_no_delete BEFORE DELETE ON groups
BEGIN SELECT RAISE(ABORT, 'groups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_group_cells_no_update BEFORE UPDATE ON group_cells
BEGIN SELECT RAISE(ABORT, 'group snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_group_cells_no_delete BEFORE DELETE ON group_cells
BEGIN SELECT RAISE(ABORT, 'group snapshots are immutable'); END;
"""

_conn: sqlite3.Connection | None = None
# 确认/写入路径上的互斥锁，配合 BEGIN IMMEDIATE 保证事务原子性
write_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.environ.get("DB_PATH", DEFAULT_DB_PATH)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)
    return _conn


def reset() -> None:
    """关闭并丢弃当前连接（测试用，配合 DB_PATH 环境变量换新库）。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
