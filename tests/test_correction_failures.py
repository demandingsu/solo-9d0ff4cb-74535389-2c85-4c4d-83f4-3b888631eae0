"""更正失败必须完全不改归卷和已入账评分。"""

from helpers import claim_first, correct, paper, setup_layout


BAD_BATCHES = [
    (
        "unknown_block",
        [
            {"paper_no": "P1", "blocks": ["B1", "B9"]},
            {"paper_no": "P2", "blocks": ["B3"]},
        ],
        "UNKNOWN_BLOCK",
    ),
    (
        "duplicate_block_across_papers",
        [
            {"paper_no": "P1", "blocks": ["B1", "B2"]},
            {"paper_no": "P2", "blocks": ["B3", "B1"]},
        ],
        "DUPLICATE_BLOCK",
    ),
    (
        "missing_block",
        [
            {"paper_no": "P1", "blocks": ["B1"]},
            {"paper_no": "P2", "blocks": ["B3"]},
        ],
        "MISSING_BLOCKS",
    ),
    (
        "within_paper_duplicate",
        [
            {"paper_no": "P1", "blocks": ["B1", "B1", "B2"]},
            {"paper_no": "P2", "blocks": ["B3"]},
        ],
        "DUPLICATE_BLOCK",
    ),
]


def test_failed_corrections_leave_state_untouched(client):
    setup_layout(client, {"P1": ["B1", "B2"], "P2": ["B3"]})
    cno = claim_first(client)["claim_no"]

    for name, batch, code in BAD_BATCHES:
        r = correct(client, batch, expected=422)
        detail = r.json()["detail"]
        assert detail["code"] == "CORRECTION_INVALID", (name, r.text)
        codes = {e["code"] for e in (detail["details"] or [])}
        assert code in codes, (name, codes)

        # 归卷不变
        assert paper(client, "P1")["blocks"] == ["B1", "B2"], name
        assert paper(client, "P2")["blocks"] == ["B3"], name

    # 失败更正没有留下更正单
    assert client.get("/corrections").json()["corrections"] == []

    # 教师仍可照常交分，且后续查询正常
    r = client.post(f"/claims/{cno}/score", json={"score": 63})
    assert r.status_code == 200, r.text
    assert paper(client, "P1")["score"] == 63

    # 已入账评分不会被之后的失败更正影响
    correct(client, BAD_BATCHES[1][1], expected=422)
    assert paper(client, "P1")["score"] == 63


def test_verify_reports_invalid_without_changing_state(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    r = client.post(
        "/corrections/verify",
        json={"assignments": [{"paper_no": "P1", "blocks": ["B1"]}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert any(e["code"] == "MISSING_BLOCKS" for e in body["errors"])

    # 核对不改状态、不落更正单
    assert paper(client, "P1")["blocks"] == ["B1", "B2"]
    assert client.get("/corrections").json()["corrections"] == []


def test_verify_valid_previews_plan(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    r = client.post(
        "/corrections/verify",
        json={"assignments": [{"paper_no": "P9", "blocks": ["B1", "B2"]}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["assignments"][0]["disposition"] == "intact_moved"
    # verify 同样不落单、不生效（P9 尚不存在）
    assert client.get("/corrections").json()["corrections"] == []
    assert client.get("/papers/P9").status_code == 404
