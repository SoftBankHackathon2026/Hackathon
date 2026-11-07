from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent.fakes import World, build_world


@pytest.fixture
def make_world(tmp_path) -> Iterator:
    worlds: list[World] = []

    def factory(**kwargs) -> World:
        world = build_world(str(tmp_path), **kwargs)
        worlds.append(world)
        return world

    yield factory
    for world in worlds:
        world.close()
