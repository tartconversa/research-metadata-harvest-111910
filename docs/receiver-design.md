# 采集端设计

`harvest/` 是对合成来源（`source/`，契约见 docs/source-contract.md）的元数据采集端。
全部交互都是真实协议请求：OAI-PMH ListRecords 翻页、GET /detail、GET /fulltext。

## 三条独立事实链

| 事实链 | 表 | 说明 |
| --- | --- | --- |
| 目录观察 | observations / catalog_revisions / catalog_state | 每次翻页原样留证（含中断运行的部分页）；版本按内容哈希去重 |
| 详情版本 | detail_versions / detail_state | 详情服务独立修订；暂时不可用只标记 unverified，不等于撤回 |
| 原件领取 | fulltext_state | 限期内记录来源声明的 open_at；不猜测发布日期等于开放时刻 |

明确区分：header status=deleted（显式删除）、setSpec 变化（集合退出）、
详情 503（暂时无法访问）是三种不同事实，引用状态分别解释。

## 覆盖证明

- 每次运行记录 [from_day, until_day]；只有完整结束的运行才写入 coverage_windows。
- 增量起点取已覆盖截止日（含当日）：天粒度边界必然重复，重复内容按哈希去重，不产生新版本。
- 非空 resumptionToken 才表示有下一页；中间空页继续翻，空凭据才是末尾。
- badResumptionToken 使本次运行中止：已观察页保留为证据，覆盖不推进，
  其他订阅已发布的数据不受任何影响；来源允许重新开始（无覆盖则回到全量）。
- 同日更正（datestamp 不变、内容变化）只在窗口覆盖该日时被发现：
  增量够不着的更正保持"仍待核实"（coverage: pending），全量核对后进入新版本。
  因此日常接续以增量为主、定期全量核对（run --mode full）。

## 标识关联

只用来源给出的 dc:relation 建立 work 关联（identity_epochs / identity_events）；
相同题名、不同 identifier 绝不合并。来源更正改变 relation 时产生关联事件，
订阅清单与已保存引用据以解释关联变化，原观察记录全部保留。

## 引用

- 新建引用要求成果当前在该团队订阅范围内、未被删除、公开依据未被撤回。
- 引用状态实时计算：显式删除标记 → invalid/with­drawn_deleted_marker；
  详情声明 metadata_public=false → invalid/public_basis_withdrawn；
  均给出失效时间与来源证据（观察记录或详情版本），并停止提供内容。
- 关联变化、详情未核实、退出订阅范围、全文限期以 notes 附加说明，不改变有效性。
- 取消订阅只停用订阅关系；原件按有效订阅引用计数保留（prune 只清理无人覆盖的）。
