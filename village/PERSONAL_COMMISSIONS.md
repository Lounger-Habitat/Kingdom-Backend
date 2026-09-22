# NPC 个人委托

NPC 通过 Agent 指令发布，只有玩家能够认领并完成。NPC 可以查询委托、撤回自己尚未被认领的委托，不能承接或完成其他 NPC 的个人委托。阶段规则复用村庄事务，但参与者、进度、报酬和交付物独立保存。原 NPC 交易订单、商店与市场流程不变；NPC 仍可参与村庄公共事务。

## 生命周期与资产

- 发布后为 `OPEN`，从发布者可用金币划出全部报酬，余额不足整单拒绝。
- 认领后为 `ACTIVE`。一份委托只允许一名承接者，发布者不能认领自己；认领不再次扣钱。
- 同阶段所有条件满足后开放下一阶段，只接受当前承接者的贡献；最后阶段完成为 `COMPLETED`。
- 物品和金币交付先托管。全部完成时，托管资产交发布者，报酬给承接者，一次事务原子结算。
- 发布者只能撤回 `OPEN` 委托，状态变为 `WITHDRAWN`。已认领时不能单方修改或撤销承诺。
- 承接者主动放弃变为 `ABANDONED`；截止后变为 `EXPIRED`。全部托管交付物和金币退回承接者，未发报酬退发布者。已经投入的劳动时间不返还。
- 退还批次保留原品质、来源、保鲜期限和菜品/次数等组件。原批次仍相容时合并，否则新建独立批次，不覆盖原批次或刷新期限。
- 世界时间推进、bootstrap、玩家操作及劳动 tick 自动处理到期，即使玩家离线也可退款。

## NPC 指令

指令必须在当前被代理 NPC 的能力白名单内。身份由命令通道绑定，参数不能指定另一个发布者或承接者。

| 指令 | 参数 |
| --- | --- |
| `PUBLISH_PERSONAL_COMMISSION` | `title`, `description?`, `reward_gold`, `valid_for_minutes`, `default_location_code?`, `stages` |
| `INSPECT_PERSONAL_COMMISSIONS` | `scope=published/all`，默认 published；查询本人发布的委托或只读浏览可见委托 |
| `WITHDRAW_PERSONAL_COMMISSION` | `commission_id` |

发布参数示例：

```json
{
  "title": "准备饭店柴火",
  "description": "准备木材后，到工地整理搬运。",
  "reward_gold": 80,
  "valid_for_minutes": 1440,
  "default_location_code": "RESTAURANT",
  "stages": [
    {"title": "准备材料", "conditions": [
      {"type": "item", "item_ids": [1002], "target": 20}
    ]},
    {"title": "整理搬运", "location_code": "REPAIR_YARD", "conditions": [
      {"type": "labor", "target": 2, "unit_minutes": 15}
    ]}
  ]
}
```

委托、阶段、条件的 ID 可由后端生成。之后使用查询或发布回执给出的 `commission_id`、`condition_id` 操作，不自行推断。

默认地点省略时取 NPC 工作地点，显式空字符串表示不限地点；每阶段地点非空时覆盖默认，否则继承。所有地点须有效登记，Unity 现场操作须有对应锚点。

物品条件可配置 `min_quality`、`requires_freshness`、`min_remaining_minutes`。多物品 ID 表示任选一种累计到同一目标；每种都要时配置多个条件。金币条件的 `target` 是承接者交付额，与发布者支付的 `reward_gold` 分开。

当前限制：每 NPC 最多 10 份开放/执行中委托；每承接者最多 5 份执行中委托；每委托 1–20 阶段、最多 100 条件；报酬和单条件数量上限 100 万；期限最多 30 游戏日；每份劳动最多 1440 游戏分钟。发布后定义固定。

NPC 发布和撤回在后端即时结算，Unity 接到终态回执后不会再次排队执行。个人委托中的交付、劳动与放弃由承接玩家操作；NPC 的 `STORY_ACT` 公共事务劳动仍按原有真实计时规则执行。

## 玩家与界面

玩家仍使用受信任 Env 的 `/api/village/player/action/`。新增 `commission_accept`、`commission_contribute`、`commission_labor_start`、`commission_abandon`，停止劳动沿用 `labor_cancel`。详见 `PLAYER_ACTIONS.md`。

个人委托入口显示发布者、报酬、截止、全部阶段和有效地点，认领后只开放当前阶段操作。“我的事务”显示本人委托及其历史。旧供货认领经迁移后也使用个人委托入口，不再显示单独的旧供货卡片。

玩家个人劳动和公共劳动共用单一在线会话，不能并行累积。只有 Env 的真实在场 tick 可增加劳动；断线、离场、观察模式、心跳失效会停止当前间隔。阶段结束时不把剩余时间计入下一阶段。

## 持久化、审计与兼容

`VillagePersonalCommission` 保存定义、参与者、进度及托管金额；`VillagePersonalCommissionEscrow` 保存实际交付批次；`VillagePersonalCommissionReceipt` 保存请求与结算回执。所有写操作按世界时钟锁顺序串行化，重复请求不会重复扣款、交付或发奖。

后台“雾溪村配置”新增个人委托、托管物品、操作记录三个只读审计入口。只能通过合法指令推进，不可在后台直接修改钱包或完成状态。

`0051_personal_commissions` 创建个人委托模型、扩展玩家劳动目标，为正式活跃 NPC 追加六项能力。后续 `0052_migrate_legacy_supply` 将存量供货转换为个人委托，再删除旧供货模型与数据表。迁移只承接原有需求，不增加自动发布机制，不修改商店与交易规则。

