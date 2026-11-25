import os
import sqlite3

from flask import current_app, g

# 小计结构版本。升级已有数据库时，如果 meta 中没有该版本号，
# 就在一个事务内按记录表全量重建一次小计（见 _init_db）。
SUBTOTALS_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id         TEXT PRIMARY KEY,        -- 记录号
    year              INTEGER NOT NULL,        -- 年份
    version           INTEGER NOT NULL,        -- 名录版（记录所依据的版本）
    sci_name          TEXT NOT NULL,           -- 学名（原名）
    count             INTEGER NOT NULL,        -- 只数
    confirmed_name    TEXT,                    -- 复核确认的学名
    confirmed_version INTEGER                  -- 复核确认的版号
);

CREATE TABLE IF NOT EXISTS mapping (
    from_version INTEGER NOT NULL,             -- 起版
    old_name     TEXT NOT NULL,                -- 旧学名
    next_name    TEXT NOT NULL,                -- 下一版学名
    PRIMARY KEY (from_version, old_name, next_name)
);

-- 按「换算起点」和年份分组的小计。换算起点 =
--   已复核： (confirmed_version, confirmed_name)
--   未复核： (version, sci_name)
-- 汇总时只需读这些小计，不必逐条扫描 records。
CREATE TABLE IF NOT EXISTS subtotals (
    start_version INTEGER NOT NULL,
    start_name    TEXT NOT NULL,
    year          INTEGER NOT NULL,
    total         INTEGER NOT NULL,            -- 该组该年只数合计
    record_count  INTEGER NOT NULL,            -- 该组该年记录条数
    PRIMARY KEY (start_version, start_name, year)
) WITHOUT ROWID;

-- 出现过的换算起点（去重）及其记录条数，供生成候选学名、按起点取小计。
CREATE TABLE IF NOT EXISTS subtotal_groups (
    start_version INTEGER NOT NULL,
    start_name    TEXT NOT NULL,
    ref_count     INTEGER NOT NULL,            -- 该起点下的记录条数
    PRIMARY KEY (start_version, start_name)
) WITHOUT ROWID;

