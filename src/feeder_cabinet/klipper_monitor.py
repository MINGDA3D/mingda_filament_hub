"""
Klipper监控模块 - 管理与Klipper的通信 (Asyncio Version)

此模块提供与Klipper的通信功能，包括：
- 获取打印机状态
- 监控打印过程
- 处理断料检测
- 暂停和恢复打印
"""

import logging
import time
import asyncio
import json
import math
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
        self.send_lock = asyncio.Lock()  # 新增：用于保护WebSocket发送操作

        # lancaigang261017 任务#447: G-code回执与PAUSE生效确认机制。
        # 背景(2026-10-17 09:40-09:57 事故): Klipper 阻塞在 START_PRINT 的 M191/M109 等温流程时,
        # moonraker 的 gcode/script 请求会积压十几分钟; 旧 _send_gcode 是"发完即返回 True",
        # 上层误判"已暂停"继续下发 UNLOAD, 14 分钟内堆了 4 条 PAUSE + 3 条 UNLOAD, 等温结束
        # 队列集中放行, 打印在 T1 换头后被第一条迟到 PAUSE 打断, extrude 反复进退。
        # 现在: 用 jsonrpc id 等回执 + 超时判失败; pause_resume.is_paused 作为"真暂停"唯一判据。
        self.gcode_ack_timeout = 15.0            # 默认回执等待上限(秒)
        self.pause_resume_is_paused = False      # 来自 pause_resume 订阅的真实暂停态
        self._pending_gcode_waiters: Dict[int, asyncio.Future] = {}
        
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
        # lancaigang261024113000 任务#554-copy108：COPY/MIRROR双头参与不能由单值toolhead推断。
        self.dual_carriage_info = {}
        self.motion_report = {}  # 新增：运动报告状态
        # lancaigang261024160000 任务#554-copy108-hotfix8: paused 下 motion_report 的
        # live_extruder_velocity 保留最后一次真实运动的速度不归零(108 实测 -0.339mm/s 正是
        # 上一次真退料速度), 单看速度会把"早已停住的退料"当"正在退料", 永久卡住恢复。
        # 运动队列是否真的在执行(toolhead.print_time 推进)才是"在动"的硬证据。
        self._last_print_time = None
        self._motion_alive_ts = None
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
        self.disconnect_callback: Optional[Callable[[], Coroutine]] = None
        
        # 重连任务
        self.reconnect_task: Optional[asyncio.Task] = None

    async def _cleanup_old_connection(self):
        """清理旧的连接和任务"""
        # lancaigang261030211000 Qoder: 重连清理由新任务接管，禁止取消或等待当前任务。
        current_task = asyncio.current_task()
        # 停止定时查询任务
        if self.periodic_query_task and not self.periodic_query_task.done():
            self.periodic_query_task.cancel()
            try:
                await self.periodic_query_task
            except asyncio.CancelledError:
                pass
            self.periodic_query_task = None
        
        # 停止WebSocket处理任务
        if (self.ws_task and self.ws_task is not current_task
                and not self.ws_task.done()):
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                pass
            self.ws_task = None
        
        # 停止重连任务
        if (self.reconnect_task and self.reconnect_task is not current_task
                and not self.reconnect_task.done()):
            self.reconnect_task.cancel()
            try:
                await self.reconnect_task
            except asyncio.CancelledError:
                pass
            self.reconnect_task = None
        
        # 关闭旧的WebSocket连接
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        
        self.ws_connected = False
        self.dual_carriage_info.clear()
        self.extruder_info.clear()
        self.extruder1_info.clear()
        self.pause_resume_is_paused = False

    async def connect(self) -> bool:
        """连接到Klipper/Moonraker"""
        if self.ws_connected:
            return True
        
        async with self.connection_lock:
            if self.ws_connected:
                return True
            
            # 清理旧的任务和连接
            await self._cleanup_old_connection()
            
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
                self.ws = None  # 确保ws为None
                
                # 如果自动重连启用，启动重连任务
                if self.auto_reconnect and not self.reconnect_task:
                    self.reconnect_task = asyncio.create_task(self._reconnect_loop())
                
                return False

    async def _ws_handler(self):
        """处理WebSocket连接的读取和重连"""
        while self.auto_reconnect:
            try:
                # 检查ws是否存在
                if not self.ws or not self.ws_connected:
                    self.logger.debug("WebSocket未连接，等待重连...")
                    break
                    
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
            # lancaigang261017 任务#447: 先兑现所有在途G-code回执等待, 再清状态缓存。
            self._abort_pending_gcode_waiters("WebSocket连接断开")
            
            # 清除状态缓存，确保重连后能正确同步状态
            self.printer_state = "unknown"
            self.dual_carriage_info.clear()
            self.extruder_info.clear()
            self.extruder1_info.clear()
            self.pause_resume_is_paused = False
            self.logger.info("WebSocket断开，已清除打印机状态缓存")
            
            # 通知断开回调
            if self.disconnect_callback:
                try:
                    await self.disconnect_callback()
                except Exception as e:
                    self.logger.error(f"断开回调执行失败: {e}", exc_info=True)
            
            # 停止定时查询任务（如果正在运行）
            if self.periodic_query_task and not self.periodic_query_task.done():
                self.periodic_query_task.cancel()
                try:
                    await self.periodic_query_task
                except asyncio.CancelledError:
                    pass
                self.periodic_query_task = None
                
            # 清理 WebSocket 连接
            if self.ws:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None
                
            if self.auto_reconnect:
                self.logger.info(f"将在 {self.reconnect_interval} 秒后尝试重连...")
                await asyncio.sleep(self.reconnect_interval)
                await self.connect()
                # connect() 已创建新读取任务，旧任务退出以避免并发 recv。
                break
        self.logger.info("WebSocket处理任务已结束")
    
    async def _reconnect_loop(self):
        """独立的重连循环任务"""
        self.logger.info("启动自动重连循环")
        
        while self.auto_reconnect:
            try:
                # 等待重连间隔
                self.logger.info(f"将在 {self.reconnect_interval} 秒后尝试重连...")
                await asyncio.sleep(self.reconnect_interval)
                
                # 尝试重连
                if await self.connect():
                    self.logger.info("重连成功")
                    # 订阅响应会自动触发状态更新，不需要额外的强制发送
                    break
                    
            except asyncio.CancelledError:
                self.logger.info("重连任务被取消")
                break
            except Exception as e:
                self.logger.error(f"重连任务异常: {e}", exc_info=True)
                
        self.logger.info("重连循环结束")
    
    async def _process_ws_message(self, message):
        """处理接收到的WebSocket消息"""
        try:
            data = json.loads(message)
            
            # 调试日志：记录收到的原始WebSocket消息
            self.logger.debug(f"收到WebSocket消息: {json.dumps(data, ensure_ascii=False)[:500]}...")  # 限制长度避免日志过大
            
            # lancaigang261017 任务#447: jsonrpc 回执分发——唤醒 _send_gcode 中等待的 waiter。
            # 必须在业务分支之前处理, 否则 gcode/script 的 result 会被当作订阅响应丢弃。
            _rid = data.get('id')
            if _rid is not None:
                _waiter = self._pending_gcode_waiters.pop(_rid, None)
                if _waiter is not None and not _waiter.done():
                    _waiter.set_result(data)

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
        
        # lancaigang261017 任务#447: pause_resume 订阅解析。
        # 这是判断"PAUSE 是否真正生效"的唯一可靠依据——printer_state 变 paused 有延迟,
        # 而 _send_gcode 的受理回执不代表已执行(Klipper 队列可能积压)。
        if 'pause_resume' in status and isinstance(status['pause_resume'], dict):
            _paused = status['pause_resume'].get('is_paused')
            if _paused is not None and bool(_paused) != self.pause_resume_is_paused:
                self.pause_resume_is_paused = bool(_paused)
                self.logger.info(f"pause_resume.is_paused 变化: {self.pause_resume_is_paused}")

        if isinstance(status.get('dual_carriage'), dict):
            self.dual_carriage_info.update(status['dual_carriage'])

        if 'toolhead' in status:
            self.logger.debug(f"toolhead更新: {status['toolhead']}")
            self.toolhead_info.update(status['toolhead'])
        
        if 'extruder' in status:
            self.logger.debug(f"extruder更新: {status['extruder']}")
            self.extruder_info.update(status['extruder'])

        if 'extruder1' in status:
            self.logger.debug(f"extruder1更新: {status['extruder1']}")
            self.extruder1_info.update(status['extruder1'])

        # 新增：处理运动报告状态
        if 'motion_report' in status:
            self.motion_report.update(status['motion_report'])
            self._handle_extruder_motion_update(status['motion_report'])

        for i, sensor_obj in enumerate(self.filament_sensor_objects):
            if sensor_obj in status and "filament_detected" in status[sensor_obj]:
                sensor_name = self.filament_sensor_names[i]
                new_state = status[sensor_obj]["filament_detected"]
                self.logger.debug(f"断料传感器 {sensor_name} 状态: {'有料' if new_state else '无料'} (原始数据: {status[sensor_obj]})")
                if self.filament_sensors_status.get(sensor_name) != new_state:
                    self.logger.info(f"断料传感器 {sensor_name} 状态变化: {'有料' if new_state else '无料'}")
                self.filament_sensors_status[sensor_name] = new_state
    
    def _handle_extruder_motion_update(self, motion_data: Dict[str, Any]):
        """处理挤出机运动状态更新"""
        extruder_velocity = motion_data.get('live_extruder_velocity', 0.0)
        # lancaigang261012 任务#411: 记录运动新鲜度(恢复闸门G2"挤出机正在退料"判据数据源)
        # lancaigang261024160000 任务#554-copy108-hotfix8: 帧到达 != 在动。paused 下 Moonraker
        # 仍每帧推送 motion_report(速度是残值), 原实现每帧刷新 _last_motion_ts, 新鲜度永远成立。
        # 改为只有 toolhead.print_time 真正推进(运动队列在执行)才刷新 _motion_alive_ts。
        self._last_motion_ts = time.monotonic()
        self._last_motion_velocity = extruder_velocity
        _pt = self.toolhead_info.get('print_time')
        if isinstance(_pt, (int, float)):
            if self._last_print_time is None:
                self._motion_alive_ts = time.monotonic()
            elif _pt > self._last_print_time + 1e-4:
                self._motion_alive_ts = time.monotonic()
            self._last_print_time = _pt
        live_position = motion_data.get('live_position', [])
        
        # 获取当前激活的挤出头
        active_extruder = self.toolhead_info.get('extruder', 'extruder')
        
        # 判断挤出机运动状态
        if extruder_velocity > 0.0:
            motion_state = "进料"
            self.logger.debug(f"{active_extruder} 正在进料，速度: {extruder_velocity:.2f} mm/s")
        elif extruder_velocity < 0.0:
            motion_state = "退料"
            self.logger.debug(f"{active_extruder} 正在退料，速度: {extruder_velocity:.2f} mm/s")
        else:
            motion_state = "停止"
            # 仅在状态变化时记录，避免日志过多
            if hasattr(self, '_last_motion_state') and self._last_motion_state != motion_state:
                self.logger.debug(f"{active_extruder} 停止运动")
        
        # 记录状态变化
        if not hasattr(self, '_last_motion_state') or self._last_motion_state != motion_state:
            if motion_state != "停止":  # 停止状态不记录info级别日志，避免过多
                self.logger.info(f"挤出机运动状态变化: {active_extruder} -> {motion_state} (速度: {extruder_velocity:.2f} mm/s)")
            self._last_motion_state = motion_state
        
        # 更新活动挤出头信息
        extruder_index = 0 if active_extruder == 'extruder' else 1
        if extruder_index != self.active_extruder:
            self.active_extruder = extruder_index
            self.logger.info(f"活动挤出头切换: extruder{extruder_index} ({active_extruder})")
        
        # 记录详细的运动数据用于调试
        self.logger.debug(f"运动报告详情 - 挤出头: {active_extruder}, 速度: {extruder_velocity:.2f}, 位置: {live_position}")
        
        # 检查两个挤出头的状态
        for extruder_name, extruder_info in [("extruder", self.extruder_info), ("extruder1", self.extruder1_info)]:
            if extruder_info:
                can_extrude = extruder_info.get("can_extrude", False)
                temperature = extruder_info.get("temperature", 0)
                target_temp = extruder_info.get("target", 0)
                is_active = (extruder_name == active_extruder)
                
                self.logger.debug(f"{extruder_name}: 温度={temperature:.1f}°C (目标:{target_temp:.1f}°C), "
                                f"可挤出={can_extrude}, 激活状态={'是' if is_active else '否'}")
                
                # 如果是激活的挤出头且在运动，记录更详细信息
                if is_active and motion_state != "停止":
                    self.logger.debug(f"激活挤出头 {extruder_name} 运动详情: {motion_state}, 速度={extruder_velocity:.2f} mm/s")
    
    async def refresh_status(self) -> bool:
        """lancaigang261017: 主动拉取一次全量对象状态(启动/重连/部署重启后对齐用)。

        connect() 里的 query 是异步发帧, 返回时未必已收到响应; 启动同步需可重试的显式刷新。
        """
        if not self.ws_connected:
            return False
        await self._query_current_status()
        return True

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
                        "motion_report": None,  # 新增：查询运动报告
                        "extruder": None,
                        "extruder1": None,
                        "dual_carriage": None,
                        # lancaigang261017 任务#447: 断线重连后必须同步真实暂停态,
                        # 否则 pause_resume_is_paused 会残留 False 导致编排误判“未暂停”。
                        "pause_resume": None
                    }
                },
                "id": self._get_next_request_id()
            }
            
            self.logger.info("主动查询当前打印机状态")
            async with self.send_lock:
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
                                "motion_report": None,  # 新增：查询运动报告
                                "extruder": None,
                                "extruder1": None,
                                "dual_carriage": None,
                                "pause_resume": None
                            }
                        },
                        "id": self._get_next_request_id()
                    }
                    
                    # 添加断料传感器查询
                    for sensor_obj in self.filament_sensor_objects:
                        query_request["params"]["objects"][sensor_obj] = None
                    
                    self.logger.debug(f"定时查询打印机状态 (间隔: {self.query_interval}秒)")
                    async with self.send_lock:
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
                "print_stats": None, 
                # hotfix8: print_time 是"运动队列是否在执行"的硬证据, 必须订阅
                "toolhead": ["extruder", "position", "print_time"],
                "motion_report": ["live_extruder_velocity", "live_position"],  # 新增：运动报告订阅
                "extruder": ["can_extrude", "temperature", "target", "motion_queue"],
                "extruder1": ["can_extrude", "temperature", "target", "motion_queue"],
                "dual_carriage": None,
                "virtual_sdcard": None, 
                "pause_resume": None,
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
            
            async with self.send_lock:
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
    
    async def _send_gcode(self, command: str, timeout: Optional[float] = None) -> bool:
        """发送G-code命令到Klipper, 并等待执行回执。

        lancaigang261017 任务#447: 旧实现发完即返回 True, 无法区分"已执行"与"堆在队列里",
        上层据此判定"打印已暂停"继续下发 UNLOAD 造成 261017 事故。现改为按 jsonrpc id 等回执,
        超时即判失败(命令可能仍在队列中迟到生效, 调用方需自行做在途守卫)。
        """
        if not self.ws_connected:
            self.logger.error("WebSocket未连接，无法发送G-code")
            return False
        _timeout = self.gcode_ack_timeout if timeout is None else timeout
        req_id = self._get_next_request_id()
        waiter = asyncio.get_running_loop().create_future()
        self._pending_gcode_waiters[req_id] = waiter
        try:
            gcode_request = {
                "jsonrpc": "2.0", "method": "printer.gcode.script",
                "params": {"script": command},
                "id": req_id
            }
            async with self.send_lock:
                await self.ws.send(json.dumps(gcode_request))
            try:
                resp = await asyncio.wait_for(waiter, timeout=_timeout)
            except asyncio.TimeoutError:
                self._pending_gcode_waiters.pop(req_id, None)
                self.logger.error(f"G-code回执超时({_timeout:.0f}s), Klipper可能被阻塞在长命令队列中, "
                                  f"命令仍可能在队列里迟到生效: {command}")
                return False
            if isinstance(resp, dict) and 'error' in resp:
                self.logger.error(f"G-code被Klipper拒绝: {command}, error={resp.get('error')}")
                return False
            self.logger.info(f"成功发送G-code: {command}")
            return True
        except websockets.exceptions.ConnectionClosed:
            self._pending_gcode_waiters.pop(req_id, None)
            self.logger.error(f"发送G-code时连接已关闭: {command}")
            return False
        except Exception as e:
            self._pending_gcode_waiters.pop(req_id, None)
            self.logger.error(f"发送G-code时发生错误: {str(e)}")
            return False

    def _abort_pending_gcode_waiters(self, reason: str):
        """lancaigang261017 任务#447: 连接中断时立即唤醒所有等回执的调用方, 避免白等到超时。

        旧实现在断线时无人兑现 Future, 调用方要等满 10s/60s 才知道失败; 断线恢复后
        队列状态已变, 拖延等待只会让上层晚一步中止编排。
        """
        if not self._pending_gcode_waiters:
            return
        self.logger.warning(f"连接已中断, 立即结束 {len(self._pending_gcode_waiters)} 个G-code回执等待: {reason}")
        for _rid, _waiter in list(self._pending_gcode_waiters.items()):
            if not _waiter.done():
                _waiter.set_result({"error": {"message": reason}})
        self._pending_gcode_waiters.clear()

    async def probe_gcode_queue(self, timeout: float = 5.0) -> bool:
        """lancaigang261017 任务#447: 探测 Klipper gcode 队列是否畅通(下发 PAUSE 前的预检)。

        用 M400 作探测: 队列畅通时它只是等当前已排队的移动跑完, 正常打印下秒级返回;
        队列被 M191/M109 升温、QUAD_GANTRY_LEVEL 调平、长宏等阻塞时, 请求会排在后面直到超时。
        超时即说明此刻下发 PAUSE 只会变成"迟到命令", 调用方应该放弃本次下发。
        M400 本身无副作用(不改变位置/温度/状态), 迟到执行也无害。
        """
        if not self.ws_connected:
            return False
        req_id = self._get_next_request_id()
        waiter = asyncio.get_running_loop().create_future()
        self._pending_gcode_waiters[req_id] = waiter
        try:
            async with self.send_lock:
                await self.ws.send(json.dumps({
                    "jsonrpc": "2.0", "method": "printer.gcode.script",
                    "params": {"script": "M400"}, "id": req_id
                }))
            try:
                resp = await asyncio.wait_for(waiter, timeout=timeout)
            except asyncio.TimeoutError:
                self._pending_gcode_waiters.pop(req_id, None)
                self.logger.warning(f"gcode队列探测超时({timeout:.0f}s): Klipper疑似阻塞在升温/调平/长命令, "
                                    f"本次不下发PAUSE(避免迟到生效打断打印)")
                return False
            if isinstance(resp, dict) and 'error' in resp:
                self.logger.warning(f"gcode队列探测被拒绝: {resp.get('error')}")
                return False
            self.logger.info("gcode队列探测通过(M400已返回, 队列畅通)")
            return True
        except Exception as e:
            self._pending_gcode_waiters.pop(req_id, None)
            self.logger.error(f"gcode队列探测失败: {e}")
            return False

    async def wait_paused(self, timeout: float = 5.0) -> bool:
        """lancaigang261017 任务#447: 轮询等待 pause_resume.is_paused 真正变为 True。

        _send_gcode 返回 True 只代表 Klipper 受理了请求; 队列积压时脚本可能十几分钟后才执行,
        因此"是否已暂停"必须以 pause_resume.is_paused 为准, 不能以受理回执为准。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.pause_resume_is_paused:
                return True
            await asyncio.sleep(0.2)
        return self.pause_resume_is_paused
    
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
            
        # lancaigang261017 任务#447: 主动断开同样要结束在途回执等待
        self._abort_pending_gcode_waiters("主动断开Klipper连接")

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
        # lancaigang261017 任务#447b: PAUSE 回执超时从 10s 放宽到 30s。
        # 实测(261017 10:55): PAUSE 宏 = PLAY_WARN_SOUND + M400 + PAUSE_BASE + 停车 + 抬Z + 状态灯,
        # pause_resume.is_paused 在宏中段就已翻 True, 但 jsonrpc 应答要等整个宏跑完(实测>10s)。
        # 10s 超时会把"已暂停"误报成"未被受理", 上层据此中止换料 -> 打印机停在 PAUSED、后续0x1D
        # 因"不在打印状态"被整条丢弃, 低重自动续料卡死等人工(261017 10:55 事故)。
        # 队列是否阻塞由上层 probe_gcode_queue(M400) 前置探测, 本超时只需覆盖宏自身执行时长。
        return await self._send_gcode(self.pause_cmd, timeout=30.0)
    
    async def cancel_print(self):
        return await self._send_gcode(self.cancel_cmd)
    
    def register_status_callback(self, callback: Callable[[Dict], Coroutine]):
        if callback not in self.status_callbacks:
            self.status_callbacks.append(callback)
            
    def unregister_status_callback(self, callback: Callable):
        if callback in self.status_callbacks:
            self.status_callbacks.remove(callback)
    
    def register_disconnect_callback(self, callback: Callable[[], Coroutine]):
        """注册断开连接回调"""
        self.disconnect_callback = callback
            
    async def execute_gcode(self, command: str, timeout: Optional[float] = None) -> bool:
        # lancaigang261017 任务#447: 宏命令(UNLOAD_FILAMENT≈13s)与 M400(等移动完成)耗时较长,
        # 默认放宽到 60s; 调用方可用 timeout 显式覆盖。
        return await self._send_gcode(command, timeout=timeout if timeout is not None else 60.0)

    async def fetch_gcode_store(self, count: int = 40) -> Optional[list]:
        """lancaigang261024143000 任务#554-copy108-hotfix4: 读取 Moonraker 最近 gcode 输出。

        _send_gcode 的 jsonrpc 回执只代表请求被受理, Klipper 运行期报错(挤出行程超限/
        越界/低温)不会体现为 error。上层用它复核"预挤命令真的被执行"。
        传输异常返回 None(不可取证), 由调用方决定是否锁死; 当前选择不因此反向锁打印。
        """
        try:
            url = "%s/server/gcode_store?count=%d" % (self.moonraker_url, max(1, int(count)))
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as response:
                    if response.status != 200:
                        self.logger.warning("读取G-code输出失败: HTTP %s", response.status)
                        return None
                    data = await response.json()
            return ((data.get('result') or {}).get('gcode_store')) or []
        except Exception as e:
            self.logger.warning("读取G-code输出异常: %s", e)
            return None

    async def get_gcode_e_position(self) -> Optional[float]:
        """读取Klipper实际G-code E坐标，供暂停期挤出行程复核。"""
        try:
            url = "%s/printer/objects/query?gcode_move" % self.moonraker_url
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as response:
                    if response.status != 200:
                        self.logger.warning("读取E坐标失败: HTTP %s", response.status)
                        return None
                    data = await response.json()
            position = (data.get('result', {}).get('status', {})
                        .get('gcode_move', {}).get('gcode_position', []))
            if len(position) < 4:
                return None
            value = float(position[3])
            return value if math.isfinite(value) else None
        except Exception as e:
            self.logger.warning("读取E坐标异常: %s", e)
            return None

    async def check_gcode_macro_exists(self, macro_name: str) -> bool:
        """
        检查Klipper中是否存在指定的宏

        Args:
            macro_name: 宏名称（如 "UNLOAD_FILAMENT"）

        Returns:
            bool: 宏是否存在
        """
        try:
            # 使用Moonraker API查询gcode帮助信息
            url = f"{self.moonraker_url}/printer/gcode/help"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        data = await response.json()
                        # 返回的数据格式是 {"result": {"COMMAND_NAME": "description", ...}}
                        if 'result' in data:
                            commands = data['result']
                            # 检查宏是否存在（不区分大小写）
                            macro_upper = macro_name.upper()
                            exists = macro_upper in commands
                            if exists:
                                self.logger.info(f"找到宏: {macro_name}")
                            else:
                                self.logger.warning(f"宏不存在: {macro_name}")
                            return exists
                    else:
                        self.logger.error(f"查询gcode帮助失败: HTTP {response.status}")
                        return False
        except Exception as e:
            self.logger.error(f"检查宏是否存在时发生错误: {e}", exc_info=True)
            return False

    def get_printer_status(self) -> Dict[str, Any]:
        """获取当前打印机状态的快照"""
        return {
            'printer_state': self.printer_state,
            'print_stats': self.print_stats,
            'toolhead': self.toolhead_info,
            'motion_report': self.motion_report,  # 新增：运动报告状态
            'extruder': self.extruder_info,
            'extruder1': self.extruder1_info,
            'active_extruder': self.active_extruder,
            'filament_sensors_status': self.filament_sensors_status,
            # lancaigang261017 任务#447: 真实暂停态(来自 pause_resume 订阅), 供上层编排判据
            'is_paused': self.pause_resume_is_paused
        }

    def get_filament_status(self) -> Dict[str, bool]:
        """获取断料传感器状态"""
        return self.filament_sensors_status.copy()

    def get_extruder_motion_status(self) -> Dict[str, Any]:
        """获取双挤出头运动状态信息"""
        active_extruder = self.toolhead_info.get('extruder', 'extruder')
        extruder_velocity = self.motion_report.get('live_extruder_velocity', 0.0)
        live_position = self.motion_report.get('live_position', [])
        
        # 判断运动状态
        if extruder_velocity > 0.0:
            motion_state = "进料"
        elif extruder_velocity < 0.0:
            motion_state = "退料" 
        else:
            motion_state = "停止"
        
        return {
            'active_extruder': active_extruder,
            'active_extruder_index': self.active_extruder,
            'extruder_velocity': extruder_velocity,
            'motion_state': motion_state,
            'live_position': live_position,
            'extruder_info': {
                'extruder': {
                    'can_extrude': self.extruder_info.get('can_extrude', False),
                    'temperature': self.extruder_info.get('temperature', 0),
                    'target': self.extruder_info.get('target', 0),
                    'is_active': active_extruder == 'extruder'
                },
                'extruder1': {
                    'can_extrude': self.extruder1_info.get('can_extrude', False),
                    'temperature': self.extruder1_info.get('temperature', 0),
                    'target': self.extruder1_info.get('target', 0),
                    'is_active': active_extruder == 'extruder1'
                }
            }
        }
