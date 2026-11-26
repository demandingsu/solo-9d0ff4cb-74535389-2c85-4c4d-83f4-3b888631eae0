"""核心领域逻辑：归卷录入、领取/交分、整批更正与评分保全。

关键不变量
----------
1. 一次整批更正只处理“同一题”，且必须覆盖全部原扫描块，每块恰好一次。
2. 一份答案的身份 = 严格有序的块序列 + 连续 lineage：
   - 更正后若某卷的有序块序列与更正前某份当前答案完全一致，则视为同一份
     答案完整保留（哪怕换到另一张卷），其领题号与有效评分随之迁移；
   - 否则（拆分 / 合并 / 调序 / 新拼）旧答案作废，生成新的待评答案，
     旧领题号与旧评分一并作废；
   - 作废答案不会再参与“序列相同”的继承匹配，因此后来拼回原样仍需重评。
3. 更正与交分都在 ``BEGIN IMMEDIATE`` 事务内完成；更正失败整体回滚，
   归卷与已入账评分保持不变。
"""

from __future__ import annotations

import json
import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from .db import Database

QUESTION_ID = "Q1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seq_key(blocks: list[str]) -> str:
    """严格有序序列的规范键（顺序不同则键不同）。"""
    return json.dumps(list(blocks), ensure_ascii=False, separators=(",", ":"))


def parse_seq(seq_json: str) -> list[str]:
    return list(json.loads(seq_json))


class ServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[list[dict[str, Any]] | dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _new_answer_id() -> str:
    return "A-" + uuid.uuid4().hex[:12]


def _norm_block_value(v: Any) -> str:
    if isinstance(v, dict):
        for k in ("block_no", "blockNo", "block", "id", "块号"):
            if k in v and v[k] is not None:
                v = v[k]
                break
    return str(v).strip()


def normalize_blocks(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        import re

        parts = [p for p in re.split(r"[,，、;；\s]+", raw.strip()) if p]
        return [str(p).strip() for p in parts]
    if isinstance(raw, (list, tuple)):
        return [_norm_block_value(v) for v in raw]
    return [str(raw).strip()]


def normalize_assignments(raw: Any) -> list[dict[str, Any]]:
    """把多种宽松输入形态归一成 [{'paper_no': str, 'blocks': [str]}]。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        for k in ("assignments", "corrections", "layout", "items", "batch"):
            if isinstance(raw.get(k), list):
                raw = raw[k]
                break
        else:
            papers = raw.get("papers")
            if isinstance(papers, dict):
                out: list[dict[str, Any]] = []
                for pno, val in papers.items():
                    blocks = val.get("blocks") if isinstance(val, dict) else val
                    out.append(
                        {"paper_no": str(pno), "blocks": normalize_blocks(blocks)}
                    )
                return out
            return []
    if not isinstance(raw, list):
        return []

    out = []
    for item in raw:
        if isinstance(item, dict):
            pno = None
            for k in (
                "paper_no",
                "paperNo",
                "paper_id",
                "paper",
                "卷号",
                "试卷号",
            ):
                if item.get(k) is not None:
                    pno = item[k]
                    break
            blocks_raw = None
            for k in (
                "blocks",
                "block_nos",
                "blockNos",
                "sequence",
                "seq",
                "order",
                "块号",
            ):
                if item.get(k) is not None:
                    blocks_raw = item[k]
                    break
            out.append(
                {
                    "paper_no": "" if pno is None else str(pno).strip(),
                    "blocks": normalize_blocks(blocks_raw),
                }
            )
        else:
            # 裸字符串没法表达卷号，跳过（校验阶段会报缺卷号）
            out.append({"paper_no": "", "blocks": normalize_blocks(item)})
    return out


def seed_defaults(db: Database) -> None:
    with db.tx() as conn:
        exists = conn.execute("SELECT id FROM question WHERE id=1").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO question(id,name,full_score,created_at) VALUES (1,?,?,?)",
                ("统一试题（本服务只处理同一题）", None, now_iso()),
            )


def seed_demo(db: Database) -> dict[str, Any]:
    """写入一套固定演示数据（幂等、追加，不清空已有数据）。"""
    blocks = [
        {"block_no": f"DB{i}", "content": f"演示：第1题扫描块 {i} 的作答内容"}
        for i in range(1, 7)
    ]
    register_blocks(db, blocks)
    result = bulk_set_sequences(
        db,
        [
            {"paper_no": "DEMO-P1", "blocks": ["DB1", "DB2", "DB3"]},
            {"paper_no": "DEMO-P2", "blocks": ["DB4", "DB5"]},
            {"paper_no": "DEMO-P3", "blocks": ["DB6"]},
        ],
    )
    return {
        "seeded": True,
        "hint": "现在可 POST /claims 领取答案，再 POST /claims/{claim_no}/score 交分",
        "papers": result["papers"],
    }


# --------------------------------------------------------------------------- #
# 试题（单例）
# --------------------------------------------------------------------------- #

def get_question(db: Database) -> dict[str, Any]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM question WHERE id=1").fetchone()
        return dict(row) if row else {"id": 1, "name": None, "full_score": None}


def update_question(
    db: Database, name: Optional[str], full_score: Optional[float]
) -> dict[str, Any]:
    if full_score is not None and (
        not math.isfinite(full_score) or full_score <= 0
    ):
        raise ServiceError(422, "INVALID_FULL_SCORE", "满分必须是大于 0 的有限数值")
    with db.tx() as conn:
        if name is not None:
            conn.execute("UPDATE question SET name=? WHERE id=1", (name.strip(),))
        if full_score is not None:
            conn.execute(
                "UPDATE question SET full_score=? WHERE id=1", (full_score,)
            )
        row = conn.execute("SELECT * FROM question WHERE id=1").fetchone()
        return dict(row)


# --------------------------------------------------------------------------- #
# 扫描块
# --------------------------------------------------------------------------- #

def register_blocks(db: Database, items: list[dict[str, Any]]) -> dict[str, Any]:
    normalized: list[dict[str, Optional[str]]] = []
    seen: set[str] = set()
    for it in items:
        bn = str(it.get("block_no", "")).strip()
        if not bn:
            raise ServiceError(
                422, "INVALID_BLOCK_NO", "存在空的扫描块号", details=[it]
            )
        if bn in seen:
            continue  # 同批重复登记按幂等处理
        seen.add(bn)
        content = it.get("content")
        normalized.append(
            {"block_no": bn, "content": content if content not in ("",) else None}
        )

    with db.tx() as conn:
        ts = now_iso()
        for it in normalized:
            conn.execute(
                """
                INSERT INTO block(block_no, content, registered_at)
                VALUES (:block_no, :content, :ts)
                ON CONFLICT(block_no) DO UPDATE SET
                    content = CASE
                        WHEN excluded.content IS NOT NULL THEN excluded.content
                        ELSE block.content
                    END
                """,
                {**it, "ts": ts},
            )
        rows = conn.execute(
            "SELECT block_no, content, registered_at FROM block ORDER BY block_no"
        ).fetchall()
        return {"registered": len(normalized), "blocks": [dict(r) for r in rows]}


def list_blocks(db: Database) -> list[dict[str, Any]]:
    with db.read() as conn:
        rows = conn.execute(
            """
            SELECT b.block_no, b.content, b.registered_at,
                   pb.paper_no, pb.position
            FROM block b
            LEFT JOIN paper_block pb ON pb.block_no = b.block_no
            ORDER BY b.block_no
            """
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 归卷录入（按卷录入有序块串）
# --------------------------------------------------------------------------- #

def _set_sequence_conn(
    conn, paper_no: str, blocks: list[str], ts: str
) -> dict[str, Any]:
    pno = (paper_no or "").strip()
    if not pno:
        raise ServiceError(422, "EMPTY_PAPER_NO", "卷号不能为空")

    clean: list[str] = []
    local_seen: set[str] = set()
    for b in blocks:
        bn = str(b).strip()
        if not bn:
            raise ServiceError(
                422, "EMPTY_BLOCK_NO", f"卷 {pno} 的序列中存在空块号"
            )
        if bn in local_seen:
            raise ServiceError(
                422,
                "DUPLICATE_BLOCK_IN_PAPER",
                f"卷 {pno} 的序列中扫描块 {bn} 重复",
                details=[{"paper_no": pno, "block_no": bn}],
            )
        local_seen.add(bn)
        clean.append(bn)

    # 录入时出现的块号即“原扫描块”；更正阶段不允许再引入新块
    for bn in clean:
        conn.execute(
            "INSERT OR IGNORE INTO block(block_no, content, registered_at) VALUES (?, NULL, ?)",
            (bn, ts),
        )

    # 不允许通过按卷录入把已归到其他卷的块“抢”过来 —— 跨卷移动必须走整批更正
    if clean:
        placeholders = ",".join("?" for _ in clean)
        conflicts = conn.execute(
            f"""
            SELECT block_no, paper_no FROM paper_block
            WHERE paper_no <> ? AND block_no IN ({placeholders})
            """,
            [pno, *clean],
        ).fetchall()
        if conflicts:
            raise ServiceError(
                409,
                "BLOCK_ASSIGNED_TO_OTHER_PAPER",
                "有些扫描块已录入到其他卷；跨卷归卷请提交整批更正 /corrections",
                details=[dict(r) for r in conflicts],
            )

    conn.execute(
        "INSERT OR IGNORE INTO paper(paper_no, created_at) VALUES (?, ?)",
        (pno, ts),
    )

    old = conn.execute(
        "SELECT * FROM answer WHERE status='active' AND current_paper_no=?",
        (pno,),
    ).fetchone()

    disposition = "empty"
    answer_id: Optional[str] = None

    if not clean:
        if old is not None:
            _void_answer_conn(
                conn, old["id"], ts, "sequence_reentered_empty", None
            )
        conn.execute("DELETE FROM paper_block WHERE paper_no=?", (pno,))
    else:
        key = seq_key(clean)
        if old is not None and old["seq_key"] == key:
            disposition = "unchanged"
            answer_id = old["id"]
        else:
            if old is not None:
                _void_answer_conn(
                    conn, old["id"], ts, "sequence_reentered_changed", None
                )
            answer_id = _new_answer_id()
            conn.execute(
                """
                INSERT INTO answer(id, seq_json, seq_key, status,
                                   current_paper_no, created_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (answer_id, json.dumps(clean, ensure_ascii=False), key, pno, ts),
            )
            disposition = "created" if old is None else "replaced_needs_grading"

        conn.execute("DELETE FROM paper_block WHERE paper_no=?", (pno,))
        for pos, bn in enumerate(clean):
            conn.execute(
                "INSERT INTO paper_block(paper_no, block_no, position) VALUES (?, ?, ?)",
                (pno, bn, pos),
            )

    return {
        "paper_no": pno,
        "blocks": clean,
        "answer_id": answer_id,
        "disposition": disposition,
    }


def set_paper_sequence(
    db: Database, paper_no: str, blocks: list[str]
) -> dict[str, Any]:
    ts = now_iso()
    with db.tx() as conn:
        result = _set_sequence_conn(conn, paper_no, blocks, ts)
        detail = _paper_detail_conn(conn, result["paper_no"])
        detail["entry_disposition"] = result["disposition"]
        return detail


def bulk_set_sequences(
    db: Database, assignments: list[dict[str, Any]]
) -> dict[str, Any]:
    if not assignments:
        raise ServiceError(
            422, "EMPTY_ASSIGNMENTS", "请至少提供一卷的块序列 assignments"
        )

    # 整批录入自身的跨卷重复检查
    paper_seen: set[str] = set()
    block_owner: dict[str, str] = {}
    for a in assignments:
        pno = str(a.get("paper_no", "")).strip()
        if not pno:
            raise ServiceError(422, "EMPTY_PAPER_NO", "存在没有卷号的录入项")
        if pno in paper_seen:
            raise ServiceError(
                422,
                "DUPLICATE_PAPER_IN_BATCH",
                f"整批录入中卷 {pno} 出现了多次",
            )
        paper_seen.add(pno)
        for bn in normalize_blocks(a.get("blocks")):
            bn = bn.strip()
            if bn in block_owner:
                raise ServiceError(
                    422,
                    "DUPLICATE_BLOCK_IN_BATCH",
                    f"扫描块 {bn} 在整批录入中被多卷使用",
                    details=[
                        {"block_no": bn, "papers": [block_owner[bn], pno]}
                    ],
                )
            block_owner[bn] = pno

    ts = now_iso()
    results = []
    with db.tx() as conn:
        for a in assignments:
            results.append(
                _set_sequence_conn(
                    conn,
                    str(a["paper_no"]).strip(),
                    normalize_blocks(a.get("blocks")),
                    ts,
                )
            )
        papers = [_paper_detail_conn(conn, r["paper_no"]) for r in results]
    return {"entered": len(results), "papers": papers}


# --------------------------------------------------------------------------- #
# 作废与查询辅助
# --------------------------------------------------------------------------- #

def _void_answer_conn(
    conn,
    answer_id: str,
    ts: str,
    reason: str,
    correction_id: Optional[int],
) -> None:
    conn.execute(
        """
        UPDATE answer
        SET status='void', voided_at=?, void_correction_id=COALESCE(?, void_correction_id)
        WHERE id=? AND status='active'
        """,
        (ts, correction_id, answer_id),
    )
    conn.execute(
        """
        UPDATE claim
        SET status='void', voided_at=?, void_reason=?
        WHERE answer_id=? AND status <> 'void'
        """,
        (ts, reason, answer_id),
    )
    conn.execute(
        """
        UPDATE score
        SET status='void', voided_at=?, void_reason=?
        WHERE answer_id=? AND status='valid'
        """,
        (ts, reason, answer_id),
    )


def _live_claim_conn(conn, answer_id: str) -> Optional[Any]:
    return conn.execute(
        """
        SELECT * FROM claim
        WHERE answer_id=? AND status IN ('active','submitted')
        ORDER BY claim_no DESC LIMIT 1
        """,
        (answer_id,),
    ).fetchone()


def _valid_score_conn(conn, answer_id: str) -> Optional[Any]:
    return conn.execute(
        """
        SELECT * FROM score
        WHERE answer_id=? AND status='valid'
        ORDER BY id DESC LIMIT 1
        """,
        (answer_id,),
    ).fetchone()


def _paper_detail_conn(conn, paper_no: str, paper_row=None) -> dict[str, Any]:
    if paper_row is None:
        paper_row = conn.execute(
            "SELECT * FROM paper WHERE paper_no=?", (paper_no,)
        ).fetchone()
    if paper_row is None:
        raise ServiceError(404, "PAPER_NOT_FOUND", f"找不到卷号 {paper_no}")

    block_rows = conn.execute(
        "SELECT block_no FROM paper_block WHERE paper_no=? ORDER BY position",
        (paper_no,),
    ).fetchall()
    blocks = [r["block_no"] for r in block_rows]

    answer = conn.execute(
        "SELECT * FROM answer WHERE status='active' AND current_paper_no=?",
        (paper_no,),
    ).fetchone()

    base: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "paper_no": paper_no,
        "blocks": blocks,
        "answer_id": answer["id"] if answer else None,
        "queried_at": now_iso(),
    }

    if answer is None or not parse_seq(answer["seq_json"]):
        base.update(
            {
                "has_answer": False,
                "status": "empty",
                "status_label": "该卷没有答题块",
                "grading_status": "empty",
                "score": None,
                "score_id": None,
                "comment": None,
                "claim_no": None,
                "teacher_id": None,
                "scored_at": None,
            }
        )
        return base

    claim = _live_claim_conn(conn, answer["id"])
    score = _valid_score_conn(conn, answer["id"])

    if score is not None:
        base.update(
            {
                "has_answer": True,
                "status": "scored",
                "status_label": "已评分",
                "grading_status": "scored",
                "score": score["score"],
                "score_id": score["id"],
                "comment": score["comment"],
                "scored_at": score["created_at"],
                "claim_no": score["claim_no"],
                "teacher_id": claim["teacher_id"] if claim else None,
            }
        )
    elif claim is not None and claim["status"] == "active":
        base.update(
            {
                "has_answer": True,
                "status": "pending",
                "status_label": "待评（教师已领取，待交分）",
                "grading_status": "claimed",
                "score": None,
                "score_id": None,
                "comment": None,
                "scored_at": None,
                "claim_no": claim["claim_no"],
                "teacher_id": claim["teacher_id"],
            }
        )
    else:
        base.update(
            {
                "has_answer": True,
                "status": "pending",
                "status_label": "待评（待领取）",
                "grading_status": "unclaimed",
                "score": None,
                "score_id": None,
                "comment": None,
                "scored_at": None,
                "claim_no": None,
                "teacher_id": None,
            }
        )
    return base


def paper_detail(db: Database, paper_no: str) -> dict[str, Any]:
    with db.read() as conn:
        return _paper_detail_conn(conn, paper_no)


def list_papers(db: Database) -> list[dict[str, Any]]:
    with db.read() as conn:
        rows = conn.execute("SELECT paper_no FROM paper ORDER BY paper_no").fetchall()
        return [_paper_detail_conn(conn, r["paper_no"]) for r in rows]


def overview(db: Database) -> dict[str, Any]:
    with db.read() as conn:
        papers = [
            _paper_detail_conn(conn, r["paper_no"])
            for r in conn.execute("SELECT paper_no FROM paper ORDER BY paper_no")
        ]
        unassigned = [
            r["block_no"]
            for r in conn.execute(
                """
                SELECT b.block_no FROM block b
                LEFT JOIN paper_block pb ON pb.block_no=b.block_no
                WHERE pb.block_no IS NULL ORDER BY b.block_no
                """
            )
        ]
        counts = Counter(p["status"] for p in papers)
        return {
            "question_id": QUESTION_ID,
            "paper_count": len(papers),
            "status_counts": {
                "scored": counts.get("scored", 0),
                "pending": counts.get("pending", 0),
                "empty": counts.get("empty", 0),
            },
            "unassigned_blocks": unassigned,
            "papers": papers,
        }


# --------------------------------------------------------------------------- #
# 教师领取 / 交分
# --------------------------------------------------------------------------- #

def _claim_dict_conn(conn, claim_row) -> dict[str, Any]:
    answer = conn.execute(
        "SELECT * FROM answer WHERE id=?", (claim_row["answer_id"],)
    ).fetchone()
    block_nos = parse_seq(answer["seq_json"]) if answer else []

    contents = {}
    if block_nos:
        ph = ",".join("?" for _ in block_nos)
        for r in conn.execute(
            f"SELECT block_no, content FROM block WHERE block_no IN ({ph})",
            block_nos,
        ):
            contents[r["block_no"]] = r["content"]

    blocks = [
        {
            "position": i,
            "block_no": bn,
            "content": contents.get(bn),
        }
        for i, bn in enumerate(block_nos)
    ]

    score = conn.execute(
        "SELECT * FROM score WHERE claim_no=? AND status='valid' ORDER BY id DESC LIMIT 1",
        (claim_row["claim_no"],),
    ).fetchone()

    data = {
        "claim_no": claim_row["claim_no"],
        "question_id": QUESTION_ID,
        "answer_id": claim_row["answer_id"],
        "teacher_id": claim_row["teacher_id"],
        "status": claim_row["status"],
        "blocks": blocks,
        "claimed_at": claim_row["created_at"],
        "submitted_at": claim_row["submitted_at"],
    }
    if claim_row["status"] == "void":
        data["voided_at"] = claim_row["voided_at"]
        data["void_reason"] = claim_row["void_reason"]
    if claim_row["status"] == "released":
        data["released_at"] = claim_row["released_at"]
    if score is not None:
        data["current_score"] = score["score"]
        data["comment"] = score["comment"]
    return data


def create_claim(
    db: Database,
    teacher_id: Optional[str],
    answer_id: Optional[str] = None,
) -> dict[str, Any]:
    tid = teacher_id.strip() if isinstance(teacher_id, str) and teacher_id.strip() else None
    aid = answer_id.strip() if isinstance(answer_id, str) and answer_id.strip() else None
    ts = now_iso()
    with db.tx() as conn:
        if aid is not None:
            answer = conn.execute(
                """
                SELECT a.* FROM answer a
                WHERE a.id=? AND a.status='active' AND a.seq_key <> '[]'
                  AND NOT EXISTS (
                      SELECT 1 FROM claim c
                      WHERE c.answer_id=a.id AND c.status IN ('active','submitted')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM score s
                      WHERE s.answer_id=a.id AND s.status='valid'
                  )
                """,
                (aid,),
            ).fetchone()
            if answer is None:
                exists = conn.execute(
                    "SELECT id,status FROM answer WHERE id=?", (aid,)
                ).fetchone()
                if exists is None:
                    raise ServiceError(
                        404, "ANSWER_NOT_FOUND", f"找不到答案 {aid}"
                    )
                raise ServiceError(
                    409,
                    "ANSWER_NOT_CLAIMABLE",
                    "该答案当前不可领取：可能已被领取、已评分，或已在更正中作废",
                )
        else:
            answer = conn.execute(
                """
                SELECT a.* FROM answer a
                WHERE a.status='active'
                  AND a.seq_key <> '[]'
                  AND NOT EXISTS (
                      SELECT 1 FROM claim c
                      WHERE c.answer_id=a.id AND c.status IN ('active','submitted')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM score s
                      WHERE s.answer_id=a.id AND s.status='valid'
                  )
                ORDER BY a.rowid ASC
                LIMIT 1
                """
            ).fetchone()
        if answer is None and aid is None:
            raise ServiceError(
                409,
                "NO_CLAIMABLE_ANSWER",
                "当前没有可领取的答案：可能都已领取待交分、已评分，或尚无归卷数据",
            )

        cur = conn.execute(
            """
            INSERT INTO claim(answer_id, teacher_id, status, created_at)
            VALUES (?, ?, 'active', ?)
            """,
            (answer["id"], tid, ts),
        )
        claim = conn.execute(
            "SELECT * FROM claim WHERE claim_no=?", (cur.lastrowid,)
        ).fetchone()
        return _claim_dict_conn(conn, claim)


def get_claim(db: Database, claim_no: int) -> dict[str, Any]:
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM claim WHERE claim_no=?", (claim_no,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "CLAIM_NOT_FOUND", f"找不到领题号 {claim_no}")
        return _claim_dict_conn(conn, row)


def list_claims(db: Database, status_filter: Optional[str] = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM claim"
    params: list[Any] = []
    if status_filter:
        q += " WHERE status=?"
        params.append(status_filter)
    q += " ORDER BY claim_no DESC"
    with db.read() as conn:
        return [_claim_dict_conn(conn, r) for r in conn.execute(q, params).fetchall()]


def release_claim(db: Database, claim_no: int) -> dict[str, Any]:
    """教师把未交分的领取释放回领取池（已交分/已作废不可释放）。"""
    ts = now_iso()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM claim WHERE claim_no=?", (claim_no,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "CLAIM_NOT_FOUND", f"找不到领题号 {claim_no}")
        if row["status"] == "void":
            raise ServiceError(
                409, "CLAIM_VOID", f"领题号 {claim_no} 已作废，无需释放"
            )
        if row["status"] == "submitted":
            raise ServiceError(
                409,
                "CLAIM_ALREADY_SUBMITTED",
                f"领题号 {claim_no} 已交分，不能释放；如需改分请直接重新提交",
            )
        if row["status"] == "active":
            conn.execute(
                "UPDATE claim SET status='released', released_at=? WHERE claim_no=?",
                (ts, claim_no),
            )
        row = conn.execute(
            "SELECT * FROM claim WHERE claim_no=?", (claim_no,)
        ).fetchone()
        return _claim_dict_conn(conn, row)


def submit_score(
    db: Database, claim_no: Any, score: float, comment: Optional[str]
) -> dict[str, Any]:
    try:
        cno = int(claim_no)
    except (TypeError, ValueError):
        raise ServiceError(
            422, "INVALID_CLAIM_NO", f"领题号必须是整数，收到 {claim_no!r}"
        )

    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ServiceError(422, "INVALID_SCORE", "分数必须是数值")
    score = float(score)
    if not math.isfinite(score):
        raise ServiceError(422, "INVALID_SCORE", "分数必须是有限数值")
    if score < 0:
        raise ServiceError(422, "SCORE_OUT_OF_RANGE", "分数不能为负")

    ts = now_iso()
    with db.tx() as conn:
        q = conn.execute("SELECT full_score FROM question WHERE id=1").fetchone()
        full_score = q["full_score"] if q is not None else None
        if full_score is not None and score > float(full_score) + 1e-9:
            raise ServiceError(
                422,
                "SCORE_OUT_OF_RANGE",
                f"分数 {score} 超过本题满分 {full_score}",
                details={"score": score, "full_score": full_score},
            )
        claim = conn.execute(
            "SELECT * FROM claim WHERE claim_no=?", (cno,)
        ).fetchone()
        if claim is None:
            raise ServiceError(404, "CLAIM_NOT_FOUND", f"找不到领题号 {cno}")
        if claim["status"] == "void":
            raise ServiceError(
                409,
                "CLAIM_VOID",
                f"领题号 {cno} 已因拆拼/调序更正而作废，答案需要重新领取并评分",
                details={
                    "voided_at": claim["voided_at"],
                    "void_reason": claim["void_reason"],
                },
            )
        if claim["status"] == "released":
            raise ServiceError(
                409,
                "CLAIM_RELEASED",
                f"领题号 {cno} 已被教师释放回领取池，请重新领取后再交分",
                details={"released_at": claim["released_at"]},
            )

        prev_count = conn.execute(
            "SELECT COUNT(*) AS n FROM score WHERE claim_no=?", (cno,)
        ).fetchone()["n"]
        revision = int(prev_count) + 1

        # 同一领题号重复交分：旧的有效分转为 superseded，保留入账痕迹
        conn.execute(
            "UPDATE score SET status='superseded' WHERE claim_no=? AND status='valid'",
            (cno,),
        )
        cur = conn.execute(
            """
            INSERT INTO score(claim_no, answer_id, score, comment,
                               revision, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'valid', ?)
            """,
            (cno, claim["answer_id"], score, comment, revision, ts),
        )
        conn.execute(
            "UPDATE claim SET status='submitted', submitted_at=? WHERE claim_no=?",
            (ts, cno),
        )
        row = conn.execute(
            "SELECT * FROM claim WHERE claim_no=?", (cno,)
        ).fetchone()
        result = _claim_dict_conn(conn, row)
        result["score_id"] = cur.lastrowid
        result["revision"] = revision
        return result


# --------------------------------------------------------------------------- #
# 整批更正：校验 / 方案 / 生效
# --------------------------------------------------------------------------- #

def _validate_batch_conn(conn, assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []

    if not isinstance(assignments, list):
        raise ServiceError(
            422, "INVALID_CORRECTION_BODY", "assignments 必须是列表"
        )

    known = {r["block_no"] for r in conn.execute("SELECT block_no FROM block")}
    if not assignments and known:
        errors.append(
            {
                "code": "EMPTY_ASSIGNMENTS",
                "message": "更正批次为空，但存在原扫描块；每块都必须恰好使用一次",
            }
        )

    norm: list[dict[str, Any]] = []
    used: Counter = Counter()
    paper_seen: set[str] = set()

    for idx, a in enumerate(assignments):
        if not isinstance(a, dict):
            errors.append(
                {"code": "INVALID_ASSIGNMENT", "index": idx, "message": "每卷必须是对象"}
            )
            continue
        pno = str(a.get("paper_no", "") or "").strip()
        blocks = [str(b).strip() for b in normalize_blocks(a.get("blocks"))]

        if not pno:
            errors.append(
                {"code": "EMPTY_PAPER_NO", "index": idx, "message": "存在空卷号"}
            )
        elif pno in paper_seen:
            errors.append(
                {
                    "code": "DUPLICATE_PAPER_IN_BATCH",
                    "paper_no": pno,
                    "message": f"卷 {pno} 在同一更正批次中出现多次",
                }
            )
        else:
            paper_seen.add(pno)

        local: set[str] = set()
        for j, bn in enumerate(blocks):
            if not bn:
                errors.append(
                    {
                        "code": "EMPTY_BLOCK_NO",
                        "paper_no": pno or None,
                        "index": j,
                        "message": "存在空扫描块号",
                    }
                )
                continue
            if bn in local:
                errors.append(
                    {
                        "code": "DUPLICATE_BLOCK_IN_PAPER",
                        "paper_no": pno,
                        "block_no": bn,
                        "message": f"扫描块 {bn} 在卷 {pno} 内重复",
                    }
                )
            local.add(bn)
            used[bn] += 1

        norm.append({"paper_no": pno, "blocks": blocks, "key": seq_key(blocks)})

    for bn, cnt in used.items():
        if cnt > 1:
            errors.append(
                {
                    "code": "DUPLICATE_BLOCK",
                    "block_no": bn,
                    "count": cnt,
                    "message": f"原扫描块 {bn} 在整批更正中被使用 {cnt} 次，必须恰好一次",
                }
            )
        if bn not in known:
            errors.append(
                {
                    "code": "UNKNOWN_BLOCK",
                    "block_no": bn,
                    "message": f"扫描块 {bn} 不是原扫描块，更正只能使用已登记的原块",
                }
            )

    missing = sorted(known - set(used))
    if missing:
        errors.append(
            {
                "code": "MISSING_BLOCKS",
                "block_nos": missing,
                "message": f"有 {len(missing)} 个原扫描块未在更正批次中出现，每块须恰好使用一次",
            }
        )

    if errors:
        raise ServiceError(
            422,
            "CORRECTION_INVALID",
            "整批更正核对未通过，归卷与评分均未改动",
            details=errors,
        )
    return norm


def _void_reason(old_blocks: list[str], norm: list[dict[str, Any]]) -> str:
    dest_paper = {}
    target_seq: dict[str, list[str]] = {}
    for a in norm:
        target_seq[a["paper_no"]] = a["blocks"]
        for b in a["blocks"]:
            dest_paper[b] = a["paper_no"]

    papers = {dest_paper.get(b) for b in old_blocks}
    if len(papers) > 1:
        return "split"
    pno = next(iter(papers))
    new_seq = target_seq.get(pno or "", [])
    if set(old_blocks).issubset(set(new_seq)) and len(new_seq) > len(old_blocks):
        if old_blocks != [b for b in new_seq if b in set(old_blocks)]:
            return "merged_and_reordered"
        return "merged"
    if set(old_blocks) == set(new_seq) and old_blocks != new_seq:
        return "reordered"
    return "rearranged"


def _build_plan_conn(conn, norm: list[dict[str, Any]]) -> dict[str, Any]:
    old_rows = conn.execute(
        "SELECT * FROM answer WHERE status='active'"
    ).fetchall()
    old_by_key: dict[str, Any] = {}
    for r in old_rows:
        old_by_key.setdefault(r["seq_key"], r)

    items: list[dict[str, Any]] = []
    matched_ids: set[str] = set()

    for a in norm:
        pno, blocks, key = a["paper_no"], a["blocks"], a["key"]
        if not blocks:
            items.append(
                {
                    "paper_no": pno,
                    "blocks": [],
                    "disposition": "empty",
                    "needs_grading": False,
                    "answer_id": None,
                }
            )
            continue

        old = old_by_key.get(key)
        if old is not None and old["id"] not in matched_ids:
            matched_ids.add(old["id"])
            claim = _live_claim_conn(conn, old["id"])
            score = _valid_score_conn(conn, old["id"])
            moved = old["current_paper_no"] != pno
            items.append(
                {
                    "paper_no": pno,
                    "blocks": blocks,
                    "disposition": "intact_moved" if moved else "intact_same_paper",
                    "needs_grading": False,
                    "answer_id": old["id"],
                    "from_paper_no": old["current_paper_no"] if moved else None,
                    "claim_no": claim["claim_no"] if claim else None,
                    "teacher_id": claim["teacher_id"] if claim else None,
                    "score": score["score"] if score else None,
                    "score_id": score["id"] if score else None,
                }
            )
        else:
            items.append(
                {
                    "paper_no": pno,
                    "blocks": blocks,
                    "disposition": "needs_grading",
                    "needs_grading": True,
                    "answer_id": None,
                    "reason": "split_merge_or_reorder",
                }
            )

    voided: list[dict[str, Any]] = []
    for old in old_rows:
        if old["id"] in matched_ids:
            continue
        old_blocks = parse_seq(old["seq_json"])
        reason = _void_reason(old_blocks, norm)
        claim_rows = conn.execute(
            "SELECT claim_no FROM claim WHERE answer_id=? AND status<>'void'",
            (old["id"],),
        ).fetchall()
        score_rows = conn.execute(
            "SELECT id, score FROM score WHERE answer_id=? AND status='valid'",
            (old["id"],),
        ).fetchall()
        voided.append(
            {
                "answer_id": old["id"],
                "paper_no": old["current_paper_no"],
                "blocks": old_blocks,
                "reason": reason,
                "voided_claim_nos": [r["claim_no"] for r in claim_rows],
                "voided_score_ids": [r["id"] for r in score_rows],
            }
        )

    return {
        "assignments": items,
        "voided_answers": voided,
        "kept_answer_count": len(matched_ids),
        "new_or_voided_answer_count": len(voided),
    }


def verify_correction(db: Database, raw: Any) -> dict[str, Any]:
    """只核对、出方案，不写任何数据。核对失败返回 valid=False + errors。"""
    norm_input = normalize_assignments(raw)
    try:
        with db.read() as conn:
            norm = _validate_batch_conn(conn, norm_input)
            plan = _build_plan_conn(conn, norm)
    except ServiceError as e:
        return {"valid": False, "error_code": e.code, "errors": e.details or []}
    return {"valid": True, "status": "verified", **plan}


def _reconcile_conn(
    conn, norm: list[dict[str, Any]], ts: str, correction_id: int
) -> dict[str, Any]:
    plan = _build_plan_conn(conn, norm)
    reason_by_answer = {
        v["answer_id"]: f"correction_{v['reason']}" for v in plan["voided_answers"]
    }

    old_rows = conn.execute(
        "SELECT * FROM answer WHERE status='active'"
    ).fetchall()
    old_by_key: dict[str, Any] = {}
    for r in old_rows:
        old_by_key.setdefault(r["seq_key"], r)

    matched: set[str] = set()

    # 1) 先作废未被完整继承的旧答案（连同领题号、有效评分）
    target_keys = {a["key"] for a in norm if a["blocks"]}
    for old in old_rows:
        will_match = old["seq_key"] in target_keys
        # 唯一索引保证同 key 至多一个 active；norm 已拒绝重复块，key 不会重复
        if will_match:
            matched.add(old["id"])
        else:
            _void_answer_conn(
                conn,
                old["id"],
                ts,
                reason_by_answer.get(old["id"], "correction_changed"),
                correction_id,
            )

    # 2) upsert 卷 + 迁移保留答案 / 新建待评答案
    for a in norm:
        pno, blocks, key = a["paper_no"], a["blocks"], a["key"]
        conn.execute(
            "INSERT OR IGNORE INTO paper(paper_no, created_at) VALUES (?, ?)",
            (pno, ts),
        )
        if not blocks:
            a["answer_id"] = None
            continue

        old = old_by_key.get(key)
        if old is not None and old["id"] in matched:
            conn.execute(
                "UPDATE answer SET current_paper_no=? WHERE id=?",
                (pno, old["id"]),
            )
            a["answer_id"] = old["id"]
        else:
            aid = _new_answer_id()
            conn.execute(
                """
                INSERT INTO answer(id, seq_json, seq_key, status,
                                   current_paper_no, born_correction_id, created_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    aid,
                    json.dumps(blocks, ensure_ascii=False),
                    key,
                    pno,
                    correction_id,
                    ts,
                ),
            )
            a["answer_id"] = aid

    # 3) 重建当前归卷表（每块恰好一行）
    conn.execute("DELETE FROM paper_block")
    for a in norm:
        for pos, bn in enumerate(a["blocks"]):
            conn.execute(
                "INSERT INTO paper_block(paper_no, block_no, position) VALUES (?, ?, ?)",
                (a["paper_no"], bn, pos),
            )

    plan["applied_at"] = ts
    return plan


def apply_correction(db: Database, raw: Any) -> dict[str, Any]:
    norm_input = normalize_assignments(raw)
    ts = now_iso()
    with db.tx() as conn:
        norm = _validate_batch_conn(conn, norm_input)
        cur = conn.execute(
            """
            INSERT INTO correction(status, request_json, created_at)
            VALUES ('applied', ?, ?)
            """,
            (
                json.dumps({"assignments": norm}, ensure_ascii=False),
                ts,
            ),
        )
        cid = cur.lastrowid
        plan = _reconcile_conn(conn, norm, ts, cid)
        payload = {
            "correction_id": cid,
            "status": "applied",
            "valid": True,
            "applied_at": ts,
            **plan,
        }
        conn.execute(
            "UPDATE correction SET plan_json=?, applied_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), ts, cid),
        )
        return payload


def stage_correction(db: Database, raw: Any) -> dict[str, Any]:
    norm_input = normalize_assignments(raw)
    ts = now_iso()
    with db.tx() as conn:
        norm = _validate_batch_conn(conn, norm_input)
        plan = _build_plan_conn(conn, norm)
        cur = conn.execute(
            """
            INSERT INTO correction(status, request_json, plan_json, created_at)
            VALUES ('checked', ?, NULL, ?)
            """,
            (
                json.dumps({"assignments": norm}, ensure_ascii=False),
                ts,
            ),
        )
        cid = cur.lastrowid
        payload = {
            "correction_id": cid,
            "status": "checked",
            "valid": True,
            "created_at": ts,
            **plan,
        }
        conn.execute(
            "UPDATE correction SET plan_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), cid),
        )
        return payload


def _correction_row_to_dict(row) -> dict[str, Any]:
    data = {
        "correction_id": row["id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "applied_at": row["applied_at"],
        "request": json.loads(row["request_json"]) if row["request_json"] else None,
    }
    if row["plan_json"]:
        data["plan"] = json.loads(row["plan_json"])
    return data


def get_correction(db: Database, correction_id: int) -> dict[str, Any]:
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM correction WHERE id=?", (correction_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(
                404, "CORRECTION_NOT_FOUND", f"找不到更正单 {correction_id}"
            )
        return _correction_row_to_dict(row)


def list_corrections(db: Database) -> list[dict[str, Any]]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM correction ORDER BY id DESC"
        ).fetchall()
        return [_correction_row_to_dict(r) for r in rows]


def apply_staged_correction(db: Database, correction_id: int) -> dict[str, Any]:
    ts = now_iso()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM correction WHERE id=?", (correction_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(
                404, "CORRECTION_NOT_FOUND", f"找不到更正单 {correction_id}"
            )
        if row["status"] == "applied":
            return json.loads(row["plan_json"]) if row["plan_json"] else {"status": "applied"}
        if row["status"] == "cancelled":
            raise ServiceError(
                409,
                "CORRECTION_CANCELLED",
                f"更正单 {correction_id} 已取消，不能生效",
            )

        stored = json.loads(row["request_json"])
        norm_input = stored.get("assignments", stored)
        norm = _validate_batch_conn(conn, norm_input)
        plan = _reconcile_conn(conn, norm, ts, correction_id)
        payload = {
            "correction_id": correction_id,
            "status": "applied",
            "valid": True,
            "applied_at": ts,
            **plan,
        }
        conn.execute(
            "UPDATE correction SET status='applied', plan_json=?, applied_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), ts, correction_id),
        )
        return payload


def cancel_staged_correction(db: Database, correction_id: int) -> dict[str, Any]:
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM correction WHERE id=?", (correction_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(
                404, "CORRECTION_NOT_FOUND", f"找不到更正单 {correction_id}"
            )
        if row["status"] != "checked":
            raise ServiceError(
                409,
                "CORRECTION_NOT_PENDING",
                f"更正单 {correction_id} 状态为 {row['status']}，不能取消",
            )
        conn.execute(
            "UPDATE correction SET status='cancelled' WHERE id=?",
            (correction_id,),
        )
        return {"correction_id": correction_id, "status": "cancelled"}
