"""暂存核对窗口：核对期间照常交分；生效后只认仍有效的领题号。"""

from helpers import claim_first, correct, paper, setup_layout


def test_staged_split_window_allows_scoring_then_voids_on_apply(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    cno = claim_first(client)["claim_no"]

    r = correct(
        client,
        [
            {"paper_no": "P1", "blocks": ["B1"]},
            {"paper_no": "P2", "blocks": ["B2"]},
        ],
        mode="stage",
    )
    assert r.status_code == 200
    cid = r.json()["correction_id"]
    assert r.json()["status"] == "checked"

    # 核对期间旧归卷与旧领题号完全有效，教师照常交分
    assert paper(client, "P1")["blocks"] == ["B1", "B2"]
    sr = client.post(f"/claims/{cno}/score", json={"score": 80})
    assert sr.status_code == 200

    # 生效
    ar = client.post(f"/corrections/{cid}/apply")
    assert ar.status_code == 200
    assert ar.json()["status"] == "applied"

    # 该答案被拆，刚交的分随旧身份一起作废
    assert client.post(f"/claims/{cno}/score", json={"score": 1}).status_code == 409
    assert paper(client, "P1")["status"] == "pending"
    assert paper(client, "P2")["status"] == "pending"


def test_staged_intact_move_score_during_window_survives(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    cno = claim_first(client)["claim_no"]

    r = correct(
        client,
        [{"paper_no": "P9", "blocks": ["B1", "B2"]}],
        mode="stage",
    )
    cid = r.json()["correction_id"]

    # 核对期间交分
    assert client.post(f"/claims/{cno}/score", json={"score": 95}).status_code == 200

    ar = client.post(f"/corrections/{cid}/apply")
    assert ar.status_code == 200
    p = paper(client, "P9")
    assert p["status"] == "scored" and p["score"] == 95 and p["claim_no"] == cno
    assert paper(client, "P1")["status"] == "empty"


def test_apply_is_idempotent_and_cancel_blocks_apply(client):
    setup_layout(client, {"P1": ["B1"]})
    cid = correct(
        client,
        [{"paper_no": "P2", "blocks": ["B1"]}],
        mode="stage",
    ).json()["correction_id"]

    first = client.post(f"/corrections/{cid}/apply")
    second = client.post(f"/corrections/{cid}/apply")
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "applied"

    cid2 = correct(
        client,
        [{"paper_no": "P3", "blocks": ["B1"]}],
        mode="stage",
    ).json()["correction_id"]
    assert client.post(f"/corrections/{cid2}/cancel").status_code == 200
    blocked = client.post(f"/corrections/{cid2}/apply")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "CORRECTION_CANCELLED"
    # 取消的更正没生效，块仍在 P2
    assert paper(client, "P2")["blocks"] == ["B1"]


def test_dry_run_flag_does_not_apply(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    r = client.post(
        "/corrections",
        json={
            "assignments": [{"paper_no": "P9", "blocks": ["B1", "B2"]}],
            "dry_run": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert client.get("/papers/P9").status_code == 404
