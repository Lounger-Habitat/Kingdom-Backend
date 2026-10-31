"""Two fixed farm sites, settled through the existing resource affair reducer.

Installing definitions never grants resources or resets crops/leases. Projections
only read; the authority calls ensure/sync under the shared clock lock.
"""
from django.db import transaction

from .farm_expansion_models import VillageFarmExpansionSite
from .models import VillageAffair, VillageFarmPlot, VillageLocationDefinition, VillageSimulationConfig
from .seed_data import FARM_EXPANSION_PLOT_SEEDS

AFFAIR_PREFIX = "FARM_EXPANSION_"
SITE_LABELS = {"E": "扩展一号区", "F": "扩展二号区"}
# Scene geometry is shared with the Unity fixed-field catalogue.
SITE_ORIGINS = {"E": (-44.2, 0.01, -78.0), "F": (-17.8, 0.01, -78.0)}


def preparation_code(site_code):
    return f"{AFFAIR_PREFIX}{site_code}_PREP"


def plot_affair_code(plot_code):
    return AFFAIR_PREFIX + plot_code.removeprefix("FARM_PLOT_")


def plot_worksite_code(plot_code):
    return "FARM_WORK_" + plot_code.removeprefix("FARM_PLOT_")


def plot_position(site_code, index):
    # Unity verified all 18 cells clear and all south work edges on the NavMesh.
    x = -42.0 if site_code == "E" else -24.4
    return [round(x + (index - 1) % 3 * 2.2, 2), 0.01,
            round(-84.0 + (index - 1) // 3 * 2.2 - 1.2, 2)]


def enabled():
    return bool(VillageSimulationConfig.objects.filter(config_key="default", farm_expansion_enabled=True).exists())


def _complete(affair):
    from .affairs import definition, condition_progress
    if affair is None or affair.status != "COMPLETED":
        return False
    progress = condition_progress(affair)
    return all(progress.get(c["id"], 0) >= c["target"]
               for stage in definition(affair)["stages"] for c in stage["conditions"])


def irrigation_ready():
    from .farm_irrigation import IRRIGATION_AFFAIR_CODE
    return _complete(VillageAffair.objects.filter(pk=IRRIGATION_AFFAIR_CODE).first())


def affair_blocked_reason(affair):
    """Gate every contribution path before it can deduct items, gold or labor."""
    if not affair.code.startswith(AFFAIR_PREFIX) or affair.status == "COMPLETED":
        return "", ""
    if not enabled():
        return "FARM_EXPANSION_DISABLED", "新田开垦暂未开放。"
    if not irrigation_ready():
        return "FARM_IRRIGATION_REQUIRED", "请先完成农田水渠通水。"
    site_code = affair.code.removeprefix(AFFAIR_PREFIX).split("_", 1)[0]
    site = VillageFarmExpansionSite.objects.select_related("preparation_affair").filter(pk=site_code).first()
    if site is None:
        return "FARM_EXPANSION_UNAVAILABLE", "片区定义尚未同步。"
    if affair.pk != site.preparation_affair_id and not _complete(site.preparation_affair):
        return "FARM_PREPARATION_REQUIRED", "请先完成该片区的公共准备。"
    if affair.pk != site.preparation_affair_id and not VillageFarmPlot.objects.filter(
            plot_code__in=site.plot_codes, reclamation_affair=affair).exists():
        return "FARM_EXPANSION_UNAVAILABLE", "田块开垦定义尚未同步。"
    places = affair.locations
    if not places or VillageLocationDefinition.objects.filter(
            location_code__in=places, is_active=True).count() != len(set(places)):
        return "FARM_LOCATION_UNAVAILABLE", "施工位置尚未同步。"
    return "", ""


def plot_access(plot):
    """Land openness is independent of crop/lease state and the feature flag."""
    status = plot.reclamation_status
    fixed = next((seed for seed in FARM_EXPANSION_PLOT_SEEDS if seed["plot_code"] == plot.plot_code), None)
    synchronized = not fixed or (plot.expansion_site_code == fixed["expansion_site_code"]
        and plot.reclamation_affair_id == plot_affair_code(plot.plot_code)
        and VillageLocationDefinition.objects.filter(location_code=plot_worksite_code(plot.plot_code), is_active=True).exists())
    if not synchronized:
        status = "UNKNOWN"
    ready = status == "READY"
    reason = "" if ready else {
        "LOCKED": "FARM_PREPARATION_REQUIRED", "AVAILABLE": "FARM_RECLAMATION_REQUIRED",
        "CLEARED": "FARM_LEVELING_REQUIRED", "LEVELED": "FARM_WATER_CONNECTION_REQUIRED",
    }.get(status, "FARM_RECLAMATION_UNKNOWN")
    result = dict(reclamation_status=status, expansion_site_code=plot.expansion_site_code,
        reclamation_affair_id=plot.reclamation_affair_id,
        reclaimed_world_minute=plot.reclaimed_world_minute, can_use=ready, blocked_reason=reason)
    if plot.expansion_site_code in SITE_LABELS and plot.plot_code.rsplit("_", 1)[-1].isdigit():
        index = int(plot.plot_code.rsplit("_", 1)[-1])
        if 1 <= index <= 9:
            result.update(worksite_code=plot_worksite_code(plot.plot_code),
                worksite_position=plot_position(plot.expansion_site_code, index))
    return result


def _preparation_requirements():
    return {"stages": [
        dict(id="entrance", title="整理入口与通路", conditions=[
            dict(id="entrance_wood", type="item", label="通路木材", item_ids=[1002], target=6),
            dict(id="entrance_labor", type="labor", label="整理入口与通路", target=1, unit_minutes=30)]),
        dict(id="water_access", title="准备水渠接入", conditions=[
            dict(id="water_wood", type="item", label="公共支渠木材", item_ids=[1002], target=6),
            dict(id="water_gold", type="gold", label="公共支渠资金", target=20),
            dict(id="water_labor", type="labor", label="公共支渠接入", target=1, unit_minutes=30)]),
    ]}


def _plot_requirements():
    return {"stages": [
        dict(id="clearing", title="清障", conditions=[
            dict(id="clearing_labor", type="labor", label="清理该块荒地", target=1, unit_minutes=20)]),
        dict(id="leveling", title="整地", conditions=[
            dict(id="leveling_wood", type="item", label="田埂木材", item_ids=[1002], target=2),
            dict(id="leveling_labor", type="labor", label="翻整土壤与田埂", target=1, unit_minutes=20)]),
        dict(id="opening", title="接渠与开放", conditions=[
            dict(id="opening_labor", type="labor", label="接通该块支渠", target=1, unit_minutes=15)]),
    ]}


def _location(code, label, position):
    VillageLocationDefinition.objects.get_or_create(location_code=code, defaults=dict(
        display_name=label, center_anchor_code="LOC_" + code, location_type="work",
        bounds={"center": list(position)}, opening_hours={"alwaysOpen": True},
        supported_action_types=["CONTRIBUTE_TO_AFFAIR"], capacity=4, is_public=True, is_active=True))


@transaction.atomic
def ensure_expansion_sites(world_minute):
    from game_time.models import GlobalGameTimeState
    from .affairs import create_resource_affair
    GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
    for site_code, label in SITE_LABELS.items():
        location = "FARM_EXPANSION_" + site_code
        _location(location, label + "·公共准备", SITE_ORIGINS[site_code])
        preparation = create_resource_affair(preparation_code(site_code), label + "公共准备",
            _preparation_requirements(), [location], world_minute)
        seeds = [seed for seed in FARM_EXPANSION_PLOT_SEEDS if seed["expansion_site_code"] == site_code]
        site, _ = VillageFarmExpansionSite.objects.get_or_create(site_code=site_code, defaults=dict(
            label=label, plot_codes=[s["plot_code"] for s in seeds], location_code=location,
            preparation_affair=preparation))
        for index, seed in enumerate(seeds, 1):
            code = seed["plot_code"]
            worksite = plot_worksite_code(code)
            _location(worksite, f"{label}·{index:02d}号田边", plot_position(site_code, index))
            affair = create_resource_affair(plot_affair_code(code), f"开垦{site_code}{index:02d}号田",
                _plot_requirements(), [worksite], world_minute)
            VillageFarmPlot.objects.get_or_create(plot_code=code, defaults={
                **{key: value for key, value in seed.items() if key != "plot_code"},
                "reclamation_affair": affair})
    sync_expansion_effects(None, world_minute)
    return project_expansion_sites(world_minute)


def _event(site, minute, kind, identity, summary, plot=None):
    from game_time.models import GlobalGameTimeState
    from .models import VillageWorldEvent
    from .phase4_visibility import enqueue_event_deliveries
    clock = GlobalGameTimeState.objects.filter(pk=1).first()
    facts = dict(site_code=site.pk, status=site.status)
    if plot is not None:
        facts.update(plot_code=plot.plot_code, **plot_access(plot))
    event, created = VillageWorldEvent.objects.get_or_create(dedupe_key=f"farm-expansion:{identity}:{kind}", defaults=dict(
        event_type=kind, world_minute=minute, time_revision=clock.time_revision if clock else 0,
        location_code=plot_worksite_code(plot.plot_code) if plot else site.location_code,
        visibility="PUBLIC", player_summary=summary, hub_summary=summary, public_data=facts))
    if created:
        enqueue_event_deliveries(event)


@transaction.atomic
def sync_expansion_effects(affair, world_minute):
    """Apply earned stages once in the contribution transaction, never on reads."""
    from game_time.models import GlobalGameTimeState
    from .farm_irrigation import IRRIGATION_AFFAIR_CODE
    if affair is not None and affair.code != IRRIGATION_AFFAIR_CODE and not affair.code.startswith(AFFAIR_PREFIX):
        return
    GlobalGameTimeState.objects.select_for_update().filter(pk=1).first()
    water_ready = irrigation_ready()
    feature_enabled = enabled()
    sites = VillageFarmExpansionSite.objects.select_for_update().select_related("preparation_affair").order_by("site_code")
    for site in sites:
        changed = []
        prep_ready = _complete(site.preparation_affair)
        if feature_enabled and water_ready and site.opened_world_minute is None:
            site.opened_world_minute = world_minute
            changed.append("opened_world_minute")
            _event(site, world_minute, "FARM_EXPANSION_AVAILABLE", site.pk,
                f"{site.label}现可参与公共准备，共九块候选田，其中三块村庄田、六块可租田。")
        plots = list(VillageFarmPlot.objects.select_for_update().select_related("reclamation_affair").filter(
            plot_code__in=site.plot_codes).order_by("plot_code"))
        for plot in plots:
            # Open land stays usable when new construction is paused or disabled.
            if plot.reclamation_status == "READY":
                continue
            status = "LOCKED"
            if water_ready and prep_ready:
                child = plot.reclamation_affair
                from .affairs import condition_progress
                progress = condition_progress(child) if child else {}
                status = "READY" if _complete(child) else "LEVELED" if progress.get("leveling_labor", 0) >= 1 and progress.get("leveling_wood", 0) >= 2 else "CLEARED" if progress.get("clearing_labor", 0) >= 1 else "AVAILABLE"
            if status != plot.reclamation_status:
                plot.reclamation_status = status
                plot.plot_version += 1
                if status == "READY":
                    plot.reclaimed_world_minute = world_minute
                plot.save(update_fields=["reclamation_status", "reclaimed_world_minute", "plot_version", "updated_at"])
                if status == "READY":
                    _event(site, world_minute, "FARM_PLOT_RECLAIMED", plot.plot_code,
                        f"{plot.plot_code}已成为可耕地。" + ("现在公开出租，按服务器成功结算租约的先后承租。" if plot.lease_enabled else "由 Bill 经营。"), plot)
        ready_count = sum(plot.reclamation_status == "READY" for plot in plots)
        status = "COMPLETED" if len(plots) == 9 and ready_count == 9 else "RECLAIMING" if water_ready and prep_ready else "PREPARING" if water_ready else "LOCKED"
        if status != site.status:
            previous = site.status
            site.status = status
            changed.append("status")
            if status == "COMPLETED" and site.completed_world_minute is None:
                site.completed_world_minute = world_minute
                changed.append("completed_world_minute")
                _event(site, world_minute, "FARM_EXPANSION_COMPLETED", site.pk, f"{site.label}九块田已全部开放，贡献记录永久保留。")
            elif status == "RECLAIMING" and previous in {"LOCKED", "PREPARING"}:
                _event(site, world_minute, "FARM_EXPANSION_PREPARED", site.pk, f"{site.label}公共准备完成，九块田可分别开垦。")
        if changed:
            site.save(update_fields=changed)


def affair_metadata(affair):
    if not affair.code.startswith(AFFAIR_PREFIX):
        return {}
    site_code = affair.code.removeprefix(AFFAIR_PREFIX).split("_", 1)[0]
    is_preparation = affair.code == preparation_code(site_code)
    return dict(expansion_site_code=site_code, expansion_role="PREPARATION" if is_preparation else "PLOT",
        expansion_plot_code="" if is_preparation else "FARM_PLOT_" + affair.code.removeprefix(AFFAIR_PREFIX),
        completion_effect="九块候选田分别开放开垦条件" if is_preparation else "该块田开放后立即可耕作，可租田同时公开出租，无贡献者优先权")


def project_expansion_sites(world_minute=None):
    from game_time.models import GlobalGameTimeState
    from .affairs import project_affair
    from .player_farm import project_plot
    if world_minute is None:
        world_minute = GlobalGameTimeState.objects.filter(pk=1).values_list("world_minute", flat=True).first() or 0
    result = dict(enabled=enabled(), irrigation_ready=irrigation_ready(), sites=[])
    for site in VillageFarmExpansionSite.objects.select_related("preparation_affair").order_by("site_code"):
        plots = [project_plot(plot, world_minute) for plot in VillageFarmPlot.objects.select_related(
            "lessee_player").filter(plot_code__in=site.plot_codes).order_by("plot_code")]
        blocked, message = affair_blocked_reason(site.preparation_affair)
        result["sites"].append(dict(site_code=site.pk, label=site.label, status=site.status,
            location_code=site.location_code, blocked_reason=blocked, unavailable_reason=message,
            preparation_affair=project_affair(site.preparation_affair), plots=plots,
            ready_plot_count=sum(plot["can_use"] for plot in plots), total_plot_count=9,
            opened_world_minute=site.opened_world_minute, completed_world_minute=site.completed_world_minute))
    return result
