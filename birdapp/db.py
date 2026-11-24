import os
import sqlite3

from flask import current_app, g

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


def _init_db():
    path = current_app.config["DATABASE"]
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        _init_db()
