"""并发与原子性自测：多连接/多线程交分与更正不能产生脏状态或 500。"""

import threading
from collections import Counter

import pytest

from app.db import Database
from app.service import (
    ServiceError,
    apply_correction,
    bulk_set_sequences,
    create_claim,
    register_blocks,
    submit_score,
)


def _seed(path: str) -> Database:
    db = Database(path)
    register_blocks(
        db, [{"block_no": f"B{i}", "content": None} for i in range(1, 5)]
    )
    bulk_set_sequences(
        db,
        [
            {"paper_no": "P1", "blocks": ["B1", "B2"]},
            {"paper_no": "P2", "blocks": ["B3", "B4"]},
        ],
    )
    return db


def test_concurrent_score_revisions_serialized(tmp_path):
    db = _seed(str(tmp_path / "c1.db"))
    cno = create_claim(db, "T")["claim_no"]

    values = list(range(10, 60))
    errors: list[Exception] = []

    def worker(vals):
        try:
            for v in vals:
                submit_score(db, cno, v, None)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(values[i::4],)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    with db.read() as conn:
        valid = conn.execute(
            "SELECT COUNT(*) n FROM score WHERE claim_no=? AND status='valid'",
            (cno,),
        ).fetchone()["n"]
        total = conn.execute(
            "SELECT COUNT(*) n FROM score WHERE claim_no=?", (cno,)
        ).fetchone()["n"]
        assert valid == 1
        assert total == len(values)
        current = conn.execute(
            "SELECT score FROM score WHERE claim_no=? AND status='valid'", (cno,)
        ).fetchone()["score"]
        assert current in values


def test_concurrent_corrections_and_scoring_keep_invariants(tmp_path):
    # 两个独立 Database 实例 = 独立连接/锁，靠 BEGIN IMMEDIATE + SQLite 写锁串行
    db_a = _seed(str(tmp_path / "c2.db"))
    db_b = Database(str(tmp_path / "c2.db"))
    cno = create_claim(db_a, "T")["claim_no"]

    layout_intact = [
        {"paper_no": "P1", "blocks": ["B1", "B2"]},
        {"paper_no": "P2", "blocks": ["B3", "B4"]},
    ]
    layout_split = [
        {"paper_no": "P1", "blocks": ["B1"]},
        {"paper_no": "P2", "blocks": ["B2", "B3", "B4"]},
    ]

    unexpected: list[Exception] = []
    outcomes = Counter()
    stop = threading.Event()

    def score_worker(n):
        i = 0
        while not stop.is_set():
            try:
                submit_score(db_a, cno, 50 + n + i % 5, None)
                outcomes["score_ok"] += 1
            except ServiceError as e:
                if e.code == "CLAIM_VOID":
                    outcomes["score_void_rejected"] += 1
                    return  # 领题号已永久作废，不必再试
                unexpected.append(e)
            except Exception as e:  # noqa: BLE001
                unexpected.append(e)
            i += 1

    def correction_worker(db, parity):
        for i in range(6):
            try:
                apply_correction(db, layout_intact if (i + parity) % 2 == 0 else layout_split)
                outcomes["correction_ok"] += 1
            except Exception as e:  # noqa: BLE001
                unexpected.append(e)

    ts = [threading.Thread(target=score_worker, args=(n,)) for n in range(3)]
    tc = [
        threading.Thread(target=correction_worker, args=(db_a, 0)),
        threading.Thread(target=correction_worker, args=(db_b, 1)),
    ]
    for t in ts + tc:
        t.start()
    for t in tc:
        t.join()
    stop.set()
    for t in ts:
        t.join(timeout=5)

    assert not unexpected, unexpected[:3]
    assert outcomes["correction_ok"] == 12

    # 不变量：4 个块当前各归一次、每块恰好一行
    with db_a.read() as conn:
        rows = conn.execute("SELECT block_no FROM paper_block").fetchall()
        assert len(rows) == 4
        assert sorted(r["block_no"] for r in rows) == ["B1", "B2", "B3", "B4"]

        dup_active = conn.execute(
            """
            SELECT seq_key, COUNT(*) c FROM answer WHERE status='active'
            GROUP BY seq_key HAVING c > 1
            """
        ).fetchall()
        assert not dup_active

    # 最终再提交一次更正必须成功（服务仍可用）
    result = apply_correction(db_a, layout_intact)
    assert result["status"] == "applied"
