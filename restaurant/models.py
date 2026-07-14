from django.db import models
from account.models import GameAccount


class RestaurantConfig(models.Model):
    """餐厅全局配置（单例模式）。"""
    business_hour = models.IntegerField("营业时间-小时", default=19, help_text="0-23")
    business_minute = models.IntegerField("营业时间-分钟", default=0)
    base_customer_count = models.IntegerField("基础顾客数", default=3, help_text="1级餐厅基础顾客数")
    customers_per_level = models.IntegerField("每级增加顾客数", default=1)
    llm_review_endpoint = models.CharField("AI点评端点", max_length=100, default="menglong/chat")
    customer_name_pool = models.JSONField("顾客姓名池", default=list, blank=True,
                                          help_text='["张三", "李四", ...]')

    class Meta:
        verbose_name = "餐厅配置"
        verbose_name_plural = "餐厅配置"

    @classmethod
    def get_instance(cls):
        """获取单例实例，不存在则创建。"""
        obj, _ = cls.objects.get_or_create(pk=1, defaults={
            "customer_name_pool": [
                "老王", "小李", "张大妈", "赵叔", "陈姐", "刘哥", "周阿姨", "吴大爷",
                "黄小妹", "林大哥", "何姐", "马叔", "孙大妈", "胡哥", "朱阿姨"
            ]
        })
        return obj

    def __str__(self):
        return "餐厅全局配置"


class RestaurantLevelConfig(models.Model):
    """餐厅等级配置，定义升级条件和奖励。"""
    level = models.IntegerField("目标等级", unique=True)
    description = models.CharField("等级描述", max_length=100, blank=True, default="")
    required_total_ratings = models.IntegerField("需要评价数", default=0)
    required_dishes_sold = models.IntegerField("需要售卖菜品数", default=0)
    required_dish_ids = models.JSONField("必须售卖的菜品ID", default=list, blank=True,
                                        help_text='[101, 102, ...]')
    required_dish_scores = models.JSONField("菜品评分要求", default=list, blank=True,
                                            help_text='[{"itemId": 1048, "minScore": 75}]')
    cost_money = models.IntegerField("升级金币消耗", default=0)
    cost_items = models.JSONField("升级物品消耗", default=list, blank=True,
                                  help_text='[{"itemId": 101, "amount": 5}]')
    reward_customer_bonus = models.IntegerField("额外顾客数", default=0,
                                                help_text="每次营业额外顾客数")
    reward_materials = models.JSONField("奖励材料", default=list, blank=True,
                                        help_text='[{"itemId": 101, "amount": 3}]')
    reward_recipes = models.JSONField("奖励菜谱", default=list, blank=True,
                                      help_text='["红烧排骨", "糖醋鱼"]')
    reward_speed_bonus_pct = models.IntegerField("顾客速度加成%", default=0)
    reward_price_bonus_pct = models.IntegerField("售卖价格加成%", default=0)

    class Meta:
        verbose_name = "餐厅等级配置"
        verbose_name_plural = "餐厅等级配置"
        ordering = ["level"]

    def __str__(self):
        return f"餐厅等级 {self.level}: {self.description}"


class PlayerRestaurantState(models.Model):
    """玩家餐厅状态。"""
    player = models.OneToOneField(GameAccount, on_delete=models.CASCADE,
                                  related_name="restaurant_state", verbose_name="玩家")
    current_level = models.IntegerField("当前等级", default=1)
    total_dishes_sold = models.IntegerField("累计售卖菜品数", default=0)
    total_gold_earned = models.IntegerField("累计售卖金币", default=0)
    total_ratings_received = models.IntegerField("累计好评数", default=0)
    dishes_sold_set = models.JSONField("已售卖菜品ID集合", default=list, blank=True,
                                       help_text="[101, 102, ...]")
    last_business_game_day = models.IntegerField("上次营业游戏天数", default=0)
    season_id = models.CharField("所属赛季ID", max_length=50, default="", blank=True)

    class Meta:
        verbose_name = "玩家餐厅状态"
        verbose_name_plural = "玩家餐厅状态"

    def __str__(self):
        return f"{self.player.username} 餐厅等级{self.current_level}"


class MenuItem(models.Model):
    """菜单菜品，当日有效，每日营业后未被消费的将被销毁。"""
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE,
                               related_name="menu_items", verbose_name="玩家")
    instance_id = models.CharField("实例ID", max_length=50, help_text="对应 InventorySlot 的 instance_id")
    item_id = models.IntegerField("菜品ID")
    item_name = models.CharField("菜品名称", max_length=100)
    dish_quality = models.IntegerField("品质等级", default=3, help_text="1-6")
    rated = models.BooleanField("是否已点评", default=False)
    rating_price = models.IntegerField("点评价格", default=-1, help_text="-1表示未点评")
    overall_score = models.FloatField("综合评分", default=0, help_text="0-100")
    ingredient = models.CharField("原料信息", max_length=200, default="", blank=True)
    cook_time = models.IntegerField("烹饪时长", default=0)
    quantity = models.IntegerField("可售卖数量", default=1)
    exhibited_game_day = models.IntegerField("上架时游戏天数", default=0,
                                             help_text="用于判断是否过期")

    class Meta:
        verbose_name = "菜单菜品"
        verbose_name_plural = "菜单菜品"
        ordering = ["-exhibited_game_day", "item_name"]

    def __str__(self):
        return f"{self.player.username} - {self.item_name} x{self.quantity}"


class CustomerTransaction(models.Model):
    """顾客交易记录。"""
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE,
                               related_name="customer_transactions", verbose_name="玩家")
    daily_log = models.ForeignKey("DailyBusinessLog", on_delete=models.CASCADE,
                                  related_name="transactions", verbose_name="营业日志",
                                  null=True, blank=True)
    season_id = models.CharField("赛季ID", max_length=50, default="", blank=True)
    game_day = models.IntegerField("游戏天数")
    customer_name = models.CharField("顾客姓名", max_length=50)
    item_id = models.IntegerField("菜品ID")
    item_name = models.CharField("菜品名称", max_length=100)
    dish_quality = models.IntegerField("品质等级", default=3)
    amount_paid = models.IntegerField("支付金额", default=0)
    rating = models.IntegerField("LLM评分", default=3, help_text="1-5")
    review_comment = models.TextField("LLM评语", default="", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "顾客交易记录"
        verbose_name_plural = "顾客交易记录"
        ordering = ["-game_day", "-created_at"]

    def __str__(self):
        return f"{self.customer_name}购买{self.item_name}({self.amount_paid}金币)"


class DailyBusinessLog(models.Model):
    """营业日志，防重复。"""
    player = models.ForeignKey(GameAccount, on_delete=models.CASCADE,
                               related_name="business_logs", verbose_name="玩家")
    game_day = models.IntegerField("累计游戏天数（废弃，保留兼容）", default=0)
    season_id = models.CharField("赛季ID", max_length=50, default="", blank=True)
    game_year = models.IntegerField("游戏年", default=2026)
    game_month = models.IntegerField("游戏月", default=3)
    game_day_of_month = models.IntegerField("游戏日", default=8)
    customer_count = models.IntegerField("顾客数", default=0)
    total_earned = models.IntegerField("本次总收入", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "营业日志"
        verbose_name_plural = "营业日志"
        unique_together = [("player", "season_id", "game_year", "game_month", "game_day_of_month")]
        ordering = ["-game_year", "-game_month", "-game_day_of_month"]

    def __str__(self):
        return f"{self.player.username} {self.season_id or 'S0'} {self.game_year}/{self.game_month}/{self.game_day_of_month} 售{self.total_earned}金币"
