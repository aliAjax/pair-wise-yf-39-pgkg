# 野生动物疫病监测与离线同步

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8305`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8305
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `observation`：现场观察；`sample`：样本与实验室结果；`cluster`：异常聚集事件。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`和`?region=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/sync`：离线批次重连同步，提交`{"batch_id":"...","items":[...]}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`、`X-Role`和`X-Region`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 片区与权限

- 每条观察、样本和聚集事件都带`region`。样本的片区自动跟随其观察记录。
- 调查员（`field`）通过`X-Region`声明所属片区，只能创建和操作本片区记录；越权操作其他片区返回`403 PermissionDenied`。
- `admin`和`epidemiologist`不受片区限制；聚集事件只有流行病学家能确认或重新确认，调查员无权确认。

## 片区交接

交接单本身是一类实体（`handover`），状态为`draft → in_transit → accepted`：

```json
POST /api/handovers
{"from_region": "A", "to_region": "B",
 "items": [{"id": "<稳定编号>", "kind": "observation"}, "<编号>"]}
```

- 原片区`dispatch`，接手片区`accept`；其他片区发起或接收都会被拒绝。
- 记录**保持原编号**随单整体转入接手片区，含已确认的聚集事件（确认结论一并交接，不再"没人接手"）。
- 逐条转移、逐条检查点：断在中途时再次`accept`只续作未完成项；已转移的记录不会二次移动或二次写审计；对已完成交接单重复`accept`幂等无写入。

## 断网重连

`POST /api/sync`以批次提交离线改动：

```json
{"batch_id": "设备生成的稳定批次号",
 "items": [{"kind": "observation", "id": "<稳定编号>", "data": {...}}]}
```

- 批次按`batch_id`幂等，重放返回首次结果，不重复建记录、不重复写审计。
- 同编号直接合并；观察按`event_id + observed_at`、样本按`sample_code`识别两侧同一条记录，合并到既有编号而不是新建。
- 载荷无变化的重复送达什么也不写（无状态变更、无审计）。
- **只合并没确认的部分**：非`draft`的聚集事件（已确认/已驳回/待重算）一律跳过，不被旧数据覆盖。
- 合并进来的观察若坐标或采样时间发生变化，会立即触发聚集事件失效。

## 聚集事件失效与重算

- 观察的坐标（`lat/lon`）或采样时间（`observed_at`）一变更（含交接后由接手片区修改），所有引用它的`confirmed`聚集事件立即变为`invalidated`并写一条`auto_invalidate`审计。
- 流行病学家调用`reconfirm_cluster`时重新按空间-时间规则计算，不再成簇则拒绝确认（应`dismiss`），通过后才恢复`confirmed`；调查员执行该动作会被拒绝。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。
