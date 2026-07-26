from .index_puppet import IndexPuppetMapping
from .index_usercamera import UserCameraMapping
from .index_virtuallens import VirtualLensMapping
from .index_vrclens import VRCLensMapping
from .mapping_base import Mapping, MappingRouter
from .osc_muteproxy import MuteProxyMapping
from .osc_vrcft import VRCFTMapping

__all__ = ["IndexPuppetMapping", "VirtualLensMapping", "UserCameraMapping", "VRCLensMapping",
           "MuteProxyMapping", "RemyMapping", "VRCFTMapping",
           "Mapping", "MappingRouter"]


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
