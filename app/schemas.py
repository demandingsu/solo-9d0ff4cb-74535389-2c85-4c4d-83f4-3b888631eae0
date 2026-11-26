"""Pydantic 请求模型（/docs 可直接试用）。

为减少调用方适配成本，解析层兼容常见别名字段，例如
``paper/paper_no/paperNo/卷号`` 与 ``blocks/block_nos/sequence`` 等；
核心文档示例始终使用规范字段 ``paper_no`` / ``blocks``。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

_PAPER_KEYS = ("paper_no", "paperNo", "paper_id", "paperId", "paper", "卷号", "试卷号")
_BLOCKS_KEYS = (
    "blocks",
    "block_nos",
    "blockNos",
    "block_numbers",
    "blockNumbers",
    "sequence",
    "seq",
    "order",
    "块号",
    "扫描块",
)
_CLAIM_KEYS = ("claim_no", "claimNo", "claim_id", "claimId", "领题号", "领取号")


def pick_key(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def as_str_list(value: Any) -> list[Any]:
    """把块序列统一成“条目列表”；逗号/顿号/空白字符串也接受。"""
    if value is None:
        return []
    if isinstance(value, str):
        import re

        return [p for p in re.split(r"[,，、;；\s]+", value.strip()) if p]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


class BlockItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    block_no: str = Field(..., examples=["B001"])
    content: Optional[str] = Field(None, examples=["第1题作答：……"])

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, (str, int, float)) and not isinstance(data, bool):
            return {"block_no": str(data).strip()}
        if isinstance(data, dict):
            bn = pick_key(
                data,
                ("block_no", "blockNo", "block_id", "id", "no", "块号", "扫描块号"),
            )
            if bn is not None and "block_no" not in data:
                data = {**data, "block_no": str(bn).strip()}
            elif data.get("block_no") is not None:
                data = {**data, "block_no": str(data["block_no"]).strip()}
        return data


class BlockRegisterRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {
                "blocks": [
                    {"block_no": "B001", "content": "第1题作答内容（可选）"},
                    "B002",
                    "B003",
                ]
            }
        },
    )

    blocks: list[BlockItem] = Field(
        default_factory=list,
        description="原扫描块列表；元素可以是块号字符串，或 {block_no, content}",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"blocks": data}
        if isinstance(data, dict):
            if not data.get("blocks"):
                for k in ("block_nos", "blockNos", "items", "list"):
                    if k in data:
                        data = {**data, "blocks": data[k]}
                        break
        return data


class PaperSequenceRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    blocks: list[BlockItem] = Field(
        ..., description="该卷本题的有序扫描块号，例如 ['B001','B002']"
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"blocks": data}
        if isinstance(data, dict):
            raw = pick_key(data, _BLOCKS_KEYS)
            if raw is not None:
                data = {**data, "blocks": as_str_list(raw)}
        return data


class Assignment(BaseModel):
    model_config = ConfigDict(extra="allow")

    paper_no: str = Field(..., examples=["P001"])
    blocks: list[BlockItem] = Field(..., examples=[["B001", "B002"]])

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "paper_no" not in out:
            pn = pick_key(out, _PAPER_KEYS)
            if pn is not None:
                out["paper_no"] = str(pn).strip()
        elif out["paper_no"] is not None:
            out["paper_no"] = str(out["paper_no"]).strip()
        if "blocks" not in out:
            raw = pick_key(out, _BLOCKS_KEYS)
            if raw is not None:
                out["blocks"] = as_str_list(raw)
        elif not isinstance(out["blocks"], list):
            out["blocks"] = as_str_list(out["blocks"])
        return out


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {
                "mode": "apply",
                "assignments": [
                    {"paper_no": "P001", "blocks": ["B001", "B002"]},
                    {"paper_no": "P002", "blocks": ["B003", "B004"]},
                ],
            }
        },
    )

    assignments: Optional[list[Assignment]] = Field(
        None,
        description="整批归卷结果：每卷一项，块号有序；所有原扫描块须恰好出现一次",
    )
    papers: Optional[dict[str, Any]] = Field(
        None, description="也支持 {卷号: [块号...]} 的映射形式"
    )
    mode: str = Field(
        "apply",
        description="apply=核对通过后立即生效（默认）；stage=仅核对并暂存，稍后调用 /corrections/{id}/apply 生效",
    )
    dry_run: Optional[bool] = Field(
        None, description="为 true 时只核对返回方案，不改动任何数据（等价于 /corrections/verify）"
    )
    auto_apply: Optional[bool] = Field(
        None, description="兼容字段：false 等价于 mode=stage"
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        # 允许直接提交裸数组 [{"paper_no":...,"blocks":[...]}]
        if isinstance(data, list):
            return {"assignments": data}
        if not isinstance(data, dict):
            return data

        out = dict(data)

        # 常见外层别名
        if not out.get("assignments"):
            for k in ("corrections", "layout", "items", "batch", "result"):
                v = out.get(k)
                if isinstance(v, list):
                    out["assignments"] = v
                    break

        # papers 映射形式：{"P1": ["B1","B2"]} 或 {"P1": {"blocks": [...]}}
        papers = out.get("papers")
        if isinstance(papers, dict):
            converted: list[dict[str, Any]] = []
            for pno, val in papers.items():
                if isinstance(val, dict):
                    blocks = pick_key(val, _BLOCKS_KEYS)
                    item = {
                        "paper_no": str(pno),
                        "blocks": as_str_list(blocks if blocks is not None else []),
                    }
                else:
                    item = {"paper_no": str(pno), "blocks": as_str_list(val)}
                converted.append(item)
            out["assignments"] = (out.get("assignments") or []) + converted

        # 额外的“裸卷号键”：{"P1": ["B1"], "mode": "apply"}
        reserved = {
            "assignments",
            "papers",
            "mode",
            "dry_run",
            "auto_apply",
            "corrections",
            "layout",
            "items",
            "batch",
            "result",
        }
        extra_items: list[dict[str, Any]] = []
        for k, v in list(out.items()):
            if k in reserved:
                continue
            if isinstance(v, (list, str)) or v is None:
                extra_items.append(
                    {"paper_no": str(k), "blocks": as_str_list(v)}
                )
        if extra_items:
            out["assignments"] = (out.get("assignments") or []) + extra_items

        if out.get("auto_apply") is False and "mode" not in data:
            out["mode"] = "stage"
        if out.get("dry_run") is True:
            out["mode"] = "verify"
        return out


class ClaimRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {"teacher_id": "T007", "answer_id": None}
        },
    )

    teacher_id: Optional[str] = Field(
        None, description="教师标识（可选，仅用于记录是谁领取的）", examples=["T007"]
    )
    answer_id: Optional[str] = Field(
        None,
        description="可选：指定要领取的答案身份（不包含卷号，通常留空由系统按顺序派发）",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if data is None:
            return {}
        if isinstance(data, dict):
            out = dict(data)
            if "teacher_id" not in out:
                tid = pick_key(out, ("teacher_id", "teacherId", "teacher", "teacher_no", "工号"))
                if tid is not None:
                    out["teacher_id"] = str(tid)
            return out
        return data


class ScoreBody(BaseModel):
    model_config = ConfigDict(
        extra="allow", json_schema_extra={"example": {"score": 92.5, "comment": "要点齐全"}}
    )

    score: float = Field(..., description="分数（有限数值）", examples=[92.5])
    comment: Optional[str] = Field(None, description="评语（可选）")

    @model_validator(mode="before")
    @classmethod
    def _coerce_score(cls, data: Any) -> Any:
        if isinstance(data, dict) and "score" not in data:
            for k in ("value", "points", "grade", "分数", "得分"):
                if k in data:
                    data = {**data, "score": data[k]}
                    break
        return data


class ScoreSubmitRequest(ScoreBody):
    claim_no: int = Field(..., description="领题号", examples=[1])

    @model_validator(mode="before")
    @classmethod
    def _coerce_claim(cls, data: Any) -> Any:
        if isinstance(data, dict):
            cn = pick_key(data, _CLAIM_KEYS)
            if cn is not None and "claim_no" not in data:
                data = {**data, "claim_no": cn}
        return data


class QuestionUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, examples=["高二数学 第18题"])
    full_score: Optional[float] = Field(None, examples=[12.0])
