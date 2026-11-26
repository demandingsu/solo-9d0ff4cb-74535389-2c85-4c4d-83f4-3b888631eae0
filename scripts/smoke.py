#!/usr/bin/env python3
"""端到端冒烟自测（仅用标准库），可对本机或 docker compose 暴露的服务运行。

用法：
    python scripts/smoke.py [BASE_URL]
环境变量：
    BASE_URL=http://127.0.0.1:8000

脚本会先调用 /admin/reset?confirm=true 清空数据，请不要对生产数据运行。
退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE_URL", "http://127.0.0.1:8000")).rstrip("/")

failures: list[str] = []
checks = 0


def call(method: str, path: str, body=None, expect: int | set[int] | None = 200):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        text = e.read().decode("utf-8")

    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text

    global checks
    if expect is not None:
        allowed = expect if isinstance(expect, set) else {expect}
        checks += 1
        if status not in allowed:
            failures.append(f"{method} {path}: 期望 {sorted(allowed)}，实得 {status}: {text[:300]}")
    return status, parsed


def check(name: str, cond: bool, detail: str = ""):
    global checks
    checks += 1
    if cond:
        print(f"  ✓ {name}")
    else:
        failures.append(f"{name} {detail}")
        print(f"  ✗ {name} {detail}")


def main() -> int:
    print(f"== 对 {BASE} 执行端到端冒烟 ==")

    s, h = call("GET", "/health")
    check("健康检查 200", s == 200 and h and h.get("status") == "ok", str(h))

    s, docs = call("GET", "/docs", expect=200)
    check("/docs 可打开", s == 200 and isinstance(docs, str) and "swagger" in docs.lower())

    call("POST", "/admin/reset?confirm=true")

    # 1) 登记原扫描块
    s, r = call(
        "POST",
        "/blocks",
        {"blocks": [{"block_no": f"B{i}", "content": f"第1题答题内容 {i}"} for i in range(1, 7)]},
    )
    check("登记 6 个原扫描块", s == 200 and r["registered"] == 6, str(r)[:200])

    # 2) 逐卷录入有序块串
    for pno, blocks in {"P1": ["B1", "B2", "B3"], "P2": ["B4", "B5"], "P3": ["B6"]}.items():
        s, _ = call("PUT", f"/papers/{pno}/blocks", {"blocks": blocks})
        check(f"录入 {pno}={blocks}", s == 200)

    s, p1 = call("GET", "/papers/P1")
    check("P1 初始待评", p1["status"] == "pending" and p1["blocks"] == ["B1", "B2", "B3"])

    # 3) 教师领取不带卷号的答案
    s, c1 = call("POST", "/claims", {"teacher_id": "T-甲"})
    check("领取答案得到领题号", s == 200 and isinstance(c1["claim_no"], int))
    check("领取响应不含卷号", "paper_no" not in c1 and "P1" not in json.dumps(c1, ensure_ascii=False))
    cno = c1["claim_no"]
    check("领取到的是 P1 的三块", [b["block_no"] for b in c1["blocks"]] == ["B1", "B2", "B3"])

    s, c2 = call("POST", "/claims", {"teacher_id": "T-乙"})
    cno2 = c2["claim_no"]
    check("第二次领取得到 P2 的两块", [b["block_no"] for b in c2["blocks"]] == ["B4", "B5"])
    s, c3 = call("POST", "/claims", {"teacher_id": "T-丙"})
    cno3 = c3["claim_no"]
    check("第三次领取得到 P3 的一块", [b["block_no"] for b in c3["blocks"]] == ["B6"])

    # 4) 凭领题号交分
    s, _ = call("POST", f"/claims/{cno}/score", {"score": 88, "comment": "要点齐全"})
    check("凭领题号交分 88", s == 200)
    s, p1 = call("GET", "/papers/P1")
    check("按卷号查到得分 88", p1["status"] == "scored" and p1["score"] == 88)

    # 5) 整份答案原样换卷：评分与领题号随答案走
    batch_move = [
        {"paper_no": "P9", "blocks": ["B1", "B2", "B3"]},
        {"paper_no": "P2", "blocks": ["B4", "B5"]},
        {"paper_no": "P3", "blocks": ["B6"]},
    ]
    s, r = call("POST", "/corrections", {"assignments": batch_move})
    check("整批更正（原样换卷）生效", s == 200 and r["status"] == "applied", str(r)[:300])
    p9_item = next(a for a in r["assignments"] if a["paper_no"] == "P9")
    check("方案标记 intact_moved 且带领题号/分数",
          p9_item["disposition"] == "intact_moved" and p9_item["claim_no"] == cno and p9_item["score"] == 88)

    _, p9 = call("GET", "/papers/P9")
    check("新卷 P9 查到原评分 88", p9["status"] == "scored" and p9["score"] == 88 and p9["claim_no"] == cno)
    _, p1 = call("GET", "/papers/P1")
    check("旧卷 P1 已无答题块", p1["status"] == "empty")
    s, claim = call("GET", f"/claims/{cno}")
    check("原领题号仍有效（submitted）", claim["status"] == "submitted")

    # 6) 拆拼：旧领题号/评分作废，两卷待评
    batch_split = [
        {"paper_no": "P9", "blocks": ["B1", "B2"]},
        {"paper_no": "P10", "blocks": ["B3"]},
        {"paper_no": "P2", "blocks": ["B4", "B5"]},
        {"paper_no": "P3", "blocks": ["B6"]},
    ]
    s, r = call("POST", "/corrections", {"assignments": batch_split})
    check("整批更正（拆分）生效", s == 200)
    check("方案有 1 个 void 答案且原领题号在作废列表",
          len(r["voided_answers"]) == 1 and cno in r["voided_answers"][0]["voided_claim_nos"])

    s, _ = call("POST", f"/claims/{cno}/score", {"score": 99}, expect=409)
    check("作废领题号交分被拒（409 CLAIM_VOID）", s == 409)
    _, p9 = call("GET", "/papers/P9")
    _, p10 = call("GET", "/papers/P10")
    check("拆分后 P9/P10 均待评且无旧分", p9["status"] == "pending" and p10["status"] == "pending"
          and p9["score"] is None and p10["score"] is None)

    # 7) 拼回原样也仍须重评
    batch_back = [
        {"paper_no": "P9", "blocks": ["B1", "B2", "B3"]},
        {"paper_no": "P2", "blocks": ["B4", "B5"]},
        {"paper_no": "P3", "blocks": ["B6"]},
    ]
    s, r = call("POST", "/corrections", {"assignments": batch_back})
    check("拼回原序列生效但标记 needs_grading",
          s == 200 and r["assignments"][0]["disposition"] == "needs_grading")
    s, _ = call("POST", f"/claims/{cno}/score", {"score": 1}, expect=409)
    check("旧领题号拼回后仍作废", s == 409)
    s, cnew = call("POST", "/claims", {"teacher_id": "T-丁"})
    check("必须重新领取", s == 200 and cnew["claim_no"] != cno)
    s, _ = call("POST", f"/claims/{cnew['claim_no']}/score", {"score": 91})
    check("新领题号交分成功", s == 200)
    _, p9 = call("GET", "/papers/P9")
    check("P9 显示新评分 91", p9["score"] == 91)

    # 8) 失败更正：未知块 / 缺块，状态不变
    bad = [
        {"paper_no": "P9", "blocks": ["B1", "B2", "B9"]},
        {"paper_no": "P2", "blocks": ["B4", "B5"]},
        {"paper_no": "P3", "blocks": ["B6"]},
    ]
    s, r = call("POST", "/corrections", {"assignments": bad}, expect=422)
    codes = {e["code"] for e in r["detail"]["details"]}
    check("使用非原块被拒（UNKNOWN_BLOCK）", s == 422 and "UNKNOWN_BLOCK" in codes)
    _, p9 = call("GET", "/papers/P9")
    check("失败后归卷/新评分不变", p9["blocks"] == ["B1", "B2", "B3"] and p9["score"] == 91)

    missing = [
        {"paper_no": "P9", "blocks": ["B1", "B2"]},
        {"paper_no": "P2", "blocks": ["B4", "B5"]},
        {"paper_no": "P3", "blocks": ["B6"]},
    ]
    s, r = call("POST", "/corrections", {"assignments": missing}, expect=422)
    check("未每块恰用一次被拒（MISSING_BLOCKS）",
          s == 422 and any(e["code"] == "MISSING_BLOCKS" for e in r["detail"]["details"]))

    # 9) 暂存核对窗口
    stage_batch = [
        {"paper_no": "P9", "blocks": ["B1", "B2", "B3"]},
        {"paper_no": "PX", "blocks": ["B4", "B5"]},
        {"paper_no": "P3", "blocks": ["B6"]},
    ]
    s, r = call("POST", "/corrections", {"assignments": stage_batch, "mode": "stage"})
    cid = r["correction_id"]
    check("更正单暂存为 checked", s == 200 and r["status"] == "checked")
    s, _ = call("POST", f"/claims/{cno2}/score", {"score": 70})
    check("核对期间教师照常交分（有效领题号）", s == 200)
    s, r = call("POST", f"/corrections/{cid}/apply")
    check("暂存更正生效", s == 200 and r["status"] == "applied")
    _, px = call("GET", "/papers/PX")
    check("P2 的答案原样移到 PX，分数 70 随答案走", px["score"] == 70)

    # 10) 总览
    s, ov = call("GET", "/overview")
    check("总览可查询且块全部归卷", s == 200 and ov["unassigned_blocks"] == [])

    # 11) 样卷校准：以已评分的 P9（91 分，B1/B2/B3）为样卷
    s, samp = call("POST", "/samples", {
        "paper_no": "P9", "teacher_ids": ["CAL1", "CAL2", "CAL3"]
    })
    check("登记样卷 200 且冻结参考分 91",
          s == 200 and samp.get("reference_score") == 91 and samp.get("sample_id"))
    sid = samp["sample_id"]

    s, k1 = call("POST", "/claims", {"teacher_id": "CAL1"})
    raw1 = json.dumps(k1, ensure_ascii=False).lower()
    check("CAL1 原接口优先领到样卷（B1,B2,B3）",
          s == 200 and [b["block_no"] for b in k1["blocks"]] == ["B1", "B2", "B3"])

    def _forbidden_keys(obj):
        bad = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                if any(w in lk for w in ("sample", "reference", "calibrat", "ref_score")):
                    bad.append(k)
                bad += _forbidden_keys(v)
        elif isinstance(obj, list):
            for v in obj:
                bad += _forbidden_keys(v)
        return bad

    def _scalar_values(obj):
        vals = []
        if isinstance(obj, dict):
            for v in obj.values():
                vals += _scalar_values(v)
        elif isinstance(obj, list):
            for v in obj:
                vals += _scalar_values(v)
        else:
            vals.append(obj)
        return vals

    # 结构上不能出现样卷/参考分字段，也不能有数值等于参考分 91
    _has_ref_value = any(
        isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) == 91.0
        for v in _scalar_values(k1)
    )
    check("领取响应看不到样卷标记/参考分",
          _forbidden_keys(k1) == [] and not _has_ref_value
          and "sample" not in raw1.lower() and "reference" not in raw1.lower())

    # 重复领取幂等返回同一号（并发时同样至多一个有效号）
    s, k1b = call("POST", "/claims", {"teacher_id": "CAL1"})
    check("重复领取返回同一领题号", s == 200 and k1b["claim_no"] == k1["claim_no"])

    s, k2 = call("POST", "/claims", {"teacher_id": "CAL2"})
    call("POST", f"/claims/{k1['claim_no']}/score", {"score": 85})
    call("POST", f"/claims/{k2['claim_no']}/score", {"score": 95})
    call("POST", f"/claims/{k2['claim_no']}/score", {"score": 94})  # 改分取最新

    # CAL3：领取 -> 退回 -> 旧号失效 -> 领新号 -> 交分
    s, k3 = call("POST", "/claims", {"teacher_id": "CAL3"})
    old3 = k3["claim_no"]
    call("POST", f"/claims/{old3}/release", expect=200)
    s, _ = call("POST", f"/claims/{old3}/score", {"score": 1}, expect=409)
    check("退回的样卷号码交分被拒", s == 409)
    s, k3n = call("POST", "/claims", {"teacher_id": "CAL3"})
    check("退回后可领新号", s == 200 and k3n["claim_no"] != old3)
    call("POST", f"/claims/{k3n['claim_no']}/score", {"score": 91})

    s, cal = call("GET", f"/samples/{sid}")
    rows = {r["teacher_id"]: r for r in cal["results"]}
    check("校准：CAL1=85 分差 -6", rows["CAL1"]["latest_score"] == 85
          and rows["CAL1"]["score_diff"] == -6)
    check("校准：CAL2 最新 94 分差 3（95 已被改分取代）",
          rows["CAL2"]["latest_score"] == 94 and rows["CAL2"]["score_diff"] == 3)
    check("校准：CAL3=91 分差 0", rows["CAL3"]["latest_score"] == 91
          and rows["CAL3"]["score_diff"] == 0)

    # 样卷评分不影响正式卷：P9 仍为正式分 91
    _, p9now = call("GET", "/papers/P9")
    check("样卷交分不影响 P9 正式分", p9now["score"] == 91)

    # 别名路径可查分差
    s, alias = call("GET", f"/calibration/samples/{sid}/scores")
    check("校准别名路径可查", s == 200 and len(alias["scores"]) == 3)

    print(f"\n通过 {checks - len(failures)} 项检查，失败 {len(failures)} 项。")
    if failures:
        print("失败明细：")
        for f in failures:
            print(" -", f)
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
