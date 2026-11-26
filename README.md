# 归卷更正与评分保全服务（FastAPI + SQLite）

扫描拼卷出错后，阅卷组用它把**同一道题**的答题块归回正确的卷，同时保住仍可使用的评分。
服务使用 FastAPI 提供 HTTP API，SQLite 持久化归卷、领题号、评分流水与更正单，
可用 Docker Compose 一键启动，并在 <http://localhost:8000/docs> 直接操作。

---

## 1. 它如何判断“评分要不要保住”

每份答案的身份是其**严格有序的扫描块序列**，并拥有连续的 lineage：

| 更正后的情况 | 判定 | 领题号 / 已有评分 |
| --- | --- | --- |
| 有序块序列与更正前某份当前答案**完全一致**（哪怕从 A 卷移到 B 卷） | `intact_moved` / `intact_same_paper` | **同领题号、同评分随答案走，继续有效** |
| 拆分（一块答案分到多卷） | `needs_grading` + 旧答案 `void/reason=split` | 旧领题号、旧评分**作废**，生成新的待评答案 |
| 合并（几份答案拼成一份） | `needs_grading` + `reason=merged` | 全部旧领题号/评分作废 |
| 调序 | `needs_grading` + `reason=reordered` | 作废，必须重评 |
| 先拆/调序，后来又拼回最初的原样 | 仍然是**新答案身份** `needs_grading` | 旧领题号/评分不会复活，必须重新领取评分 |

其它业务规则：

- 服务范围固定为**同一题**（响应中带 `question_id="Q1"`，顶部可设置题目标题/满分）。
- 每卷录入一串**有顺序**的扫描块号；录入时出现的块号即“原扫描块”。
- 整批更正只能使用**已登记的原扫描块**，且全部原块在批次中**恰好出现一次**
  （不能多、不能少、不能重复），否则整批失败。
- 教师 `POST /claims` 领取答案，返回内容**不含卷号**；凭返回的整数 `claim_no`（领题号）交分。
- 更正可先 `mode=stage` 暂存核对：核对期间旧归卷/旧领题号一切照常；
  `POST /corrections/{id}/apply` 生效后，只有仍有效的领题号能交分，作废领题号返回 `409 CLAIM_VOID`。
- 更正的校验、身份迁移、领题号/评分作废、归卷重写在**同一个 `BEGIN IMMEDIATE` 事务**中完成；
  任何校验失败整体回滚，**归卷与已入账评分不变**，也不会留下更正单。
- 评分采用流水表：同一领题号可重复交分（旧分标记 `superseded` 保留），
  被更正作废的分标记 `void` 保留，均不物理删除。
- 按卷号 `GET /papers/{paper_no}` 返回：
  - `status=scored` + `score`（当前得分），或
  - `status=pending` + `grading_status=unclaimed|claimed`（待评/已领取待交分），或
  - `status=empty`（该卷当前没有答题块）。

---

## 2. Docker Compose 启动（推荐）

```bash
docker compose up --build
# 若 8000 端口被占用： HOST_PORT=18000 docker compose up --build
```

启动后：

- Swagger UI：<http://localhost:8000/docs>（可直接 “Try it out”）
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

SQLite 文件位于容器内 `/data/correction.db`，通过命名卷 `correction-data` 持久化
（`docker compose down` 不会丢数据；加 `-v` 才会删除卷）。

想在 `/docs` 里立刻有数据可点，可调用一次 `POST /admin/seed-demo`
生成 `DEMO-P1/P2/P3` 三套演示卷；`POST /admin/reset?confirm=true` 可清空自测数据。

---

## 3. 不用 Docker 的本地运行 / 自测

需要 Python 3.11+。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000
# 打开 http://127.0.0.1:8000/docs
```

跑单元/集成测试（36 个用例，含并发与回滚）：

```bash
python -m pytest tests/ -q
```

对一个已经跑起来的服务做 70 项端到端 HTTP 冒烟（仅用标准库）：

```bash
python scripts/smoke.py                 # 默认 http://127.0.0.1:8000
python scripts/smoke.py http://HOST:PORT
```

冒烟脚本会先调用 `/admin/reset?confirm=true`，**不要对生产数据运行**。

---

## 4. `/docs` 五分钟走查

也可以用 curl 按下面顺序操作（完整可执行版本见 `scripts/smoke.py`）：

```bash
B=http://localhost:8000

# (可选) 造演示数据
curl -X POST $B/admin/seed-demo

# 1. 登记原扫描块（初次 PUT 录入时未登记的块也会自动登记为原块）
curl -X POST $B/blocks -H 'Content-Type: application/json' -d '{
  "blocks": [{"block_no":"B1","content":"作答内容1"}, "B2", "B3", "B4", "B5"]
}'

# 2. 每卷录入有序块串
curl -X PUT $B/papers/P1/blocks -H 'Content-Type: application/json' -d '{"blocks":["B1","B2","B3"]}'
curl -X PUT $B/papers/P2/blocks -H 'Content-Type: application/json' -d '{"blocks":["B4","B5"]}'

# 3. 教师领取（响应里没有 paper_no），拿到 claim_no
curl -X POST $B/claims -H 'Content-Type: application/json' -d '{"teacher_id":"T007"}'

# 4. 凭领题号交分（假设 claim_no=1）
curl -X POST $B/claims/1/score -H 'Content-Type: application/json' -d '{"score":88}'

