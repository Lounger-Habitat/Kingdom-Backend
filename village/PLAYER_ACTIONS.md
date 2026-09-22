# 真人村庄动作（P0–P2）

`POST /api/village/player/action/`（同时接受无末斜杠路径），仅接受带 `env_server` scope、`kingdom-env` audience 的服务 JWT。Env 从已验证连接填入 `GameAccount.pk` 与当前位置，客户端不能填写它们。

```json
{"player_id":"42","request_id":"UUID","action":"commission_accept","params":{"commission_id":"UUID"},"current_location_code":""}
```

每次业务尝试使用新 UUID；网络超时重试必须保留原 UUID 与 action/params。响应为 `{success,result_code,message,request_id,data}`；成功 data 为 `{result,overview}`。业务失败也保存回执；相同 ID 改参数返回 HTTP 409 `REQUEST_CONFLICT`。已保存响应保持原样，重试之后另发新 overview 请求获得当前状态。位置不参与业务参数摘要。

Env 判断玩家不在现场时可设置顶层 `replay_only:true`：只查旧回执，无回执返回 `LOCATION_REQUIRED`，不会执行业务或写失败回执。真实账号分别拥有自己的回执命名空间。

| action | params | 允许地点 |
| --- | --- | --- |
| overview | `{}` | 任意 |
| commission_accept | commission_id | 不限地点，需玩家模式 |
| commission_contribute | commission_id、condition_id、quantity；物品另带 item_id、可选 instance_id | 委托当前阶段有效地点 |
| commission_labor_start | commission_id、condition_id | 委托当前阶段有效地点 |
| commission_abandon | commission_id | 不限地点，退还托管资产并关闭委托 |
| labor_start / labor_cancel | 公共劳动开始带 affair_id、condition_id；停止无参数 | 开始遵循阶段地点；停止不限地点 |
| contribute | affair_id、condition_id、kind=`item/gold`、quantity；物品另带 item_id、可选 instance_id | 公共事务当前阶段有效地点 |
| buy_marketplace | listing_id、quantity、可选 expected_unit_price | MARKET_STREET |
| list_marketplace | instance_id(UUID)、quantity、unit_price | MARKET_STREET |
| delist_marketplace | listing_id(数值或数值字符串) | MARKET_STREET |
| buy_store | listing_id(UUID)、quantity、可选 expected_unit_price | GENERAL_STORE、RESTAURANT；还需匹配货架所属店铺 |
| collect | collect_type=`采集食材` | FOREST_EDGE |
| collect | collect_type=`采集肉类` | 已停用，返回 `REAL_HUNT_REQUIRED`；真实狩猎见 `WILDLIFE.md` |

Env 还必须校验实际坐标距离；后端只接受受信任的地点编码做第二层门控。账号必须 active 且非 AI。主包按 `(player, character_id=player.username)` 解析。

Env 在观察模式传顶层 `read_only:true`。已有请求仍先返回原回执；新写动作返回 `MODE_READ_ONLY`；overview 不刷新委托、不恢复行动力、不创建背包/玩家状态、不保存回执。新业务失败时初始化和委托刷新也随业务保存点回滚，仅保留失败回执。

现场市场成功 `data.result` 额外保留旧 API 的 `listing` / `transaction` / `bag`（适用时）。店铺购买收取展示单价，价变返回 `PRICE_CHANGED`，复用真实货架 FEFO、店铺每日/公共配给限购、饭店固定餐价和公共费用分成；玩家限购用 `PLAYER:<id>` 独立账目累计，不借用 NPC 身份。公共补贴餐资格不向玩家扩展。店铺交易写入原经济账目、货架历史和店主通知。

默认服务端配置 `VILLAGE_REQUIRE_ENV_MARKET_WRITES=true`，全部玩家的旧 HTTP 市场上架/下架/购买返回 HTTP 403 `ENV_REQUIRED`，请求参数或 header 无法绕过。仅非村庄独立部署可由运维显式设置 false 并重启后恢复旧 API。市场接入沿用已有迁移；个人委托需应用至 `0053_player_only_personal_commissions`。小范围验证：`python manage.py test village.tests.test_player_site_trade --settings=config.settings_player_test --noinput`。

Lucy 自动食材采购已由 NPC 自主发布、仅玩家承接的个人委托替换。NPC 只可发布、查询及撤回尚未被认领的自己委托；玩家独占认领、按阶段履约或放弃。`commissions` 为可接及本人委托，`my_commissions` 为本人认领。发布时预留全部报酬，最后阶段完成才转移托管交付物并发放报酬。完整契约见 `PERSONAL_COMMISSIONS.md`。

旧供货的 `claim`、`deliver`、`cancel` 动作已移除，调用返回 `INVALID_PARAMS`，也不再返回 `contracts`、`my_claims` 字段。存量认领由 `0052_migrate_legacy_supply` 迁移到个人委托后，统一使用新的委托标识和条件标识操作。旧请求与财务记录用于历史审计，不提供旧履约入口。个人委托的物品、品质、保鲜、地点由发布定义决定，不固定为食材或饭店。

采集复用现有 CollectionPoint 等级、覆盖、权重、数量和行动力成本；按现实时间恢复行动力，锁定 PlayerState 后扣点。新采集批次由服务器赋产出时间、2游戏日保鲜与 request_id，绝不延长旧批次保鲜。没有采集配置时明确返回不可用，不自动重置/填充世界。新玩家通过初始行动力采集，再交付挣钱，无需免费发金币。

