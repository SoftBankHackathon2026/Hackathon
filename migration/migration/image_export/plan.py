from __future__ import annotations

from migration.image_migration.plan import plan as image_plan

from .config import Config
from .target import TagReader


def plan(config: Config, inventory: dict, source_registry, target) -> dict:
    existing = {
        name: target.repository(name)
        for name in {
            config.target(repo["repository"]) for repo in inventory["repositories"]
        }
    }
    reader = TagReader(target, {name for name, record in existing.items() if record})
    catalog = image_plan(config, inventory, source_registry, lambda _project: reader)
    for repo in catalog["repositories"]:
        record = existing[repo["target"]]
        repo["target_exists"] = record is not None
        repo["settings_mismatch"] = target.differences(record) if record else []
        for item in repo["items"]:
            item["present_tags"] = [
                tag
                for tag in item["target_tags"]
                if reader.found.get((repo["target"], tag)) == item["digest"]
            ]
    return catalog
