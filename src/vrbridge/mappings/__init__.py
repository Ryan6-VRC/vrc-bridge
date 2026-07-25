from .index_puppet import IndexPuppetMapping
from .index_remy import RemyMapping
from .index_usercamera import UserCameraMapping
from .index_virtuallens import VirtualLensMapping
from .index_vrclens import VRCLensMapping
from .mapping_base import Mapping, MappingRouter
from .osc_muteproxy import MuteProxyMapping
from .osc_vrcft import VRCFTMapping

__all__ = ["IndexPuppetMapping", "VirtualLensMapping", "UserCameraMapping", "VRCLensMapping",
           "MuteProxyMapping", "RemyMapping", "VRCFTMapping",
           "Mapping", "MappingRouter"]
