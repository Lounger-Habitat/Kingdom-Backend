import random
import json
import requests
from django.db import transaction
from django.conf import settings
from account.models import GameAccount
from inventory.models import InventoryBag, InventorySlot
from items.models import Item
from player.models import PlayerState
from .models import (
    RestaurantConfig, RestaurantLevelConfig, PlayerRestaurantState,
    MenuItem, CustomerTransaction, DailyBusinessLog
)


def _get_quality_coeff(dish_quality):
    """品质售价系数：1→0.3，2→0.7，3+→1.0"""
    if dish_quality <= 1:
        return 0.3
    if dish_quality == 2:
        return 0.7
    return 1.0


class RestaurantService:
    """餐厅管理核心业务逻辑。"""

    @staticmethod
    def get_or_create_state(player):
        """获取或创建玩家餐厅状态。"""
        state, created = PlayerRestaurantState.objects.get_or_create(
            player=player,
            defaults={"current_level": 1, "season_id": ""}
        )
        return state

    @staticmethod
    def get_restaurant_state_data(player):
        """获取餐厅完整状态数据（含菜单和升级信息）。"""
        state = RestaurantService.get_or_create_state(player)
        config = RestaurantConfig.get_instance()

        # 获取游戏日历和当前赛季，判断今日是否已营业
        current_day = 0
        current_season_id = ""
        game_year = 0
        game_month = 0
        game_day_of_month = 0
        try:
            player_state = PlayerState.objects.get(player_id=str(player.id))
            current_day = player_state.total_game_days
            current_season_id = player_state.current_season_id or ""
            game_year = player_state.game_year
            game_month = player_state.game_month
            game_day_of_month = player_state.game_day
        except PlayerState.DoesNotExist:
            pass

        has_done_business_today = DailyBusinessLog.objects.filter(
            player=player,
            season_id=current_season_id,
            game_year=game_year,
            game_month=game_month,
            game_day_of_month=game_day_of_month,
        ).exists() and current_day > 0

        # 获取菜单
        menu_items = MenuItem.objects.filter(player=player)

        # 获取升级信息
        next_level_config = RestaurantLevelConfig.objects.filter(
            level=state.current_level + 1
        ).first()

        upgrade_info = None
        if next_level_config:
            can_upgrade, reason = RestaurantService._check_upgrade_conditions(
                state, next_level_config, player
            )
            upgrade_info = {
                "targetLevel": next_level_config.level,
                "description": next_level_config.description,
                "requiredTotalRatings": next_level_config.required_total_ratings,
                "requiredDishesSold": next_level_config.required_dishes_sold,
                "requiredDishIds": next_level_config.required_dish_ids,
                "requiredDishScores": next_level_config.required_dish_scores,
                "costMoney": next_level_config.cost_money,
                "costItems": next_level_config.cost_items,
                "rewardCustomerBonus": next_level_config.reward_customer_bonus,
                "rewardMaterials": next_level_config.reward_materials,
                "rewardSpeedBonusPct": next_level_config.reward_speed_bonus_pct,
                "rewardPriceBonusPct": next_level_config.reward_price_bonus_pct,
                "canUpgrade": can_upgrade,
                "reason": reason,
            }

        # 计算当前等级的总加成
        level_configs = RestaurantLevelConfig.objects.filter(level__lte=state.current_level)
        total_customer_bonus = sum(lc.reward_customer_bonus for lc in level_configs)
        total_price_bonus = sum(lc.reward_price_bonus_pct for lc in level_configs)
        total_speed_bonus = sum(lc.reward_speed_bonus_pct for lc in level_configs)

        # 获取当前等级名称
        current_level_config = RestaurantLevelConfig.objects.filter(level=state.current_level).first()
        current_level_name = current_level_config.description if current_level_config else ""

        return {
            "currentState": {
                "currentLevel": state.current_level,
                "currentLevelName": current_level_name,
                "totalDishesSold": state.total_dishes_sold,
                "totalGoldEarned": state.total_gold_earned,
                "totalRatingsReceived": state.total_ratings_received,
                "dishesSoldSet": state.dishes_sold_set,
                "lastBusinessGameDay": state.last_business_game_day,
                "seasonId": state.season_id,
                "hasDoneBusinessToday": has_done_business_today,
                "currentGameDay": current_day,
            },
            "businessConfig": {
                "businessHour": config.business_hour,
                "businessMinute": config.business_minute,
                "baseCustomerCount": config.base_customer_count,
                "customersPerLevel": config.customers_per_level,
                "totalCustomerBonus": total_customer_bonus,
                "totalPriceBonusPct": total_price_bonus,
                "totalSpeedBonusPct": total_speed_bonus,
            },
            "menuItems": [
                {
                    "id": item.id,
                    "instanceId": item.instance_id,
                    "itemId": item.item_id,
                    "itemName": item.item_name,
                    "dishQuality": item.dish_quality,
                    "rated": item.rated,
                    "ratingPrice": item.rating_price,
                    "overallScore": item.overall_score,
                    "ingredient": item.ingredient,
                    "cookTime": item.cook_time,
                    "quantity": item.quantity,
                    "exhibitedGameDay": item.exhibited_game_day,
                }
                for item in menu_items
            ],
            "upgradeInfo": upgrade_info,
        }

    @staticmethod
    def exhibit_dish(player, data):
        """上架菜品到菜单。"""
        instance_id = data["instance_id"]
        item_id = data["item_id"]
        quantity = data["quantity"]

        # 验证物品在背包中
        try:
            player_state = PlayerState.objects.get(player_id=str(player.id))
        except PlayerState.DoesNotExist:
            print(f"[exhibit_dish] PlayerState not found for player_id={player.id} (username={player.username})")
            return False, "物品不存在于背包中"

        try:
            bag = InventoryBag.objects.get(
                player=player,
                character_id=player.username
            )
        except InventoryBag.DoesNotExist:
            existing_bags = list(InventoryBag.objects.filter(player=player).values_list("character_id", flat=True))
            print(f"[exhibit_dish] InventoryBag not found for character_id={player.username}, existing bags: {existing_bags}")
            return False, "物品不存在于背包中"

        try:
            slot = InventorySlot.objects.get(bag=bag, instance_id=instance_id)
        except InventorySlot.DoesNotExist:
            slot_ids = list(bag.slots.filter(item_amount__gt=0).values_list("instance_id", flat=True)[:10])
            print(f"[exhibit_dish] Slot not found for instance_id={instance_id}, bag has slots: {slot_ids}")
            return False, "物品不存在于背包中"

        if slot.item_amount < quantity:
            return False, f"背包中物品数量不足（拥有{slot.item_amount}，需要{quantity}）"

        # 获取累计游戏天数
        game_day = player_state.total_game_days

        # 创建菜单项
        with transaction.atomic():
            menu_item = MenuItem.objects.create(
                player=player,
                instance_id=instance_id,
                item_id=item_id,
                item_name=data.get("item_name", ""),
                dish_quality=data.get("dish_quality", 3),
                rated=data.get("rated", False),
                rating_price=data.get("rating_price", -1),
                overall_score=data.get("overall_score", 0),
                ingredient=data.get("ingredient", ""),
                cook_time=data.get("cook_time", 0),
                quantity=quantity,
                exhibited_game_day=game_day
            )

            # 扣减背包数量
            slot.item_amount -= quantity
            if slot.item_amount <= 0:
                slot.delete()
            else:
                slot.save()

        return True, menu_item

    @staticmethod
    def withdraw_dish(player, menu_item_id):
        """下架菜品，退回背包。"""
        try:
            menu_item = MenuItem.objects.get(id=menu_item_id, player=player)
        except MenuItem.DoesNotExist:
            return False, "菜单菜品不存在"

        with transaction.atomic():
            # 退回背包
            bag, _ = InventoryBag.objects.get_or_create(
                player=player,
                character_id=player.username,
                defaults={"money": 0}
            )

            # 查找是否有同类物品可堆叠
            existing_slot = InventorySlot.objects.filter(
                bag=bag,
                item_id=menu_item.item_id,
                rated=menu_item.rated,
                rating_price=menu_item.rating_price,
                overall_score=menu_item.overall_score,
                ingredient=menu_item.ingredient,
                dish_quality=menu_item.dish_quality,
                cook_time=menu_item.cook_time,
            ).first()

            if existing_slot:
                existing_slot.item_amount += menu_item.quantity
                existing_slot.save()
            else:
                # 找一个空槽位
                max_index = InventorySlot.objects.filter(bag=bag).order_by("-slot_index").first()
                new_index = (max_index.slot_index + 1) if max_index else 0
                InventorySlot.objects.create(
                    bag=bag,
                    slot_index=new_index,
                    instance_id=menu_item.instance_id,
                    item_id=menu_item.item_id,
                    item_amount=menu_item.quantity,
                    rated=menu_item.rated,
                    rating_price=menu_item.rating_price,
                    overall_score=menu_item.overall_score,
                    ingredient=menu_item.ingredient,
                    item_bag_name=menu_item.item_bag_name or f"{menu_item.item_id}_{new_index}",
                    cook_time=menu_item.cook_time,
                    dish_quality=menu_item.dish_quality,
                )

            menu_item.delete()

        return True, "下架成功"

    @staticmethod
    def cleanup_expired_menu_items(player):
        """清理过期的菜单菜品（昨日残留）。"""
        try:
            player_state = PlayerState.objects.get(player_id=str(player.id))
            current_day = player_state.total_game_days
        except PlayerState.DoesNotExist:
            return 0

        expired_items = MenuItem.objects.filter(
            player=player,
            exhibited_game_day__lt=current_day
        )
        count = expired_items.count()
        expired_items.delete()
        return count

    @staticmethod
    def process_daily_business(player):
        """每日营业主流程。"""
        config = RestaurantConfig.get_instance()
        state = RestaurantService.get_or_create_state(player)

        # 获取累计游戏天数
        try:
            player_state = PlayerState.objects.get(player_id=str(player.id))
            current_day = player_state.total_game_days
        except PlayerState.DoesNotExist:
            return False, "玩家状态不存在"

        # 检查是否已营业（以 DailyBusinessLog 为准，按赛季+日历日期过滤）
        current_season_id = player_state.current_season_id or ""
        if DailyBusinessLog.objects.filter(
            player=player,
            season_id=current_season_id,
            game_year=player_state.game_year,
            game_month=player_state.game_month,
            game_day_of_month=player_state.game_day,
        ).exists():
            return False, "今日已营业"

        # 计算顾客数
        level_configs = RestaurantLevelConfig.objects.filter(level__lte=state.current_level)
        total_customer_bonus = sum(lc.reward_customer_bonus for lc in level_configs)
        customer_count = (
            config.base_customer_count
            + (state.current_level - 1) * config.customers_per_level
            + total_customer_bonus
        )

        # 获取菜单
        menu_items = list(MenuItem.objects.filter(player=player, quantity__gt=0))
        if not menu_items:
            return False, "菜单为空，无法接待顾客"

        # 计算价格加成
        total_price_bonus_pct = sum(lc.reward_price_bonus_pct for lc in level_configs)

        # 获取顾客姓名池
        name_pool = config.customer_name_pool or ["顾客"]

        # ====== 阶段1：事务外完成所有 LLM 调用（避免 SQLite 锁） ======
        customer_results = []  # [(customer_name, chosen_item, final_price, rating, comment), ...]
        for _ in range(customer_count):
            if not menu_items:
                break

            customer_name = random.choice(name_pool)

            weights = [item.quantity for item in menu_items]
            chosen_item = random.choices(menu_items, weights=weights, k=1)[0]

            if chosen_item.rated and chosen_item.rating_price > 0:
                base_price = chosen_item.rating_price
            else:
                item_obj = Item.objects.filter(item_id=chosen_item.item_id).first()
                base_price = item_obj.item_price if item_obj else 100
                base_price = int(base_price * _get_quality_coeff(chosen_item.dish_quality))

            final_price = int(base_price * (100 + total_price_bonus_pct) / 100)

            dish_info = {
                "item_name": chosen_item.item_name,
                "dish_quality": chosen_item.dish_quality,
                "ingredient": chosen_item.ingredient,
                "price": final_price,
            }
            rating, comment = RestaurantService._request_llm_review(dish_info, config)

            customer_results.append((customer_name, chosen_item, final_price, rating, comment))

            # 模拟扣减（只用于后续顾客选择计算，不实际修改数据库）
            chosen_item.quantity -= 1
            if chosen_item.quantity <= 0:
                menu_items.remove(chosen_item)

        # ====== 阶段2：事务内只做数据库操作 ======
        # 重新加载菜单（阶段1 的 quantity 修改只是模拟，需重新查询）
        menu_items_db = list(MenuItem.objects.filter(player=player, quantity__gt=0))
        menu_map = {item.id: item for item in menu_items_db}

        transactions_list = []
        total_earned = 0

        with transaction.atomic():
            # 先创建营业日志（后续 transaction 通过 FK 关联）
            daily_log, _ = DailyBusinessLog.objects.update_or_create(
                player=player,
                season_id=current_season_id,
                game_year=player_state.game_year,
                game_month=player_state.game_month,
                game_day_of_month=player_state.game_day,
                defaults={
                    "game_day": current_day,
                    "customer_count": 0,
                    "total_earned": 0,
                }
            )

            for customer_name, chosen_item, final_price, rating, comment in customer_results:
                # 从数据库重新获取菜品（可能已被前一轮删除）
                db_item = menu_map.get(chosen_item.id)
                if db_item is None or db_item.quantity <= 0:
                    continue

                trans = CustomerTransaction.objects.create(
                    player=player,
                    daily_log=daily_log,
                    season_id=state.season_id,
                    game_day=current_day,
                    customer_name=customer_name,
                    item_id=db_item.item_id,
                    item_name=db_item.item_name,
                    dish_quality=db_item.dish_quality,
                    amount_paid=final_price,
                    rating=rating,
                    review_comment=comment,
                )
                transactions_list.append(trans)
                total_earned += final_price

                db_item.quantity -= 1
                if db_item.quantity <= 0:
                    db_item.delete()
                else:
                    db_item.save()

                state.total_dishes_sold += 1
                state.total_gold_earned += final_price
                if rating >= 3.8:
                    state.total_ratings_received += 1
                if db_item.item_id not in state.dishes_sold_set:
                    state.dishes_sold_set = state.dishes_sold_set + [db_item.item_id]

            # 销毁剩余菜品
            remaining = MenuItem.objects.filter(player=player, quantity__gt=0)
            destroyed_count = remaining.count()
            remaining.delete()

            # 更新营业记录
            state.last_business_game_day = current_day
            state.save()

            # 更新营业日志的最终统计
            daily_log.customer_count = len(transactions_list)
            daily_log.total_earned = total_earned
            daily_log.save()

            # 增加玩家金币
            try:
                bag = InventoryBag.objects.get(player=player, character_id=player.username)
                bag.money += total_earned
                bag._money_change_source = "restaurant_income"
                bag.save()
            except InventoryBag.DoesNotExist:
                pass

        # 构建返回数据
        result = {
            "transactions": [
                {
                    "customerName": t.customer_name,
                    "itemId": t.item_id,
                    "itemName": t.item_name,
                    "dishQuality": t.dish_quality,
                    "amountPaid": t.amount_paid,
                    "rating": t.rating,
                    "reviewComment": t.review_comment,
                }
                for t in transactions_list
            ],
            "totalEarned": total_earned,
            "destroyedDishes": destroyed_count,
            "customerCount": len(transactions_list),
            "money": bag.money,
        }

        return True, result

    @staticmethod
    def upgrade_restaurant(player):
        """升级餐厅。"""
        state = RestaurantService.get_or_create_state(player)

        # 获取下一级配置
        next_config = RestaurantLevelConfig.objects.filter(
            level=state.current_level + 1
        ).first()

        if not next_config:
            return False, "已达到最高等级"

        # 检查升级条件
        can_upgrade, reason = RestaurantService._check_upgrade_conditions(
            state, next_config, player
        )
        if not can_upgrade:
            return False, reason

        with transaction.atomic():
            # 扣除金币
            if next_config.cost_money > 0:
                try:
                    bag = InventoryBag.objects.get(player=player, character_id=player.username)
                    if bag.money < next_config.cost_money:
                        return False, f"金币不足（需要{next_config.cost_money}，拥有{bag.money}）"
                    bag.money -= next_config.cost_money
                    bag._money_change_source = "restaurant_upgrade"
                    bag.save()
                except InventoryBag.DoesNotExist:
                    return False, "背包不存在"

            # 扣除达到评分要求的菜品（每样1份）
            if next_config.required_dish_scores:
                bag = InventoryBag.objects.get(player=player, character_id=player.username)
                for req in next_config.required_dish_scores:
                    item_id = req["itemId"]
                    min_score = req["minScore"]
                    slots = bag.slots.filter(item_id=item_id, item_amount__gt=0)
                    qualified = [s for s in slots if s.overall_score >= min_score]
                    if not qualified:
                        return False, f"菜品{item_id}评分不足（需要>={min_score}）"
                    # 扣评分最低的达标份，保留高分菜品
                    best_slot = min(qualified, key=lambda s: s.overall_score)
                    if best_slot.item_amount <= 1:
                        best_slot.delete()
                    else:
                        best_slot.item_amount -= 1
                        best_slot.save()

            # 升级
            state.current_level += 1
            state.save()

            # 发放奖励材料
            if next_config.reward_materials:
                from items.models import Item
                bag = InventoryBag.objects.get(player=player, character_id=player.username)
                for reward in next_config.reward_materials:
                    item_id = reward["itemId"]
                    amount = reward["amount"]
                    max_index = InventorySlot.objects.filter(bag=bag).order_by("-slot_index").first()
                    new_index = (max_index.slot_index + 1) if max_index else 0
                    remaining_uses = None
                    item_obj = Item.objects.filter(item_id=item_id).first()
                    if item_obj and item_obj.item_type == "Usable":
                        remaining_uses = item_obj.max_use_count
                    item_name = item_obj.item_name if item_obj else f"物品#{item_id}"
                    InventorySlot.objects.create(
                        bag=bag,
                        slot_index=new_index,
                        item_id=item_id,
                        item_amount=amount,
                        item_bag_name=f"{item_name}_{new_index}",
                        remaining_uses=remaining_uses,
                    )

        # 读取升级后的金币余额
        try:
            bag = InventoryBag.objects.get(player=player, character_id=player.username)
            current_money = bag.money
        except InventoryBag.DoesNotExist:
            current_money = 0

        return True, {
            "newLevel": state.current_level,
            "rewardMaterials": next_config.reward_materials,
            "money": current_money,
        }

    @staticmethod
    def _check_upgrade_conditions(state, level_config, player):
        """检查升级条件。"""
        # 检查好评数（评分>=3.8才算好评）
        if state.total_ratings_received < level_config.required_total_ratings:
            return False, f"好评数不足（需要{level_config.required_total_ratings}，当前{state.total_ratings_received}）"

        # 检查售卖数
        if state.total_dishes_sold < level_config.required_dishes_sold:
            return False, f"售卖数不足（需要{level_config.required_dishes_sold}，当前{state.total_dishes_sold}）"

        # 检查特定菜品
        if level_config.required_dish_ids:
            sold_set = set(state.dishes_sold_set)
            required_set = set(level_config.required_dish_ids)
            missing = required_set - sold_set
            if missing:
                return False, f"未售卖过必需菜品：{list(missing)}"

        # 检查菜品评分
        if level_config.required_dish_scores:
            try:
                bag = InventoryBag.objects.get(player=player, character_id=player.username)
                for req in level_config.required_dish_scores:
                    item_id = req["itemId"]
                    min_score = req["minScore"]
                    slots = bag.slots.filter(item_id=item_id, item_amount__gt=0)
                    if not slots.exists():
                        return False, f"背包中没有菜品{item_id}"
                    best_score = max(s.overall_score for s in slots)
                    if best_score < min_score:
                        return False, f"菜品{item_id}评分不足（需要>={min_score}，当前{best_score}）"
            except InventoryBag.DoesNotExist:
                return False, "背包不存在"

        # 检查金币
        if level_config.cost_money > 0:
            try:
                bag = InventoryBag.objects.get(player=player, character_id=player.username)
                if bag.money < level_config.cost_money:
                    return False, f"金币不足（需要{level_config.cost_money}，拥有{bag.money}）"
            except InventoryBag.DoesNotExist:
                return False, "背包不存在"

        return True, "满足所有条件"

    @staticmethod
    def _request_llm_review(dish_info, config):
        """调用 LLM 进行点评。"""
        try:
            # 构建请求
            system_prompt = """你是一个普通饭店顾客，正在品尝一道菜。请给出1-5星的评分和简短评语。
评分标准：
- 1星：很难吃，浪费钱
- 2星：味道一般，不太满意
- 3星：中规中矩，还行吧
- 4星：味道不错，值得推荐
- 5星：非常好吃，下次还来

请以JSON格式返回：{"rating": 评分, "comment": "评语"}"""

            user_message = f"""菜品信息：
- 菜名：{dish_info['item_name']}
- 品质等级：{dish_info['dish_quality']}（1略逊 2一般 3正常 4优秀 5极品 6绝品）
- 原料：{dish_info['ingredient'] or '未知'}
- 价格：{dish_info['price']}金币

请给出你的评价。"""

            # 调用 AI 服务
            endpoint = config.llm_review_endpoint or "menglong/chat"
            url = f"http://localhost:8000/{endpoint}"

            response = requests.post(
                url,
                json={
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ]
                },
                timeout=10
            )

            if response.status_code == 200:
                result = response.json()
                content = result.get("content", "")
                # 解析 JSON
                try:
                    # 尝试从 content 中提取 JSON
                    if "{" in content:
                        json_str = content[content.index("{"):content.rindex("}") + 1]
                        parsed = json.loads(json_str)
                        rating = parsed.get("rating", 3)
                        comment = parsed.get("comment", "味道还行")
                        return max(1, min(5, rating)), comment
                except (json.JSONDecodeError, ValueError):
                    pass

            # 降级方案：随机评分
            return RestaurantService._get_fallback_review(dish_info)

        except Exception:
            return RestaurantService._get_fallback_review(dish_info)

    @staticmethod
    def _get_fallback_review(dish_info):
        """降级点评方案。"""
        # 根据品质调整评分范围
        quality = dish_info.get("dish_quality", 3)
        if quality >= 5:
            rating = random.choice([4, 5, 5])
        elif quality >= 4:
            rating = random.choice([3, 4, 4, 5])
        elif quality >= 3:
            rating = random.choice([2, 3, 3, 4])
        elif quality >= 2:
            rating = random.choice([2, 2, 3])
        else:
            rating = random.choice([1, 1, 2])

        comments = {
            1: ["味道不咋地", "不太好吃", "有点失望"],
            2: ["一般般吧", "凑合能吃", "马马虎虎"],
            3: ["味道还行", "中规中矩", "还可以"],
            4: ["不错不错", "挺好吃的", "值得推荐"],
            5: ["太好吃了！", "强烈推荐", "下次还来！"],
        }

        comment = random.choice(comments.get(rating, ["味道还行"]))
        return rating, comment

    @staticmethod
    def get_review_history(player, page=1, page_size=20):
        """获取评价历史（分页）。"""
        offset = (page - 1) * page_size
        total = CustomerTransaction.objects.filter(player=player).count()
        transactions = CustomerTransaction.objects.filter(
            player=player
        )[offset:offset + page_size]

        return {
            "total": total,
            "page": page,
            "pageSize": page_size,
            "totalPages": (total + page_size - 1) // page_size,
            "reviews": [
                {
                    "id": t.id,
                    "seasonId": t.season_id,
                    "gameDay": t.game_day,
                    "customerName": t.customer_name,
                    "itemId": t.item_id,
                    "itemName": t.item_name,
                    "dishQuality": t.dish_quality,
                    "amountPaid": t.amount_paid,
                    "rating": t.rating,
                    "reviewComment": t.review_comment,
                    "createdAt": t.created_at.isoformat() if t.created_at else "",
                }
                for t in transactions
            ]
        }

    @staticmethod
    def reset_player_restaurant(player):
        """赛季重置时清除玩家餐厅数据。"""
        try:
            state = PlayerRestaurantState.objects.get(player=player)
            state.delete()
        except PlayerRestaurantState.DoesNotExist:
            pass

        MenuItem.objects.filter(player=player).delete()
        CustomerTransaction.objects.filter(player=player).delete()
        DailyBusinessLog.objects.filter(player=player).delete()
