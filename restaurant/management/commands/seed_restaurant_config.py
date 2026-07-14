from django.core.management.base import BaseCommand
from restaurant.models import RestaurantConfig, RestaurantLevelConfig


class Command(BaseCommand):
    help = "初始化餐厅配置种子数据"

    def handle(self, *args, **options):
        self.stdout.write("开始初始化餐厅配置...")

        # 创建全局配置
        config = RestaurantConfig.get_instance()
        self.stdout.write(self.style.SUCCESS(f"全局配置已创建: {config}"))

        # 创建等级配置
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
                "reward_materials": [],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 0,
                "reward_price_bonus_pct": 0,
            },
            {
                "level": 2,
                "description": "温馨小店",
                "required_total_ratings": 5,
                "required_dishes_sold": 10,
                "required_dish_ids": [],
                "required_dish_scores": [
                    {"itemId": 1048, "minScore": 75},  # 西红柿炒鸡蛋>=75
                    {"itemId": 1053, "minScore": 75},  # 辣子鸡丁>=75
                ],
                "cost_money": 500,
                "cost_items": [],
                "reward_customer_bonus": 1,
                "reward_materials": [
                    {"itemId": 3006, "amount": 1},  # 红烧排骨菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 10,
                "reward_price_bonus_pct": 5,
            },
            {
                "level": 3,
                "description": "家庭餐馆",
                "required_total_ratings": 15,
                "required_dishes_sold": 30,
                "required_dish_ids": [1048],  # 西红柿炒鸡蛋
                "required_dish_scores": [
                    {"itemId": 1072, "minScore": 78},  # 酸辣土豆丝>=78
                    {"itemId": 1068, "minScore": 78},  # 蒜蓉西兰花>=78
                ],
                "cost_money": 1500,
                "cost_items": [],
                "reward_customer_bonus": 2,
                "reward_materials": [
                    {"itemId": 3007, "amount": 1},  # 鱼香肉丝菜谱
                    {"itemId": 3005, "amount": 1},  # 地三鲜菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 15,
                "reward_price_bonus_pct": 10,
            },
            {
                "level": 4,
                "description": "知名饭馆",
                "required_total_ratings": 30,
                "required_dishes_sold": 60,
                "required_dish_ids": [1048, 1053],  # 西红柿炒鸡蛋、辣子鸡丁
                "required_dish_scores": [
                    {"itemId": 1060, "minScore": 80},  # 地三鲜>=80
                    {"itemId": 1062, "minScore": 80},  # 鱼香肉丝>=80
                ],
                "cost_money": 3000,
                "cost_items": [],
                "reward_customer_bonus": 3,
                "reward_materials": [
                    {"itemId": 3003, "amount": 1},  # 红烧肉菜谱
                    {"itemId": 3018, "amount": 1},  # 麻婆豆腐菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 20,
                "reward_price_bonus_pct": 15,
            },
            {
                "level": 5,
                "description": "星级餐厅",
                "required_total_ratings": 50,
                "required_dishes_sold": 100,
                "required_dish_ids": [1048, 1053, 1060],  # 西红柿炒鸡蛋、辣子鸡丁、地三鲜
                "required_dish_scores": [
                    {"itemId": 1061, "minScore": 82},  # 红烧排骨>=82
                    {"itemId": 1063, "minScore": 82},  # 菠萝咕噜肉>=82
                ],
                "cost_money": 5000,
                "cost_items": [],
                "reward_customer_bonus": 5,
                "reward_materials": [
                    {"itemId": 3008, "amount": 1},  # 菠萝咕噜肉菜谱
                    {"itemId": 3016, "amount": 1},  # 糖醋里脊菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 25,
                "reward_price_bonus_pct": 20,
            },
            {
                "level": 6,
                "description": "美食广场",
                "required_total_ratings": 75,
                "required_dishes_sold": 150,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063],
                "required_dish_scores": [
                    {"itemId": 1071, "minScore": 82},  # 糖醋里脊>=82
                    {"itemId": 1065, "minScore": 82},  # 海岛炒饭>=82
                    {"itemId": 1064, "minScore": 82},  # 农家小炒肉>=82
                ],
                "cost_money": 7500,
                "cost_items": [],
                "reward_customer_bonus": 6,
                "reward_materials": [
                    {"itemId": 3009, "amount": 1},  # 农家小炒肉菜谱
                    {"itemId": 3010, "amount": 1},  # 海岛炒饭菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 30,
                "reward_price_bonus_pct": 25,
            },
            {
                "level": 7,
                "description": "御膳房",
                "required_total_ratings": 110,
                "required_dishes_sold": 210,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063, 1065, 1064],
                "required_dish_scores": [
                    {"itemId": 1061, "minScore": 88},  # 红烧排骨>=88
                    {"itemId": 1062, "minScore": 88},  # 鱼香肉丝>=88
                    {"itemId": 1064, "minScore": 88},  # 农家小炒肉>=88
                ],
                "cost_money": 10000,
                "cost_items": [],
                "reward_customer_bonus": 8,
                "reward_materials": [
                    {"itemId": 3011, "amount": 1},  # 凉拌海带丝菜谱
                    {"itemId": 3012, "amount": 1},  # 奶酪焗玉米菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 35,
                "reward_price_bonus_pct": 30,
            },
            {
                "level": 8,
                "description": "皇家厨房",
                "required_total_ratings": 150,
                "required_dishes_sold": 280,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063, 1065, 1064, 1071, 1072],
                "required_dish_scores": [
                    {"itemId": 1063, "minScore": 90},  # 菠萝咕噜肉>=90
                    {"itemId": 1071, "minScore": 90},  # 糖醋里脊>=90
                    {"itemId": 1060, "minScore": 90},  # 地三鲜>=90
                ],
                "cost_money": 14000,
                "cost_items": [],
                "reward_customer_bonus": 10,
                "reward_materials": [
                    {"itemId": 3013, "amount": 1},  # 蒜蓉西兰花菜谱
                    {"itemId": 3014, "amount": 1},  # 洋葱炒火腿菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 40,
                "reward_price_bonus_pct": 35,
            },
            {
                "level": 9,
                "description": "天下第一楼",
                "required_total_ratings": 200,
                "required_dishes_sold": 360,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063, 1065, 1064, 1071, 1072, 1068],
                "required_dish_scores": [
                    {"itemId": 1065, "minScore": 92},  # 海岛炒饭>=92
                    {"itemId": 1068, "minScore": 92},  # 蒜蓉西兰花>=92
                    {"itemId": 1072, "minScore": 92},  # 酸辣土豆丝>=92
                ],
                "cost_money": 18000,
                "cost_items": [],
                "reward_customer_bonus": 12,
                "reward_materials": [
                    {"itemId": 3015, "amount": 1},  # 蘑菇焖鸡菜谱
                    {"itemId": 3019, "amount": 1},  # 土豆烧牛肉菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 45,
                "reward_price_bonus_pct": 40,
            },
            {
                "level": 10,
                "description": "传奇食府",
                "required_total_ratings": 260,
                "required_dishes_sold": 450,
                "required_dish_ids": [1048, 1053, 1060, 1061, 1063, 1065, 1064, 1071, 1072, 1068],
                "required_dish_scores": [
                    {"itemId": 1061, "minScore": 95},  # 红烧排骨>=95
                    {"itemId": 1063, "minScore": 95},  # 菠萝咕噜肉>=95
                    {"itemId": 1071, "minScore": 95},  # 糖醋里脊>=95
                ],
                "cost_money": 23000,
                "cost_items": [],
                "reward_customer_bonus": 15,
                "reward_materials": [
                    {"itemId": 3020, "amount": 1},  # 葱爆牛肉菜谱
                    {"itemId": 3021, "amount": 1},  # 西红柿炖牛腩菜谱
                ],
                "reward_recipes": [],
                "reward_speed_bonus_pct": 50,
                "reward_price_bonus_pct": 45,
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
