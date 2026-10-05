# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者，记录已撤回用途 `withdrawn_purposes`。
- `consent`：同意版本，`scope` 按用途登记（如 `research`、`genetic_analysis`），可激活/失效/撤回。
- `sample`：样本，入库时登记本次用到的用途 `purposes` 及当时覆盖各用途的同意快照 `consent_snapshot`。
- `withdrawal`：撤回申请，按用途定向（`purposes` + `sample_ids`）。

## 按用途的同意与撤回

- 用途：`research`（科研）、`genetic_analysis`（遗传分析）。同意激活后按 `scope` 覆盖对应用途，过期或被撤回/失效即失去覆盖。
- 入库（`store`）：`purposes` 中的每个用途都必须有参与者的有效同意覆盖，否则拒绝入库。
- 撤回执行（`withdrawal.execute`）：只停用本次指定用途；样本只要还有有效同意覆盖剩余用途就保持 `stored`/`on_loan` 继续可用，未被覆盖的用途才使样本转 `pending_disposal`（待处置）。
- 正在借出的样本先转 `pending_recall`（待召回）；`return` 归还时重新评估覆盖——恢复覆盖则回 `stored`，否则转 `pending_disposal`，之后才能 `destroy`/`anonymize`。
- 同意被撤回或失效同样按用途级联评估在用样本，审计动作记为 `consent_effect`。
- 撤回执行与样本借出并发提交时互斥：以审批时的样本版本为基准，先提交者成功，另一方得到 `409 ConflictError`（SQLite 乐观锁 + WAL）。
- 审计 `GET /api/audit` 中 `withdrawal_effect`/`consent_effect` 记录了哪些用途失去覆盖、前后覆盖这些用途的同意、剩余用途及处置决定，可据此判断样本失效原因。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
