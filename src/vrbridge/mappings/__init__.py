from .index_puppet import IndexPuppetMapping
from .index_usercamera import UserCameraMapping
from .index_virtuallens import VirtualLensMapping
from .index_vrclens import VRCLensMapping
from .mapping_base import Mapping, MappingRouter
from .osc_leash import LeashMapping
from .osc_muteproxy import MuteProxyMapping
from .osc_paramlog import ParamLogMapping
from .osc_persist import BridgePersistMapping
from .osc_quant import QuantChannelDirectory
from .osc_vrcft import VRCFTMapping
from .osc_wardrobe import WardrobeMapping

__all__ = ["BridgePersistMapping", "IndexPuppetMapping", "LeashMapping", "VirtualLensMapping",
           "UserCameraMapping", "VRCLensMapping", "MuteProxyMapping", "ParamLogMapping",
           "QuantChannelDirectory", "VRCFTMapping", "WardrobeMapping", "Mapping",
           "MappingRouter"]

