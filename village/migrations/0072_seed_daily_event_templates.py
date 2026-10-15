from django.db import migrations


TEMPLATES = [{'code': 'LUCY_PREPARATION',
  'title': '饭店下一轮备料',
  'rule_type': 'RECIPE_PREPARATION',
  'parameters': {'npc_id': '10014', 'location_code': 'RESTAURANT'},
  'summary': 'Lucy的现有原料暂时无法制作已配置的菜谱，下一轮备料出现了缺口。',
  'participation_hint': '可以与Lucy商量备料、通过已有交易补货，或查看她自主发布的委托。',
  'resolved_summary': 'Lucy已有至少一种菜谱的可用原料；是否制作、出售或供餐由她决定。'},
 {'code': 'BILL_PREPARATION',
  'title': '下一轮农活准备',
  'rule_type': 'FARM_PREPARATION',
  'parameters': {'npc_id': '10011', 'location_code': 'BILL_FARM'},
  'summary': 'Bill有可自行耕作的空田，但当前缺少可用种子或农具。',
  'participation_hint': '可以与Bill交易种子或农具，或查看他自主发布的备货委托。',
  'resolved_summary': '原农活准备条件已改变：资源可用或空田已有新的安排。'},
 {'code': 'JACK_FRESH_STOCK',
  'title': '鲜货要及时处理',
  'rule_type': 'PERISHABLE_STOCK',
  'parameters': {'npc_id': '10012',
                 'location_code': 'BUTCHER_YARD',
                 'warning_minutes': 600,
                 'item_ids': [29101, 29102, 29103, 1009, 1034, 1056, 1057, 1077]},
  'summary': 'Jack有一批仍可用的猎物或肉类接近保鲜期限，需要决定如何处理。',
  'participation_hint': '可以与Jack商量加工、购买或转售；处理能力与鲜期按现有规则校验。',
  'resolved_summary': '本次关注的鲜货已从原库存处理或转出。'},
 {'code': 'DISH_REVIEW',
  'title': '一次真实的菜品点评',
  'rule_type': 'DISH_REVIEW',
  'parameters': {'npc_id': '10013', 'location_code': 'RECORD_HALL'},
  'summary': '已有真实菜品样品送交Mark，等待他的独立品鉴。',
  'participation_hint': '可以了解这次菜品与制作目标、与相关人物交流；评分由Mark独立提交。',
  'resolved_summary': 'Mark已完成这次样品的实际品鉴，评分和评语以原点评记录为准。'},
 {'code': 'NORA_CARAVAN',
  'title': '这一趟商队怎么补货',
  'rule_type': 'CARAVAN_VISIT',
  'parameters': {'npc_id': '10015'},
  'summary': '商队已经实际到访并带来有限现货，Nora可以决定这一趟的补货。',
  'participation_hint': '可以了解现货、与Nora交流需求；采购仍受真实资金、库存和开放时间限制。',
  'resolved_summary': 'Nora已完成这一趟商队的实际进口交易，后续商品以真实库存为准。'},
 {'code': 'PUBLIC_LUNCH',
  'title': '今天的公共午饭',
  'rule_type': 'PUBLIC_MEAL',
  'parameters': {'npc_id': '10014', 'store_code': 'RESTAURANT_STORE', 'location_code': 'RESTAURANT'},
  'summary': 'Lucy已经上架可实际领取的公共餐，目前库存、公共资金和全村额度允许领取。',
  'participation_hint': '可以到饭店查看公共餐；每人额度和实际可领取情况由已有购买规则检查。',
  'resolved_summary': '本次公共餐已有实际领取。'},
 {'code': 'BRIDGE_STAGE',
  'title': '一起推进旧桥修复',
  'rule_type': 'AFFAIR_STAGE',
  'parameters': {'affair_id': 'BRIDGE_REPAIR'},
  'enabled': False,
  'summary': '旧桥修复有一个正在推进的阶段，现有事务详情列出了剩余条件。',
  'participation_hint': '查看当前缺口，在对应地点提交材料、金币或参加施工。',
  'resolved_summary': '本阶段条件已实际完成，工程继续推进。'},
 {'code': 'BRIDGE_FIRST_CARAVAN',
  'title': '修桥后的第一趟正常商队',
  'rule_type': 'NORMAL_CARAVAN_RETURN',
  'parameters': {'affair_id': 'BRIDGE_REPAIR'},
  'summary': '旧桥已实际修复，之后的第一趟正常路线商队已经实际到访。',
  'participation_hint': '可以了解恢复后的货物供应、与Nora交流；实际交易仍受现货和资金限制。',
  'resolved_summary': '恢复通路后的第一趟正常商队已有实际进口交易。'}]


def seed(apps, schema_editor):
    Template = apps.get_model("village", "VillageDailyEventTemplate")
    for row in TEMPLATES:
        values = dict(row)
        code = values.pop("code")
        Template.objects.using(schema_editor.connection.alias).get_or_create(code=code, defaults=values)


class Migration(migrations.Migration):
    dependencies = [("village", "0071_daily_event_templates")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
