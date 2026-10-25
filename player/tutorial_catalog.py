"""Versioned personal teaching routes. Stable codes preserve progress across edits."""
from .progression_rules import SKILLS

VERSION = 5
ROUTE_NAMES = dict(basics="入村第一天", gathering="采集与工具", farming="种植生活",
    forestry="林地伐木", hunting="狩猎与加工", cooking="烹饪与品鉴", business="摆摊经营")
SKILL_NAMES = dict(SKILLS)
SKILL_ACTIONS = dict(gathering="采摘植物", farming="收获自己种植的作物", forestry="砍倒树木",
    hunting="击败猎物", cooking="制作料理", business="出售商品或完成供货")
# Client acknowledgements are restricted to learning actions and carry no rewards.
HINTS = frozenset(("movement", "camera", "bag", "item", "growth", "recipe", "review",
    "business", "farm_time", "lease", "equipment", "training", "training_block", "training_dodge", "cooking_preview"))


def task(code, title, description, condition, location="", panel="", goal=""):
    return dict(code=code, title=title, description=description, condition=condition,
        location_code=location, panel=panel, legacy_goal_code=goal)


ROUTES = {
    "basics": [
        task("basics_move", "学会移动和调整视角", "按 WASD 向一个方向走约 3 米。\n再拖动鼠标转动视角，并滚动滚轮拉近或拉远镜头。", ("hints", "movement", "camera")),
        task("basics_collect", "采摘一株植物", "前往森林边缘，靠近并面向可采摘的植物，按 E。\n站在原地等采摘结束，物品放入背包后即完成。", ("event", "collect", 1), "FOREST_EDGE", goal="first_gathering"),
        task("basics_bag", "打开背包查看收获", "点击下方“打开背包”，看看刚采到的物品。\n打开背包即完成；选中物品还能查看数量、品质和用途。", ("hints", "bag"), panel="bag"),
        task("basics_income", "完成一次出售或供货", "前往诺拉杂货店，在“出售”页选择物品和数量，确认出售。\n也可完成一单供货；获得这笔金币后即完成。", ("event", "income", 1), "GENERAL_STORE", goal="first_supply"),
        task("basics_growth", "打开成长查看技能", "点击下方“打开成长”，查看技能等级和经验。\n打开成长即完成。以后可在这里查看升级要求。", ("hints", "growth"), panel="growth"),
        task("basics_route", "选择并追踪生活路线", "在左侧选一条生活路线，再点击“追踪这条路线”。\n先从感兴趣的一条开始，之后可以随时切换。", ("event", "route", 1), panel="routes"),
    ],
    "gathering": [
        task("gathering_first", "再采摘一株植物", "前往森林边缘，靠近并面向植物，按 E 并等采摘结束。\n本步骤需要新采摘一次，入村教学中的采摘另行记录。", ("event", "collect", 1), "FOREST_EDGE", goal="first_gathering"),
        task("gathering_level2", "升到 2 级，解锁采集工具", "采集工具需要采集 2 级。继续采摘植物，获得经验。\n达到 2 级后即可学习使用采集包；后续升级可自由安排。", ("level", "gathering", 2), "FOREST_EDGE", "growth", "gathering_level_2"),
        task("gathering_tool", "准备一个采集工具包", "前往诺拉杂货店，在“购买”页购买基础采集工具包。\n背包里已有可用采集包时，可直接继续。", ("tool", "gathering"), "GENERAL_STORE", "bag", "first_gathering_tool"),
        task("gathering_equipped", "装备采集包再采摘一次", "打开“背包 → 装备”，选中采集工具包，点击“装备”。\n关闭背包，到植物旁按 E；带着采集包成功采摘一次即完成。", ("event", "tool_collect", 1), "FOREST_EDGE", "bag", "first_targeted_gathering"),
        task("gathering_recipe", "查看采集食材的配方", "点击下方“打开配方”，看看采到的食材能做什么料理。\n查看配方页即完成这一步。", ("hints", "recipe"), panel="recipes"),
        task("gathering_use", "出售或烹饪一次", "前往诺拉杂货店，在“出售”页卖出一批物品。\n也可在灶台完成一次烹饪，或完成一单供货。任选一种即可。", ("any_event", "cook", "income"), "GENERAL_STORE"),
    ],
    "farming": [
        task("farming_lease", "租下一块空闲田地", "前往田地，选择一块可租的空田，设置租赁天数。\n核对租金后确认租赁，租下田地即完成。", ("event", "lease", 1), "BILL_FARM"),
        task("farming_prepare", "备齐种子和农具", "前往诺拉杂货店，购买想种的作物种子和可用农具。\n把种子和农具放在主背包中，两样备齐即可。", ("farm_ready",), "GENERAL_STORE", "farm"),
        task("farming_plant", "在自己的田里播种", "走到租下的空田旁，点击“选择种子”，选好种子和农具。\n确认播种，田地开始生长后即完成。", ("event", "plant", 1), "BILL_FARM"),
        task("farming_wait", "查看作物什么时候成熟", "打开农田界面，选择自己刚播种的田地，查看成熟时间。\n看过时间即完成这一步；等成熟时可以先做其他任务。", ("hints", "farm_time"), "BILL_FARM", "farm"),
        task("farming_harvest", "收获一批成熟作物", "等自己的田地显示“可收获”，再走到田边。\n选择可用农具并点击“收获”，作物放入背包后即完成。", ("event", "harvest", 1), "BILL_FARM"),
        task("farming_next", "为田地办理一次续租", "选择自己租下的田地，设置续租天数并核对费用。\n确认续租成功即完成，以后可按需要安排种植。", ("event", "renew", 1), "BILL_FARM"),
    ],
    "forestry": [
        task("forestry_equipment", "装备斧头并试挥一次", "打开“背包 → 装备”，选中初始斧头并点击“装备”。\n关闭背包，拿出斧头后点击鼠标左键，试挥一次。", ("event", "axe", 1), panel="bag"),
        task("forestry_first", "砍倒一棵树获得木材", "前往西侧伐木林，拿着斧头靠近并面向普通树木。\n点击鼠标左键砍树，直到树倒下、木材放入背包。", ("event", "wood", 1), "FOREST_LOGGING"),
        task("forestry_maintenance", "查看斧头耐久和维修", "打开“背包 → 装备”，选中斧头，查看耐久和“维修”页。\n这一步只需查看；以后耐久不足时，可按这里的材料要求维修。", ("hints", "equipment"), panel="bag"),
    ],
    "hunting": [
        task("hunting_equipment", "装备剑并试挥一次", "打开“背包 → 装备”，选中初始剑并点击“装备”。\n关闭背包，拿出剑后点击鼠标左键，试挥一次。", ("event", "weapon", 1), panel="bag"),
        task("hunting_training", "命中训练木桩一次", "定位训练木桩，靠近并面向木桩，按 T 开始练习。\n用剑左键攻击，看到“命中训练木桩”才完成；空挥不计。", ("hints", "training"), "COMBAT_TRAINING", "training"),
        task("hunting_defense", "成功格挡并闪避木桩攻击", "面向木桩，站在 2 米内按 T 开始练习。\n蓄力时按住 G 格挡；下一次预警时按 R 翻滚，各成功一次。", ("hints", "training_block", "training_dodge"), "COMBAT_TRAINING", "training"),
        task("hunting_first", "击败一只猎物", "结束木桩训练，拿着剑前往猎场寻找猎物。\n靠近并面向猎物，用鼠标左键攻击，击败一只后即完成。", ("event", "hunt", 1), "HUNTING_GATE"),
        task("hunting_process", "加工猎物并领取产物", "带猎物前往 Jack 屠宰棚，选择猎物并确认加工费用。\n等待加工完成，再领取产物。领到产物后即完成。", ("event", "process", 1), "BUTCHER_YARD"),
        task("hunting_use", "出售产物或完成烹饪", "前往诺拉杂货店，在“出售”页卖出一批物品。\n也可在灶台完成一次烹饪，或完成一单供货。任选一种即可。", ("any_event", "cook", "income"), "GENERAL_STORE"),
    ],
    "cooking": [
        task("cooking_recipe", "查看一道料理的配方", "点击下方“打开配方”，查看已知料理需要的食材和炊具。\n打开配方页即完成，先选一道材料容易准备的料理。", ("hints", "recipe"), panel="recipes"),
        task("cooking_prepare", "在灶台选好食材和炊具", "带上食材和炊具，前往 Lucy 饭店灶台。\n在烹饪界面选择食材和炊具，核对结果显示可以制作后即完成。", ("hints", "cooking_preview"), "RESTAURANT_KITCHEN"),
        task("cooking_first", "完成一次烹饪", "在 Lucy 饭店灶台选好食材和炊具，确认用料后开始烹饪。\n完成制作、获得料理后即完成这一步。", ("event", "cook", 1), "RESTAURANT_KITCHEN"),
        task("cooking_review", "送一份料理给 Mark 点评", "带一份料理前往议事厅，选择料理并提交送评。\n等 Mark 完成点评后，这一步才完成；等待时可以做其他事。", ("event", "review", 1), "RECORD_HALL"),
        task("cooking_read", "查看完成的料理点评", "点击下方“查看点评记录”，找到刚完成的点评。\n查看评语、料理品质和结果，了解下一次可以怎样改进。", ("hints", "review"), "RECORD_HALL", "reviews"),
    ],
    "business": [
        task("business_stock", "查看摊位和可售料理", "带着料理前往集市摊位，打开“货架管理”。\n查看背包里哪些料理可以上架，以及各自的售价。", ("hints", "business"), "PLAYER_STALL", "business"),
        task("business_open", "开办自己的摊位", "在集市摊位点击“开办基础摊位”，按界面要求办理。\n开办后才能上架料理；已经开办的摊位可直接继续。", ("stall_open",), "PLAYER_STALL"),
        task("business_list", "上架一批料理", "在摊位的“货架管理”页，选中背包里的可售料理。\n确认数量和售价后点击“上架”，货物出现在货架上即完成。", ("event", "list", 1), "PLAYER_STALL"),
        task("business_sale", "等顾客买走一份商品", "让摊位保持营业，并确认货架上有可售料理。\n等顾客完成一笔购买后即完成，等待期间可以去做其他事。", ("event", "sale", 1), "PLAYER_STALL"),
        task("business_accounts", "查看摊位营业记录", "打开集市摊位，切换到“营业记录”。\n查看刚才卖出了多少料理、获得多少收入，以及已记录的成本。", ("hints", "business"), "PLAYER_STALL", "business"),
        task("business_restock", "学会下架一批商品", "在“货架管理”页，选中货架上的一批商品并下架。\n商品退回背包后即完成，以后可自行补货或调整货架。", ("event", "withdraw", 1), "PLAYER_STALL"),
    ],
}
TASKS = {row["code"]: dict(row, route_code=route, index=index + 1)
    for route, rows in ROUTES.items() for index, row in enumerate(rows)}
