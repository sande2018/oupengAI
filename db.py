# -*- coding: utf-8 -*-
"""SQLite 数据层：建表、连接管理、查询helpers。

连接按线程缓存（Flask 开发服务器 / 后台线程各自一条连接），开启 WAL 与
外键约束，写操作自带 commit，避免上层反复 bookkeeping。
"""
import os
import sqlite3
import threading
import time

from werkzeug.security import generate_password_hash

from config import DB_PATH, ADMIN_DEFAULT_PASSWORD

_local = threading.local()

SCHEMA = """
-- 渠道：一个上游 OpenAI 兼容服务（官方/中转/自建）
CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    base_url        TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    sort            INTEGER NOT NULL DEFAULT 0,
    timeout         INTEGER NOT NULL DEFAULT 180,
    video_path      TEXT    NOT NULL DEFAULT '/videos/generations',
    remark          TEXT    NOT NULL DEFAULT '',
    fail_count      INTEGER NOT NULL DEFAULT 0,
    disabled_until  REAL    NOT NULL DEFAULT 0,
    last_error      TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);

-- 渠道内的多组 API Key（轮换 + 失效冷却）
CREATE TABLE IF NOT EXISTS channel_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    api_key         TEXT    NOT NULL,
    label           TEXT    NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    sort            INTEGER NOT NULL DEFAULT 0,
    success_count   INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    disabled_until  REAL    NOT NULL DEFAULT 0,
    last_used_at    REAL    NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL,
    UNIQUE (channel_id, api_key)
);
CREATE INDEX IF NOT EXISTS idx_keys_channel ON channel_keys(channel_id, sort);

-- 渠道内的模型，types 是能力类型 JSON 数组，如 ["text","vision"]
CREATE TABLE IF NOT EXISTS channel_models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    model_name      TEXT    NOT NULL,
    types           TEXT    NOT NULL DEFAULT '["text"]',
    enabled         INTEGER NOT NULL DEFAULT 1,
    sort            INTEGER NOT NULL DEFAULT 0,
    priority        INTEGER NOT NULL DEFAULT 100,
    weight          INTEGER NOT NULL DEFAULT 100,
    timeout         INTEGER NOT NULL DEFAULT 0,
    remark          TEXT    NOT NULL DEFAULT '',
    fail_count      INTEGER NOT NULL DEFAULT 0,
    disabled_until  REAL    NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    UNIQUE (channel_id, model_name)
);
CREATE INDEX IF NOT EXISTS idx_models_channel ON channel_models(channel_id, sort);

-- 自定义模型（对外暴露给客户端的模型名 = 一个调度组）
CREATE TABLE IF NOT EXISTS custom_models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    display_name    TEXT    NOT NULL DEFAULT '',
    description     TEXT    NOT NULL DEFAULT '',
    strategy        TEXT    NOT NULL DEFAULT 'priority',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);

-- 自定义模型勾选参与调度的渠道模型
CREATE TABLE IF NOT EXISTS custom_model_targets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    custom_model_id   INTEGER NOT NULL REFERENCES custom_models(id) ON DELETE CASCADE,
    channel_model_id  INTEGER NOT NULL REFERENCES channel_models(id) ON DELETE CASCADE,
    enabled           INTEGER NOT NULL DEFAULT 1,
    priority          INTEGER NOT NULL DEFAULT 100,
    weight            INTEGER NOT NULL DEFAULT 100,
    sort              INTEGER NOT NULL DEFAULT 0,
    created_at        REAL    NOT NULL,
    UNIQUE (custom_model_id, channel_model_id)
);
CREATE INDEX IF NOT EXISTS idx_targets_cm ON custom_model_targets(custom_model_id);

-- 知识库
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    chunk_size      INTEGER NOT NULL DEFAULT 500,
    chunk_overlap   INTEGER NOT NULL DEFAULT 80,
    top_k           INTEGER NOT NULL DEFAULT 4,
    min_score       REAL    NOT NULL DEFAULT 0.05,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);

-- 知识库文档
CREATE TABLE IF NOT EXISTS kb_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id           INTEGER NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    title           TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT '',
    content         TEXT    NOT NULL DEFAULT '',
    char_count      INTEGER NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_kb ON kb_documents(kb_id, id);

-- 切片与向量（vector 为 float32 小端 BLOB，已做 L2 归一化）
CREATE TABLE IF NOT EXISTS kb_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id           INTEGER NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    doc_id          INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL DEFAULT 0,
    content         TEXT    NOT NULL,
    vector          BLOB    NOT NULL,
    dim             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_kb ON kb_chunks(kb_id);

-- 站内 API Key
CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL DEFAULT '',
    key             TEXT    NOT NULL UNIQUE,
    prefix          TEXT    NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    rpm_limit       INTEGER NOT NULL DEFAULT 0,
    notes           TEXT    NOT NULL DEFAULT '',
    total_calls     INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL,
    last_used_at    REAL    NOT NULL DEFAULT 0
);

-- API Key 关联的知识库（多对多）
CREATE TABLE IF NOT EXISTS api_key_kbs (
    api_key_id      INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    kb_id           INTEGER NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    PRIMARY KEY (api_key_id, kb_id)
);

-- 调用日志
CREATE TABLE IF NOT EXISTS call_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT    NOT NULL DEFAULT '',
    endpoint            TEXT    NOT NULL DEFAULT '',
    api_key_id          INTEGER NOT NULL DEFAULT 0,
    api_key_name        TEXT    NOT NULL DEFAULT '',
    request_model       TEXT    NOT NULL DEFAULT '',
    resolved_type       TEXT    NOT NULL DEFAULT '',
    channel_id          INTEGER NOT NULL DEFAULT 0,
    channel_name        TEXT    NOT NULL DEFAULT '',
    upstream_model      TEXT    NOT NULL DEFAULT '',
    key_label           TEXT    NOT NULL DEFAULT '',
    rag_hits            INTEGER NOT NULL DEFAULT 0,
    ok                  INTEGER NOT NULL DEFAULT 0,
    status_code         INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    error               TEXT    NOT NULL DEFAULT '',
    created_at          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON call_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_ok ON call_logs(ok, created_at DESC);

-- 系统设置
CREATE TABLE IF NOT EXISTS settings (
    k   TEXT PRIMARY KEY,
    v   TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_SETTINGS = {
    'admin_password_hash': generate_password_hash(ADMIN_DEFAULT_PASSWORD),
    'admin_password_default': '1',
    'rag_top_k': '4',
    'rag_min_score': '0.05',
    'rag_max_chars': '3000',
    'rag_enabled': '1',
}

_DONE_INIT = False
_INIT_LOCK = threading.Lock()


def get_conn():
    """取当前线程的 SQLite 连接（不存在则创建并初始化库）。"""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        return conn
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=5000')
    _local.conn = conn
    init_db()
    return conn


def init_db(force=False):
    """建表 + 写入默认设置（幂等）。"""
    global _DONE_INIT
    with _INIT_LOCK:
        if _DONE_INIT and not force:
            return
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute('INSERT OR IGNORE INTO settings (k, v) VALUES (?, ?)', (k, v))
            # 一次性迁移：旧版本默认最低相似度 0.12 对哈希向量偏高，降到 0.05
            # （只迁移仍是旧默认值的配置，用户手动改成其它值的不动）
            if not conn.execute(
                    "SELECT 1 FROM settings WHERE k='rag_defaults_v2'").fetchone():
                row = conn.execute(
                    "SELECT v FROM settings WHERE k='rag_min_score'").fetchone()
                if row and row[0] in ('0.12', ''):
                    conn.execute("UPDATE settings SET v='0.05' WHERE k='rag_min_score'")
                conn.execute("UPDATE knowledge_bases SET min_score=0.05 WHERE min_score=0.12")
                conn.execute("INSERT OR REPLACE INTO settings (k, v) "
                             "VALUES ('rag_defaults_v2', '1')")
            # 首次初始化时创建一个默认对外模型，做到开箱即用（客户端可直接用 model=oupeng）
            seeded = conn.execute(
                "SELECT v FROM settings WHERE k='seeded_default_model'").fetchone()
            if not seeded:
                if not conn.execute('SELECT id FROM custom_models LIMIT 1').fetchone():
                    now = time.time()
                    conn.execute(
                        'INSERT INTO custom_models (name, display_name, description, '
                        'strategy, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
                        ('oupeng', '欧朋智能调度',
                         '默认调度组：自动使用全部启用的渠道模型', 'priority', 1, now, now))
                conn.execute("INSERT OR REPLACE INTO settings (k, v) "
                             "VALUES ('seeded_default_model', '1')")
            conn.commit()
        finally:
            conn.close()
        _DONE_INIT = True


def reset_conn():
    """丢弃当前线程连接（测试切换数据库时使用）。"""
    global _DONE_INIT
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
    _DONE_INIT = False


def ts():
    """当前时间戳（秒，float）。"""
    return time.time()


# ---------- 查询helpers（与 ai.py 保持同名同风格） ----------
def query_all(sql, params=()):
    return get_conn().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_conn().execute(sql, params).fetchone()


def execute(sql, params=()):
    conn = get_conn()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def executemany(sql, seq):
    conn = get_conn()
    cur = conn.executemany(sql, seq)
    conn.commit()
    return cur


def insert(table, data):
    """插入一行并返回 lastrowid。"""
    cols = list(data.keys())
    sql = 'INSERT INTO %s (%s) VALUES (%s)' % (
        table, ', '.join(cols), ', '.join('?' for _ in cols))
    return execute(sql, [data[c] for c in cols]).lastrowid


def update(table, data, where, params=()):
    """更新一行，返回受影响行数。"""
    cols = list(data.keys())
    sql = 'UPDATE %s SET %s WHERE %s' % (
        table, ', '.join('%s=?' % c for c in cols), where)
    return execute(sql, [data[c] for c in cols] + list(params)).rowcount


# ---------- settings ----------
def get_setting(name, default=''):
    row = query_one('SELECT v FROM settings WHERE k=?', (name,))
    return row['v'] if row else default


def set_setting(name, value):
    execute('INSERT INTO settings (k, v) VALUES (?, ?) '
            'ON CONFLICT(k) DO UPDATE SET v=excluded.v', (name, str(value)))