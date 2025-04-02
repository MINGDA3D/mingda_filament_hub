"""
G-code宏模块 - 为Klipper提供G-code命令集成

此模块提供了一系列G-code宏，用于控制送料柜，包括：
- START_FEEDER_CABINET: 启动送料柜
- QUERY_FEEDER_CABINET: 查询送料柜状态
- CANCEL_FEEDER_CABINET: 取消送料柜操作
"""

"""
要在Klipper配置文件中添加以下内容：

[gcode_macro START_FEEDER_CABINET]
description: 请求送料柜送料
gcode:
    {% set EXTRUDER = params.EXTRUDER|default(0)|int %}
    {% set FORCE = params.FORCE|default(0)|int %}
    
    {% if printer.pause_resume.is_paused or FORCE == 1 %}
        # 只在打印暂停或强制模式下执行送料
        # 通过自定义命令后端处理
        ACTION_CALL_REMOTE_METHOD METHOD=request_feed EXTRUDER={EXTRUDER}
        RESPOND TYPE=echo MSG="已请求送料柜送料，使用挤出机 {EXTRUDER}"
    {% else %}
        RESPOND TYPE=error MSG="打印未暂停，如需强制送料请使用 FORCE=1 参数"
    {% endif %}

[gcode_macro QUERY_FEEDER_CABINET]
description: 查询送料柜状态
gcode:
    # 通过自定义命令后端处理
    ACTION_CALL_REMOTE_METHOD METHOD=query_status
    RESPOND TYPE=echo MSG="已查询送料柜状态，详情请查看日志"

[gcode_macro CANCEL_FEEDER_CABINET]
description: 取消送料柜当前操作
gcode:
    {% set EXTRUDER = params.EXTRUDER|default(0)|int %}
    
    # 通过自定义命令后端处理
    ACTION_CALL_REMOTE_METHOD METHOD=stop_feed EXTRUDER={EXTRUDER}
    RESPOND TYPE=echo MSG="已取消送料柜操作，使用挤出机 {EXTRUDER}"

[gcode_macro ENABLE_FILAMENT_RUNOUT]
description: 启用断料检测
gcode:
    # 通过自定义命令后端处理
    ACTION_CALL_REMOTE_METHOD METHOD=enable_runout_detection
    RESPOND TYPE=echo MSG="已启用断料检测"

[gcode_macro DISABLE_FILAMENT_RUNOUT]
description: 禁用断料检测
gcode:
    # 通过自定义命令后端处理
    ACTION_CALL_REMOTE_METHOD METHOD=disable_runout_detection
    RESPOND TYPE=echo MSG="已禁用断料检测"
"""

from typing import Dict, Any, Optional, Callable
import logging
import re

from .klipper_monitor import KlipperMonitor
from .can_communication import FeederCabinetCAN

