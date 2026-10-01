# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

空域限制、飞行计划和批准记录通过**时隙台账**打通：低空走廊按固定粒度切分时隙并占用容量，容量满后计划进入 FIFO 排队并返回最早释放时间；台账版本支持并发审核的乐观校验；紧急授权可以插队但单独标记，且不能绕过载荷与高度硬限制；新增禁飞区立即失效时空重叠的已批准计划并释放占用。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。`kind=no_fly` 立即对时空/高度重叠的已批准计划生效：计划置为 `invalidated`、释放走廊时隙、通知运营方并唤醒排队。
- `POST /api/corridors`：维护低空走廊（矩形范围、高度层、`capacity` 同时段容量、`slot_seconds` 时隙粒度）。
- `GET /api/corridors`、`GET /api/corridors/{id}/ledger`：走廊列表与时隙台账（每槽容量/占用/紧急占用/剩余、等待队列及队首位置）。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束、相邻交通冲突和走廊时隙占用（含每槽剩余容量与最早释放时间）。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
  - `approve` 可选 `expected_ledger_versions: {"走廊id": 版本}` 做台账乐观校验；版本已变化时返回 `409 ledger_version_conflict` 并附带最新剩余容量。
  - 走廊时隙满或队列前方有计划时返回 `409 slot_capacity_full` / `ahead_in_queue`，计划自动入队，详情含队首位置、剩余容量、`earliest_release`；容量释放后运营方收到 `slot_available` 通知。
  - `emergency: true` + `override_reason` 仅指挥官可用，可越过容量/队列插队（允许超售），占用在台账中以 `emergency` 单独标记；载荷（≤25kg）与高度（≤120m）硬限制对紧急授权同样生效。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消；已批准计划变更/取消会释放时隙并唤醒排队。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理（到期同样释放时隙）。
- `GET /api/state`：按角色返回计划、限制、走廊和公开信息。

### 并发与一致性

写事务使用 `BEGIN IMMEDIATE` 串行化（每线程独立 SQLite 连接 + WAL），两个审核员同时批准同一走廊的最后容量时只有一个成功，后到者拿到最新台账并排队；走廊 `ledger_version` 随每次占用/释放递增，供审核端做乐观并发控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准；走廊为矩形 + 单一高度层，容量是统一槽位容量，不区分机型/速度。紧急授权只能覆盖空域、交通与容量冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite（多线程串行写）适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
