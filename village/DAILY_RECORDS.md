# NPC 每日日程与日记

2026-09-20：后端、Unity Env 六条指令和 Unity 客户端阅读 UI 已实现。客户端按确认的侧栏设计接入。

2026-09-21：取消日程、日记的调用版本号和版本冲突校验。修改只提交日期和完整正文，按保存顺序覆盖；工具定义、返回数据和 Unity 参数错误提示同步更新。

每个 NPC 每游戏日一份 SCHEDULE、一篇 DIARY，存入 NpcDailyRecord。正文是自由文本，最多 4000 字符。读写记录不修改身体需求、不推进世界时间、不进入身体行动队列，也不会自动执行日程。

## Agent 指令

| 指令 | 参数 |
|---|---|
| WRITE_SCHEDULE / WRITE_DIARY | content；可选 game_date，默认首次受理时的游戏日期 |
| INSPECT_SCHEDULE / INSPECT_DIARIES | 默认当天全文；可选 game_date；或 view=list、before_date、limit（默认 10，最大 50） |
| UPDATE_SCHEDULE / UPDATE_DIARY | game_date、content（完整正文） |

game_date 为 YYYY-MM-DD；日期基于权威 world_minute 的公历投影。日记不能写未来。列表按日期倒序返回短摘要和 next_before_date；detail 返回 record 或 null（尚未记录）。

正文、日期、创建及修改世界时间放在 record 中，接口不再返回版本号。同日重复创建仍拒绝并返回当前记录；更新不存在的记录仍拒绝。每次更新完整替换正文，补充时应先读取、合并再提交；多个不同请求依次更新同一记录时，后保存的正文生效。数据库原有 version 字段保留为内部计数，不参与调用或更新校验，无需数据库迁移。

作者来自绑定的 sender；不能指定别的作者。现有 profile capabilities 继续控制权限。迁移 0067 为五名正式 NPC 添加六项能力，seed_data 同步包含这些能力。实际指令介绍和参数说明在 command_catalog.json，包含 daily schedule 的安排依据以及日记的事实记录、回顾和修正规则。

## API 与回执

- Agent 写入：POST /api/village/runtime/daily-record，使用 EnvServerJWTAuthentication，body 为现有规范化 envelope。
- Agent 查询：GET /api/village/npcs/{npc_id}/query/{INSPECT_SCHEDULE|INSPECT_DIARIES}，仍走受鉴权、受能力限制的现有查询接口。
- 客户端只读：GET /api/village/public/npcs/{npc_id}/daily-records/{schedule|diary}，使用玩家 JWT，沿用 presentation 开关；不提供客户端写方法。

写入沿用 request_id 幂等，摘要只包含作者、动作名和原始参数。时间戳或运行状态版本变化不影响同一请求重放；省略日期的请求跨午夜重试仍返回原日期。相同 request_id 换内容返回 DAILY_RECORD_REQUEST_CONFLICT，修改内容应使用新的请求 ID。较早请求重试只返回该请求原来的结果，不重新写入，也不覆盖后续更新。

文本与回执同事务保存。Unity 不再为这些写操作追加通用指令回执，避免网络不确定时的失败回执占用原请求 ID。查询和写入返回的记录全文会透传给 Agent。

世界重置命令已包含 NpcDailyRecord 的清理，但此次未运行重置。0067 已应用到本地数据库；未重启或操作正在运行的游戏。其他部署需执行迁移并重新加载代码、刷新 Agent 工具发现。

本次取消版本号后，重启后端和 Unity 服务端，再让 Agent 重连并刷新工具定义。已有记录继续使用；旧请求的已保存回执保留原样。

## Unity 客户端查看

进入「观察 NPC」，选择村民，在身份卡下切换「状态 / 日程 / 日记」。原有状态模块保留；日程和日记提供游戏日期前后切换、返回今天、长文滚动、更新时间和手动刷新。日期来自权威观察数据及只读响应，不使用电脑日期。两页的查看日期均限制在 2026-03-08（游戏开始日）至当前游戏日之间；边界箭头置灰，点击逻辑也拒绝越界。游戏日期尚未加载时，两个箭头均不可用。

`NpcObservationPanel.DailyRecords.cs` 复用现有 uGUI 侧栏。通过 `VillagePresentationStore.RefreshNpcDailyRecord` 使用玩家 JWT 查询，不依赖聊天会话。只在阅读页打开时每 15 秒刷新当前 NPC 当前日期；切换 NPC、日期、页签或关闭面板会使旧响应失效，断线/会话重置清除旧正文。当天页跨游戏午夜跟随到新一天，历史页保持选中日期。

无记录显示空状态；请求失败单独提示并提供重试，刷新失败保留上次正文。NPC 正文以普通文本呈现，不解释 TMP 富文本标记或转义控制字符。客户端没有写入或修改入口。预览使用临时示例文字，未给 NPC 创建记录。

## 有限验证

- 2026-09-21 取消版本号：原有 5 项后端定向测试全部通过，覆盖仅日期和正文更新、连续更新以后保存内容为准、较早请求重试不覆盖后续正文、返回数据无版本号、跨日重试、身份与缺失记录校验、实际工具参数定义。使用隔离内存数据库。Unity Editor 实际编译通过，0 错误；未运行全量测试或游戏内端到端测试。
- 5 项 TestCase：读写修改与身体行动隔离、跨午夜重试、重复/非法输入与身份隔离、分页和实际指令介绍、客户端只读与 Env 鉴权配置。使用隔离内存数据库，全部通过。
- Unity Runtime 使用项目实际引用独立编译：0 错误，30 个警告。
- 2026-09-20 历史回执检查通过；其中版本返回和版本冲突要求已由 2026-09-21 的无版本号接口替代。
- 本地检查：0067 已应用，五名正式 NPC 均拥有六条指令。
- 客户端：Unity Editor 实际编译通过（0 错误）；使用真实 prefab 在独立预览场景检查状态、日程、日记布局，4000 字正文可滚动，普通文本显示已确认。未保存或修改当前场景。
- 日期边界修正：Unity Editor 编译通过；一次独立场景定向检查覆盖两页的首日、历史日期、当天、日期未加载、越界点击拦截及旧越界选择恢复，全部通过。未发起网络请求或推进游戏。
- 未执行全套测试、真实 Hub 联调或登录游戏端到端操作；客户端接入本轮没有重复跑后端测试。

验证命令：

```powershell
python manage.py test village.tests.test_daily_records --settings=config.settings_player_test --noinput --verbosity 1
```
