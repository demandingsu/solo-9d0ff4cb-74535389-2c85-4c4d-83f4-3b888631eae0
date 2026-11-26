from helpers import claim_first, paper, setup_layout


def test_health_docs_openapi(client):
    assert client.get("/").status_code == 200
    h = client.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok"

    docs = client.get("/docs")
    assert docs.status_code == 200 and "text/html" in docs.headers["content-type"]

    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    assert "/corrections" in spec.json()["paths"]


def test_register_and_list_blocks_idempotent(client):
    r = client.post(
        "/blocks",
        json={"blocks": [{"block_no": "B1", "content": "答题内容1"}, "B2"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["registered"] == 2

    # 重复登记幂等，且带 content 时更新内容
    r2 = client.post(
        "/blocks", json={"blocks": [{"block_no": "B1", "content": "新内容"}]}
    )
    assert r2.status_code == 200
    blocks = {b["block_no"]: b for b in client.get("/blocks").json()["blocks"]}
    assert blocks["B1"]["content"] == "新内容"
    assert blocks["B2"]["content"] is None


def test_intake_creates_pending_paper(client):
    setup_layout(client, {"P1": ["B1", "B2", "B3"]})
    p = paper(client, "P1")
    assert p["status"] == "pending"
    assert p["grading_status"] == "unclaimed"
    assert p["score"] is None
    assert p["blocks"] == ["B1", "B2", "B3"]
    assert p["answer_id"]


def test_unknown_paper_returns_404(client):
    r = client.get("/papers/NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "PAPER_NOT_FOUND"


def test_duplicate_block_inside_paper_rejected(client):
    client.post("/blocks", json={"blocks": ["B1", "B2"]})
    r = client.put("/papers/P1/blocks", json={"blocks": ["B1", "B1", "B2"]})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "DUPLICATE_BLOCK_IN_PAPER"


def test_block_cannot_be_entered_on_two_papers(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    r = client.put("/papers/P2/blocks", json={"blocks": ["B2", "B3"]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "BLOCK_ASSIGNED_TO_OTHER_PAPER"


def test_bulk_assignments_ok_and_cross_duplicate_rejected(client):
    client.post("/blocks", json={"blocks": ["B1", "B2", "B3", "B4"]})
    ok = client.post(
        "/assignments",
        json={
            "assignments": [
                {"paper_no": "P1", "blocks": ["B1", "B2"]},
                {"paper_no": "P2", "blocks": ["B3", "B4"]},
            ]
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["entered"] == 2

    bad = client.post(
        "/assignments",
        json={
            "assignments": [
                {"paper_no": "Q1", "blocks": ["B1"]},
                {"paper_no": "Q2", "blocks": ["B1"]},
            ]
        },
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "DUPLICATE_BLOCK_IN_BATCH"


def test_overview_counts_and_unassigned(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    client.post("/blocks", json={"blocks": ["B3"]})  # 未归卷
    ov = client.get("/overview").json()
    assert ov["paper_count"] == 1
    assert ov["status_counts"]["pending"] == 1
    assert ov["unassigned_blocks"] == ["B3"]


def test_reset_requires_confirm(client):
    setup_layout(client, {"P1": ["B1"]})
    assert client.post("/admin/reset").status_code == 400
    assert client.post("/admin/reset?confirm=true").status_code == 200
    assert client.get("/papers").json() == []
    assert client.get("/blocks").json()["blocks"] == []