# 5. 按卷号查得分/待评状态
curl $B/papers/P1

# 6. 整批更正：整份答案原样换到 P9，P2 不动（所有原块恰好一次）
curl -X POST $B/corrections -H 'Content-Type: application/json' -d '{
  "mode": "apply",
  "assignments": [
    {"paper_no":"P9", "blocks":["B1","B2","B3"]},
    {"paper_no":"P2", "blocks":["B4","B5"]}
  ]
}'
curl $B/papers/P9     # 88 分和原 claim_no 已随答案到 P9
curl $B/papers/P1     # status=empty

# 7. 只核对不生效（返回每份答案是 intact 还是 needs_grading、哪些领题号将作废）
curl -X POST $B/corrections/verify -H 'Content-Type: application/json' -d '{
  "assignments": [
    {"paper_no":"P9", "blocks":["B1","B2"]},
    {"paper_no":"P10","blocks":["B3"]},
    {"paper_no":"P2", "blocks":["B4","B5"]}
  ]
}'

# 8. 暂存 -> 核对期间教师仍可交分 -> 生效
CID=$(curl -X POST $B/corrections -H 'Content-Type: application/json' -d '{
  "mode":"stage",
  "assignments":[
    {"paper_no":"P9","blocks":["B1","B2"]},
    {"paper_no":"P10","blocks":["B3"]},
    {"paper_no":"P2","blocks":["B4","B5"]}
  ]
}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["correction_id"])')
curl -X POST $B/corrections/$CID/apply
curl -X POST $B/claims/1/score -H 'Content-Type: application/json' -d '{"score":90}'
# -> 409 CLAIM_VOID（P9 被拆，旧领题号作废）
```

---

## 5. API 速览

| 方法 & 路径 | 作用 |
| --- | --- |
| `GET /health` | 健康检查（探活 DB） |
| `GET /question` / `PUT /question` | 单题上下文 / 设置标题满分 |
| `POST /blocks` / `GET /blocks` | 批量登记原扫描块（幂等）/ 列表含当前归属 |
| `PUT /papers/{paper_no}/blocks` | 按卷录入/覆盖有序块串（初次归卷） |
| `POST /assignments` | 整批初次录入（事务，跨卷重复拒绝） |
| `GET /papers` / `GET /papers/{paper_no}` | 全部 / 单卷当前得分与待评状态 |
| `GET /papers/{paper_no}/result`、`.../status` | 同上别名 |
| `GET /overview` | 状态计数、未归卷块、各卷明细 |
| `POST /claims` | 领取不带卷号的答案（可空体；可选 `teacher_id` / `answer_id`） |
| `GET /claims` / `GET /claims/{claim_no}` | 领题号列表 / 查询是否仍有效 |
| `POST /claims/{claim_no}/score`（也支持 PUT、`POST /scores`） | 凭领题号交分/改分 |
| `POST /claims/{claim_no}/release` | 未交分领取释放回池 |
| `POST /corrections/verify` | 只核对并返回继承/作废方案，不生效 |
| `POST /corrections` | 提交整批更正：`mode=apply`（默认）/ `stage` / `verify` |
| `GET /corrections` / `GET /corrections/{id}` | 更正单列表 / 详情方案 |
| `POST /corrections/{id}/apply` / `.../cancel` | 暂存单生效（幂等）/ 取消 |
| `POST /admin/seed-demo` / `POST /admin/reset?confirm=true` | 演示数据 / 清空自测数据 |

请求体兼容常见别名与多种形态，例如更正批次也支持
`{"papers": {"P1": ["B1","B2"]}}`、裸数组 `[{"paper_no":"P1","blocks":[...]}]`、
`block_nos/sequence`、`paper/paperNo/卷号` 等。

### 常见错误码

- `422 CORRECTION_INVALID`：整批更正未通过（`details[].code` 可能是
  `UNKNOWN_BLOCK` / `DUPLICATE_BLOCK` / `MISSING_BLOCKS` / `DUPLICATE_PAPER_IN_BATCH` 等）。
- `409 CLAIM_VOID`：领题号已因拆拼/调序作废。
- `409 CLAIM_RELEASED` / `409 NO_CLAIMABLE_ANSWER` / `409 BLOCK_ASSIGNED_TO_OTHER_PAPER`。
- `404 PAPER_NOT_FOUND` / `404 CLAIM_NOT_FOUND` / `404 CORRECTION_NOT_FOUND`。
- `422 SCORE_OUT_OF_RANGE`：负分，或设置 `full_score` 后交分超过满分（默认不设上限）。

---

## 6. 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `data/correction.db`（Compose 中为 `/data/correction.db`） | SQLite 文件路径 |
| `HOST_PORT` | `8000` | Compose 映射到宿主机的端口 |

单 uvicorn 进程 + 线程池；所有写事务经进程内锁串行化并使用 `BEGIN IMMEDIATE`，
SQLite 写锁与 `busy_timeout` 也保证多连接/多线程下的原子性（有用例验证）。

---

## 7. 目录结构

```
app/
  main.py       # FastAPI 路由与异常处理
  service.py    # 领域逻辑：答案身份、继承/作废、更正事务、交分
  db.py         # SQLite 连接、schema、串行写事务、轻量迁移
  schemas.py    # Pydantic 模型与宽松入参兼容
tests/          # 36 个 pytest 用例（规则/回滚/暂存/并发）
scripts/smoke.py# 70 项标准库端到端冒烟
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
