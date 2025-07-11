from enum import Enum, auto
import logging
import time
from typing import Dict, Any, Optional, Callable

class SystemStateEnum(Enum):
    """系统运行状态枚举"""
    STARTING = auto()          # 启动中
    IDLE = auto()              # 空闲
    PRINTING = auto()          # 打印中
    PAUSED = auto()            # 已暂停（普通暂停）
    RUNOUT = auto()            # 断料
    FEEDING = auto()           # 送料中
    RESUMING = auto()          # 恢复打印中
    ERROR = auto()             # 错误状态
    DISCONNECTED = auto()      # 连接断开

class StateManager:
    """
    中央状态管理器
    
    负责管理和转换系统的核心状态，确保状态转换的原子性和一致性。
    """
    def __init__(self, logger: logging.Logger):
        self._state = SystemStateEnum.STARTING
        self.logger = logger
        self.state_payload: Dict[str, Any] = {}
        self._on_state_change_callback: Optional[Callable] = None

    @property
    def state(self) -> SystemStateEnum:
        """获取当前状态"""
        return self._state

    def set_state_change_callback(self, callback: Callable):
        """设置状态变化时的回调函数"""
        self._on_state_change_callback = callback

    def transition_to(self, new_state: SystemStateEnum, **kwargs):
        """
        转换到新的状态，并附带额外信息
        
        Args:
            new_state (SystemStateEnum): 目标状态
            **kwargs: 附加的状态信息 (payload)
        """
        if self._state == new_state:
            return

        old_state = self._state
        self._state = new_state
        self.state_payload = kwargs
        
        self.logger.info(f"系统状态转换: {old_state.name} -> {new_state.name}")
        
        # 如果设置了回调，则调用它
        if self._on_state_change_callback:
            try:
                self._on_state_change_callback(old_state, new_state, self.state_payload)
            except Exception as e:
                self.logger.error(f"执行状态转换回调时出错: {e}", exc_info=True)

    def get_payload(self) -> Dict[str, Any]:
        """获取当前状态的附加信息"""
        return self.state_payload

    def is_state(self, state: SystemStateEnum) -> bool:
        """检查当前是否处于某个状态"""
        return self._state == state 