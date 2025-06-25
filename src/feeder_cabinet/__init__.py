"""
自动续料系统 - Feeder Cabinet System

此模块提供了3D打印机与送料柜之间的自动化通信和控制功能，
实现了自动检测断料、请求送料、恢复打印的完整流程。

开发者: Mingda
版本: 1.0.0
"""

__version__ = "1.0.0"

# 导出主要的类和函数
from .main import FeederCabinetApp, main
from .can_communication import FeederCabinetCAN
from .klipper_monitor import KlipperMonitor
from .log_manager import LogManager

__all__ = [
    'FeederCabinetApp',
    'main',
    'FeederCabinetCAN', 
    'KlipperMonitor',
    'LogManager'
] 