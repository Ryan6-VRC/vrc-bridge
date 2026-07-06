"""Smoke tests: package wiring and pure helpers. No network, no SteamVR."""

from vrbridge import cli
from vrbridge.mappings import MappingRouter
from vrbridge.routers import CameraPrefabRouter, DefaultRouter, FullRouter
from vrbridge.utils import clamp, clamp01


def test_router_registry():
    assert set(cli.ROUTERS) == {"default", "camera", "remy"}
    assert cli.DEFAULT_ROUTER == "default"
    assert "playspace" not in cli.ROUTERS


def test_routers_are_mapping_routers():
    for cls in (DefaultRouter, CameraPrefabRouter, FullRouter):
        assert issubclass(cls, MappingRouter)


def test_playspace_removed():
    import vrbridge.mappings as m

    assert "PlayspaceMapping" not in m.__all__


def test_clamp_helpers():
    assert clamp(5.0) == 1.0
    assert clamp(-5.0) == -1.0
    assert clamp(0.25) == 0.25
    assert clamp01(2.0) == 1.0
    assert clamp01(-1.0) == 0.0
