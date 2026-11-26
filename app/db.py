"""SQLite 持久层：连接管理、表结构与串行写事务。

所有“更正生效 / 交分”等写操作都通过 :meth:`Database.tx` 进入，
进程内用一把可重入锁串行化，同时事务使用 ``BEGIN IMMEDIATE``，
即使有多个进程/连接也由 SQLite 写锁保证原子性。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS question (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT NOT NULL,
    full_score  REAL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS block (
    block_no       TEXT PRIMARY KEY,   -- 原扫描块号
    content        TEXT,               -- 扫描块上的答案内容（可选，供教师评阅）
    registered_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper (
    paper_no    TEXT PRIMARY KEY,      -- 卷号
    created_at  TEXT NOT NULL
);

-- 当前归卷结果：每个扫描块至多属于一卷、一个位置
CREATE TABLE IF NOT EXISTS paper_block (
    paper_no  TEXT NOT NULL REFERENCES paper(paper_no),
    block_no  TEXT NOT NULL PRIMARY KEY REFERENCES block(block_no),
    position  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_block_paper ON paper_block(paper_no, position);

-- 一份“完整答案”的连续身份：键为严格有序的块序列。
-- 只有 status='active' 的答案是当前答案；被拆拼/调序后旧答案置 void，
-- 即使后来块序列恰好相同，也只会新建答案身份，从而必须重评。
CREATE TABLE IF NOT EXISTS answer (
    id                    TEXT PRIMARY KEY,
    seq_json              TEXT NOT NULL,
    seq_key               TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'active', -- active | void
    current_paper_no      TEXT,
    born_correction_id    INTEGER,
    void_correction_id    INTEGER,
    created_at            TEXT NOT NULL,
    voided_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_answer_paper ON answer(current_paper_no);
CREATE INDEX IF NOT EXISTS idx_answer_status ON answer(status);
-- 当前有效答案的有序块序列必须唯一（物理上块号全局唯一，天然成立）
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_answer_seq
    ON answer(seq_key) WHERE status = 'active';

-- 领题号。答案被作废时其全部领题号一并置 void。
CREATE TABLE IF NOT EXISTS claim (
    claim_no     INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_id    TEXT NOT NULL REFERENCES answer(id),
    teacher_id   TEXT,
    status       TEXT NOT NULL DEFAULT 'active', -- active | submitted | void
    created_at   TEXT NOT NULL,
    submitted_at TEXT,
    released_at  TEXT,
    voided_at    TEXT,
    void_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_claim_answer ON claim(answer_id);

-- 评分流水：同一领题号重新交分以 superseded 保留旧记录，
-- 更正作废时相关 valid 记录置 void，绝不物理删除“已入账评分”。
CREATE TABLE IF NOT EXISTS score (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_no    INTEGER NOT NULL REFERENCES claim(claim_no),
    answer_id   TEXT NOT NULL REFERENCES answer(id),
    score       REAL NOT NULL,
    comment     TEXT,
    revision    INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'valid', -- valid | superseded | void
    created_at  TEXT NOT NULL,
    voided_at   TEXT,
    void_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_score_claim ON score(claim_no);
CREATE INDEX IF NOT EXISTS idx_score_answer ON score(answer_id);

-- 整批更正单（apply 直接生效；stage 先核对、稍后 apply）
CREATE TABLE IF NOT EXISTS correction (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    status       TEXT NOT NULL, -- checked | applied | cancelled
    request_json TEXT NOT NULL,
    plan_json    TEXT,
    created_at   TEXT NOT NULL,
    applied_at   TEXT
);
"""


class Database:
    """持有数据库路径与进程内写锁。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.write_lock = threading.RLock()
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,  # 自己管理 BEGIN/COMMIT
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            # WAL 让核对/查询不阻塞于写事务
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def init_schema(self) -> None:
        with self.write_lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA)
                self._migrate(conn)
            finally:
                conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """对早期版本的库做幂等补列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。"""
        additions = {
            "claim": [("released_at", "TEXT")],
        }
        for table, cols in additions.items():
            present = {
                r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in cols:
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """串行化的立即写事务；异常整体回滚。"""
        with self.write_lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def reset(self) -> None:
        """删除全部业务数据（管理/自测用）。"""
        with self.write_lock:
            conn = self.connect()
            try:
                conn.execute("PRAGMA foreign_keys = OFF")
                tables = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                ]
                for name in tables:
                    if name.startswith("sqlite_"):
                        continue
                    conn.execute(f'DROP TABLE IF EXISTS "{name}"')
                conn.execute("PRAGMA foreign_keys = ON")
            finally:
                conn.close()
        self.init_schema()
