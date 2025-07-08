"""
Klipper监控模块 - 管理与Klipper的通信 (Asyncio Version)

此模块提供与Klipper的通信功能，包括：
- 获取打印机状态
- 监控打印过程
- 处理断料检测
- 暂停和恢复打印
"""

import logging
import asyncio
import json
import aiohttp
import websockets
from typing import Optional, Dict, Any, List, Callable, Coroutine

from .can_communication import FeederCabinetCAN

class KlipperMonitor:
    """Klipper监控类，负责与Klipper通信并获取状态"""
    
    def __init__(self, can_comm: FeederCabinetCAN, moonraker_url: str = "http://localhost:7125", extruder_config: Dict = None):
        """
        初始化Klipper监控器
        
        Args:
            can_comm: CAN通信实例
            moonraker_url: Moonraker API URL
            extruder_config: 挤出机配置
        """
        self.logger = logging.getLogger("feeder_cabinet.klipper")
        self.can_comm = can_comm
        self.moonraker_url = moonraker_url
        self.ws_url = moonraker_url.replace("http://", "ws://") + "/websocket"
        
        # WebSocket和HTTP相关
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.ws_connected = False
        self.next_request_id = 1
        self.auto_reconnect = True
        self.reconnect_interval = 5
        self.connection_lock = asyncio.Lock()
        self.ws_task: Optional[asyncio.Task] = None
        
        # 状态变量 (将被状态机替代，暂时保留用于兼容)
        self.printer_state = "unknown"
        self.print_stats = {}
        self.toolhead_info = {}
        self.extruder_info = {}
        self.extruder1_info = {}
        self.filament_sensors_status: Dict[str, bool] = {}
        self.active_extruder = 0

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
        
        # 断料检测相关 (逻辑将移出)
        self.filament_sensor_names = ["Filament_Sensor0", "Filament_Sensor1"]
        self.filament_sensor_objects = ["filament_switch_sensor Filament_Sensor0", "filament_switch_sensor Filament_Sensor1"]
        self.runout_detection_enabled = False
        
        # 挤出机配置
        self.extruder_config = extruder_config or {}
        self.extruder_to_buffer = self.extruder_config.get('mapping', {0: 0, 1: 1})
        
        # Gcode命令模板
        self.pause_cmd = "PAUSE"
        self.resume_cmd = "RESUME"
        self.cancel_cmd = "CANCEL_PRINT"
        
        # 回调函数
        self.status_callbacks: List[Callable[[Dict], Coroutine]] = []

    async def connect(self) -> bool:
        """连接到Klipper/Moonraker"""
        if self.ws_connected:
            return True
        
        async with self.connection_lock:
            if self.ws_connected:
                return True
            
            self.logger.info(f"正在连接到WebSocket: {self.ws_url}")
            try:
                self.ws = await websockets.connect(self.ws_url, ping_interval=10, ping_timeout=5)
                self.ws_connected = True
                self.logger.info("WebSocket连接成功")
                
                # 启动后台任务
                self.ws_task = asyncio.create_task(self._ws_handler())
                
                await self._subscribe_objects()
                return True
            except (websockets.exceptions.InvalidURI, websockets.exceptions.WebSocketException, OSError) as e:
                self.logger.error(f"连接WebSocket失败: {e}")
                self.ws_connected = False
                return False

    async def _ws_handler(self):
        """处理WebSocket连接的读取和重连"""
        while self.auto_reconnect:
            try:
                async for message in self.ws:
                    try:
                        await self._process_ws_message(message)
                    except Exception as e:
                        self.logger.error(f"处理WebSocket消息时发生错误: {e}", exc_info=True)
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(f"WebSocket连接关闭: {e}")
            except asyncio.CancelledError:
                self.logger.info("WebSocket处理任务被取消")
                break
            except Exception as e:
                self.logger.error(f"WebSocket处理循环中发生未知错误: {e}", exc_info=True)

            self.ws_connected = False
            if self.auto_reconnect:
                self.logger.info(f"将在 {self.reconnect_interval} 秒后尝试重连...")
                await asyncio.sleep(self.reconnect_interval)
                await self.connect()
        self.logger.info("WebSocket处理任务已结束")
    
    async def _process_ws_message(self, message):
        """处理接收到的WebSocket消息"""
        try:
            data = json.loads(message)
            
            if 'method' in data and data['method'] == 'notify_status_update':
                status_data = data['params'][0]
                await self._handle_status_update(status_data)
            
            elif 'result' in data and 'status' in data['result']:
                status_data = data['result']['status']
                await self._handle_status_update(status_data)
                
        except json.JSONDecodeError:
            self.logger.warning(f"无法解析WebSocket消息: {message}")
        except Exception as e:
            self.logger.error(f"处理WebSocket消息时发生内部错误: {e}", exc_info=True)

    async def _handle_status_update(self, status):
        """处理状态更新数据, 并通过回调上报"""
        # 调用状态回调，将原始数据上报
        for callback in self.status_callbacks:
            asyncio.create_task(callback(status))

        # --- 以下为内部状态缓存，仅为get_printer_status提供快照 ---
        if 'print_stats' in status:
            self.print_stats.update(status['print_stats'])
            new_state = self.print_stats.get('state')
            if new_state and new_state != self.printer_state:
                self.printer_state = new_state
                self.logger.info(f"打印机状态变化: {self.printer_state}")
                if self.printer_state in self.state_map:
                    cmd = self.state_map[self.printer_state]
                    asyncio.create_task(self.can_comm.send_message(cmd)) # 使用create_task避免阻塞
        
        if 'toolhead' in status:
            self.toolhead_info.update(status['toolhead'])
        
        if 'extruder' in status:
            self.extruder_info.update(status['extruder'])

        if 'extruder1' in status:
            self.extruder1_info.update(status['extruder1'])

        for i, sensor_obj in enumerate(self.filament_sensor_objects):
            if sensor_obj in status and "filament_detected" in status[sensor_obj]:
                sensor_name = self.filament_sensor_names[i]
                new_state = status[sensor_obj]["filament_detected"]
                if self.filament_sensors_status.get(sensor_name) != new_state:
                    self.logger.info(f"断料传感器 {sensor_name} 状态变化: {'有料' if new_state else '无料'}")
                self.filament_sensors_status[sensor_name] = new_state
    
    async def _subscribe_objects(self):
        """订阅Klipper对象状态"""
        if not self.ws_connected:
            self.logger.error("WebSocket未连接，无法订阅对象")
            return
            
        try:
            objects_dict = {
                "print_stats": None, "toolhead": ["extruder", "position"],
                "extruder": None, "extruder1": None,
                "virtual_sdcard": None, "pause_resume": None,
            }
            for sensor_obj in self.filament_sensor_objects:
                objects_dict[sensor_obj] = None
                
            subscribe_request = {
                "jsonrpc": "2.0", "method": "printer.objects.subscribe",
                "params": {"objects": objects_dict},
                "id": self._get_next_request_id()
            }
            await self.ws.send(json.dumps(subscribe_request))
            self.logger.info("已发送WebSocket订阅请求")
        except websockets.exceptions.ConnectionClosed:
            self.logger.error("订阅对象时连接已关闭")
        except Exception as e:
            self.logger.error(f"订阅打印机对象时发生错误: {str(e)}")
    
    def _get_next_request_id(self):
        """获取下一个请求ID"""
        self.next_request_id += 1
        return self.next_request_id
    
    async def _send_gcode(self, command: str) -> bool:
        """发送G-code命令到Klipper"""
        if not self.ws_connected:
            self.logger.error("WebSocket未连接，无法发送G-code")
            return False
        try:
            gcode_request = {
                "jsonrpc": "2.0", "method": "printer.gcode.script",
                "params": {"script": command},
                "id": self._get_next_request_id()
            }
            await self.ws.send(json.dumps(gcode_request))
            self.logger.info(f"成功发送G-code: {command}")
            return True
        except websockets.exceptions.ConnectionClosed:
            self.logger.error(f"发送G-code时连接已关闭: {command}")
            return False
        except Exception as e:
            self.logger.error(f"发送G-code时发生错误: {str(e)}")
            return False
    
    def start_monitoring(self, interval: float = 5.0):
        """开始监控打印机状态 (在asyncio模式下，此方法主要用于启用相关逻辑)"""
        self.logger.info("Klipper监控已通过WebSocket启动")

    async def stop_monitoring(self):
        """停止监控打印机状态 (在asyncio模式下，此方法主要用于禁用相关逻辑)"""
        self.logger.info("停止Klipper监控")
    
    async def disconnect(self):
        """断开与Klipper/Moonraker的连接"""
        self.auto_reconnect = False
        if self.ws_task:
            self.ws_task.cancel()
            await asyncio.gather(self.ws_task, return_exceptions=True)
        if self.ws and self.ws.open:
            await self.ws.close()
        self.ws_connected = False
        self.logger.info("Klipper监控已断开")

    def enable_filament_runout_detection(self):
        self.runout_detection_enabled = True
        self.logger.info(f"启用断料检测，使用传感器: {self.filament_sensor_names}")
    
    def disable_filament_runout_detection(self):
        self.runout_detection_enabled = False
        self.logger.info("禁用断料检测")

    async def resume_print(self):
        return await self._send_gcode(self.resume_cmd)
    
    async def pause_print(self):
        return await self._send_gcode(self.pause_cmd)
    
    async def cancel_print(self):
        return await self._send_gcode(self.cancel_cmd)
    
    def register_status_callback(self, callback: Callable[[Dict], Coroutine]):
        if callback not in self.status_callbacks:
            self.status_callbacks.append(callback)
            
    def unregister_status_callback(self, callback: Callable):
        if callback in self.status_callbacks:
            self.status_callbacks.remove(callback)
            
    async def execute_gcode(self, command: str) -> bool:
        return await self._send_gcode(command)
        
    def get_printer_status(self) -> Dict[str, Any]:
        """获取当前打印机状态的快照"""
        return {
            'printer_state': self.printer_state,
            'print_stats': self.print_stats,
            'toolhead': self.toolhead_info,
            'extruder': self.extruder_info,
            'extruder1': self.extruder1_info,
            'active_extruder': self.active_extruder,
            'filament_sensors_status': self.filament_sensors_status
        }

    def get_filament_status(self) -> Dict[str, bool]:
        """获取断料传感器状态"""
        return self.filament_sensors_status.copy()
