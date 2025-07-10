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
        
        # 定时查询相关
        self.periodic_query_task: Optional[asyncio.Task] = None
        self.query_interval = 5.0  # 默认5秒查询一次
        self.periodic_query_enabled = True
        
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
        self.extruder_to_tube = self.extruder_config.get('mapping', {0: 0, 1: 1})
        
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
                
                # 主动查询当前状态
                await self._query_current_status()
                
                # 如果定时查询已启用，启动定时查询任务
                if self.periodic_query_enabled:
                    self._start_periodic_query_task()
                
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
            
            # 停止定时查询任务（如果正在运行）
            if self.periodic_query_task and not self.periodic_query_task.done():
                self.periodic_query_task.cancel()
                try:
                    await self.periodic_query_task
                except asyncio.CancelledError:
                    pass
                self.periodic_query_task = None
                
            if self.auto_reconnect:
                self.logger.info(f"将在 {self.reconnect_interval} 秒后尝试重连...")
                await asyncio.sleep(self.reconnect_interval)
                await self.connect()
        self.logger.info("WebSocket处理任务已结束")
    
    async def _process_ws_message(self, message):
        """处理接收到的WebSocket消息"""
        try:
            data = json.loads(message)
            
            # 调试日志：记录收到的原始WebSocket消息
            self.logger.debug(f"收到WebSocket消息: {json.dumps(data, ensure_ascii=False)[:500]}...")  # 限制长度避免日志过大
            
            if 'method' in data and data['method'] == 'notify_status_update':
                status_data = data['params'][0]
                self.logger.debug(f"收到状态更新通知，包含的对象: {list(status_data.keys())}")
                await self._handle_status_update(status_data)
            
            elif 'result' in data and 'status' in data['result']:
                status_data = data['result']['status']
                self.logger.debug(f"收到订阅响应，包含的对象: {list(status_data.keys())}")
                await self._handle_status_update(status_data)
                
        except json.JSONDecodeError:
            self.logger.warning(f"无法解析WebSocket消息: {message}")
        except Exception as e:
            self.logger.error(f"处理WebSocket消息时发生内部错误: {e}", exc_info=True)

    async def _handle_status_update(self, status):
        """处理状态更新数据, 并通过回调上报"""
        # 调试日志：记录收到的所有状态更新
        self.logger.debug(f"处理状态更新，包含的键: {list(status.keys())}")
        
        # 调用状态回调，将原始数据上报
        for callback in self.status_callbacks:
            asyncio.create_task(callback(status))

        # --- 以下为内部状态缓存，仅为get_printer_status提供快照 ---
        if 'print_stats' in status:
            # 调试日志：详细记录print_stats的内容
            self.logger.debug(f"print_stats更新前: {self.print_stats}")
            self.logger.debug(f"print_stats更新内容: {status['print_stats']}")
            
            self.print_stats.update(status['print_stats'])
            new_state = self.print_stats.get('state')
            
            # 调试日志：记录状态变化详情
            self.logger.debug(f"print_stats更新后: {self.print_stats}")
            self.logger.debug(f"当前printer_state: {self.printer_state}, 新state: {new_state}")
            
            if new_state and new_state != self.printer_state:
                old_state = self.printer_state
                self.printer_state = new_state
                self.logger.info(f"打印机状态变化: {old_state} -> {self.printer_state}")
                # 移除直接发送CAN消息的代码，避免在CAN断开时导致异常
                # 状态通知应该由main.py的_handle_klipper_status_update处理
        
        if 'toolhead' in status:
            self.logger.debug(f"toolhead更新: {status['toolhead']}")
            self.toolhead_info.update(status['toolhead'])
        
        if 'extruder' in status:
            self.logger.debug(f"extruder更新: {status['extruder']}")
            self.extruder_info.update(status['extruder'])

        if 'extruder1' in status:
            self.logger.debug(f"extruder1更新: {status['extruder1']}")
            self.extruder1_info.update(status['extruder1'])

        for i, sensor_obj in enumerate(self.filament_sensor_objects):
            if sensor_obj in status and "filament_detected" in status[sensor_obj]:
                sensor_name = self.filament_sensor_names[i]
                new_state = status[sensor_obj]["filament_detected"]
                self.logger.debug(f"断料传感器 {sensor_name} 状态: {'有料' if new_state else '无料'} (原始数据: {status[sensor_obj]})")
                if self.filament_sensors_status.get(sensor_name) != new_state:
                    self.logger.info(f"断料传感器 {sensor_name} 状态变化: {'有料' if new_state else '无料'}")
                self.filament_sensors_status[sensor_name] = new_state
    
    async def _query_current_status(self):
        """主动查询当前打印机状态"""
        if not self.ws_connected:
            self.logger.error("WebSocket未连接，无法查询状态")
            return
            
        try:
            # 查询所有订阅的对象
            query_request = {
                "jsonrpc": "2.0", 
                "method": "printer.objects.query",
                "params": {
                    "objects": {
                        "print_stats": None,
                        "toolhead": None,
                        "extruder": None,
                        "extruder1": None
                    }
                },
                "id": self._get_next_request_id()
            }
            
            self.logger.info("主动查询当前打印机状态")
            await self.ws.send(json.dumps(query_request))
            
        except Exception as e:
            self.logger.error(f"查询打印机状态时发生错误: {str(e)}")
    
    async def _periodic_query_task(self):
        """定时查询任务"""
        self.logger.info(f"启动定时查询任务，间隔: {self.query_interval}秒")
        
        while self.periodic_query_enabled:
            try:
                await asyncio.sleep(self.query_interval)
                
                if self.ws_connected and self.periodic_query_enabled:
                    # 查询所有订阅的对象（包括断料传感器）
                    query_request = {
                        "jsonrpc": "2.0", 
                        "method": "printer.objects.query",
                        "params": {
                            "objects": {
                                "print_stats": None,
                                "toolhead": None,
                                "extruder": None,
                                "extruder1": None
                            }
                        },
                        "id": self._get_next_request_id()
                    }
                    
                    # 添加断料传感器查询
                    for sensor_obj in self.filament_sensor_objects:
                        query_request["params"]["objects"][sensor_obj] = None
                    
                    self.logger.debug(f"定时查询打印机状态 (间隔: {self.query_interval}秒)")
                    await self.ws.send(json.dumps(query_request))
                    
            except asyncio.CancelledError:
                self.logger.info("定时查询任务被取消")
                break
            except Exception as e:
                self.logger.error(f"定时查询任务发生错误: {str(e)}", exc_info=True)
                # 发生错误后等待一段时间再继续
                await asyncio.sleep(min(self.query_interval, 10))
        
        self.logger.info("定时查询任务结束")
    
    async def resubscribe_objects(self):
        """重新订阅对象（用于状态同步问题时）"""
        if not self.ws_connected:
            self.logger.error("WebSocket未连接，无法重新订阅")
            return
            
        self.logger.info("重新订阅Klipper对象以同步状态")
        await self._subscribe_objects()
        # 订阅后立即查询一次当前状态
        await self._query_current_status()
    
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
            
            # 调试日志：记录要订阅的对象
            self.logger.debug(f"准备订阅的对象列表: {json.dumps(objects_dict, ensure_ascii=False)}")
                
            subscribe_request = {
                "jsonrpc": "2.0", "method": "printer.objects.subscribe",
                "params": {"objects": objects_dict},
                "id": self._get_next_request_id()
            }
            
            # 调试日志：记录完整的订阅请求
            self.logger.debug(f"发送订阅请求: {json.dumps(subscribe_request, ensure_ascii=False)}")
            
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
        """开始监控打印机状态，启动定时查询任务"""
        self.query_interval = interval
        self.periodic_query_enabled = True
        
        # 如果WebSocket已连接，启动定时查询任务
        if self.ws_connected:
            self._start_periodic_query_task()
        
        self.logger.info(f"Klipper监控已启动，定时查询间隔: {interval}秒")

    def _start_periodic_query_task(self):
        """启动定时查询任务"""
        if self.periodic_query_task is None or self.periodic_query_task.done():
            self.periodic_query_task = asyncio.create_task(self._periodic_query_task())
            self.logger.info("定时查询任务已启动")

    async def stop_monitoring(self):
        """停止监控打印机状态，停止定时查询任务"""
        self.periodic_query_enabled = False
        
        # 停止定时查询任务
        if self.periodic_query_task and not self.periodic_query_task.done():
            self.periodic_query_task.cancel()
            try:
                await self.periodic_query_task
            except asyncio.CancelledError:
                pass
            self.periodic_query_task = None
            
        self.logger.info("停止Klipper监控")
    
    async def disconnect(self):
        """断开与Klipper/Moonraker的连接"""
        self.auto_reconnect = False
        self.periodic_query_enabled = False
        
        # 停止定时查询任务
        if self.periodic_query_task and not self.periodic_query_task.done():
            self.periodic_query_task.cancel()
            try:
                await self.periodic_query_task
            except asyncio.CancelledError:
                pass
            self.periodic_query_task = None
            
        # 停止WebSocket任务
        if self.ws_task:
            self.ws_task.cancel()
            await asyncio.gather(self.ws_task, return_exceptions=True)
            
        # 关闭WebSocket连接
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass  # WebSocket可能已经关闭
                
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
