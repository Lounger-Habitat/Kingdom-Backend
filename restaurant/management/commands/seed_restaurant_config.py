from django.core.management.base import BaseCommand
from restaurant.models import RestaurantConfig, RestaurantLevelConfig


class Command(BaseCommand):
    help = "初始化餐厅配置种子数据（每小时售卖版本）"

    def handle(self, *args, **options):
        self.stdout.write("开始初始化餐厅配置...")

        # 创建全局配置
        config = RestaurantConfig.get_instance()
        # 更新为每小时售卖配置
        config.opening_hour = 10
        config.closing_hour = 19
        config.base_hourly_sales = 3
        config.hourly_sales_per_level = 1
        config.base_menu_capacity = 5
        config.menu_capacity_per_level = 2
        config.save()
        self.stdout.write(self.style.SUCCESS(f"全局配置已创建: {config}"))

        # 创建等级配置
        # 设计思路：
        # - 偶数等级：每小时售卖增加（递增）
        # - 奇数等级：菜单容量增加（递增）
        # - 增加量随等级递增
        # - 价格加成：每级增加5-8%
        # - 升级条件：基于累计售卖和好评数
        # - 升级消耗：金币
        # - 升级奖励：菜谱

        level_configs = [
            {
                "level": 1,
                "description": "街边小摊",
                "required_total_ratings": 0,
                "required_dishes_sold": 0,
                "required_dish_ids": [],
                "required_dish_scores": [],
                "cost_money": 0,
                "cost_items": [],
                "reward_customer_bonus": 0,
                "reward_menu_capacity_bonus": 0,
                "reward_materials": [],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 0,
            },
            {
                "level": 2,
                "description": "温馨小店",
                "required_total_ratings": 20,
                "required_dishes_sold": 50,
                "required_dish_ids": [],
                "required_dish_scores": [],
                "cost_money": 500,
                "cost_items": [],
                # 偶数等级：每小时售卖+1
                "reward_customer_bonus": 1,
                "reward_menu_capacity_bonus": 0,
                "reward_materials": [
                    {"itemId": 3006, "amount": 1},  # 红烧排骨菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 5,
            },
            {
                "level": 3,
                "description": "家庭餐馆",
                "required_total_ratings": 60,
                "required_dishes_sold": 150,
                "required_dish_ids": [1048],  # 必须卖过西红柿炒鸡蛋
                "required_dish_scores": [],
                "cost_money": 1500,
                "cost_items": [],
                # 奇数等级：菜单容量+2
                "reward_customer_bonus": 0,
                "reward_menu_capacity_bonus": 2,
                "reward_materials": [
                    {"itemId": 3007, "amount": 1},  # 鱼香肉丝菜谱
                    {"itemId": 3005, "amount": 1},  # 地三鲜菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 5,
            },
            {
                "level": 4,
                "description": "知名饭馆",
                "required_total_ratings": 120,
                "required_dishes_sold": 300,
                "required_dish_ids": [1048, 1053],  # 西红柿炒鸡蛋、辣子鸡丁
                "required_dish_scores": [
                    {"itemId": 1048, "minScore": 75},  # 西红柿炒鸡蛋>=75
                ],
                "cost_money": 3000,
                "cost_items": [],
                # 偶数等级：每小时售卖+1
                "reward_customer_bonus": 1,
                "reward_menu_capacity_bonus": 0,
                "reward_materials": [
                    {"itemId": 3003, "amount": 1},  # 红烧肉菜谱
                    {"itemId": 3018, "amount": 1},  # 麻婆豆腐菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 5,
            },
            {
                "level": 5,
                "description": "星级餐厅",
                "required_total_ratings": 200,
                "required_dishes_sold": 500,
                "required_dish_ids": [1048, 1053, 1060],  # 西红柿炒鸡蛋、辣子鸡丁、地三鲜
                "required_dish_scores": [
                    {"itemId": 1053, "minScore": 78},  # 辣子鸡丁>=78
                    {"itemId": 1060, "minScore": 78},  # 地三鲜>=78
                ],
                "cost_money": 6000,
                "cost_items": [],
                # 奇数等级：菜单容量+3
                "reward_customer_bonus": 0,
                "reward_menu_capacity_bonus": 3,
                "reward_materials": [
                    {"itemId": 3008, "amount": 1},  # 菠萝咕噜肉菜谱
                    {"itemId": 3016, "amount": 1},  # 糖醋里脊菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 8,
            },
            {
                "level": 6,
                "description": "美食广场",
                "required_total_ratings": 300,
                "required_dishes_sold": 750,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063],  # 加入红烧排骨、菠萝咕噜肉
                "required_dish_scores": [
                    {"itemId": 1061, "minScore": 80},  # 红烧排骨>=80
                    {"itemId": 1063, "minScore": 80},  # 菠萝咕噜肉>=80
                ],
                "cost_money": 10000,
                "cost_items": [],
                # 偶数等级：每小时售卖+2
                "reward_customer_bonus": 2,
                "reward_menu_capacity_bonus": 0,
                "reward_materials": [
                    {"itemId": 3009, "amount": 1},  # 农家小炒肉菜谱
                    {"itemId": 3010, "amount": 1},  # 海岛炒饭菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 8,
            },
            {
                "level": 7,
                "description": "御膳房",
                "required_total_ratings": 450,
                "required_dishes_sold": 1100,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063, 1065, 1064],  # 加入海岛炒饭、农家小炒肉
                "required_dish_scores": [
                    {"itemId": 1065, "minScore": 82},  # 海岛炒饭>=82
                    {"itemId": 1064, "minScore": 82},  # 农家小炒肉>=82
                ],
                "cost_money": 16000,
                "cost_items": [],
                # 奇数等级：菜单容量+4
                "reward_customer_bonus": 0,
                "reward_menu_capacity_bonus": 4,
                "reward_materials": [
                    {"itemId": 3011, "amount": 1},  # 凉拌海带丝菜谱
                    {"itemId": 3012, "amount": 1},  # 奶酪焗玉米菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 8,
            },
            {
                "level": 8,
                "description": "皇家厨房",
                "required_total_ratings": 650,
                "required_dishes_sold": 1600,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063, 1065, 1064, 1071, 1072],  # 加入糖醋里脊、酸辣土豆丝
                "required_dish_scores": [
                    {"itemId": 1071, "minScore": 85},  # 糖醋里脊>=85
                    {"itemId": 1072, "minScore": 85},  # 酸辣土豆丝>=85
                ],
                "cost_money": 24000,
                "cost_items": [],
                # 偶数等级：每小时售卖+2
                "reward_customer_bonus": 2,
                "reward_menu_capacity_bonus": 0,
                "reward_materials": [
                    {"itemId": 3013, "amount": 1},  # 蒜蓉西兰花菜谱
                    {"itemId": 3014, "amount": 1},  # 洋葱炒火腿菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 10,
            },
            {
                "level": 9,
                "description": "天下第一楼",
                "required_total_ratings": 900,
                "required_dishes_sold": 2200,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063, 1065, 1064, 1071, 1072, 1068],  # 加入蒜蓉西兰花
                "required_dish_scores": [
                    {"itemId": 1068, "minScore": 88},  # 蒜蓉西兰花>=88
                    {"itemId": 1060, "minScore": 88},  # 地三鲜>=88
                ],
                "cost_money": 35000,
                "cost_items": [],
                # 奇数等级：菜单容量+5
                "reward_customer_bonus": 0,
                "reward_menu_capacity_bonus": 5,
                "reward_materials": [
                    {"itemId": 3015, "amount": 1},  # 蘑菇焖鸡菜谱
                    {"itemId": 3019, "amount": 1},  # 土豆烧牛肉菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 10,
            },
            {
                "level": 10,
                "description": "传奇食府",
                "required_total_ratings": 1200,
                "required_dishes_sold": 3000,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063, 1065, 1064, 1071, 1072, 1068],
                "required_dish_scores": [
                    {"itemId": 1061, "minScore": 90},  # 红烧排骨>=90
                    {"itemId": 1063, "minScore": 90},  # 菠萝咕噜肉>=90
                    {"itemId": 1065, "minScore": 90},  # 海岛炒饭>=90
                ],
                "cost_money": 50000,
                "cost_items": [],
                # 偶数等级：每小时售卖+3
                "reward_customer_bonus": 3,
                "reward_menu_capacity_bonus": 0,
                "reward_materials": [
                    {"itemId": 3020, "amount": 1},  # 葱爆牛肉菜谱
                    {"itemId": 3021, "amount": 1},  # 西红柿炖牛腩菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 12,
            },
        ]

        for config_data in level_configs:
            obj, created = RestaurantLevelConfig.objects.update_or_create(
                level=config_data["level"],
                defaults=config_data
            )
            status = "创建" if created else "更新"
            self.stdout.write(f"  {status}等级配置: {obj}")

        self.stdout.write(self.style.SUCCESS("餐厅配置初始化完成！"))
