"""SQLite 存储层：单写连接 + 每请求只读连接，确认操作在单个事务内完成。

- 写路径（录入/复测/确认）共用一个写连接并由 ``write_lock`` 串行化，配合
  ``BEGIN IMMEDIATE`` 保证事务原子性，确认事务期间并发写入只能排队，无法插进
  事务中间。
- 读路径（GET /preselect）使用每请求独立连接，SQLite 的读事务只能看到查询
  开始前已提交的数据（read-committed）：确认事务未提交或回滚时，读连接看不到
  半成品组记录与临时占用；确认提交后才统一看到整组占用与完整快照。
- 开启 WAL，读写连接互不阻塞。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

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
# 写连接初始化锁（get_conn 惰性建库）与写路径互斥锁分开，避免读连接建库时
# 与写路径竞争。
_init_lock = threading.Lock()
# 确认/写入路径上的互斥锁，配合 BEGIN IMMEDIATE 保证事务原子性
write_lock = threading.Lock()


def _db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def _connect(autocommit: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _db_path(),
        check_same_thread=False,
        timeout=5.0,
        isolation_level=None if autocommit else "",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """返回唯一的写连接（惰性初始化并建库）。

    只能在持有 ``write_lock`` 时用于事务性读写；读请求请改用
    :func:`read_connection`，以免读到该连接上尚未提交的确认事务。
    """
    global _conn
    if _conn is None:
        with _init_lock:
            if _conn is None:
                conn = _connect()
                # WAL：读写互不阻塞，读连接在确认事务进行期间只看到旧的已提交状态
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                _conn = conn
    return _conn


@contextmanager
def read_connection():
    """每请求只读连接：整个读序列在同一个读事务中，看到一致的已提交快照。

    读事务只包含 SELECT，正常退出时提交（释放快照），异常时回滚；连接用完即关，
    绝不参与写事务，因而不可能提前看到未提交/将回滚的组与临时占用。
    """
    get_conn()  # 确保库文件与 schema 已初始化
    conn = _connect(autocommit=True)
    started = False
    try:
        conn.execute("BEGIN")
        started = True
        yield conn
        conn.execute("COMMIT")
    except Exception:
        if started:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def reset() -> None:
    """关闭并丢弃当前连接（测试用，配合 DB_PATH 环境变量换新库）。"""
    global _conn
    with _init_lock:
        if _conn is not None:
            _conn.close()
            _conn = None
