# Agent 可调用指令

根据本地数据库中五名正式 NPC 的实际权限生成。执行仍需满足地点、资源、健康、营业状态和功能开关等条件。

系统创建村庄事务；NPC 仅查询或贡献，不创建事务、不投票、不采购公款物资、不审核事务账目或发布决议。

Mark 是外聘品鉴师，专属职责仅为查询待评菜品和评价菜品；从聘用生效日开始按后台配置每日发薪（初始为 100 金币），系统日结从公共资金自动转入个人钱包。评分不触发发薪；新制欠薪可在后续日结补发，旧制欠薪不补发。发布价格指数、审计公款、查询公共经济状态和领取公共工资四个指令已停用，已有财务历史保留。

共 54 种指令（包含动态提供的 WAKE_UP）。

| 指令 | 中文名称 | 可用 NPC | 详情 |
|---|---|---|---|
| `BROWSE_MARKETPLACE` | 浏览全服市场 | 全部 | 查看全服 Marketplace 在售挂单，可按物品筛选并限制返回数量。 |
| `BUY_FROM_MARKETPLACE` | 购买全服商品 | 全部 | 在市场街（地点代码 MARKET_STREET）购买全服 Marketplace 挂单；耗时由系统决定（单位：世界分钟），无需传参；执行结果在 duration_minutes 字段返回实际消耗。 |
| `BUY_FROM_STORE` | 商店购买 | 全部 | 其他 NPC 按上架记录价格购买商品；耗时由系统决定（单位：世界分钟），无需传参；执行结果在 duration_minutes 字段返回实际消耗。 |
| `CANCEL_DISH_REVIEW` | 取消自己提交的待评菜 | Lucy | 取消自己提交的待评菜。Lucy提交时须到RECORD_HALL，样品预留最多180游戏分钟；取消后解锁，不改变鲜期。 |
| `CANCEL_TRADE_OFFER` | 取消交易报价 | 全部 | 发起方取消待处理报价；耗时由系统决定（单位：世界分钟），无需传参；执行结果在 duration_minutes 字段返回实际消耗。 |
| `CHECK_MARKETPLACE_HISTORY` | 查看全服成交历史 | 全部 | 查看自己相关的全服 Marketplace 成交历史。 |
| `CHECK_MY_MARKETPLACE_LISTINGS` | 查看我的全服挂单 | 全部 | 查看自己当前在全服 Marketplace 的挂单。 |
| `CONTACT_CARAVAN` | 联络商队 | Nora | 在杂货铺登记补货品种，先用INSPECT_CARAVAN_REQUESTS查看完整可登记目录和目标到访时间。item_ids为完整需求集合，新登记替换该目标批次的旧需求。11:00前完成针对当天，否则针对次日。只登记品种不指定数量，品种数不得超过该路线配置的进口总量；到访时按配置总量配货。成功回执仅说明登记成功、route_open路线状态、arrival_world_time目标到访时间及当前世界时间；完整登记情况用INSPECT_CARAVAN_REQUESTS查询。登记不购买、不自动开通路线；后台变更会影响后续批次，请到访后查询库存和报价。 |
| `CONTACT_EMERGENCY_CARAVAN` | 联系应急商队 | Nora | Nora在杂货铺或市场街支付个人费用开通应急商队；已开通不重复收费。可附带登记物品需求。 |
| `CONTRIBUTE_TO_AFFAIR` | 贡献事务资源或劳动 | 全部 | 向指定事务的开放条件贡献个人物品、金币或实际劳动。kind必须匹配条件type；item需要item_id和amount，gold需要amount，labor不传amount，按条件unit_minutes完成一份劳动。系统验证资源品质、剩余需求和地点；中断劳动不计进度。 |
| `COOK` | 烹饪 | Lucy | 在餐厅（地点代码 RESTAURANT）烹饪，提交 recipe_id。配方和材料清单查 GET_RULE.cooking_recipes，本人可用材料查 INSPECT_INVENTORY。缺料会在开工前拒绝，返回 missing_items 和补齐后再重试的条件；未补齐时不要重复调用同一配方，可采购、换配方或等待补货。execution_duration_minutes 为实际耗时，planned_duration_minutes 为计划时长，单位均为世界分钟。 |
| `DELIST_FROM_MARKETPLACE` | 下架全服商品 | 全部 | 在市场街下架自己的全服 Marketplace 挂单；耗时由系统决定（单位：世界分钟），无需传参；执行结果在 duration_minutes 字段返回实际消耗。 |
| `DELIST_STORE_ITEM` | 下架商品 | Lucy、Nora | 撤下本店商品，库存留在商店未上架仓库，可重新上架；不会转入个人背包。 |
| `DEPOSIT_STORE_FUNDS` | 商店存款 | Nora | Nora在自己的杂货铺将个人可用金币存入商店账户。不收费，不产生营业收入，金额不能超过未被交易占用的个人资金。 |
| `EAT` | 进食 | 全部 | 先INSPECT_INVENTORY选择available_quantity>0、未过期且未预留的具体熟食，必须同时传source=PERSONAL_INVENTORY与food_instance_id；进食按实际配置耗时和结算需求。公共餐先BUY_FROM_STORE以0金领取到个人库存，再EAT；旧source=COMMUNITY_MEAL停用。 |
| `EXPORT_GOODS` | 外部出货 | Nora | 向在场商队出售个人可用物品。允许品种和报价取自本趟配置快照，所有品种共用本趟出口收购总量；先用INSPECT_MARKET_STATE查看export_remaining_amount和export_unit_price。出口扣个人背包、收入归个人，并保留村庄安全库存。超过剩余额度整笔失败，不部分成交。 |
| `GET_RULE` | 获取村庄规则 | 全部 | 获取当前适用于你自己的村庄环境规则（包含你的 NPC 编号、姓名、地点代码、需求数值、行动耗时和各指令前提；不同村民可执行的规则不同）。连接后、规则可能变化时或不确定时调用。 |
| `GO_HOME` | 回家 | 全部 | 返回 NPC 住所；以寻路到达为准，仅“导航超时（秒）”生效。 |
| `HARVEST` | 收获 | Bill | 在 Bill 农田收获；必须先 MOVE 到农田地点 BILL_FARM，plot_code 从 INSPECT_FARM_STATE 返回的成熟地块中选择；耗时由系统决定（单位：世界分钟），执行结果在 duration_minutes 字段返回实际消耗。 |
| `HUNT` | 狩猎 | Jack | 从HUNTING_GATE进入共享猎场，追击并击杀真实动物后获得1只对应待处理猎物；需可用猎具和健康体力。数据库时长为追猎期限，到时不自动产物。同日第二次及以后另扣配置的精力。去BUTCHER_YARD处理后才得到肉类。 |
| `IMPORT_GOODS` | 外部进货 | Nora | 每天11:00至15:00向当前商队进货，默认耗时10世界分钟，必须能在15点前完成。先查市场caravan地点及库存并移动到该处；只从现有库存中自主选择购买。仅使用NPC个人可用金币，货物进入个人背包；需要售卖时再用LIST_STORE_ITEM从个人背包自主上架。 商品范围和价格使用本趟到访快照；后台配置修改不影响本趟现货和成交价。 |
| `INSPECT_AFFAIRS` | 查询村庄事务 | 全部 | 查询系统创建的村庄事务；省略affair_id列出事务，指定编号查看阶段、条件、进度和本人贡献。查询不创建事务。 |
| `INSPECT_CARAVAN_REQUESTS` | 查看商队补货需求 | Nora | 查看完整可登记进口目录requestable_items及已登记标记requested=true，不分页、不随机抽取。arrival_world_time是当前可登记的目标到访时间：11:00前当天，否则次日；import_total_amount是当前配置下目标路线的进口总数量。route_open和arrival_conditional说明到访条件。目录来自后台配置，不代表当前现货；当前买卖用INSPECT_MARKET_STATE，提交或替换整份需求用CONTACT_CARAVAN。 |
| `INSPECT_FARM_STATE` | 查看农田状态 | Bill | 查看农田状态。省略 plot_code 返回全部36块；指定 plot_code 时只返回该地块。空地只返回 EMPTY；生长中返回作物和剩余成熟时间；成熟地块返回作物及可收获状态。空值或非法编号返回 INVALID_QUERY_PARAMS。 |
| `INSPECT_INVENTORY` | 查看背包 | 全部 | 查看自己的金币和物品。可选item_ids仅查指定物品，此时不附带钱包和店铺信息。工具查询包含范围内的tool_summaries，可用总次数不等于某一把工具的剩余次数。每件物品返回编号、名称、类型、数量、可用数量及出售信息；工具有实例和耐久，食物有food_instance_id及可读有效期。sale.unit_price为向sale.target出售每件可获得的金币；sale.allowed=false时reason说明不可出售原因。 |
| `INSPECT_LIVING_OPTIONS` | 查看生活资源 | 全部 | 远程查询成品食物和公共餐的商店、地点、营业状态、价格与购买限制，以及自己的休息和睡眠地点。只返回公开资源摘要和本人资格，不返回他人私产或商品批次；详细库存和listing_id仍须到店INSPECT_STORE_INVENTORY。available表示满足当前资源条件，执行仍需到场并复核。 |
| `INSPECT_MARKET_STATE` | 查看商队交易状态 | Nora | 查看商队状态、地点、到访时间、本趟进口余货和进出口报价。caravan的import_total_amount/import_remaining_amount表示进口初始总量/剩余总量，export_total_amount/export_remaining_amount表示本趟跨品种共享的收购上限/剩余额度。items含当趟进口现货或收购报价；export_unit_price不代表还有收购额度，须同时检查export_remaining_amount。不按个人余额或库存筛选。item_id仅筛选当前交易商品；page/page_size仅分页items，默认20、最大50；item_found仅表示在当前交易目录中找到商品。补货目录及登记情况请用INSPECT_CARAVAN_REQUESTS。 |
| `INSPECT_NEARBY` | 观察附近 | 全部 | 查看自身周围10米内的NPC、在线参与村庄的玩家和地点。nearby_players返回player_id、name、distance、within_speech_range、activity、movement_state、is_in_dialogue。activity可为IDLE空闲、MOVING移动、SOCIALIZING对话、WORKING劳动、REVIEWING品鉴、ACTING交互或UNKNOWN未知；movement_state区分WALKING行走和RUNNING奔跑。对话状态优先，移动状态单独保留；不返回聊天内容和私人资产。players_observation_available=false表示实时玩家观察不可用，不代表附近无人。 |
| `INSPECT_PERSONAL_COMMISSIONS` | 查询个人委托 | 全部 | 查询自己发布的个人委托、玩家承接进度、报酬和期限；scope=all仅浏览可见委托，NPC不能认领或完成个人委托。 |
| `INSPECT_RATEABLE_DISHES` | 查看待评分菜品 | Mark | 默认读取仍有效的待评请求；空列表只表示当前没有待评，不代表历史请求未完成或已过期。可传review_id查询该请求的最终状态，已完成时返回原评分和价格。仅返回本人可见的请求。 |
| `INSPECT_RESTAURANT_STATE` | 查看饭店状态 | Lucy | 查询餐厅营业状态和在售菜单。经营者另可查看餐厅店款、未上架可用库存、待评样品预留和公共餐补贴、实际公款余额、每日共享份额及供应阻碍。菜谱与材料清单请查GET_RULE.cooking_recipes，个人可用食材查INSPECT_INVENTORY。只读查询不批准或预留公款。 |
| `INSPECT_SOCIAL_STATE` | 查看社交状态 | 全部 | 查看关系、承诺及当前可用的社交信息。 |
| `INSPECT_STORE_INVENTORY` | 查看商店库存 | 全部 | 查看指定商店库存，调用前必须先移动到商店所在地点。Nora杂货铺使用GENERAL_STORE，地点为GENERAL_STORE；Lucy饭店使用RESTAURANT_STORE，地点为RESTAURANT。只读查询，不进入经济结算。 |
| `INSPECT_TRADE_OFFERS` | 查看交易报价 | 全部 | 查看自己收到和发出的待处理交易报价。 |
| `LIST_STORE_ITEM` | 上架商品 | Lucy、Nora | 经营者将真实批次上架本店。Lucy饭店必须指定food_instance_id，price_mode=ORIGINAL，普通菜后端按品质默认价值/有效点评价定价，不提交custom_unit_price；公共餐只需另传public_meal_code，无需授权；NPC和玩家0金领取，每人每日1份、全村共享10份。Nora杂货铺仍可CUSTOM自由定价。stock_source=STORE从本店未上架库存取货；下架只停售留店。 |
| `MOVE` | 移动 | 全部 | 前往指定地点；以寻路到达为准，仅“导航超时（秒）”生效。location_code 必须是地点代码（如 BILL_FARM、GENERAL_STORE、BILL_HOME、MARKET_STREET），农田 plot_code 不能作为 location_code。 |
| `OBSERVE` | 观察自身状态 | 全部 | 查看当前时间、地点、动作和生活需求，返回你的 self_npc_id 与 self_name；只返回当前动态状态，规则请使用 GET_RULE。 |
| `PLANT` | 播种 | Bill | 在 Bill 农田播种；必须先 MOVE 到农田地点 BILL_FARM，plot_code 从 INSPECT_FARM_STATE 返回的空地中选择；耗时由系统决定（单位：世界分钟），执行结果在 duration_minutes 字段返回实际消耗。 |
| `PROCESS_GAME` | 处理猎物 | Jack | 在BUTCHER_YARD处理库存查询中的指定猎物实例，每次1件，消耗1次猎具，产出肉和羽毛或粗皮革；肉继承猎物到期时间。时长仅由数据库决定。 |
| `PROPOSE_TRADE` | 交易报价或向商店售货 | 全部 | 向杂货铺卖货：指定side=SELL、store_code=GENERAL_STORE、items物品列表，可一次卖多种物品；无需target_npc_id、报价金额或期限，无需Nora回应或在场。先查INSPECT_INVENTORY：sale.unit_price为每件实际售价；sale.allowed=false表示当前不可出售，reason说明原因。工具只收满耐久批次。商店营业且全部钱货充足才整批成交；任一物品不足整批失败，店款不足提示商店金币不足，无法收购。重复item_id合并数量；兼容单品item_id+amount，但不可与items混填。个人交易及Nora主动商店BUY仍是单品报价，必填target_npc_id、item_id、amount、gold_amount，由对方回应。耗时由系统决定，结果返回duration_minutes。 |
| `PUBLISH_PERSONAL_COMMISSION` | 发布个人委托 | 全部 | 根据自己的实际需求向玩家发布委托，预留全部金币报酬。阶段按序完成，每阶段条件全部满足才开放下一阶段；发布后不可改价或改条件。NPC不承接个人委托。 |
| `RATE_DISH` | 菜品评分 | Mark | Mark在RECORD_HALL对Lucy送来的具体菜使用本人LLM品鉴。先INSPECT_RATEABLE_DISHES读取真实快照，再提交review_id、四项1–10分和评语。后台计算总分及点评价；不再接受旧单个1–5分。 |
| `REQUEST_DISH_REVIEW` | 提交具体菜品给Mark品鉴 | Lucy | 提交具体菜品给Mark品鉴。Lucy提交时须到RECORD_HALL，样品预留最多180游戏分钟；取消后解锁，不改变鲜期。 |
| `RESPOND_TRADE` | 响应交易报价 | 全部 | 接受或拒绝交易报价；耗时由系统决定（单位：世界分钟），无需传参；执行结果在 duration_minutes 字段返回实际消耗。 |
| `REST` | 休息 | 全部 | 在家或餐厅休息；可指定正整数世界分钟时长，省略时使用后端配置。 |
| `SELL_ON_MARKETPLACE` | 上架全服商品 | 全部 | 在市场街将个人库存上架到全服 Marketplace；耗时由系统决定（单位：世界分钟），无需传参；执行结果在 duration_minutes 字段返回实际消耗。 |
| `SET_STORE_LIMITS` | 设置商店限购 | Nora | 设置每人每天购买上限及到期时间；daily_limit=0取消本店规则，不影响公共限购。到期时间适用于本店当前整组规则。 |
| `SLEEP` | 睡眠 | 全部 | 在家睡眠；可指定正整数世界分钟时长，省略时使用后端配置。 |
| `UPDATE_STORE_PRICE` | 修改商品价格 | Nora | Nora可修改本人杂货铺普通上架价格。Lucy饭店价格由后端固定，不能使用此动作任意改价。 |
| `WITHDRAW_PERSONAL_COMMISSION` | 撤回个人委托 | 全部 | 仅可撤回自己发布且尚未认领的委托，退回预留报酬。 |
| `WITHDRAW_STORE_FUNDS` | 商店提款 | Lucy、Nora | 经营者在本人商店提取可用金币到个人钱包。Lucy在RESTAURANT提RESTAURANT_STORE、储备0；Nora在GENERAL_STORE按现有储备提款。 |
| `WITHDRAW_STORE_ITEM` | 商店取货 | Nora | Nora在自己的杂货铺将未上架、未占用、未过期的库存取到个人背包。已上架商品先下架；保留批次属性，之后可自行到商队出口，收入归个人。 |
| `WAKE_UP` | 提前醒来 | 全部 | 结束自己的当前睡眠，只结算实际已睡时间；由睡眠能力动态提供。 |

## 系统配置

事务通过现有管理后台创建；贡献要求显式 affair_id、condition_id 和 kind。

公共餐沿用 LIST_STORE_ITEM / BUY_FROM_STORE，传 public_meal_code 即可，无需授权或 decision_id。玩家通过店铺领取（buy_store）。NPC 和玩家每个游戏日各限一份，三种菜单共享全村每日十份额度。
Lucy 制作真实成品后上架；领取时公共资金按 C1/C2/R1 支付 9/12/9 金到饭店，余额不足整笔失败。领取后食物进入个人背包，吃饭不重复补贴；旧授权和历史账目只保留存档。

旧会议、投票、剧情操作、占位工作与订单入口已关闭；CREATE_COMMITMENT、CANCEL_COMMITMENT、PERFORM_COMMITMENT 已停用；历史记录保留。聊天通过现有发言协议进行，不再使用 SOCIALIZE。

验证：本次针对性后端回归测试通过；Unity 运行时及 EditMode 测试程序集编译通过，未执行 Unity 编辑器内测试。
