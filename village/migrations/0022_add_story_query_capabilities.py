from django.db import migrations


STORY_QUERIES = ("INSPECT_RELEVANT_STORIES", "INSPECT_STORY_DETAIL")


def add_story_queries(apps, schema_editor):
    NpcProfile = apps.get_model("village", "NpcProfile")
    for profile in NpcProfile.objects.filter(npc__is_test_only=False, is_active=True):
        capabilities = list(profile.capabilities or [])
        for query in STORY_QUERIES:
            if query not in capabilities:
                capabilities.append(query)
        if capabilities != profile.capabilities:
            profile.capabilities = capabilities
            profile.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("village", "0021_add_agent_query_capabilities")]
    operations = [migrations.RunPython(add_story_queries, migrations.RunPython.noop)]
