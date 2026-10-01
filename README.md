# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻交通提示。批准记录接入走廊时隙台账：按走廊时段占用容量，满员即排队并给出最早释放时间；审核通过离线编号幂等回传，并携带容量版本做乐观并发；紧急授权可插队但单独标记，且不能绕过载荷与高度硬限制；禁飞区更新后已批准计划立即失效并释放占用。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 时隙台账

- 走廊按固定时长切分时隙（默认 30 分钟），每个时隙有容量上限（默认 3 架）。计划批准后按其飞行时间占用对应时隙；`slot_ledger` 记录每个时隙的占用与释放。
- 容量满员时审核不再批准，而是把计划置为 `queued` 排队状态，并返回 `earliest_release_at`（当前占用计划结束、腾出容量的最早时刻）。
- 台账有全局 `capacity_version`，每次占用/释放/失效自增。审核请求可带 `expected_capacity_version`：两个审核员同时提交只有一人能批过，后到者收到 `capacity_version_conflict`，响应里带当前剩余容量与最新版本。
- 指挥官（`commander`）带 `override_reason` 的紧急授权可以插队占用满员时隙，但会在批准记录上单独标记 `override_kind=emergency_authority`；载荷（>25kg）与高度（>120m）硬限制任何角色都不能绕过。
- 新建或更新禁飞区/限制后，与其几何、时间、高度重叠的已批准计划立即失效（回到 `submitted`），释放时隙占用并生成 `airspace_change` 通知。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区（重叠的已批准计划立即失效并释放占用）。
- `POST /api/restrictions/{id}/update`：更新限制，重叠的已批准计划同样立即失效。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束、空域冲突、相邻交通提示与时隙容量。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等，并可用 `expected_capacity_version` 做并发控制。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，释放时隙占用并生成通知。
- `GET /api/corridor/ledger`：时隙台账（各时隙占用/剩余容量、排队计划、容量版本）。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理（释放时隙）。
- `GET /api/state`：按角色返回计划、限制、容量版本等公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。时隙容量为简化的固定时隙计数，`earliest_release_at` 取当前占用计划结束时刻的最大值，未考虑时隙内精确四维航迹与尾流间隔。紧急授权只能插队占用时隙、覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
