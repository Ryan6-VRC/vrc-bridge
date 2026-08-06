from .index_puppet import IndexPuppetMapping
from .index_usercamera import UserCameraMapping
from .index_virtuallens import VirtualLensMapping
from .index_vrclens import VRCLensMapping
from .mapping_base import Mapping, MappingRouter
from .osc_muteproxy import MuteProxyMapping
from .osc_paramlog import ParamLogMapping
from .osc_vrcft import VRCFTMapping
from .osc_wardrobe import WardrobeMapping

# RemyMapping is deliberately absent: `import *` walks __all__ with getattr, which
# would fire the lazy hook and pull in httpx and Pillow -- the exact cost the extra
# exists to avoid, and a hard failure on an install without it. Import it by name.
__all__ = ["IndexPuppetMapping", "VirtualLensMapping", "UserCameraMapping", "VRCLensMapping",
           "MuteProxyMapping", "ParamLogMapping", "VRCFTMapping", "WardrobeMapping",
           "Mapping", "MappingRouter"]

#: Everything importable from here, including the lazily-resolved names, so tab
#: completion and dir() still show them without resolving anything.
LAZY = ["RemyMapping"]


def __dir__():
    return sorted(__all__ + LAZY)


def __getattr__(name: str):
    """Resolve RemyMapping on first access (PEP 562).

    Importing it eagerly pulled httpx and Pillow into every launch of every
    router, including the ones that never touch Remy. They are an optional extra.
    """
    if name == "RemyMapping":
        try:
            from .index_remy import RemyMapping
        except ImportError as exc:
            raise ImportError(
                f"RemyMapping needs the optional 'remy' extra (missing: {exc.name}). "
                "Install it with: pip install 'vrc-bridge[remy]'"
            ) from exc
        return RemyMapping
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