修桥属于独立村庄事务，不再依赖会议决议。阶段和条件由 `affairs.py` 统一校验；修桥先在料场（`REPAIR_YARD`）交付100木材、20合格餐食和500金币，再到西桥头（`OLD_BRIDGE`）完成20份劳动，每份60游戏分钟。玩家和NPC使用相同地点规则。迁移 `0062_bridge_worksites` 更新旧事务地点并保留进度、贡献记录和完成状态；料场内未完成的施工计时停止，桥头计时保留。个人委托独立记录进度和资产，不推进公共修桥、不动用公共基金。世界事件经现有 outbox 广播。

overview 包含 `world_minute/player_id/player_name/money/action_points/action_points_max`，以及 `commissions/my_commissions/inventory/project/contributions/market/collection_points/affairs/my_labor`。inventory 是 snake_case 批次数组，仅用于面板；Unity 应刷新既有背包 API 更新真实会话库存。要求保鲜或最低剩余保鲜分钟的物品条件不接受空 expiry。市场只显示真实可交易挂单，不自动转移 Nora 店铺/商队私产。

部署前备份并应用至 `0053_player_only_personal_commissions`。前置 `0052` 将旧供货与认领迁移为个人委托并删除旧供货数据表；`0053` 限定玩家为唯一承接者，关闭潜在的 NPC 承接并退款。历史账目和已结算资产保留。验证命令：

```powershell
$env:DISABLE_BACKGROUND_SCHEDULERS = 'true'
python manage.py test village.tests.test_player_actions village.tests.test_player_commissions village.tests.test_phase5_story village.tests.test_unity_authority marketplace --settings=config.settings_player_test --noinput
python manage.py test village.tests.test_player_actions_postgresql --settings=config.settings_postgres_test --noinput
```

第一条仅使用内存 SQLite 并验证完整迁移；第二条必须连接专用隔离 PostgreSQL 测试实例，用真实多连接争用验证锁行为。不会将 SQLite 顺序测试当成部署数据库并发证明。

## 可复现的 Unity 双 WebSocket 集成测试

使用 `Assets/Scripts/Village/Tests/PlayMode/VillagePlayerNetworkE2ETests.cs` 的
`TwoAuthenticatedPlayersSettleAndReplayThroughRealEnvWebSockets`。它通过真实 Env WebSocket（18091）和 Django HTTP（18088）验证两个玩家身份、个人委托认领、非承接者拒绝、实物交付与报酬结算，以及远程重放和断线重连。旧 Lucy 自动供货不得重新生成。缺少凭证文件或测试服务未运行时跳过。

1. 在项目 `Backend` 目录准备一次全新的隔离数据：

   ```powershell
   $env:DJANGO_SETTINGS_MODULE = 'config.settings_player_e2e'
   $env:DISABLE_BACKGROUND_SCHEDULERS = 'true'
   python -m village.tests.setup_player_e2e
   ```

   脚本只允许专用 settings，固定创建项目 `Temp/village-player-e2e.sqlite3`，不会使用 `Backend/db.sqlite3`。数据库迁移后创建两名测试玩家（100/200金币，各8份合格玉米）、Lucy预算1000和world_minute=100，再通过正式发布服务创建两份单阶段物品委托，每份要求1份合格玉米，分别预留7和11金币报酬。随机测试签名密钥写入 `Temp/village-player-e2e.key`；6小时有效的服务/玩家令牌、委托标识和条件标识写入 `Temp/village-player-e2e.json`，不输出到终端。两文件均为临时凭证，不应提交。已有数据库、密钥或manifest时脚本直接拒绝，绝不重置现有fixture。

2. 保持在 `Backend` 目录，用隐藏后台进程启动服务并保存PID：

   ```powershell
   $e2ePython = (Get-Command python).Source
   $e2eProcess = Start-Process -FilePath $e2ePython -ArgumentList @('manage.py','runserver','127.0.0.1:18088','--noreload','--settings=config.settings_player_e2e') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -RedirectStandardOutput '../Temp/village-player-e2e.stdout.log' -RedirectStandardError '../Temp/village-player-e2e.stderr.log' -PassThru
   Set-Content -LiteralPath '../Temp/village-player-e2e.pid' -Value $e2eProcess.Id
   ```

   专用 settings 另有强制 `DISABLE_BACKGROUND_SCHEDULERS=True`，独立签名密钥使这些令牌无法用于真实开发服务。不应修改真实 Env 配置。

3. Unity Test Runner 切换到 PlayMode，选择 `Village.Tests.PlayMode.VillagePlayerNetworkE2ETests.TwoAuthenticatedPlayersSettleAndReplayThroughRealEnvWebSockets` 运行。测试自身临时搭建场景/地点/NavMesh/Env与两条客户端连接，结束恢复配置；断言摘要不应包含令牌。初始fixture会被真实结算消耗，重新完整复现时需准备全新fixture。

4. 测试结束停止自己刚启动的进程：

   ```powershell
   Stop-Process -Id $e2eProcess.Id
   ```

   若已经关闭原终端，先根据 `Temp/village-player-e2e.pid` 核实进程仍是本次18088测试服务，再停止对应PID。确认服务停止后，可仅删除 `Temp/village-player-e2e.sqlite3`、同名前缀的SQLite `-wal/-shm`、`.key`、`.json`、`.pid`、`.stdout.log`、`.stderr.log` 这些测试产物，再运行准备步骤。保留真实数据库和其他Temp文件。
