"""测试辅助：常用业务流程封装。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def setup_layout(
    client: TestClient, layout: dict[str, list[str]], with_contents: bool = False
) -> list[str]:
    """登记全部块并按 layout 逐卷录入。dict 顺序即录入顺序。"""
    all_blocks = sorted({b for blocks in layout.values() for b in blocks})
    if with_contents:
        payload = {
            "blocks": [
                {"block_no": b, "content": f"内容-{b}"} for b in all_blocks
            ]
        }
    else:
        payload = {"blocks": all_blocks}
    r = client.post("/blocks", json=payload)
    assert r.status_code == 200, r.text

    for paper_no, blocks in layout.items():
        r = client.put(f"/papers/{paper_no}/blocks", json={"blocks": blocks})
        assert r.status_code == 200, r.text
    return all_blocks


def claim_first(client: TestClient, teacher: str | None = "T1") -> dict:
    r = client.post("/claims", json={"teacher_id": teacher})
    assert r.status_code == 200, r.text
    return r.json()


def score_claim(
    client: TestClient, claim_no: int, score, *, expected: int = 200
):
    r = client.post(f"/claims/{claim_no}/score", json={"score": score})
    assert r.status_code == expected, r.text
    return r


def correct(
    client: TestClient,
    assignments,
    *,
    mode: str | None = None,
    expected: int = 200,
):
    body: dict = {"assignments": assignments}
    if mode:
        body["mode"] = mode
    r = client.post("/corrections", json=body)
    assert r.status_code == expected, r.text
    return r


def paper(client: TestClient, paper_no: str) -> dict:
    r = client.get(f"/papers/{paper_no}")
    assert r.status_code == 200, r.text
    return r.json()
