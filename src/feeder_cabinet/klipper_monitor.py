"""
Klipper监控模块 - 管理与Klipper的通信

此模块提供与Klipper的通信功能，包括：
- 获取打印机状态
- 监控打印过程
- 处理断料检测
- 暂停和恢复打印
"""

import logging
import threading
import time
import json
import requests
from typing import Optional, Dict, Any, List, Callable

from .can_communication import FeederCabinetCAN

class KlipperMonitor:
    """Klipper监控类，负责与Klipper通信并获取状态"""
    
    def __init__(self, can_comm, moonraker_url: str = "http://localhost:7125"):
        """
        初始化Klipper监控器
        
        Args:
            can_comm: CAN通信实例
            moonraker_url: Moonraker API URL
        """
        self.logger = logging.getLogger("feeder_cabinet.klipper")
        self.can_comm = can_comm
        self.moonraker_url = moonraker_url
        
        # 状态变量
        self.printer_state = "unknown"
        self.print_stats = {}
        self.toolhead_info = {}
        self.extruder_info = {}
        self.is_monitoring = False
        self.monitoring_thread = None
        
        # 状态映射到CAN命令
        self.state_map = {
            "ready": self.can_comm.CMD_PRINTER_IDLE,
            "printing": self.can_comm.CMD_PRINTING,
            "paused": self.can_comm.CMD_PRINT_PAUSE,
            "complete": self.can_comm.CMD_PRINT_COMPLETE,
            "cancelled": self.can_comm.CMD_PRINT_CANCEL,
            "error": self.can_comm.CMD_PRINTER_ERROR,
            "shutdown": self.can_comm.CMD_PRINTER_ERROR
        }
        
        # 断料检测相关
        self.filament_present = True
        self.filament_sensor_pin = None
        self.runout_detection_enabled = False
        self.feed_requested = False
        self.feed_resume_pending = False
        
        # Gcode命令模板
        self.pause_cmd = "PAUSE"
        self.resume_cmd = "RESUME"
        self.cancel_cmd = "CANCEL_PRINT"
        
        # 回调函数
        self.status_callbacks = []
        
    def connect(self) -> bool:
        """
        连接到Klipper/Moonraker
        
        Returns:
            bool: 连接是否成功
        """
        try:
            # 检查Moonraker连接
            server_info = self._get_server_info()
            if not server_info or not server_info.get('klippy_connected', False):
                self.logger.error("无法连接到Klipper/Moonraker")
                return False
                
            self.logger.info(f"成功连接到Klipper/Moonraker，版本: {server_info.get('moonraker_version', 'unknown')}")
            
            # 订阅打印机对象
            self._subscribe_objects()
            
            # 获取初始状态
            self.update_printer_state()
            
            return True
        except Exception as e:
            self.logger.error(f"连接Klipper/Moonraker失败: {str(e)}")
            return False
    
    def _get_server_info(self) -> Optional[dict]:
        """获取Moonraker服务器信息"""
        try:
            response = requests.get(f"{self.moonraker_url}/server/info", timeout=5)
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"获取服务器信息失败，状态码: {response.status_code}")
                return None
        except Exception as e:
            self.logger.error(f"获取服务器信息时发生错误: {str(e)}")
            return None
    
    def _subscribe_objects(self):
        """订阅Klipper对象状态"""
        try:
            data = {
                "jsonrpc": "2.0",
                "method": "printer.objects.subscribe",
                "params": {
                    "objects": {
                        "print_stats": None,
                        "toolhead": None,
                        "extruder": None,
                        "virtual_sdcard": None,
                        "pause_resume": None
                    }
                },
                "id": 5656
            }
            
            response = requests.post(f"{self.moonraker_url}/printer/objects/subscribe", json=data, timeout=5)
            if response.status_code != 200:
                self.logger.error(f"订阅打印机对象失败，状态码: {response.status_code}")
        except Exception as e:
            self.logger.error(f"订阅打印机对象时发生错误: {str(e)}")
    
    def _send_gcode(self, command: str) -> bool:
        """
        发送G-code命令到Klipper
        
        Args:
            command: G-code命令
            
        Returns:
            bool: 发送是否成功
        """
        try:
            data = {
                "jsonrpc": "2.0",
                "method": "printer.gcode.script",
                "params": {
                    "script": command
                },
                "id": 7823
            }
            
            response = requests.post(f"{self.moonraker_url}/printer/gcode/script", json=data, timeout=5)
            if response.status_code == 200:
                self.logger.info(f"成功发送G-code: {command}")
                return True
            else:
                self.logger.error(f"发送G-code失败，状态码: {response.status_code}")
                return False
        except Exception as e:
            self.logger.error(f"发送G-code时发生错误: {str(e)}")
            return False
    
    def update_printer_state(self) -> Dict[str, Any]:
        """
        更新打印机状态
        
        Returns:
            Dict: 当前打印机状态
        """
        try:
            # 查询打印机对象
            data = {
                "jsonrpc": "2.0",
                "method": "printer.objects.query",
                "params": {
                    "objects": {
                        "print_stats": None,
                        "toolhead": None,
                        "extruder": None,
                        "virtual_sdcard": None,
                        "pause_resume": None
                    }
                },
                "id": 4542
            }
            
            response = requests.post(f"{self.moonraker_url}/printer/objects/query", json=data, timeout=5)
            if response.status_code != 200:
                self.logger.error(f"查询打印机对象失败，状态码: {response.status_code}")
                return {}
            
            resp_data = response.json()
            status = resp_data.get('result', {}).get('status', {})
            
            # 更新状态变量
            if 'print_stats' in status:
                self.print_stats = status['print_stats']
                self.printer_state = self.print_stats.get('state', 'unknown')
                
            if 'toolhead' in status:
                self.toolhead_info = status['toolhead']
                
            if 'extruder' in status:
                self.extruder_info = status['extruder']
            
            # 调用状态回调
            state_info = {
                'printer_state': self.printer_state,
                'print_stats': self.print_stats,
                'toolhead': self.toolhead_info,
                'extruder': self.extruder_info
            }
            
            for callback in self.status_callbacks:
                try:
                    callback(state_info)
                except Exception as e:
                    self.logger.error(f"执行状态回调时发生错误: {str(e)}")
            
            return state_info
        except Exception as e:
            self.logger.error(f"获取打印机状态时发生错误: {str(e)}")
            return {}
    
    def start_monitoring(self, interval: float = 5.0):
        """
        开始监控打印机状态
        
        Args:
            interval: 状态更新间隔（秒）
        """
        if self.is_monitoring:
            self.logger.info("监控已经在运行中")
            return
            
        self.is_monitoring = True
        self.monitoring_thread = threading.Thread(
            target=self._monitoring_loop,
            args=(interval,),
            daemon=True
        )
        self.monitoring_thread.start()
        self.logger.info(f"开始监控打印机状态，间隔: {interval}秒")
    
    def stop_monitoring(self):
        """停止监控打印机状态"""
        self.is_monitoring = False
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=2.0)
        self.logger.info("停止监控打印机状态")
    
    def _monitoring_loop(self, interval: float):
        """
        监控循环，定期获取打印机状态并处理
        
        Args:
            interval: 状态更新间隔（秒）
        """
        last_state = None
        
        while self.is_monitoring:
            try:
                # 获取当前状态
                self.update_printer_state()
                
                # 如果状态变化，发送状态给送料柜
                if self.printer_state != last_state:
                    self.logger.info(f"打印机状态变化: {last_state} -> {self.printer_state}")
                    last_state = self.printer_state
                    
                    # 根据状态映射发送相应命令
                    if self.printer_state in self.state_map:
                        cmd = self.state_map[self.printer_state]
                        self.can_comm.send_message(cmd)
                        self.logger.debug(f"发送状态变化命令: {hex(cmd)}")
                    
                # 检查断料状态
                if self.runout_detection_enabled:
                    self._check_filament_status()
                
                # 检查是否可以恢复打印
                if self.feed_resume_pending:
                    self._check_resume_conditions()
                
            except Exception as e:
                self.logger.error(f"监控循环中发生错误: {str(e)}")
            
            # 等待下一次更新
            time.sleep(interval)
    
    def enable_filament_runout_detection(self, sensor_pin: str = None):
        """
        启用断料检测
        
        Args:
            sensor_pin: 断料传感器引脚
        """
        self.runout_detection_enabled = True
        self.filament_sensor_pin = sensor_pin
        self.logger.info(f"启用断料检测，传感器引脚: {sensor_pin if sensor_pin else '使用Klipper内置检测'}")
    
    def disable_filament_runout_detection(self):
        """禁用断料检测"""
        self.runout_detection_enabled = False
        self.logger.info("禁用断料检测")
    
    def _check_filament_status(self):
        """检查断料状态"""
        try:
            # 这里可以添加与Klipper传感器通信的代码
            # 目前简化为通过打印状态判断
            if self.printer_state == "paused" and not self.feed_requested:
                self.logger.info("检测到打印暂停，可能是因为断料")
                self._handle_filament_runout()
        except Exception as e:
            self.logger.error(f"检查断料状态时发生错误: {str(e)}")
    
    def _handle_filament_runout(self):
        """处理断料事件"""
        self.logger.info("处理断料事件")
        
        # 发送补料请求
        if self.can_comm.request_feed():
            self.feed_requested = True
            self.logger.info("已发送补料请求")
        else:
            self.logger.error("发送补料请求失败")
    
    def _check_resume_conditions(self):
        """检查是否可以恢复打印"""
        # 这里可以添加传感器检测，确认是否有新耗材
        # 简化实现为检查送料柜状态
        status = self.can_comm.get_last_status()
        if status and status.get('status') == self.can_comm.STATUS_COMPLETE:
            self.logger.info("检测到送料完成，准备恢复打印")
            self.resume_print()
    
    def pause_print(self):
        """暂停打印"""
        result = self._send_gcode(self.pause_cmd)
        if result:
            self.logger.info("打印已暂停")
        return result
    
    def resume_print(self):
        """恢复打印"""
        if self.feed_requested:
            self.feed_requested = False
            self.feed_resume_pending = False
            
        result = self._send_gcode(self.resume_cmd)
        if result:
            self.logger.info("打印已恢复")
        return result
    
    def cancel_print(self):
        """取消打印"""
        if self.feed_requested:
            self.feed_requested = False
            self.feed_resume_pending = False
            # 通知送料柜停止送料
            self.can_comm.stop_feed()
            
        result = self._send_gcode(self.cancel_cmd)
        if result:
            self.logger.info("打印已取消")
        return result
    
    def register_status_callback(self, callback: Callable):
        """
        注册状态回调函数
        
        Args:
            callback: 回调函数，接收状态字典作为参数
        """
        if callback not in self.status_callbacks:
            self.status_callbacks.append(callback)
            
    def unregister_status_callback(self, callback: Callable):
        """
        取消注册状态回调函数
        
        Args:
            callback: 回调函数
        """
        if callback in self.status_callbacks:
            self.status_callbacks.remove(callback)
            
    def execute_gcode(self, command: str) -> bool:
        """
        执行任意G-code命令
        
        Args:
            command: G-code命令
            
        Returns:
            bool: 执行是否成功
        """
        return self._send_gcode(command)
        
    def get_printer_status(self) -> Dict[str, Any]:
        """
        获取当前打印机状态
        
        Returns:
            Dict: 打印机状态信息
        """
        # 获取服务器信息
        server_info = self._get_server_info() or {}
        
        # 更新打印机状态
        printer_state = self.update_printer_state()
        
        # 组合状态信息
        status = {
            'server': server_info,
            'printer': printer_state
        }
        
        return status 