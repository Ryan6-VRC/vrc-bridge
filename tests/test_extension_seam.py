"""The two documented extension routes: the Mapping subclass hook, and the router entry point."""
import pytest

from vrbridge import cli
from vrbridge.mappings import Mapping, MappingRouter
from vrbridge.routers import CameraPrefabRouter, DefaultRouter, FullRouter


class _Bridge:
    """Enough of a VRBridge for register()/activate() to run."""
    class _Log:
        def info(self, *a): pass
        def warning(self, *a): pass
    log = _Log()


class _Counting(Mapping):
    name = "counting"

    def __init__(self, bridge):
        super().__init__(bridge)
        self.attach_calls = 0

    def _attach(self) -> None:
        self.attach_calls += 1


def test_attach_runs_exactly_once_however_often_register_is_called():
    """VRBridge.on_controller appends without de-duplicating, so a second round of
    bindings means every press fires twice: two photos per capture, and a toggle
    that flips and flips back. The base has to enforce this, not each subclass."""
    m = _Counting(_Bridge())
    for _ in range(5):
        m.register()
    assert m.attach_calls == 1


def test_every_shipped_mapping_uses_the_attach_hook():
    """A mapping that overrode register() would silently opt out of the guard."""
    import vrbridge.mappings as pkg

    # __all__ + LAZY, so a mapping moved behind the lazy hook stays covered.
    names = [n for n in list(pkg.__all__) + list(pkg.LAZY)
             if n.endswith("Mapping") and n != "Mapping"]
    assert len(names) == 7, f"expected all seven shipped mappings, got {names}"
    for name in names:
        cls = getattr(pkg, name)  # RemyMapping resolves through the lazy __getattr__
        assert issubclass(cls, Mapping)
        assert "register" not in vars(cls), f"{name} overrides register(); it should override _attach()"


# ---- router discovery ------------------------------------------------------

class _PluginRouter(MappingRouter):
    def evaluate(self) -> None: pass


class _NotARouter:
    pass


def _fake_eps(monkeypatch, *eps):
    monkeypatch.setattr(cli, "entry_points", lambda group=None: list(eps))


class _EP:
    """Stand-in for an EntryPoint whose load() we control."""
    def __init__(self, name, value, loader):
        self.name, self.value, self._loader = name, value, loader

    def load(self):
        return self._loader()


def test_builtins_are_discovered_without_any_plugin(monkeypatch):
    _fake_eps(monkeypatch)
    assert cli.discover_routers() == {
        "default": DefaultRouter, "camera": CameraPrefabRouter, "remy": FullRouter}


def test_a_plugin_router_becomes_selectable(monkeypatch):
    _fake_eps(monkeypatch, _EP("mine", "pkg:R", lambda: _PluginRouter))
    found = cli.discover_routers()
    assert found["mine"] is _PluginRouter
    assert "default" in found


@pytest.mark.parametrize("ep, reason", [
    (_EP("boom", "pkg:R", lambda: (_ for _ in ()).throw(ImportError("no module"))), "import fails"),
    (_EP("wrong", "pkg:X", lambda: _NotARouter), "not a MappingRouter"),
])
def test_a_broken_plugin_is_skipped_not_fatal(monkeypatch, ep, reason):
    _fake_eps(monkeypatch, ep)
    found = cli.discover_routers()
    assert ep.name not in found, reason
    assert "default" in found, "a broken plugin must not take the CLI down"


def test_a_plugin_may_not_shadow_a_builtin(monkeypatch):
    _fake_eps(monkeypatch, _EP("default", "pkg:R", lambda: _PluginRouter))
    assert cli.discover_routers()["default"] is DefaultRouter


def test_star_import_does_not_pull_in_the_optional_extra():
    """`import *` walks __all__ with getattr, so a lazily-hooked name listed there
    resolves anyway -- pulling in httpx and Pillow, and hard-failing on an install
    without the extra."""
    import vrbridge.mappings as pkg

    assert "RemyMapping" not in pkg.__all__
    assert "RemyMapping" in pkg.LAZY
    assert "RemyMapping" in dir(pkg), "dir() should still advertise it"


def test_overriding_register_is_refused_at_class_creation():
    """"Do not override" has to be a contract, not a docstring: the entry-point
    seam hands this base to third parties."""
    with pytest.raises(TypeError, match="must stay idempotent"):
        class Bad(Mapping):
            name = "bad"

            def register(self) -> None:
                pass
