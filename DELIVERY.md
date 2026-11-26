# 交付说明（实现 / 验证 / 未完成）

## 已实现

一个可直接 `docker compose up --build` 启动的 FastAPI + SQLite 服务，全部围绕“同一题”：

- **基础数据**
  - 原扫描块批量登记（`POST /blocks`，幂等，可带文字内容供教师评阅）、列表与当前归属。
  - 按卷录入有序块串：`PUT /papers/{卷号}/blocks`；另有 `POST /assignments` 整批录入。
  - 录入时未登记的块号自动成为“原扫描块”；同卷重复块、跨卷抢块均被拒绝。
- **教师领取与交分**
  - `POST /claims` 按队列领取答案，响应**不含卷号**（测试用醒目卷号断言整个响应文本不泄露）。
  - 返回整数**领题号 `claim_no`**；`POST /claims/{id}/score`（兼容 PUT、`POST /scores`）凭号交分。
  - 支持重复交分（旧分 `superseded` 留痕）、查询领题号状态、释放领取、按 `answer_id` 指定领取。
- **整批更正（核心）**
  - `POST /corrections` 提交 `{assignments:[{paper_no, blocks:[...]}]}`；只允许原扫描块，
    且全部原块**恰好一次**：未知块 / 缺失块 / 重复块 / 同批重复卷都有结构化错误。
  - **完整有序序列不变** → 同一答案身份：评分与领题号随答案迁移，支持“换卷也一样”。
  - **拆分 / 合并 / 调序** → 旧答案、旧领题号、有效评分全部置 `void`，生成新的待评答案。
  - 作废身份不再参与序列匹配，因此**后来拼回原样仍是新身份、仍须重评**。
  - `POST /corrections/verify` 只核对出方案；`mode=stage` 暂存，
    `POST /corrections/{id}/apply|cancel` 控制生效/取消。
  - 核对暂存期间旧领题号照常交分；生效后作废领题号交分返回 `409 CLAIM_VOID`。
  - 更正的校验 + 归卷重写 + 身份/领题号/评分迁移在**单个 `BEGIN IMMEDIATE` 事务**内，
    失败整体回滚，不留更正单、不动归卷与已入账评分。
- **查询**
  - `GET /papers/{卷号}`：`scored + score` 或 `pending + grading_status(unclaimed/claimed)`
    或 `empty`，并返回当前领题号、评语、有序块列。
  - `GET /papers`、`/overview`、`/blocks`、`/claims`、`/corrections` 等列表/总览。
- **交付/运维**
  - `Dockerfile`（python:3.11-slim、非 root、volume `/data`、HEALTHCHECK）。
  - `docker-compose.yml`（端口默认 8000，可用 `HOST_PORT` 覆盖；命名卷持久化；健康检查）。
  - 交互式文档 `/docs`（含请求示例）、`/redoc`、`/openapi.json`。
  - `POST /admin/seed-demo` 一键造演示数据、`POST /admin/reset?confirm=true` 清空自测。
  - `README.md` 含规则表、Compose/本地启动、curl 走查、API 与错误码速览。

## 已验证

在本工作区用 Python 3.11 虚拟环境实际执行：

1. **pytest：36 个用例全部通过**（`python -m pytest tests/ -q`），覆盖：
   - 健康检查 / `/docs` / OpenAPI；
   - 块登记幂等、按卷录入、重复块与跨卷冲突、总览、reset；
   - 领取响应无卷号、领取→交分→按卷查分、凭 body 交分、错误领题号/非法分数、
     领取池耗尽、重复交分版本、领取列表、释放、按 answer_id 领取；
   - **原样换卷保住分数与领题号（已交分 / 未交分两种）**、相同布局 no-op；
   - **拆分 / 调序 / 合并作废**、**拼回原样仍须重评**；
   - 4 类非法更正（未知块、跨卷重复、缺块、卷内重复）全部 422 且状态与已入账分数不变、
     失败更正不落单；
   - verify 不生效；stage 窗口内可交分、apply 后按规则保全/作废；apply 幂等、cancel；
   - 并发：4 线程对同一领题号交 20 个修订 → 始终恰有 1 条 valid；
     两个独立数据库连接/实例、12 次交替更正 + 并发交分 → 无 500/锁错误，
     最终每块仍恰好一行、无重复 active 序列。
2. **真实 uvicorn HTTP 冒烟：70 项检查全部通过**
   （`python scripts/smoke.py http://127.0.0.1:8000`，脚本仅用标准库），
   完整走了“登记→录入→盲领→交分→原样换卷保分→拆分作废→拼回仍重评→
   非法更正回滚→stage 窗口→apply 保分→总览”。
3. **持久化**：通过 HTTP 写入分数后，另起全新 Python 进程直接打开同一个 SQLite 文件，
   能读到归卷与 `73.5` 分（模拟重启）。
4. 服务日志中冒烟期间只有预期的 `200/409/422`，无 traceback / 500。
5. `docker-compose.yml` 已用 PyYAML 解析校验语法与端口/卷结构。

## 未完成 / 受环境限制未验证

- **本运行环境没有安装 docker / docker compose，因此 `docker compose up --build`
  未在本机实际跑过**；已做的替代验证是：按 Dockerfile 的 `CMD` 用等价的
  `uvicorn app.main:app --host 0.0.0.0 --port 8000` 实跑并通过全部 HTTP 冒烟，
  requirements 版本即实测版本，compose YAML 已解析校验。镜像构建/容器健康检查
  需在有 Docker 的环境最终确认一次（预期无问题）。
- 需求未要求登录鉴权，因此“教务员 / 教师”只通过字段（`teacher_id`）和路径语义区分，
  **没有做身份认证与权限控制**；如公网部署需另加鉴权。
- “扫描块”只保存块号与可选文字 `content`，**没有做扫描图片/文件上传与对象存储**；
  教师在 `/docs` 看到的是块号序列和文字内容。若要原图评阅，可在 `block` 上加 URL/对象键。
- 按题意服务被设计成**单题**（顶部有单例 question，可改标题/满分但不区分多题）；
  没有实现多题目隔离。
- 评分默认不设上限（避免误拒不同试卷的分值），仅拒绝非有限数值与负分；
  通过 `PUT /question` 设置 `full_score` 后，交分超过满分会返回 `422 SCORE_OUT_OF_RANGE`。
- 未配置 CI workflow；测试以仓库内 pytest + 冒烟脚本形式交付。
