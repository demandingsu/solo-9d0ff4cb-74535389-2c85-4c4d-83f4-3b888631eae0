"""核心保全规则：完整答案随卷走；拆拼/调序作废；拼回原样仍须重评。"""

from helpers import claim_first, correct, paper, setup_layout


def test_intact_scored_answer_moves_to_another_paper(client):
    setup_layout(client, {"P1": ["B1", "B2", "B3"], "P2": ["B4", "B5"]})
    cno = claim_first(client)["claim_no"]
    client.post(f"/claims/{cno}/score", json={"score": 88})

    # 整份答案原样从 P1 移到新卷 P9；P2 不动
    r = correct(
        client,
        [
            {"paper_no": "P9", "blocks": ["B1", "B2", "B3"]},
            {"paper_no": "P2", "blocks": ["B4", "B5"]},
        ],
    )
    plan = r.json()
    p9 = next(a for a in plan["assignments"] if a["paper_no"] == "P9")
    assert p9["disposition"] == "intact_moved"
    assert p9["claim_no"] == cno
    assert p9["score"] == 88

    # 得分与领题号随答案走
    assert paper(client, "P9")["score"] == 88
    assert paper(client, "P9")["claim_no"] == cno
    assert paper(client, "P1")["status"] == "empty"

    claim = client.get(f"/claims/{cno}").json()
    assert claim["status"] == "submitted"
    assert claim["current_score"] == 88


def test_intact_active_claim_moves_and_can_still_submit(client):
    setup_layout(client, {"P1": ["B1", "B2"], "P2": ["B3", "B4"]})
    cno = claim_first(client)["claim_no"]  # 领取但未交分

    correct(
        client,
        [
            {"paper_no": "P9", "blocks": ["B1", "B2"]},
            {"paper_no": "P2", "blocks": ["B3", "B4"]},
        ],
    )

    # 同一个领题号在换卷后仍然有效，可以正常交分
    r = client.post(f"/claims/{cno}/score", json={"score": 77})
    assert r.status_code == 200, r.text
    p = paper(client, "P9")
    assert p["status"] == "scored" and p["score"] == 77 and p["claim_no"] == cno


def test_identical_layout_correction_keeps_everything(client):
    layout = [
        {"paper_no": "P1", "blocks": ["B1", "B2"]},
        {"paper_no": "P2", "blocks": ["B3"]},
    ]
    setup_layout(client, {"P1": ["B1", "B2"], "P2": ["B3"]})
    cno = claim_first(client)["claim_no"]
    client.post(f"/claims/{cno}/score", json={"score": 50})

    r = correct(client, layout)
    dispositions = {a["paper_no"]: a["disposition"] for a in r.json()["assignments"]}
    assert dispositions["P1"] == "intact_same_paper"
    assert paper(client, "P1")["score"] == 50


def test_split_voids_old_claim_and_score(client):
    setup_layout(client, {"P1": ["B1", "B2", "B3"]})
    cno = claim_first(client)["claim_no"]
    client.post(f"/claims/{cno}/score", json={"score": 90})

    r = correct(
        client,
        [
            {"paper_no": "P1", "blocks": ["B1", "B2"]},
            {"paper_no": "P2", "blocks": ["B3"]},
        ],
    )
    body = r.json()
    assert len(body["voided_answers"]) == 1
    assert body["voided_answers"][0]["reason"] == "split"
    assert cno in body["voided_answers"][0]["voided_claim_nos"]

    # 旧领题号作废，不能再交分
    claim = client.get(f"/claims/{cno}").json()
    assert claim["status"] == "void"
    rejected = client.post(f"/claims/{cno}/score", json={"score": 91})
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "CLAIM_VOID"

    # 两张卷都待评，不显示旧分
    for pno in ("P1", "P2"):
        p = paper(client, pno)
        assert p["status"] == "pending"
        assert p["score"] is None

    # 两份新答案各可领取一次
    c1 = claim_first(client)["claim_no"]
    c2 = claim_first(client)["claim_no"]
    assert c1 != c2


def test_reorder_requires_reevaluation(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    cno = claim_first(client)["claim_no"]
    client.post(f"/claims/{cno}/score", json={"score": 66})

    r = correct(client, [{"paper_no": "P1", "blocks": ["B2", "B1"]}])
    assert r.json()["voided_answers"][0]["reason"] == "reordered"

    assert client.post(f"/claims/{cno}/score", json={"score": 1}).status_code == 409
    p = paper(client, "P1")
    assert p["status"] == "pending" and p["score"] is None


def test_reassemble_original_sequence_still_requires_reevaluation(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    old_cno = claim_first(client)["claim_no"]
    client.post(f"/claims/{old_cno}/score", json={"score": 99})

    # 第一次更正：拆开 -> 旧身份作废
    correct(
        client,
        [
            {"paper_no": "P1", "blocks": ["B1"]},
            {"paper_no": "P2", "blocks": ["B2"]},
        ],
    )
    assert (
        client.post(f"/claims/{old_cno}/score", json={"score": 1}).status_code == 409
    )

    # 第二次更正：拼回最初的原样 [B1, B2] —— 仍然必须重评
    r = correct(client, [{"paper_no": "P1", "blocks": ["B1", "B2"]}])
    item = r.json()["assignments"][0]
    assert item["disposition"] == "needs_grading"

    assert (
        client.post(f"/claims/{old_cno}/score", json={"score": 2}).status_code == 409
    )
    p = paper(client, "P1")
    assert p["status"] == "pending" and p["score"] is None

    new_cno = claim_first(client)["claim_no"]
    assert new_cno != old_cno
    res = client.post(f"/claims/{new_cno}/score", json={"score": 81})
    assert res.status_code == 200
    assert paper(client, "P1")["score"] == 81


def test_merge_voids_both_source_scores(client):
    setup_layout(client, {"P1": ["B1", "B2"], "P2": ["B3", "B4"]})
    c1 = claim_first(client, "A")["claim_no"]
    client.post(f"/claims/{c1}/score", json={"score": 70})
    c2 = claim_first(client, "B")["claim_no"]
    client.post(f"/claims/{c2}/score", json={"score": 71})

    correct(client, [{"paper_no": "P9", "blocks": ["B1", "B2", "B3", "B4"]}])

    assert client.post(f"/claims/{c1}/score", json={"score": 1}).status_code == 409
    assert client.post(f"/claims/{c2}/score", json={"score": 1}).status_code == 409
    p = paper(client, "P9")
    assert p["status"] == "pending" and p["score"] is None


def test_correction_accepts_papers_mapping_shape(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    # 兼容 {papers: {卷号: [块号]}} 的写法
    r = client.post("/corrections", json={"papers": {"P9": ["B2", "B1"]}})
    assert r.status_code == 200, r.text
    assert paper(client, "P9")["blocks"] == ["B2", "B1"]
    assert paper(client, "P1")["status"] == "empty"
