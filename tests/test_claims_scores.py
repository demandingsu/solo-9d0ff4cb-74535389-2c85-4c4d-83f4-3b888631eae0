from helpers import claim_first, paper, setup_layout


def test_claim_payload_has_no_paper_number(client):
    # 用醒目的卷号，确保整个领取响应都不泄露卷号
    setup_layout(client, {"SECRET-PAPER-X": ["B1", "B2"]}, with_contents=True)
    r = client.post("/claims", json={"teacher_id": "T9"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "paper_no" not in body
    assert "SECRET" not in r.text
    assert body["claim_no"] >= 1
    assert body["status"] == "active"
    assert [b["block_no"] for b in body["blocks"]] == ["B1", "B2"]
    assert body["blocks"][0]["content"] == "内容-B1"


def test_claim_score_and_query_by_paper(client):
    setup_layout(client, {"P1": ["B1", "B2"], "P2": ["B3"]})

    claim = claim_first(client)
    cno = claim["claim_no"]

    # 未交分前：已领取待交分
    p = paper(client, "P1")
    assert p["status"] == "pending"
    assert p["grading_status"] == "claimed"
    assert p["claim_no"] == cno

    r = client.post(f"/claims/{cno}/score", json={"score": 92.5, "comment": "很好"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"

    p = paper(client, "P1")
    assert p["status"] == "scored"
    assert p["grading_status"] == "scored"
    assert p["score"] == 92.5
    assert p["comment"] == "很好"
    assert p["claim_no"] == cno


def test_submit_score_by_body_alias(client):
    setup_layout(client, {"P1": ["B1"]})
    cno = claim_first(client)["claim_no"]
    r = client.post("/scores", json={"claim_no": cno, "score": 77})
    assert r.status_code == 200, r.text
    assert paper(client, "P1")["score"] == 77


def test_unknown_claim_and_invalid_score(client):
    setup_layout(client, {"P1": ["B1"]})
    r = client.post("/claims/9999/score", json={"score": 1})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "CLAIM_NOT_FOUND"

    cno = claim_first(client)["claim_no"]
    bad = client.post(f"/claims/{cno}/score", json={"score": "not-a-number"})
    assert bad.status_code == 422


def test_no_claimable_answer_after_claim(client):
    setup_layout(client, {"P1": ["B1"]})
    claim_first(client)
    r = client.post("/claims", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NO_CLAIMABLE_ANSWER"


def test_repeated_submission_revises_score_and_keeps_ledger(client):
    setup_layout(client, {"P1": ["B1"]})
    cno = claim_first(client)["claim_no"]
    client.post(f"/claims/{cno}/score", json={"score": 80})
    r = client.post(f"/claims/{cno}/score", json={"score": 91})
    assert r.status_code == 200
    assert r.json()["revision"] == 2
    assert paper(client, "P1")["score"] == 91

    # 领取详情能看到当前分；评分流水里两版都在（旧版 superseded）
    detail = client.get(f"/claims/{cno}").json()
    assert detail["current_score"] == 91


def test_claims_list_filter(client):
    setup_layout(client, {"P1": ["B1"], "P2": ["B2"]})
    claim_first(client)
    claim_first(client, "T2")
    rows = client.get("/claims").json()["claims"]
    assert len(rows) == 2
    active = client.get("/claims?status=active").json()["claims"]
    assert len(active) == 2


def test_score_boundary_rules(client):
    setup_layout(client, {"P1": ["B1"], "P2": ["B2"]})
    c1 = claim_first(client)["claim_no"]

    # 默认不设满分上限：较大的分数也接受
    assert client.post(f"/claims/{c1}/score", json={"score": 150}).status_code == 200
    # 但负数拒绝
    assert client.post(f"/claims/{c1}/score", json={"score": -1}).status_code == 422

    # 设置满分后超上限拒绝，范围内接受
    assert client.put("/question", json={"full_score": 100}).status_code == 200
    c2 = claim_first(client, "T2")["claim_no"]
    over = client.post(f"/claims/{c2}/score", json={"score": 101})
    assert over.status_code == 422
    assert over.json()["detail"]["code"] == "SCORE_OUT_OF_RANGE"
    assert client.post(f"/claims/{c2}/score", json={"score": 100}).status_code == 200


def test_targeted_claim_by_answer_id(client):
    setup_layout(client, {"P1": ["B1"], "P2": ["B2"]})
    p2 = paper(client, "P2")

    # 教务查询能看到 answer_id；教师凭这个不透明 id 指定领取，响应仍无卷号
    r = client.post("/claims", json={"answer_id": p2["answer_id"]})
    assert r.status_code == 200, r.text
    assert [b["block_no"] for b in r.json()["blocks"]] == ["B2"]
    assert "paper_no" not in r.json()

    # 同一答案不能重复领取
    again = client.post("/claims", json={"answer_id": p2["answer_id"]})
    assert again.status_code == 409


def test_release_returns_answer_to_pool(client):
    setup_layout(client, {"P1": ["B1"]})
    cno = claim_first(client)["claim_no"]

    # 已领取后队列空了
    assert client.post("/claims", json={}).status_code == 409

    r = client.post(f"/claims/{cno}/release")
    assert r.status_code == 200
    assert r.json()["status"] == "released"

    # 已释放的领题号不能再交分
    assert client.post(f"/claims/{cno}/score", json={"score": 3}).status_code == 409

    # 答案回到池中，可以重新领取
    again = client.post("/claims", json={})
    assert again.status_code == 200
    assert again.json()["claim_no"] != cno