-- 出现过的起点版号（去重）。汇总时只需为这些版本反推对照状态。
-- 删除记录后可能残留旧版号，但对应小计已不存在，不影响结果。
CREATE TABLE IF NOT EXISTS subtotal_versions (
    start_version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 差额明细只按换算起点回查记录，表达式索引让这种回查不必全表扫描。
CREATE INDEX IF NOT EXISTS idx_records_start_key
ON records (
    COALESCE(confirmed_version, version),
    COALESCE(confirmed_name, sci_name),
    year,
    record_id
);

-- 最新版号只需读索引，无需扫描明细。
CREATE INDEX IF NOT EXISTS idx_records_version ON records (version);
CREATE INDEX IF NOT EXISTS idx_records_confirmed_version
ON records (confirmed_version) WHERE confirmed_version IS NOT NULL;

-- 换表核验与首页计数只涉及已复核记录。
CREATE INDEX IF NOT EXISTS idx_records_confirmed
ON records (record_id) WHERE confirmed_name IS NOT NULL;

-- 按（起点版, 起点学名）精确取小计的覆盖索引。
CREATE INDEX IF NOT EXISTS idx_subtotals_key
ON subtotals (start_version, start_name, year, total, record_count);

-- 明细写入/更新/删除时，同步维护小计；同一条 SQL 事务内的明细与小计一起生效。
CREATE TRIGGER IF NOT EXISTS trg_records_subtotal_insert
AFTER INSERT ON records
BEGIN
    INSERT INTO subtotals (start_version, start_name, year, total, record_count)
    VALUES (
        COALESCE(NEW.confirmed_version, NEW.version),
        COALESCE(NEW.confirmed_name, NEW.sci_name),
        NEW.year, NEW.count, 1
    )
    ON CONFLICT (start_version, start_name, year)
    DO UPDATE SET total = total + excluded.total,
                  record_count = record_count + excluded.record_count;
    INSERT INTO subtotal_groups (start_version, start_name, ref_count)
    VALUES (
        COALESCE(NEW.confirmed_version, NEW.version),
        COALESCE(NEW.confirmed_name, NEW.sci_name),
        1
    )
    ON CONFLICT (start_version, start_name)
    DO UPDATE SET ref_count = ref_count + 1;
    INSERT INTO subtotal_versions (start_version)
    VALUES (COALESCE(NEW.confirmed_version, NEW.version))
    ON CONFLICT (start_version) DO NOTHING;
END;

CREATE TRIGGER IF NOT EXISTS trg_records_subtotal_delete
AFTER DELETE ON records
BEGIN
    UPDATE subtotals
       SET total = total - OLD.count,
           record_count = record_count - 1
     WHERE start_version = COALESCE(OLD.confirmed_version, OLD.version)
       AND start_name = COALESCE(OLD.confirmed_name, OLD.sci_name)
       AND year = OLD.year;
    DELETE FROM subtotals
     WHERE start_version = COALESCE(OLD.confirmed_version, OLD.version)
       AND start_name = COALESCE(OLD.confirmed_name, OLD.sci_name)
       AND year = OLD.year
       AND record_count <= 0;
    UPDATE subtotal_groups SET ref_count = ref_count - 1
     WHERE start_version = COALESCE(OLD.confirmed_version, OLD.version)
       AND start_name = COALESCE(OLD.confirmed_name, OLD.sci_name);
    DELETE FROM subtotal_groups
     WHERE start_version = COALESCE(OLD.confirmed_version, OLD.version)
       AND start_name = COALESCE(OLD.confirmed_name, OLD.sci_name)
       AND ref_count <= 0;
END;

-- 年份或换算起点变化：旧组扣减、新组累加，移出与移入两边都更新。
CREATE TRIGGER IF NOT EXISTS trg_records_subtotal_move
AFTER UPDATE OF year, version, sci_name, confirmed_name, confirmed_version ON records
WHEN OLD.year <> NEW.year
  OR COALESCE(OLD.confirmed_version, OLD.version)
     <> COALESCE(NEW.confirmed_version, NEW.version)
  OR COALESCE(OLD.confirmed_name, OLD.sci_name)
     <> COALESCE(NEW.confirmed_name, NEW.sci_name)
BEGIN
    UPDATE subtotals
       SET total = total - OLD.count,
           record_count = record_count - 1
     WHERE start_version = COALESCE(OLD.confirmed_version, OLD.version)
       AND start_name = COALESCE(OLD.confirmed_name, OLD.sci_name)
       AND year = OLD.year;
    DELETE FROM subtotals
     WHERE start_version = COALESCE(OLD.confirmed_version, OLD.version)
       AND start_name = COALESCE(OLD.confirmed_name, OLD.sci_name)
       AND year = OLD.year
       AND record_count <= 0;
    UPDATE subtotal_groups SET ref_count = ref_count - 1
     WHERE start_version = COALESCE(OLD.confirmed_version, OLD.version)
       AND start_name = COALESCE(OLD.confirmed_name, OLD.sci_name);
    DELETE FROM subtotal_groups
     WHERE start_version = COALESCE(OLD.confirmed_version, OLD.version)
       AND start_name = COALESCE(OLD.confirmed_name, OLD.sci_name)
       AND ref_count <= 0;

    INSERT INTO subtotals (start_version, start_name, year, total, record_count)
    VALUES (
        COALESCE(NEW.confirmed_version, NEW.version),
        COALESCE(NEW.confirmed_name, NEW.sci_name),
        NEW.year, NEW.count, 1
    )
    ON CONFLICT (start_version, start_name, year)
    DO UPDATE SET total = total + excluded.total,
                  record_count = record_count + excluded.record_count;
    INSERT INTO subtotal_groups (start_version, start_name, ref_count)
    VALUES (
        COALESCE(NEW.confirmed_version, NEW.version),
        COALESCE(NEW.confirmed_name, NEW.sci_name),
        1
    )
    ON CONFLICT (start_version, start_name)
    DO UPDATE SET ref_count = ref_count + 1;
    INSERT INTO subtotal_versions (start_version)
    VALUES (COALESCE(NEW.confirmed_version, NEW.version))
    ON CONFLICT (start_version) DO NOTHING;
END;

-- 只改只数：分组不变，直接调整组内合计。
CREATE TRIGGER IF NOT EXISTS trg_records_subtotal_count
AFTER UPDATE OF count ON records
WHEN OLD.year = NEW.year
 AND COALESCE(OLD.confirmed_version, OLD.version)
     = COALESCE(NEW.confirmed_version, NEW.version)
 AND COALESCE(OLD.confirmed_name, OLD.sci_name)
     = COALESCE(NEW.confirmed_name, NEW.sci_name)
BEGIN
    UPDATE subtotals
       SET total = total - OLD.count + NEW.count
     WHERE start_version = COALESCE(NEW.confirmed_version, NEW.version)
       AND start_name = COALESCE(NEW.confirmed_name, NEW.sci_name)
       AND year = NEW.year;
END;
"""


def get_db():
    if "db" not in g:
        path = current_app.config["DATABASE"]
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        g.db = conn
    return g.db


def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _backfill_subtotals(conn):
    """按当前记录表全量重建小计与起点集合（只在首次升级时执行一次）。"""
    conn.execute("DELETE FROM subtotals")
    conn.execute("DELETE FROM subtotal_groups")
    conn.execute("DELETE FROM subtotal_versions")
    conn.execute(
        """
        INSERT INTO subtotals (start_version, start_name, year, total, record_count)
        SELECT COALESCE(confirmed_version, version),
               COALESCE(confirmed_name, sci_name),
               year, SUM(count), COUNT(*)
          FROM records
         GROUP BY 1, 2, 3
        """
    )
    conn.execute(
        """
        INSERT INTO subtotal_groups (start_version, start_name, ref_count)
        SELECT start_version, start_name, SUM(record_count)
          FROM subtotals
         GROUP BY start_version, start_name
        """
    )
    conn.execute(
        """
        INSERT INTO subtotal_versions (start_version)
        SELECT DISTINCT start_version FROM subtotals
        """
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('subtotals_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (SUBTOTALS_VERSION,),
    )


def _init_db():
    path = current_app.config["DATABASE"]
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        # 初始化/升级与写入串行化：多进程同时首次启动时只有一个执行全量重建。
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'subtotals_version'"
            ).fetchone()
            if row is None or row[0] != SUBTOTALS_VERSION:
                _backfill_subtotals(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        _init_db()
