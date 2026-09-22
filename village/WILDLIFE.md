# 真实狩猎接口

仅接受 `env_server` scope、`kingdom-env` audience 的服务 JWT。客户端身份、参与模式、实时位置、冷却、方向、遮挡和伤害均由 Env 校验。后端只接受 Env 的真实死亡事实；客户端不提供物品、数量或奖励。玩家狩猎全程不读取、恢复、检查、预留、扣除或退还行动力。

`POST /api/village/wildlife/action/` 使用顶层 JSON 字段，无 `params` 包装：

| action | 公共字段以外的参数 |
| --- | --- |
| register | species=`CHICKEN/PIG/CATTLE`，region_id，position=`{x,y,z}` |
| begin | attempt_id，actor_type=`NPC/PLAYER`，actor_id；仅 NPC 必填 command_id |
| complete | attempt_id；可选 claim_version、death_world_minute（默认当前世界分钟） |
| cancel | attempt_id；可选 claim_version |

公共字段为 `action`、`request_id`（UUID）、`prey_id`（UUID）。每个动作使用独立 request_id，重试原样保存整个 JSON 和原 request_id；相同 ID 改 payload 返回 HTTP 409。业务失败也有稳定回执；通信异常使用同一请求重试。成功响应：

```json
{"success":true,"result_code":"OK","request_id":"UUID","data":{"prey":{"prey_id":"UUID","species":"PIG","region_id":"HUNTING_GROUNDS","position":{"x":0,"y":0,"z":0},"status":"CLAIMED_REWARD","claim_version":1,"attempt_id":"UUID","born_world_minute":700,"death_world_minute":702},"attempt":{"attempt_id":"UUID","prey_id":"UUID","actor_type":"PLAYER","actor_id":"42","command_id":null,"claim_version":1,"status":"SUCCEEDED","started_world_minute":700},"items_added":[{"item_id":29102,"item_name":"猪（待处理猎物）","amount":1,"instance_id":"UUID","produced_world_minute":702,"expires_world_minute":3582}]}}
```

失败为 `{success:false,result_code,request_id,data:{}}`。常见错误为 `PREY_BUSY`、`ACTOR_BUSY`、`ATTEMPT_NOT_ACTIVE`、`STALE_CLAIM`、`PREY_NOT_ALIVE`、`HUNT_TIMEOUT`、`INVALID_DEATH_TIME`。register 只返回 prey；begin/cancel 返回 prey 和 attempt；NPC complete 额外返回 `data.economy_result`（现有正式经济结算响应）。

`GET /api/village/wildlife/bootstrap` 返回 `{success:true,result_code:"OK",data:{world_minute,time_revision,prey:[...],attempts:[...],settled_attempts:[...]}}`。prey 包含所有生命期及已领取墓碑，attempts 仅 ACTIVE；settled_attempts 为 NPC 已成功 attempt 的完整 result（含 economy_result），供本地旧快照恢复。当前返回所有墓碑和成功结果，长期增长需后续分页/归档。状态含义：ALIVE=活且无归属，CLAIMED=活且有归属，CLAIMED_REWARD=已死且已入包。DEAD 仅是结算事务内的临时状态，不会通过快照暴露。后端未逐刀保存 HP；Env 重启后未完成活体可满血恢复，但先重放已持久化的死亡待提交请求，不能将它们回血或取消。已领取生命期不复活，新刷新须新 UUID。

同一个 actor 只允许一个 ACTIVE attempt，每只动物只允许一个 ACTIVE attempt 和一个 SUCCEEDED 结果；竞争、取消后迟到回调、换 request_id 重领均被阻止。取消释放归属且不产生物品；不自动终止 Jack 的 HUNT，以支持时限内换目标。正式 HUNT 被取消或身体条件中断时，会同时释放对应动物。

Jack 使用原 HUNT 命令的后端 command_id，后端自行读取原经济 request_id/message_id。begin 只绑定正式 RUNNING 命令，不重复扣开工精力。complete 使用真实物种，消耗一次预留猎具，沿用原经济事务、事件、Outbox、库存实例和加工链。原 CompleteEconomyAction 可重放同命令，观察时间和地点不同不冲突。没有已确认死亡时，普通计时结算返回 `HUNT_KILL_REQUIRED`；旧 HUNT_CHICKEN 也不再产物。

数据库 HUNT duration 在接收命令时冻结，语义改为追猎期限。实际死亡必须在 begin 之后、权威当前时间之前、命令开始+duration 之前（均包含边界），网络晚到不会仅因到达时已过期而失败。Env 应在死亡请求未知时避免取消该 NPC。`POST /api/village/runtime/tick` 可带 `deferred_hunt_command_ids:[command UUID]`：后端仍推进权威时钟与其他 NPC，只对拥有 ACTIVE 猎物尝试的对应 RUNNING HUNT 暂缓工作和基础 needs，响应对应 `npc_results` 带 `deferred_hunt:true`。等待时钟达到死亡分钟后提交 complete，再恢复普通 tick，避免时钟同步死锁或身体提前中断。该列表属于 tick payload 幂等摘要，重试同 request_id 必须原样冻结列表及整个 payload。恢复时先重放 pending complete，再发送取消，以免把已确认终止的命令覆盖成成功。新鲜期为实际死亡+2880世界分钟；加工与市场转移继续保留该鲜期。

增量迁移 `0054_real_wildlife_hunting` 创建三个表，并仅把旧 HUNT 时长5改为120分钟；自定义其他值保留，缺失配置仍报错。不重置库存、角色或真实数据库。村庄 `collect(采集肉类)` 和村庄经济开启时的旧 inventory collect 肉类入口均关闭；植物采集保持原规则。

隔离测试：`python manage.py test village.tests.test_wildlife village.tests.test_jack_production village.tests.test_jack_cycle village.tests.test_jack_supply village.tests.test_phase3_production village.tests.test_player_actions --settings=config.settings_player_test --noinput`。
