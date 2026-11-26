"""样卷校准：登记快照、样卷优先领取、并发唯一号、退回重领、分差与生命周期独立。"""

from concurrent.futures import ThreadPoolExecutor

from helpers import setup_layout


def _scored_p1(client, score=88, teacher="OFFICIAL"):
    """P1=[B1,B2] 并完成正式评分；返回正式 claim_no。"""
    setup_layout(client, {"P1": ["B1", "B2"], "P2": ["B3"]}, with_contents=True)
    r = client.post("/claims", json={"teacher_id": teacher})
    assert r.status_code == 200, r.text
    cno = r.json()["claim_no"]
    r = client.post(f"/claims/{cno}/score", json={"score": score})
    assert r.status_code == 200, r.text
    return cno


def _register_sample(client, teachers=("T1", "T2", "T3"), paper="P1"):
    r = client.post(
        "/samples", json={"paper_no": paper, "teacher_ids": list(teachers)}
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_register_sample_freezes_blocks_content_and_reference(client):
    _scored_p1(client, 88)
    body = _register_sample(client)
    assert body["sample_id"] >= 1 and body["id"] == body["sample_id"]
    assert body["reference_score"] == 88
    assert body["block_nos"] == ["B1", "B2"]
    # 块内容在登记时冻结
    contents = {b["block_no"]: b["content"] for b in body["blocks"]}
    assert contents["B1"] == "内容-B1"
    assert body["teacher_ids"] == ["T1", "T2", "T3"]
    # 每位教师初始 unclaimed，分差为空
    for row in body["results"]:
        assert row["grading_status"] == "unclaimed"
        assert row["score_diff"] is None


def test_register_requires_scored_paper_and_teachers(client):
    setup_layout(client, {"P1": ["B1"], "P2": ["B2"]})
    # P1 未评分
    r = client.post("/samples", json={"paper_no": "P1", "teacher_ids": ["T1"]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PAPER_NOT_SCORED"

    # 卷不存在
    r = client.post("/samples", json={"paper_no": "NOPE", "teacher_ids": ["T1"]})
    assert r.status_code == 404

    # 没有教师
    r = client.post("/claims", json={"teacher_id": "X"})  # no-op to keep style
    r = client.post("/samples", json={"paper_no": "P1", "teacher_ids": []})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NO_TEACHER_IDS"


def test_claim_prioritizes_own_sample_and_hides_markers(client):
    _scored_p1(client, 88)
    _register_sample(client)

    # P2=B3 也在普通领取池里，但 T1 优先拿到自己的样卷 B1,B2
    r = client.post("/claims", json={"teacher_id": "T1"})
    assert r.status_code == 200, r.text
    claim = r.json()
    assert [b["block_no"] for b in claim["blocks"]] == ["B1", "B2"]

    # 与普通领取响应完全相同的键集合，且没有任何样卷/参考分痕迹
    normal = client.post("/claims", json={"teacher_id": "OTHER"}).json()
    assert set(claim.keys()) == set(normal.keys())
    for forbidden in ("sample", "reference", "ref_score", "calibrat"):
        assert forbidden not in r.text.lower()
    assert "88" not in r.text  # 参考分不得出现在教师响应里


def test_sequential_duplicate_claim_converges_to_same_number(client):
    _scored_p1(client, 88)
    _register_sample(client, ["T1"])
    first = client.post("/claims", json={"teacher_id": "T1"}).json()
    second = client.post("/claims", json={"teacher_id": "T1"}).json()
    assert first["claim_no"] == second["claim_no"]


def test_release_then_reclaim_gives_new_number_and_old_dies(client):
    _scored_p1(client, 88)
    _register_sample(client, ["T1"])
    old = client.post("/claims", json={"teacher_id": "T1"}).json()["claim_no"]

    rel = client.post(f"/claims/{old}/release")
    assert rel.status_code == 200 and rel.json()["status"] == "released"

    # 退回的号码随即失效：交分 409 CLAIM_RELEASED
    bad = client.post(f"/claims/{old}/score", json={"score": 50})
    assert bad.status_code == 409
    assert bad.json()["detail"]["code"] == "CLAIM_RELEASED"

    new = client.post("/claims", json={"teacher_id": "T1"}).json()
    assert new["claim_no"] != old
    ok = client.post(f"/claims/{new['claim_no']}/score", json={"score": 70})
    assert ok.status_code == 200


def test_sample_scores_only_affect_calibration(client):
    off = _scored_p1(client, 88)
    sid = _register_sample(client, ["T1", "T2"])["sample_id"]

    c1 = client.post("/claims", json={"teacher_id": "T1"}).json()["claim_no"]
    c2 = client.post("/claims", json={"teacher_id": "T2"}).json()["claim_no"]
    client.post(f"/claims/{c1}/score", json={"score": 80})
    client.post(f"/claims/{c2}/score", json={"score": 95})
    # 改分保留最新
    r = client.post(f"/claims/{c2}/score", json={"score": 90})
    assert r.json()["revision"] == 2

    # 正式卷分数与正式领题号不受样卷评分影响
    p = client.get("/papers/P1").json()
    assert p["score"] == 88
    assert p["claim_no"] == off

    d = client.get(f"/samples/{sid}").json()
    rows = {x["teacher_id"]: x for x in d["results"]}
    assert rows["T1"]["latest_score"] == 80
    assert rows["T1"]["score_diff"] == -8
    assert rows["T2"]["latest_score"] == 90
    assert rows["T2"]["score_diff"] == 2


def test_reference_remains_frozen_after_official_regrade(client):
    off = _scored_p1(client, 88)
    sid = _register_sample(client, ["T1"])["sample_id"]
    c1 = client.post("/claims", json={"teacher_id": "T1"}).json()["claim_no"]
    client.post(f"/claims/{c1}/score", json={"score": 84})

    # 正式改分 91：参考分仍为登记时的 88
    assert client.post(f"/claims/{off}/score", json={"score": 91}).status_code == 200
    d = client.get(f"/samples/{sid}").json()
    assert d["reference_score"] == 88
    row = d["results"][0]
    assert row["reference_score"] == 88
    assert row["score_diff"] == -4  # 84 - 88
    assert client.get("/papers/P1").json()["score"] == 91


def test_block_content_update_does_not_change_sample_snapshot(client):
    _scored_p1(client, 88)
    sid = _register_sample(client, ["T1", "T2"])["sample_id"]

    claimed = client.post("/claims", json={"teacher_id": "T1"}).json()
    assert claimed["blocks"][0]["content"] == "内容-B1"

    # 教务更新扫描块内容
    r = client.post(
        "/blocks", json={"blocks": [{"block_no": "B1", "content": "全新内容"}]}
    )
    assert r.status_code == 200

    # 已领样卷看到的仍是冻结内容
    got = client.get(f"/claims/{claimed['claim_no']}").json()
    assert got["blocks"][0]["content"] == "内容-B1"

    # T2 尚未领取；现在领取也仍拿到登记时快照
    fresh = client.post("/claims", json={"teacher_id": "T2"}).json()
    assert fresh["blocks"][0]["content"] == "内容-B1"
    assert [b["block_no"] for b in fresh["blocks"]] == ["B1", "B2"]

    # 管理端快照同样不变
    d = client.get(f"/samples/{sid}").json()
    contents = {b["block_no"]: b["content"] for b in d["blocks"]}
    assert contents["B1"] == "内容-B1"


def test_correction_split_keeps_pending_and_claimed_samples_gradable(client):
    _scored_p1(client, 88)
    sid = _register_sample(client, ["T1", "T2"])["sample_id"]

    # T1 已领未交；T2 待领
    c1 = client.post("/claims", json={"teacher_id": "T1"}).json()["claim_no"]

    # 原答案被拆分：B1 留 P1，B2 去 P9，B3 不动
    r = client.post(
        "/corrections",
        json={
            "assignments": [
                {"paper_no": "P1", "blocks": ["B1"]},
                {"paper_no": "P9", "blocks": ["B2"]},
                {"paper_no": "P2", "blocks": ["B3"]},
            ]
        },
    )
    assert r.status_code == 200, r.text

    # 已领样卷号不被作废，仍可交分，且仍是冻结的完整两块
    got = client.get(f"/claims/{c1}").json()
    assert got["status"] == "active"
    assert [b["block_no"] for b in got["blocks"]] == ["B1", "B2"]
    assert client.post(f"/claims/{c1}/score", json={"score": 77}).status_code == 200

    # 待领教师 T2 仍可领到同样的完整样卷并交分
    c2 = client.post("/claims", json={"teacher_id": "T2"}).json()
    assert [b["block_no"] for b in c2["blocks"]] == ["B1", "B2"]
    assert client.post(f"/claims/{c2['claim_no']}/score", json={"score": 81}).status_code == 200

    d = client.get(f"/samples/{sid}").json()
    rows = {x["teacher_id"]: x for x in d["results"]}
    assert rows["T1"]["score_diff"] == -11
    assert rows["T2"]["score_diff"] == -7


def test_concurrent_claims_same_teacher_single_live_number(client):
    _scored_p1(client, 88)
    sid = _register_sample(client, ["T1", "T2"])["sample_id"]
    db = client.app.state.db

    from app.service import create_claim

    def hammer(tid):
        return create_claim(db, tid)["claim_no"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        t1_nos = list(pool.map(lambda _: hammer("T1"), range(20)))
        t2_nos = list(pool.map(lambda _: hammer("T2"), range(20)))

    assert len(set(t1_nos)) == 1
    assert len(set(t2_nos)) == 1

    with db.read() as conn:
        for tid in ("T1", "T2"):
            live = conn.execute(
                """
                SELECT COUNT(*) n FROM claim
                WHERE sample_id=? AND teacher_id=?
                  AND status IN ('active','submitted')
                """,
                (sid, tid),
            ).fetchone()["n"]
            assert live == 1

    # 退回后并发重领：同样恰好一个新有效号
    old = t1_nos[0]
    client.post(f"/claims/{old}/release")
    with ThreadPoolExecutor(max_workers=8) as pool:
        new_nos = list(pool.map(lambda _: hammer("T1"), range(12)))
    assert len(set(new_nos)) == 1
    assert new_nos[0] != old
    with db.read() as conn:
        n = conn.execute(
            """
            SELECT COUNT(*) n FROM claim
            WHERE sample_id=? AND teacher_id='T1'
              AND status IN ('active','submitted')
            """,
            (sid,),
        ).fetchone()["n"]
        assert n == 1


def test_teacher_in_two_samples_claims_each_then_converges(client):
    _scored_p1(client, 88)
    # 第二份样卷来自 P2：先给 P2 评分
    c = client.post("/claims", json={"teacher_id": "OFF2"}).json()["claim_no"]
    client.post(f"/claims/{c}/score", json={"score": 50})

    s1 = _register_sample(client, ["T1"], paper="P1")["sample_id"]
    s2 = client.post(
        "/samples", json={"paper_no": "P2", "teacher_ids": ["T1"]}
    ).json()["sample_id"]

    first = client.post("/claims", json={"teacher_id": "T1"}).json()
    assert [b["block_no"] for b in first["blocks"]] == ["B1", "B2"]
    second = client.post("/claims", json={"teacher_id": "T1"}).json()
    assert [b["block_no"] for b in second["blocks"]] == ["B3"]
    assert second["claim_no"] != first["claim_no"]
    # 两个席位都有有效号后，再领幂等返回最近的有效样卷号
    third = client.post("/claims", json={"teacher_id": "T1"}).json()
    assert third["claim_no"] in (first["claim_no"], second["claim_no"])

    d1 = client.get(f"/samples/{s1}").json()
    d2 = client.get(f"/samples/{s2}").json()
    assert d1["results"][0]["claim_no"] == first["claim_no"]
    assert d2["results"][0]["claim_no"] == second["claim_no"]


def test_calibration_alias_endpoints(client):
    _scored_p1(client, 88)
    sid = _register_sample(client)["sample_id"]
    c = client.post("/claims", json={"teacher_id": "T1"}).json()["claim_no"]
    client.post(f"/claims/{c}/score", json={"score": 83})

    for path in (
        f"/samples/{sid}/scores",
        f"/samples/{sid}/results",
        f"/sample-papers/{sid}",
        f"/calibration/samples/{sid}",
        f"/calibrations/{sid}/differences",
        "/calibration",
        "/samples",
        "/papers/P1/calibration",
        "/teachers/T1/samples",
    ):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])

    # 卷号也可直接作为样本引用
    r = client.get("/samples/P1/calibration")
    assert r.status_code == 200
    assert r.json()["reference_score"] == 88

    # 教师维度查询能看到 T1 的 -5
    body = client.get("/teachers/T1/scores").json()
    assert body["results"][0]["score_diff"] == -5
