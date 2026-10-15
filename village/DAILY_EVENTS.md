# 日常事件配置与验证

事件提供可参与的机会；NPC 根据自己的资源和计划决定是否行动。模板不扣钱、不生成货物、不强迫发布委托，也不改变已认领委托的报酬与交付承诺。业务进度继续由原库存、交易、点评、供餐和公共事务负责。

## 在哪里配置

迁移 `0071_daily_event_templates` 和 `0072_seed_daily_event_templates` 创建数据表及首批模板；`0073_daily_event_npc_scope` 增加个人/公共NPC通知范围并修正历史待发记录。Django Admin 的“日常事件模板”可修改或新增同类型事件，无需编辑 Python：选择事实规则、NPC通知范围，填写参数、邀请期、提醒间隔和文案。

`npc_scope=PERSONAL` 为默认，只通知参数中指定的NPC；`npc_scope=PUBLIC` 通知五位村民。Lucy备料、Bill农活准备、Jack鲜货、Mark点评、Nora补货为个人；公共午饭、修桥阶段和修桥后首趟商队为公共。事件开启、解决和到期均使用同一范围。公共餐虽然由Lucy经营，其他NPC也会收到并可查询。

NPC收到个人需求后自主决定自己处理、交易、求助或发布委托；系统不把这个需求直接广播给其他NPC。玩家仍通过现有事务页和日志查看参与机会，这个字段只控制NPC的通知与事件查询。范围在开启时保存到事件规则快照，之后改模板只影响新事件。

默认日常邀请期为 **600 游戏分钟**（当前时钟约10现实分钟）。鲜货期限、样品有效期、商队离开、日额度结束可能更早关闭机会。NPC 实际解决会提前关闭。`cooldown_minutes` 默认1440，限制同一库存来源反复提醒；送评、商队、供餐、公共工程阶段和首趟恢复商队按来源只提醒一次。

模板启用仍需村庄配置 `village_story_enabled=true`。停用模板停止新邀请，已开启的邀请按其开启时保存的规则继续收尾。修改参数或期限不会改写已开启记录。

修桥阶段邀请模板 `BRIDGE_STAGE` 暂时停用，等待确定阶段邀请期后在Admin设置时长并启用。现有修桥事件、进度、贡献和完工判定继续工作。

## 首批模板

| 模板代码 | 真实触发事实 | 结果依据 |
| --- | --- | --- |
| LUCY_PREPARATION | Lucy个人可用原料无法制作任何现有可执行菜谱 | 至少一种真实菜谱的原料可用；不代表已经做菜 |
| BILL_PREPARATION | Bill有未出租、未留作出租的可耕作空田，但缺可用种子或耐久农具 | 实际资源或原空田安排改变；转为出租不计补货成功 |
| JACK_FRESH_STOCK | 可用猎物或肉类距离鲜期不超过600分钟 | 原批次实际处理/转出；仍留在库存且过期计邀请结束 |
| DISH_REVIEW | 有真实、未过期、仍可品鉴的Mark待评样品 | 原点评记录完成；取消或样品失效计邀请结束 |
| NORA_CARAVAN | 已实际到访、有余货且这一趟尚未进口 | Nora真实进口交易；到访前、旧交易或重放不算完成 |
| PUBLIC_LUNCH | Lucy饭店有有效公共餐，真实批次、资金和全村额度允许领取 | 实际领取；余餐按原业务仍可领取 |
| BRIDGE_STAGE | 对应公共工程存在实际活动阶段 | 原阶段完成或完工；邀请过期不取消工程 |
| BRIDGE_FIRST_CARAVAN | 修桥完成以后实际创建的首趟NORMAL商队 | 实际进口交易或本次参与机会结束；不承诺降价 |

旧商队批次没有实际创建分钟时保持空值，不推测历史到访或补写剧情。

## 支持的规则参数

| 规则 | 参数 |
| --- | --- |
| INVENTORY_SHORTAGE | `npc_id`，`item_ids`，`minimum`（默认1），可选`location_code`；任一物品可用量低于阈值触发 |
| RECIPE_PREPARATION | `npc_id`，可选`recipe_ids`、`location_code`；配方须属于现有可执行目录 |
| FARM_PREPARATION | `npc_id="10011"`，可选现有`seed_item_ids`、`tool_item_id=4001`、`location_code` |
| PERISHABLE_STOCK | `npc_id`，`item_ids`，`warning_minutes`（默认600），可选`location_code` |
| DISH_REVIEW | `npc_id="10013"`，可选`location_code` |
| CARAVAN_VISIT | `npc_id="10015"` |
| PUBLIC_MEAL | `npc_id="10014"`，可选`store_code="RESTAURANT_STORE"`、`location_code` |
| AFFAIR_STAGE | `affair_id` |
| NORMAL_CARAVAN_RETURN | `affair_id` |

例如新增库存备货机会：

```json
{"npc_id":"10014","item_ids":[1030],"minimum":2,"location_code":"RESTAURANT"}
```

这只表示原料可用量不足2件；不能写成NPC已承诺某道菜或系统自动创建订单。新增借贷、失窃调查等现有业务没有的机制，仍需扩展代码。

## 传递与恢复

统一公开对象为 `story_event`，只含标题、公共说明、来源关联、状态、参与提示及世界分钟。实时与历史使用同一白名单，私人背包、钱包和未公开动机不进入玩家事件。

状态 `OPEN` 是邀请；`RESOLVED` 表示已有实际结果；`EXPIRED` 表示本次机会结束，原业务继续。已结束邀请不会因后续贡献重新打开。事实更晚改变时，后续业务结果仍以原事务/交易记录为准。

NPC实时消息最终保留同一对象的数值时间、ID和`npc_scope`。GET_RULE/current context和INSPECT_AFFAIRS仅恢复本人的个人事件和全村公共事件。Agent已有通用接收和记忆能力，未修改其运行代码。旧记录仅补通知范围，不重写当时状态与事实；未发送的错误NPC投递以`DAILY_NPC_SCOPE_SUPPRESSED`标记结束，领取时再次校验范围，已收到的消息保持历史记录。

玩家事务页显示日常机会及工程关联邀请，实时新邀请只提示一次；结束后显示回顾。详情仍查询后端最新业务进度。

权威时间推进负责到期维护；Tick完成、NPC经济/生活/普通行动、玩家行动/施工、日结和启动恢复在真实结算后评估。只读查询不会创建事件，重复业务回执不重复开启邀请。

## 验证入口

所有后端测试使用隔离内存数据库：

```powershell
python manage.py test village.tests.test_daily_events village.tests.test_daily_event_projections village.tests.test_day_close_daily_events --settings=config.settings_player_test --noinput
```

Unity使用EditMode验证玩家事务页、事件投影、重复提示和最终NPC wire；不启动场景运行。Agent仓库只新增契约测试，验证session、perception、记忆和任务上下文。
