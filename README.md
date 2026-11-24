# 光纤接续核验台

验收员从 A、B 两端 OTDR 事件原件和设计清单复算接续损耗。首页比对双端轨迹帧与设计接头，按 `J`、`K` 或使用跳检按钮定位证据。左侧七个入口分别处理采信、校准、配对、判损、对账、留档和签发。

`docker compose up --build` 后访问 `http://localhost:18468/`。本地先执行 `pip install -r requirements.txt`，再运行 `python -m flask --app app.main run --port=18468`，用 `pytest -q` 验收。`FIBER_DB` 设置 SQLite 文件，Compose 的命名卷保留运行记录。

各预览和留档 API 接收 `{"sample":"dirty"}`、`{"sample":"clean"}` 或 `{"traces":{"a":"CSV","b":"CSV","design":"CSV"},"settings":{...}}`。参数示例见 `sample_data/dirty/link.json`。从留档页提交，在签发页下载核验包。

到期清理采用“原件内容哈希 + 批次引用 + 争议封存”：

- `POST /api/batches` 可用 `inspection_id`、`batch_id`、`due_at`/`retention_until` 建立业务批次；`POST /api/inspections` 也可附带 `batches:[...]`。
- `POST /api/disputes` 受理争议并固化齐全的复核材料清单；争议批次的 `/api/disputes/<id>/originals` 或 `/api/batches/<id>/originals` 可下载完整原件。
- `POST /api/cleanup`（可传 `as_of`）执行可恢复清理：到期且无未结争议的批次先释放引用，最后一个活动引用消失后永久清空原件；到期批次的签发结论和完整性摘要仍可经 `/api/certificate/<inspection_id>?batch_id=<batch_id>` 查询。
- 中断后调用 `POST /api/cleanup/resume`；同一旧快照重跑为幂等操作，不会恢复已清除的原件。

- [模块与独立复算](docs/DEPTH.md)
- [字段与规则](docs/DATA-DICTIONARY.md)
