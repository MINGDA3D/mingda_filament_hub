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
import websocket
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
        self.ws_url = moonraker_url.replace("http://", "ws://") + "/websocket"
        
        # WebSocket相关
        self.ws = None
        self.ws_thread = None
        self.ws_connected = False
        self.next_request_id = 1
        self.reconnect_count = 0
        self.max_reconnect_attempts = 10
        self.reconnect_interval = 5
        self.auto_reconnect = True
        self.reconnect_thread = None
        
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
            self.reconnect_count = 0
            return self._establish_connection()
        except Exception as e:
            self.logger.error(f"连接Klipper/Moonraker失败: {str(e)}")
            return False
    
    def _establish_connection(self) -> bool:
        """
        建立WebSocket连接
        
        Returns:
            bool: 连接是否成功
        """
        # 如果已有连接，先关闭
        if self.ws:
            self.ws.close()
            if self.ws_thread and self.ws_thread.is_alive():
                self.ws_thread.join(timeout=1.0)
        
        # 初始化WebSocket连接
        self.logger.info(f"正在连接到WebSocket: {self.ws_url}")
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close
        )
        
        # 启动WebSocket线程
        self.ws_thread = threading.Thread(
            target=self.ws.run_forever,
            daemon=True
        )
        self.ws_thread.start()
        
        # 等待连接建立
        timeout = 5
        start_time = time.time()
        while not self.ws_connected and (time.time() - start_time) < timeout:
            time.sleep(0.1)
            
        if not self.ws_connected:
            self.logger.error("WebSocket连接超时")
            return False
            
        self.logger.info(f"成功连接到Klipper/Moonraker WebSocket: {self.ws_url}")
        return True
    
    def _on_ws_open(self, ws):
        """WebSocket连接打开后的回调"""
        self.logger.info("WebSocket连接已打开")
        self.ws_connected = True
        self.reconnect_count = 0  # 重置重连计数
        
        # 订阅打印机对象
        self._subscribe_objects()
    
    def _on_ws_message(self, ws, message):
        """处理WebSocket接收到的消息"""
        try:
            data = json.loads(message)
            
            # 处理状态更新通知
            if 'method' in data and data['method'] == 'notify_status_update':
                self._handle_status_update(data['params'][0])
            
            # 处理查询响应
            elif 'result' in data and 'status' in data.get('result', {}):
                self._handle_status_update(data['result'].get('status', {}))
                
            # 其他消息类型可以根据需要添加处理
                
        except Exception as e:
            self.logger.error(f"处理WebSocket消息时发生错误: {str(e)}")
    
    def _on_ws_error(self, ws, error):
        """处理WebSocket错误"""
        self.logger.error(f"WebSocket错误: {str(error)}")
    
    def _on_ws_close(self, ws, close_status_code, close_msg):
        """处理WebSocket连接关闭"""
        self.logger.info(f"WebSocket连接关闭: {close_status_code} - {close_msg}")
        self.ws_connected = False
        
        # 如果启用了自动重连，则尝试重连
        if self.auto_reconnect:
            self._schedule_reconnect()
    
    def _schedule_reconnect(self):
        """安排重连任务"""
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            return  # 已经有一个重连线程在运行
            
        if self.reconnect_count >= self.max_reconnect_attempts:
            self.logger.error(f"重连尝试达到最大次数 ({self.max_reconnect_attempts})，停止重连")
            return
            
        self.reconnect_count += 1
        backoff_time = min(30, self.reconnect_interval * (2 ** (self.reconnect_count - 1)))  # 指数退避策略
        
        self.logger.info(f"计划在 {backoff_time} 秒后进行第 {self.reconnect_count} 次重连")
        self.reconnect_thread = threading.Thread(
            target=self._delayed_reconnect,
            args=(backoff_time,),
            daemon=True
        )
        self.reconnect_thread.start()
    
    def _delayed_reconnect(self, delay):
        """延迟重连"""
        time.sleep(delay)
        self.logger.info(f"正在尝试第 {self.reconnect_count} 次重连...")
        
        if self._establish_connection():
            self.logger.info("重连成功")
        else:
            self.logger.error("重连失败")
            # 如果仍启用自动重连，则安排下一次重连
            if self.auto_reconnect:
                self._schedule_reconnect()
    
    def _handle_status_update(self, status):
        """处理状态更新数据"""
        # 更新状态变量
        if 'print_stats' in status:
            self.print_stats = status['print_stats']
            new_state = self.print_stats.get('state')
            if new_state and new_state != self.printer_state:
                self.printer_state = new_state
                self.logger.info(f"打印机状态变化: {self.printer_state}")
                
                # 根据状态映射发送相应命令
                if self.printer_state in self.state_map:
                    cmd = self.state_map[self.printer_state]
                    self.can_comm.send_message(cmd)
                    self.logger.debug(f"发送状态变化命令: {hex(cmd)}")
                
        if 'toolhead' in status:
            self.toolhead_info.update(status['toolhead'])
            
        if 'extruder' in status:
            self.extruder_info.update(status['extruder'])
        
        # 检查断料状态
        if self.runout_detection_enabled:
            self._check_filament_status()
        
        # 检查是否可以恢复打印
        if self.feed_resume_pending:
            self._check_resume_conditions()
        
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
        if not self.ws_connected:
            self.logger.error("WebSocket未连接，无法订阅对象")
            return
            
        try:
            subscribe_request = {
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
                "id": self._get_next_request_id()
            }
            
            self.ws.send(json.dumps(subscribe_request))
            self.logger.info("已发送WebSocket订阅请求")
        except Exception as e:
            self.logger.error(f"订阅打印机对象时发生错误: {str(e)}")
    
    def _get_next_request_id(self):
        """获取下一个请求ID"""
        request_id = self.next_request_id
        self.next_request_id += 1
        return request_id
    
    def _send_gcode(self, command: str) -> bool:
        """
        发送G-code命令到Klipper
        
        Args:
            command: G-code命令
            
        Returns:
            bool: 发送是否成功
        """
        if not self.ws_connected:
            self.logger.error("WebSocket未连接，无法发送G-code")
            return False
            
        try:
            gcode_request = {
                "jsonrpc": "2.0",
                "method": "printer.gcode.script",
                "params": {
                    "script": command
                },
                "id": self._get_next_request_id()
            }
            
            self.ws.send(json.dumps(gcode_request))
            self.logger.info(f"成功发送G-code: {command}")
            return True
        except Exception as e:
            self.logger.error(f"发送G-code时发生错误: {str(e)}")
            return False
    
    def update_printer_state(self) -> Dict[str, Any]:
        """
        更新打印机状态
        
        Returns:
            Dict: 当前打印机状态
        """
        if not self.ws_connected:
            self.logger.error("WebSocket未连接，无法更新打印机状态")
            return {}
            
        try:
            # 查询打印机对象
            query_request = {
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
                "id": self._get_next_request_id()
            }
            
            self.ws.send(json.dumps(query_request))
            
            # 注意：查询结果将通过WebSocket回调处理
            # 这里直接返回当前状态
            state_info = {
                'printer_state': self.printer_state,
                'print_stats': self.print_stats,
                'toolhead': self.toolhead_info,
                'extruder': self.extruder_info
            }
            
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
        # 注意：使用WebSocket后不再需要轮询
        # 这个方法保留用于兼容性，但实际上不再需要单独的监控线程
        if self.is_monitoring:
            self.logger.info("监控已经在运行中")
            return
            
        self.is_monitoring = True
        self.logger.info("开始通过WebSocket监控打印机状态")
    
    def stop_monitoring(self):
        """停止监控打印机状态"""
        self.is_monitoring = False
        self.logger.info("停止监控打印机状态")
    
    def disconnect(self):
        """断开与Klipper/Moonraker的连接"""
        self.auto_reconnect = False  # 禁用自动重连
        if self.ws:
            self.ws.close()
            self.logger.info("WebSocket连接已关闭")
        
        # 等待重连线程结束
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            self.reconnect_thread.join(timeout=1.0)
    
    def enable_auto_reconnect(self, enable=True, max_attempts=10, interval=5):
        """
        启用或禁用自动重连
        
        Args:
            enable: 是否启用自动重连
            max_attempts: 最大重连尝试次数
            interval: 初始重连间隔（秒）
        """
        self.auto_reconnect = enable
        self.max_reconnect_attempts = max_attempts
        self.reconnect_interval = interval
        self.logger.info(f"自动重连{'启用' if enable else '禁用'}, 最大尝试次数: {max_attempts}, 初始间隔: {interval}秒")
    
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
            # 如果打印机处于打印状态，检查是否断料
            if self.printer_state == "printing":
                # 检查是否有断料传感器信号
                has_runout = self._check_runout_sensor()
                
                if has_runout and not self.feed_requested:
                    self.logger.info("检测到断料，准备暂停打印并补料")
                    self._handle_filament_runout()
            
            # 如果打印机处于暂停状态，可能是由于断料引起的
            elif self.printer_state == "paused" and not self.feed_requested:
                self.logger.info("检测到打印已暂停，可能是因为断料")
                self._handle_filament_runout()
                
        except Exception as e:
            self.logger.error(f"检查断料状态时发生错误: {str(e)}")
    
    def _check_runout_sensor(self) -> bool:
        """
        检查断料传感器状态
        
        Returns:
            bool: 是否断料
        """
        # 如果有配置传感器引脚，通过GCODE获取传感器状态
        if self.filament_sensor_pin:
            try:
                # 发送查询传感器状态的GCODE
                query_cmd = f"QUERY_FILAMENT_SENSOR SENSOR=filament_sensor"
                self._send_gcode(query_cmd)
                
                # 注意：这里依赖于Klipper的响应会在WebSocket回调中处理
                # 实际实现可能需要添加更复杂的状态跟踪
                
                # 简化实现:直接检查打印机状态中是否有断料指示
                return self.print_stats.get("filament_detected", True) == False
            except Exception as e:
                self.logger.error(f"查询断料传感器状态失败: {str(e)}")
                return False
        else:
            # 如果没有配置传感器引脚，检查打印机状态中是否有断料指示
            return self.print_stats.get("filament_detected", True) == False
    
    def _handle_filament_runout(self):
        """处理断料事件"""
        self.logger.info("处理断料事件开始")
        
        # 步骤1: 暂停打印（如果还没暂停）
        if self.printer_state == "printing":
            self.logger.info("暂停打印")
            self.pause_print()
            # 等待打印机进入暂停状态
            timeout = 10  # 设置超时，防止无限等待
            start_time = time.time()
            while self.printer_state != "paused" and time.time() - start_time < timeout:
                time.sleep(0.5)
        
        # 步骤2: 保存打印状态（Klipper会自动处理）
        self.logger.info("打印状态已保存")
        
        # 步骤3: 发送补料请求到送料柜
        self.logger.info("发送补料请求到送料柜")
        if self.can_comm.request_feed():
            self.feed_requested = True
            self.feed_resume_pending = True
            self.logger.info("已发送补料请求")
        else:
            self.logger.error("发送补料请求失败")
            # 尝试重新发送补料请求
            retry_count = 3
            for i in range(retry_count):
                self.logger.info(f"尝试重新发送补料请求 ({i+1}/{retry_count})")
                time.sleep(1)
                if self.can_comm.request_feed():
                    self.feed_requested = True
                    self.feed_resume_pending = True
                    self.logger.info("重新发送补料请求成功")
                    break
            
            if not self.feed_requested:
                self.logger.error(f"经过{retry_count}次尝试后，仍无法发送补料请求")
                # 此处可添加通知用户的代码
    
    def _check_resume_conditions(self):
        """检查是否可以恢复打印"""
        if not self.feed_resume_pending:
            return
            
        try:
            # 获取送料柜最新状态
            status = self.can_comm.get_last_status()
            
            if not status:
                return
                
            # 检查送料柜状态
            status_code = status.get('status')
            error_code = status.get('error_code')
            
            if status_code == self.can_comm.STATUS_COMPLETE:
                # 送料完成，准备恢复打印
                self.logger.info("检测到送料完成，准备恢复打印")
                
                # 检查是否有新耗材（简化实现，实际可能需要更复杂的传感器检测）
                if self._check_new_filament_loaded():
                    self.logger.info("检测到新耗材已装载，恢复打印")
                    self.resume_print()
                else:
                    self.logger.warning("送料完成但未检测到新耗材，等待")
                    
            elif status_code == self.can_comm.STATUS_ERROR:
                # 送料出错
                error_msg = self._get_error_message(error_code)
                self.logger.error(f"送料过程出错: {error_msg}")
                # 此处可添加通知用户的代码
                
            elif status_code == self.can_comm.STATUS_FEEDING:
                # 送料中，等待
                progress = status.get('progress', 0)
                self.logger.debug(f"送料进行中，进度: {progress}%")
                
        except Exception as e:
            self.logger.error(f"检查恢复条件时发生错误: {str(e)}")
    
    def _check_new_filament_loaded(self) -> bool:
        """
        检查新耗材是否已经装载
        
        Returns:
            bool: 新耗材是否已装载
        """
        # 如果有配置传感器引脚，通过GCODE获取传感器状态
        if self.filament_sensor_pin:
            try:
                # 发送查询传感器状态的GCODE
                query_cmd = f"QUERY_FILAMENT_SENSOR SENSOR=filament_sensor"
                self._send_gcode(query_cmd)
                
                # 简化实现:检查打印机状态中是否有耗材检测
                return self.print_stats.get("filament_detected", False) == True
            except Exception as e:
                self.logger.error(f"查询耗材传感器状态失败: {str(e)}")
                return False
        else:
            # 如果没有配置传感器引脚，假设送料柜完成后耗材已装载
            return True
    
    def _get_error_message(self, error_code: int) -> str:
        """
        根据错误码获取错误消息
        
        Args:
            error_code: 错误码
            
        Returns:
            str: 错误消息
        """
        error_messages = {
            self.can_comm.ERROR_NONE: "无错误",
            self.can_comm.ERROR_MECHANICAL: "机械错误",
            self.can_comm.ERROR_MATERIAL_MISSING: "材料缺失",
            self.can_comm.ERROR_OTHER: "其他错误",
            self.can_comm.ERROR_KLIPPER: "Klipper错误",
            self.can_comm.ERROR_MOONRAKER: "Moonraker错误",
            self.can_comm.ERROR_COMMUNICATION: "通信错误"
        }
        
        return error_messages.get(error_code, f"未知错误 ({error_code})")
    
    def resume_print(self):
        """恢复打印"""
        if self.feed_requested:
            self.feed_requested = False
            self.feed_resume_pending = False
            
        # 恢复前准备工作
        self._prepare_for_resume()
            
        result = self._send_gcode(self.resume_cmd)
        if result:
            self.logger.info("打印已恢复")
        return result
    
    def _prepare_for_resume(self):
        """
        恢复打印前的准备工作
        """
        try:
            # 如果需要，可以在此处添加恢复前的预热、挤出等操作
            
            # 示例：确保热端达到打印温度
            if self.extruder_info.get('temperature', 0) < self.extruder_info.get('target', 0) - 5:
                self.logger.info("等待热端达到目标温度")
                # 实际项目中可以添加等待逻辑或通知用户手动确认
            
            # 示例：操作完成后进行少量挤出，确保耗材正常
            self._send_gcode("G91")  # 设置为相对坐标
            self._send_gcode("G1 E10 F100")  # 慢速挤出10mm
            self._send_gcode("G90")  # 恢复为绝对坐标
            
        except Exception as e:
            self.logger.error(f"恢复前准备工作时发生错误: {str(e)}")
    
    def pause_print(self):
        """暂停打印"""
        result = self._send_gcode(self.pause_cmd)
        if result:
            self.logger.info("打印已暂停")
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
        # 获取服务器信息（仍使用HTTP，因为这只在初始化时调用一次）
        server_info = self._get_server_info() or {}
        
        # 当前打印机状态（从WebSocket更新的状态获取）
        printer_state = {
            'printer_state': self.printer_state,
            'print_stats': self.print_stats,
            'toolhead': self.toolhead_info,
            'extruder': self.extruder_info
        }
        
        # 组合状态信息
        status = {
            'server': server_info,
            'printer': printer_state
        }
        
        return status 