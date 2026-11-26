"""FastAPI 入口：归卷更正服务。

启动：
    uvicorn app.main:app --host 0.0.0.0 --port 8000
然后打开 http://127.0.0.1:8000/docs 交互操作。
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import service
from .db import Database
from .schemas import (
    BlockRegisterRequest,
    ClaimRequest,
    CorrectionRequest,
    PaperSequenceRequest,
    QuestionUpdateRequest,
    ScoreBody,
    ScoreSubmitRequest,
)

TAGS_METADATA = [
    {"name": "meta", "description": "服务信息与健康检查"},
    {"name": "question", "description": "本题上下文（服务只处理同一题）"},
    {"name": "blocks", "description": "原扫描块登记/查询"},
    {"name": "papers", "description": "按卷录入有序块串、按卷查得分/待评状态"},
    {"name": "claims", "description": "教师领取无卷号答案、凭领题号交分"},
    {"name": "corrections", "description": "教务员整批更正：核对、暂存、生效、查询"},
    {"name": "admin", "description": "管理/自测工具"},
]


def create_app(db_path: Optional[str] = None) -> FastAPI:
    path = db_path or os.environ.get("DB_PATH") or os.path.join("data", "correction.db")
    db = Database(path)
    service.seed_defaults(db)

    app = FastAPI(
        title="归卷更正与评分保全服务",
        description=(
            "扫描拼卷出错后，把同一题的答题块归回正确的卷，并保住仍可用的评分。\n\n"
            "## 核心规则\n"
            "- 每卷录入一串**有顺序**的扫描块号；整批更正只能用**原扫描块**，且每块**恰好一次**。\n"
            "- 教师领取的答案**不含卷号**，凭**领题号**交分。\n"
            "- 完整答案（严格有序块序列）原样保留时，已有评分和领题号**随答案走，换卷也一样**。\n"
            "- 一旦拆分/合并/调序，旧评分与领题号立即**作废**，答案变为待评；"
            "即使以后拼回原序列也会生成新答案身份，仍须重评。\n"
            "- 更正可先 `stage` 核对暂存：核对期间教师照常交分；调用 apply 生效后，"
            "只接收仍有效的领题号。\n"
            "- 更正核对/生效失败时整体回滚，**归卷与已入账评分不变**。\n"
            "- 可按卷号查询当前得分或待评状态。\n\n"
            "推荐顺序：`POST /blocks` → `PUT /papers/{卷号}/blocks`（或 `POST /assignments`）"
            " → `POST /claims` → `POST /claims/{领题号}/score`"
            " → `POST /corrections` → `GET /papers/{卷号}`。"
        ),
        version="1.0.0",
        openapi_tags=TAGS_METADATA,
    )
    app.state.db = db

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(service.ServiceError)
    async def _service_error_handler(_: Request, exc: service.ServiceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    # ------------------------------- meta ---------------------------------- #
    @app.get("/", tags=["meta"], summary="服务信息")
    def root():
        return {
            "service": "correction-service",
            "question_id": service.QUESTION_ID,
            "docs": "/docs",
            "health": "/health",
            "workflow": [
                "POST /blocks",
                "PUT /papers/{paper_no}/blocks",
                "POST /claims",
                "POST /claims/{claim_no}/score",
                "POST /corrections  (或先 POST /corrections/verify)",
                "GET /papers/{paper_no}",
            ],
        }

    @app.get("/health", tags=["meta"], summary="健康检查")
    def health():
        with db.read() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "db": path}

    # ----------------------------- question -------------------------------- #
    @app.get("/question", tags=["question"], summary="查看当前试题（单例）")
    def get_question():
        return service.get_question(db)

    @app.put("/question", tags=["question"], summary="设置题目标题/满分（可选）")
    def update_question(body: QuestionUpdateRequest):
        return service.update_question(db, body.name, body.full_score)

    # ------------------------------ blocks --------------------------------- #
    @app.post("/blocks", tags=["blocks"], summary="登记原扫描块（可批量、幂等）")
    def register_blocks(body: BlockRegisterRequest):
        items = [b.model_dump() for b in body.blocks]
        return service.register_blocks(db, items)

    @app.get("/blocks", tags=["blocks"], summary="列出全部原扫描块及当前归属")
    def list_blocks():
        return {"blocks": service.list_blocks(db)}

    # ------------------------------ papers --------------------------------- #
    @app.get("/papers", tags=["papers"], summary="全部卷的当前得分/待评状态")
    def list_papers():
        return service.list_papers(db)

    @app.get("/overview", tags=["papers"], summary="总览：状态计数与未归卷块")
    def overview():
        return service.overview(db)

    @app.put(
        "/papers/{paper_no}/blocks",
        tags=["papers"],
        summary="按卷录入/覆盖本题的有序扫描块串（初次归卷录入）",
    )
    def put_paper_blocks(paper_no: str, body: PaperSequenceRequest):
        blocks = [b.block_no for b in body.blocks]
        return service.set_paper_sequence(db, paper_no, blocks)

    @app.post(
        "/papers/{paper_no}/blocks",
        tags=["papers"],
        summary="按卷录入（POST 别名，等价 PUT）",
        include_in_schema=False,
    )
    def post_paper_blocks_alias(paper_no: str, body: PaperSequenceRequest):
        blocks = [b.block_no for b in body.blocks]
        return service.set_paper_sequence(db, paper_no, blocks)

    @app.post(
        "/assignments",
        tags=["papers"],
        summary="整批初次录入各卷块串（事务，跨卷重复会拒绝）",
    )
    def bulk_assignments(body: CorrectionRequest):
        if not body.assignments:
            raise service.ServiceError(
                422,
                "EMPTY_ASSIGNMENTS",
                "请提供 assignments：[{paper_no, blocks:[...]}]",
            )
        raw = [a.model_dump() for a in body.assignments]
        return service.bulk_set_sequences(db, raw)

    @app.get("/papers/{paper_no}", tags=["papers"], summary="按卷号查当前得分或待评状态")
    def paper_detail(paper_no: str):
        return service.paper_detail(db, paper_no)

    @app.get(
        "/papers/{paper_no}/result",
        tags=["papers"],
        summary="按卷号查结果（/papers/{paper_no} 别名）",
    )
    def paper_result(paper_no: str):
        return service.paper_detail(db, paper_no)

    @app.get(
        "/papers/{paper_no}/status",
        tags=["papers"],
        summary="按卷号查待评/评分状态（别名）",
    )
    def paper_status(paper_no: str):
        return service.paper_detail(db, paper_no)

    @app.get(
        "/papers/{paper_no}/score",
        tags=["papers"],
        summary="按卷号只看当前得分/状态（别名）",
        include_in_schema=False,
    )
    def paper_score_alias(paper_no: str):
        return service.paper_detail(db, paper_no)

    # ------------------------------ claims --------------------------------- #
    @app.post("/claims", tags=["claims"], summary="教师领取下一份不带卷号的答案")
    def create_claim(body: Optional[ClaimRequest] = None):
        teacher_id = body.teacher_id if body else None
        answer_id = body.answer_id if body else None
        return service.create_claim(db, teacher_id, answer_id)

    @app.get("/claims", tags=["claims"], summary="领题号列表（可按 status 过滤）")
    def list_claims(status: Optional[str] = Query(None)):
        return {"claims": service.list_claims(db, status)}

    @app.get("/claims/{claim_no}", tags=["claims"], summary="查询领题号是否仍有效")
    def get_claim(claim_no: int):
        return service.get_claim(db, claim_no)

    @app.post(
        "/claims/{claim_no}/release",
        tags=["claims"],
        summary="释放未交分的领取，使答案回到待领取池",
    )
    def release_claim(claim_no: int):
        return service.release_claim(db, claim_no)

    def _score_response(claim_no: int, body: ScoreBody):
        return service.submit_score(db, claim_no, body.score, body.comment)

    @app.post(
        "/claims/{claim_no}/score",
        tags=["claims"],
        summary="凭领题号交分（更正后作废的领题号返回 409）",
    )
    def score_by_claim(claim_no: int, body: ScoreBody):
        return _score_response(claim_no, body)

    @app.put(
        "/claims/{claim_no}/score",
        tags=["claims"],
        summary="凭领题号交分（PUT 别名；重复交分按新版本保留）",
    )
    def update_score_by_claim(claim_no: int, body: ScoreBody):
        return _score_response(claim_no, body)

    @app.post("/scores", tags=["claims"], summary="在请求体中带 claim_no 交分")
    def score_by_body(body: ScoreSubmitRequest):
        return service.submit_score(db, body.claim_no, body.score, body.comment)

    # ---------------------------- corrections ------------------------------ #
    def _raw_assignments(body: CorrectionRequest):
        if not body.assignments:
            raise service.ServiceError(
                422,
                "EMPTY_ASSIGNMENTS",
                "整批更正必须包含 assignments：[{paper_no, blocks:[...]}]，"
                "且全部原扫描块恰好出现一次",
            )
        return [a.model_dump() for a in body.assignments]

    @app.post(
        "/corrections/verify",
        tags=["corrections"],
        summary="只核对整批更正并返回继承/作废方案，不生效",
    )
    def verify_correction(body: CorrectionRequest):
        raw = _raw_assignments(body)
        return service.verify_correction(db, raw)

    @app.post(
        "/corrections",
        tags=["corrections"],
        summary="提交整批更正（默认核对通过立即生效；mode=stage 可暂存待生效）",
    )
    def submit_correction(body: CorrectionRequest):
        raw = _raw_assignments(body)
        mode = (body.mode or "apply").lower()
        if body.dry_run is True or mode == "verify":
            return service.verify_correction(db, raw)
        if mode == "stage":
            return service.stage_correction(db, raw)
        if mode in ("apply", "commit", "execute"):
            return service.apply_correction(db, raw)
        raise service.ServiceError(
            422,
            "INVALID_MODE",
            f"不支持的 mode={mode!r}，可选 apply / stage / verify",
        )

    @app.get("/corrections", tags=["corrections"], summary="更正单列表")
    def list_corrections():
        return {"corrections": service.list_corrections(db)}

    @app.get("/corrections/{correction_id}", tags=["corrections"], summary="更正单详情/方案")
    def get_correction(correction_id: int):
        return service.get_correction(db, correction_id)

    @app.post(
        "/corrections/{correction_id}/apply",
        tags=["corrections"],
        summary="让 stage 暂存的更正正式生效（幂等：已生效直接返回原方案）",
    )
    def apply_correction_by_id(correction_id: int):
        return service.apply_staged_correction(db, correction_id)

    @app.post(
        "/corrections/{correction_id}/cancel",
        tags=["corrections"],
        summary="取消暂存中的更正单（不影响归卷/评分）",
    )
    def cancel_correction(correction_id: int):
        return service.cancel_staged_correction(db, correction_id)

    # ------------------------------- admin --------------------------------- #
    @app.post(
        "/admin/seed-demo",
        tags=["admin"],
        summary="写入一套幂等的演示数据（DEMO-P1/P2/P3，便于在 /docs 体验）",
    )
    def seed_demo():
        return service.seed_demo(db)

    @app.post(
        "/admin/reset",
        tags=["admin"],
        summary="清空全部业务数据（自测用，需 confirm=true）",
    )
    def reset(confirm: bool = Query(False)):
        if not confirm:
            raise service.ServiceError(
                400,
                "CONFIRM_REQUIRED",
                "这会清空所有归卷、领题号、评分与更正单；如确认请加 ?confirm=true",
            )
        db.reset()
        service.seed_defaults(db)
        return {"status": "reset", "question_id": service.QUESTION_ID}

    return app


# uvicorn app.main:app
app = create_app()
