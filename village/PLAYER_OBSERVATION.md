# INSPECT_NEARBY 玩家观察

现有指令新增 `nearby_players`，默认观察半径 10 米，与 NPC 使用相同半径。
数据来自 Unity Env 的已认证玩家会话，按距离排序；不查询玩家账号、背包或聊天内容。
只列出已完成初始化、在线、参与村庄且服务端角色模型有效的玩家。

```json
{
  "players_observation_available": true,
  "nearby_players": [
    {
      "player_id": "example-player-id",
      "name": "村庄访客",
      "distance": 3.2,
      "within_speech_range": true,
      "activity": "SOCIALIZING",
      "movement_state": "WALKING",
      "is_in_dialogue": true
    }
  ]
}
```

示例是结构说明，不代表实际在线玩家。
`distance` 单位为米；`within_speech_range` 使用当前说话距离配置，配置未知时为 null。

| activity | 中文解释 | 判定依据 |
|---|---|---|
| SOCIALIZING | 对话中 | 服务端存在尚未结束的该玩家对话会话 |
| WORKING | 劳动中 | 服务端已确认的劳动记录，未中断且属于当前连接 |
| MOVING | 移动中 | 服务器会话中的行走或奔跑状态 |
| REVIEWING | 品鉴中 | 服务端角色动作标记为 reviewing |
| ACTING | 执行操作中 | 服务端记录 interacting 或 using_panel；不推测具体交易或购买结果 |
| IDLE | 空闲 | 移动状态为 idle，且没有上述动作 |
| UNKNOWN | 状态未知 | 无法解释动作或移动数据已过期 |

状态优先级为对话、劳动、移动、品鉴、其他操作、空闲/未知。
`movement_state` 独立返回 `IDLE`、`WALKING`、`RUNNING` 或 `UNKNOWN`；
边走边聊时可同时返回 `activity=SOCIALIZING` 和 `movement_state=WALKING`。
真人客户端的行走/奔跑上报超过 2 秒未被服务器重新确认时，移动状态变为 UNKNOWN。
静止客户端会省略重复位置包，因此已确认的 idle 不按移动上报超时处理。
真人位置来自服务器校验后保存的位置；Agent 控制玩家的位置来自服务端角色模型。

`players_observation_available=false` 表示没有实时观察来源，不能据此认定附近无人。
直接调用 Django 查询接口无法读取 Unity 玩家会话，因此返回空数组与该 false 标志；
正常 Agent 链路由 Unity Env 补充真实玩家数据。原有 `nearby_npcs` 和 `nearby_locations` 保留。

## 验证

- 后端附近查询回归通过；指令说明包含新增字段。
- Unity Runtime、EditMode 测试程序集编译通过；游戏代码使用 Unity 已编译的依赖程序集独立编译通过。
- 已添加玩家状态、距离边界、断线/观察模式过滤、公开字段及 Agent 消息透传测试；未运行 Unity 编辑器测试或真实玩家联调。
- 完整旧查询测试有 5 项失败：旧承诺、公共经济、剧情查询测试仍期待已删除的接口，以及农田查询仍期待旧字段。对应日志：`Artifacts/nearby-players-backend-tests.log`。
- 全依赖 dotnet 构建受第三方 Best.HTTP 等程序集的现有编译错误阻碍，独立游戏代码构建使用 `Library/ScriptAssemblies` 中的 Unity 依赖，不修改第三方包。
