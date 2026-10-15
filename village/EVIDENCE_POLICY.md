# NPC 证据有效期调整（2026-09-23）

本次落实 `evidence_refs` 的细化失效方案。修改位于 Backend 与
`C:/Users/ForSF/Desktop/star-agent`；Unity 现有工具投影会保留新增元数据，
这次无需修改 Unity 生产代码，也没有数据库迁移。

## 通俗说明

过去类似“Bill 吃了饭，刚查到的水渠进度也不能引用了”。现在把背包、钱包、
位置、身体状态、当前活动、各项工程、日程、日记分别管理：相关内容变了才失效。
例如吃饭会使旧背包和饥饿状态失效，水渠进度可继续使用，直到工程发生变化或查询超时。

失效内容先从当前证据输入中移除；模型生成期间若又发生变化，发送前仍重新检查。
模型不能靠编造一个 `evidence_refs` 绕过检查。单纯退出展示缓存不会撤销本轮已经拿到、
仍然有效的引用。

## 已实现的规则

1. **状态与事件分开。** “赠送成功”只证明那次赠送发生，可以保留；“剩余 34 粒种子”
   是状态快照，库存再次变化后不可引用。事件只保留允许的完成字段，不携带永久有效的余额或位置。
2. **依赖按对象区分。** `affair:FARM_IRRIGATION` 与其他工程独立；工程列表依赖
   `affair:*`。未提供参数或缺少可用对象 ID 时使用通配符，保守失效。
3. **查询条件参与缓存键。** 工具、私有会话、参数共同确定一次观察的范围。
   完整库存与筛选库存并存；同一条件的新查询替换旧引用。输入明确保留筛选/分页条件，
   局部查询不能用于推断整个背包“没有其他东西”。
4. **先过滤，再截取窗口。** 回复证据和内部决策的当前观察都只提供有效投影。
   已入队但尚未生成的回复也会在开始生成时再过滤。执行日志和对话历史仍保留，
   用于说明以前发生过什么，不能替代当前状态。
5. **动作结果区别处理。** 正在执行或结果未知时，相关证据暂不可用；明确未执行时解除阻塞，
   不改变版本；已完成或可能发生部分执行的终态失败使相关状态失效。
   晚到回执可以解除待确认状态，但不会把旧状态刷新成“刚查询到的状态”。
6. **外部变化也生效。** 只信任配置环境发来的结构化事件，不信任玩家台词提供的失效信息。
   工程部分贡献现在也发布 `VILLAGE_AFFAIR_PROGRESS`，不必等整个阶段完成。
   相同请求重放不重复发布。失效信息仅使用对该 NPC 已可见的字段。
7. **时间与世界边界。** 状态记录运行实例、环境 ID/epoch、现实时间和世界时钟。
   换环境、重启或超时后重新查询；私有证据继续受原会话限制。
8. **错误可定位。** 引用错误会列出具体 ref，以及 `not_supplied`、`state_changed`、
   `observation_replaced`、`expired`、`action_unconfirmed` 等原因。

## 默认时效与配置

配置集中在 `village/evidence_policy.py`，作为工具说明顶层的 `evidence_policy`
发给 Agent，不是让模型填写的工具参数，也不改变动作权限。

| 观察 | 最长现实时间 | 额外规则 |
| --- | --- | --- |
| 背包、钱包 | 60 秒 | 分开投影、分别失效 |
| 附近人物 | 10 秒 | 位置或附近状态变化时立即失效 |
| 工程进度 | 30 秒 | 对应工程变化时立即失效 |
| 自身观察 | 30 秒 | 位置、身体状态、当前活动分别投影 |
| 自身身体状态、当前活动 | 同上 | 另限制最多 1 世界分钟；世界时钟未知时不提供 |
| 日程、日记 | 60 秒 | 按本人及查询日期区分 |
| 吃饭后的身体状态 | — | 最多 1 世界分钟；状态变化时立即失效 |

工程 `available_actions` 取决于 NPC 当时的体力等条件，不能作为长期可用的工程进度事实，
因此从回复证据中移除；实际执行仍使用最新查询与原有动作权限校验。

目前细化了 18 个工具：`INSPECT_INVENTORY`、`INSPECT_NEARBY`、`INSPECT_AFFAIRS`、
`OBSERVE`、`INSPECT_SCHEDULE`、`INSPECT_DIARIES`、`WRITE_SCHEDULE`、`UPDATE_SCHEDULE`、
`WRITE_DIARY`、`UPDATE_DIARY`、`EAT`、`MOVE`、`GO_HOME`、`REST`、`SLEEP`、`WAKE_UP`、
`GIVE_ITEMS`、`CONTRIBUTE_TO_AFFAIR`。未声明策略的工具和旧格式事件仍保守地使用全局失效，
不能把“没有配置”解释成“绝不会改变状态”。

## 验证与保存

修改前已提交快照：

- Kingdom：`c4a40550e`，`chore: snapshot Kingdom before evidence lifecycle changes`。
- Backend：`9a4bcbf`，`chore: snapshot backend before evidence lifecycle changes`。
- Agent 修改基线：`2310cbc`，修改前工作区干净。

验证包括：

- Agent 相关回归 113 项通过，覆盖过滤、按域失效、缓存淘汰、查询/写操作竞态、
  晚到回执、私有会话、环境/时钟变化，以及真实 TaskSystem/Broca 接线。
- Backend 相关回归 71 项通过，覆盖策略发布、部分工程贡献事件、赠送、工程和水渠流程。
  使用 `config.settings_player_test` 内存测试数据库，没有修改运行数据库。
- Backend 的全部 18 项策略可通过 Agent 策略解析校验；修改的 Agent 代码通过 Ruff。
- 没有运行 Unity 实机对话，也没有宣称全部仓库测试通过。

扩展检查发现下列 3 项既有失败，并在临时目录导出的未修改 Git 基线上复现：

- Agent `test_complete_factory_inventory_has_bound_module_and_output_contracts`：
  收件人参数描述仍比较旧的协议 ID 说明与新的准确名字说明。
- Agent `test_closing_without_speech_does_not_require_an_invented_focus`：
  测试缺少退出通知的本地发送完成，超时。
- Backend `test_two_projects_offer_actions_for_their_own_stage`：
  仍断言旧的工程动作集合。

这些问题未在本次证据修改中顺带调整。本次也没有包含文档中其余独立问题的修复，
例如新消息中断旧回复、错误事务 ID，以及离开对话的任务取消流程。

## 启用

修改已写入本地工作区。运行中的服务未在本次工作中重启。
下次运行时使用更新后的 Backend 与 Agent，并让 Unity 环境重新获取工具说明、
向 Agent 发布 specification；旧说明没有策略时会继续使用保守规则。
更新说明后可检查 `INSPECT_AFFAIRS.evidence_policy.reads` 是否为 `["affair:{affair_id}"]`。

建议实机复测：Bill 查询水渠后吃饭，仍能使用尚未超时的水渠进度；玩家随后贡献一部分劳动，
Bill 的旧水渠进度立即失效并需重查。再检查赠送成功后继续消耗库存，旧赠送仍可证明发生过，
旧余额不可继续引用。