- 尚有效的已认领供货转换为 `ACTIVE` 个人委托，保留承接者、目标数量和已交付进度；剩余预留金币直接转为委托专款，不再扣一次发布者钱包。
- 已交付的物品和已付报酬保持原结算，不重复进入托管或再次支付。新交付仍按个人委托规则托管，完成剩余条件时支付剩余报酬。
- 已完成、已取消和已过期认领分别转换为 `COMPLETED`、`ABANDONED`、`EXPIRED` 历史委托；到期尚未释放的预留款只退还一次。
- 原供货中尚有效且未分配的剩余数量独立转成待认领委托，从发布者钱包实际预留全部报酬；预算不足则转为关闭记录，不发布没有资金保障的委托。
- 迁移审计回执记录原数量、金额、状态及历史玩家回执和贡献记录编号。旧供货表删除后仍可追溯来源。

当前玩家协议仅提供 `commission_accept`、`commission_contribute`、`commission_labor_start`、`commission_abandon` 等新入口。旧 `claim`、`deliver`、`cancel` 均返回 `INVALID_PARAMS`；overview 不包含 `contracts` 或 `my_claims`。历史请求与账目保留审计用途，不再启用旧收购履约逻辑。

`0053_player_only_personal_commissions` 移除 NPC 的个人委托认领、履约和放弃能力。核心服务同时校验承接者必须为玩家；即使旧客户端保留过期能力，也无法从 NPC 命令通道认领或履约。已有 NPC 承接的未完成委托关闭并退还托管物资与报酬；匹配的未完成 NPC 命令安全终止，释放其运行状态。玩家委托与 NPC 公共事务不受影响。

个人委托只执行已支持的条件与资产结算。描述文本不能直接改变桥梁、建筑或公共基金；额外世界效果需要明确业务接入。

## 本地升级记录

2026-09-17 首次升级应用 0051（以下为该次升级时的历史记录，后续 0052 另行记录）。备份：`Temp/VillageAffairBackups/before_personal_commissions_20260917T111319Z.sqlite3`；核对结果：同目录 `after_personal_commissions_verification.json`。33 张资源/公共事务/认领/世界时间相关表逐表哈希保持一致；4 份旧供货记录、2 份旧认领保留，5 个正式活跃 NPC 追加指令权限，实际存档未创建新委托。

0051 升级时验证通过：核心、玩家、NPC 指令和后台审计联合回归 41 项；PostgreSQL 玩家入口并发 12 项、核心并发 3 项；Unity 玩家 EditMode 29 项、NPC 指令 EditMode 5 项；固定 18088 隔离后端的双玩家 WebSocket PlayMode 测试 1 项。另有玩家旧认领、公共事务及商店兼容联合回归 62 项通过，部分用例与前述套件重叠，不累计计数。

双玩家联网验证涵盖独占认领、交付扣物、一次报酬结算、重复请求及断线重连后的回执重放。Unity 重编译无错误，测试结束后已退出运行模式并停止临时联网和 PostgreSQL 测试服务。

2026-09-17 随后已应用 `0052_migrate_legacy_supply`，完成旧供货系统清除。备份：`Temp/VillageAffairBackups/before_procurement_replacement_20260917T120042Z.sqlite3`；核对结果：同目录 `after_procurement_replacement_verification.json`。4 条原供货、2 条原认领转换为 5 份个人委托及 6 条迁移审计记录，两张旧表已删除：玉米 12 份、报酬 60 金币的委托可认领；其余为 2 份过期、1 份放弃、1 份完成历史。Lucy 钱包从 350 变为 290，委托托管从 0 变为 60；资金总量守恒，其余 120 张表逐表哈希未变化，物品、玩家资产及世界时间未改动。

0052 验证通过：迁移专项 9 项、核心 18 项；玩家操作和个人委托 27 项；PostgreSQL 玩家并发 12 项、核心并发 3 项；Unity 玩家 EditMode 30 项、双玩家联网 PlayMode 1 项。商店、公共事务、Lucy 饭店、NPC 指令及后台审计兼容回归共 82 项，81 项首次通过，1 项同步既有评价查询的分页返回结构后单项复测通过。正式应用前还在真实存档副本完成了迁移预演，独立审查确认旧已交付与已支付部分不重复结算。测试服务已关闭。

2026-09-17 已应用 `0053_player_only_personal_commissions`。备份：`Temp/VillageAffairBackups/before_player_only_commissions_20260917T121523Z.sqlite3`；核对结果：同目录 `after_player_only_commissions_verification.json`。实际存档没有 NPC 活跃承接或待执行的旧个人委托命令，因此仅清除 5 位 NPC 的三项承接权限；其余 122 张表逐表哈希未变化，现有个人委托、玩家与 NPC 资产、公共事务及世界时间保持一致。5 位 NPC 的 `STORY_ACT` 与 `STORY_VOTE` 权限保留，村庄事务功能已开启。

0053 验证通过：个人委托核心与迁移 24 项；NPC 指令、玩家个人委托及灵活公共事务联合 27 项；玩家操作与后台审计联合 28 项；显式关闭开发快速模式后的公共事务和生活动作联合 65 项；Unity NPC 指令 6 项、玩家 30 项。以上套件存在重叠，不累计计数。Unity 编译无错误，编辑器已退出运行模式。额外生活需求扩展测试发现 2 项既有预期不一致：旧测试期待 MOVE 没有标签、整夜不睡不损失健康，而当前规则已有移动标签与夜间健康惩罚；本次未改变这些生活规则。