class GCodeMacroHandler:
    """G-code宏处理类，用于处理远程G-code命令"""
    
    def __init__(self, klipper_monitor: KlipperMonitor, can_comm: FeederCabinetCAN):
        """
        初始化G-code宏处理类
        
        Args:
            klipper_monitor: Klipper监控器实例
            can_comm: CAN通信实例
        """
        self.logger = logging.getLogger("feeder_cabinet.gcode")
        self.klipper_monitor = klipper_monitor
        self.can_comm = can_comm
        
        # 命令映射
        self.command_map = {
            "FEED_REQUEST": self.request_feed,
            "FEED_STOP": self.stop_feed,
            "GET_FEEDER_STATUS": self.get_status,
            "SET_ACTIVE_EXTRUDER": self.set_active_extruder,
            "GET_ACTIVE_EXTRUDER": self.get_active_extruder
        }
    
    def handle_gcode_command(self, cmd: str) -> Dict[str, Any]:
        """
        处理G-code命令
        
        Args:
            cmd: G-code命令字符串
            
        Returns:
            Dict: 包含处理结果的字典
        """
        self.logger.info(f"接收到G-code命令: {cmd}")
        
        # 解析命令和参数
        command_match = re.match(r'^([A-Z_]+)(?:\s+(.*))?$', cmd.strip())
        if not command_match:
            self.logger.error(f"无效的G-code命令格式: {cmd}")
            return {"success": False, "message": "无效的命令格式"}
            
        command_name = command_match.group(1)
        params_str = command_match.group(2) or ""
        
        # 解析参数
        params = {}
        if params_str:
            param_matches = re.finditer(r'([A-Z]+)=([^\s]+)', params_str)
            for match in param_matches:
                param_name = match.group(1)
                param_value = match.group(2)
                
                # 尝试转换为数值
                try:
                    if '.' in param_value:
                        params[param_name] = float(param_value)
                    else:
                        params[param_name] = int(param_value)
                except ValueError:
                    params[param_name] = param_value
        
        # 执行命令
        if command_name in self.command_map:
            self.logger.debug(f"执行命令 {command_name} 参数: {params}")
            return self.command_map[command_name](**params)
        else:
            self.logger.error(f"未知命令: {command_name}")
            return {"success": False, "message": f"未知命令: {command_name}"}
    
    def request_feed(self, extruder: int = 0) -> Dict[str, Any]:
        """
        请求送料
        
        Args:
            extruder: 挤出机编号
            
        Returns:
            Dict: 包含操作结果的字典
        """
        self.logger.info(f"G-code命令: 请求送料，挤出机 {extruder}")
        
        result = self.can_comm.request_feed(extruder=extruder)
        if result:
            self.logger.info("送料请求已发送")
            return {"success": True, "message": "送料请求已发送"}
        else:
            self.logger.error("送料请求发送失败")
            return {"success": False, "message": "送料请求发送失败"}
    
    def stop_feed(self, extruder: int = 0) -> Dict[str, Any]:
        """
        停止送料
        
        Args:
            extruder: 挤出机编号
            
        Returns:
            Dict: 包含操作结果的字典
        """
        self.logger.info(f"G-code命令: 停止送料，挤出机 {extruder}")
        
        result = self.can_comm.stop_feed(extruder=extruder)
        if result:
            self.logger.info("停止送料请求已发送")
            return {"success": True, "message": "停止送料请求已发送"}
        else:
            self.logger.error("停止送料请求发送失败")
            return {"success": False, "message": "停止送料请求发送失败"}
    
    def get_status(self) -> Dict[str, Any]:
        """
        获取送料柜状态
        
        Returns:
            Dict: 包含状态信息的字典
        """
        self.logger.info("G-code命令: 获取送料柜状态")
        
        status = self.can_comm.get_last_status() or {}
        printer_status = self.klipper_monitor.get_printer_status() or {}
        
        return {
            "success": True, 
            "status": {
                "feeder": status,
                "printer": printer_status
            }
        }
        
    def set_active_extruder(self, extruder: int = 0) -> Dict[str, Any]:
        """
        设置当前活跃挤出机
        
        Args:
            extruder: 挤出机编号
            
        Returns:
            Dict: 包含操作结果的字典
        """
        self.logger.info(f"G-code命令: 设置活跃挤出机为 {extruder}")
        
        result = self.klipper_monitor.set_active_extruder(extruder)
        if result:
            return {"success": True, "message": f"已设置活跃挤出机为 {extruder}"}
        else:
            return {"success": False, "message": f"设置活跃挤出机失败"}
    
    def get_active_extruder(self) -> Dict[str, Any]:
        """
        获取当前活跃挤出机
        
        Returns:
            Dict: 包含活跃挤出机信息的字典
        """
        self.logger.info("G-code命令: 获取活跃挤出机")
        
        # 主动更新活跃挤出机信息
        self.klipper_monitor._update_active_extruder()
        
        active_extruder = self.klipper_monitor.active_extruder
        active_name = self.klipper_monitor.toolhead_info.get('active_extruder', '未知')
        
        return {
            "success": True,
            "active_extruder": active_extruder,
            "active_extruder_name": active_name
        }

    def register_methods(self, register_method_func: Callable):
        """
        注册远程方法
        
        Args:
            register_method_func: 注册方法的回调函数
        """
        # 定义要注册的方法
        methods = {
            "request_feed": self.request_feed,
            "stop_feed": self.stop_feed,
            "query_status": self.query_status,
            "enable_runout_detection": self.enable_runout_detection,
            "disable_runout_detection": self.disable_runout_detection
        }
        
        # 注册所有方法
        for name, method in methods.items():
            register_method_func(name, method)
            self.logger.info(f"已注册远程方法: {name}")
    
    def query_status(self) -> Dict[str, Any]:
        """
        查询状态
        
        Returns:
            Dict: 包含状态信息的字典
        """
        self.logger.info("G-code命令: 查询状态")
        
        # 查询送料柜状态
        can_result = self.can_comm.query_status()
        if not can_result:
            self.logger.error("状态查询发送失败")
            return {"success": False, "message": "状态查询发送失败"}
        
        # 获取最新状态
        status = self.can_comm.get_last_status()
        printer_status = self.klipper_monitor.get_printer_status()
        
        # 格式化状态信息
        result = {
            "success": True,
            "message": "状态查询成功",
            "feeder_cabinet": self._format_status(status),
            "printer": {
                "state": printer_status.get('printer', {}).get('printer_state', 'unknown')
            }
        }
        
        self.logger.info(f"状态查询结果: {result}")
        return result
    
    def _format_status(self, status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        格式化状态信息
        
        Args:
            status: 原始状态数据
            
        Returns:
            Dict: 格式化后的状态数据
        """
        if not status:
            return {"status": "unknown", "progress": 0, "error": "none"}
            
        # 状态码映射
        status_map = {
            self.can_comm.STATUS_IDLE: "idle",
            self.can_comm.STATUS_READY: "ready",
            self.can_comm.STATUS_FEEDING: "feeding",
            self.can_comm.STATUS_COMPLETE: "complete",
            self.can_comm.STATUS_ERROR: "error"
        }
        
        # 错误码映射
        error_map = {
            self.can_comm.ERROR_NONE: "none",
            self.can_comm.ERROR_MECHANICAL: "mechanical",
            self.can_comm.ERROR_MATERIAL_MISSING: "material_missing",
            self.can_comm.ERROR_OTHER: "other",
            self.can_comm.ERROR_KLIPPER: "klipper_error",
            self.can_comm.ERROR_MOONRAKER: "moonraker_error",
            self.can_comm.ERROR_COMMUNICATION: "communication_error"
        }
        
        # 格式化状态
        return {
            "status": status_map.get(status.get('status'), "unknown"),
            "progress": status.get('progress', 0),
            "error": error_map.get(status.get('error_code'), "unknown")
        }
    
    def enable_runout_detection(self, sensor_pin: str = None) -> Dict[str, Any]:
        """
        启用断料检测
        
        Args:
            sensor_pin: 断料传感器引脚
            
        Returns:
            Dict: 包含操作结果的字典
        """
        self.logger.info(f"G-code命令: 启用断料检测 sensor_pin={sensor_pin}")
        
        self.klipper_monitor.enable_filament_runout_detection(sensor_pin)
        return {"success": True, "message": "已启用断料检测"}
    
    def disable_runout_detection(self) -> Dict[str, Any]:
        """
        禁用断料检测
        
        Returns:
            Dict: 包含操作结果的字典
        """
        self.logger.info("G-code命令: 禁用断料检测")
        
        self.klipper_monitor.disable_filament_runout_detection()
        return {"success": True, "message": "已禁用断料检测"} 