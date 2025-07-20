"""
CAN通信模块 - 管理与送料柜之间的CAN通信 (Asyncio Version)

此模块提供与送料柜的CAN总线通信功能，包括：
- 初始化CAN连接和握手
- 发送命令和状态查询
- 接收和解析状态消息
- 错误处理和重连机制
"""

import can
import logging
import asyncio
from typing import Optional, Callable, Dict, List, Any, Union, TYPE_CHECKING, Coroutine

if TYPE_CHECKING:
    import can

class FeederCabinetCAN:
    """送料柜CAN通信类"""
    
    # 状态码定义
    STATUS_IDLE = 0x00      # 空闲
    STATUS_READY = 0x01     # 就绪/准备送料
    STATUS_FEEDING = 0x02   # 送料中
    STATUS_COMPLETE = 0x03  # 完成
    STATUS_ERROR = 0x04     # 错误
    
    # 错误码定义
    ERROR_NONE = 0x00          # 无错误
    ERROR_MECHANICAL = 0x01    # 机械错误
    ERROR_MATERIAL_MISSING = 0x02  # 材料缺失
    ERROR_OTHER = 0x03         # 其他错误
    ERROR_KLIPPER = 0x04       # Klipper错误
    ERROR_MOONRAKER = 0x05     # Moonraker错误
    ERROR_COMMUNICATION = 0x06 # 通信错误
    
    # 命令类型定义
    CMD_REQUEST_FEED           = 0x01       # 请求送料
    CMD_STOP_FEED              = 0x02       # 停止送料
    CMD_QUERY_STATUS           = 0x03       # 查询状态
    CMD_PRINTING               = 0x04       # 打印中
    CMD_PRINT_COMPLETE         = 0x05       # 打印完成
    CMD_PRINT_PAUSE            = 0x06       # 打印暂停
    CMD_PRINT_CANCEL           = 0x07       # 打印取消 
    CMD_PRINTER_IDLE           = 0x08       # 打印机空闲
    CMD_PRINTER_ERROR          = 0x09       # 打印机错误
    CMD_HEARTBEAT              = 0x0A       # 心跳包
    CMD_LOAD_FILAMENT          = 0x0B       # 进料
    CMD_UNLOAD_FILAMENT        = 0x0C       # 退料
    CMD_QUERY_PRINTER_FILAMENT_STATUS  = 0x0D       # 送料柜查询左右料管对应打印机挤出机余料状态
    CMD_PRINTER_FILAMENT_STATUS_RESPONSE = 0x0E    # 送料柜左右料管对应打印机挤出机余料状态响应
    CMD_SET_FEEDER_MAPPING             = 0x0F       # 设置料管与挤出机对应关系
    CMD_QUERY_FEEDER_MAPPING           = 0x10       # 查询料管与挤出机对应关系
    CMD_FEEDER_MAPPING_RESPONSE        = 0x11       # 料管与挤出机对应关系响应
    
    # RFID相关命令
    CMD_RFID_RAW_DATA_NOTIFY   = 0x14       # 主动通知RFID原始数据（起始包）
    CMD_RFID_RAW_DATA_REQUEST  = 0x15       # 请求RFID原始数据
    CMD_RFID_RAW_DATA_RESPONSE = 0x16       # RFID原始数据响应（起始包）
    CMD_RFID_DATA_PACKET       = 0x17       # RFID数据包
    CMD_RFID_DATA_END          = 0x18       # RFID数据传输结束
    CMD_RFID_READ_ERROR        = 0x19       # RFID读取错误
    
    def __init__(self, interface: str = 'can0', bitrate: int = 1000000):
        """
        初始化CAN通信类
        
        Args:
            interface: CAN接口名称
            bitrate: CAN总线波特率
        """
        self.interface = interface
        self.bitrate = bitrate
        self.bus: Optional[can.BusABC] = None
        self.logger = logging.getLogger("feeder_cabinet.can")
        
        # CAN消息ID定义
        self.SEND_ID = 0x10A       # 打印机 -> 送料柜
        self.RECEIVE_ID = 0x10B    # 送料柜 -> 打印机
        self.HANDSHAKE_SEND_ID = 0x3F0  # 握手发送ID
        self.HANDSHAKE_RECEIVE_ID = 0x3F1  # 握手接收ID
        
        # 握手消息定义
        self.HANDSHAKE_DATA = [0x01, 0xF0, 0x10, 0x00, 0x00, 0x06, 0x01, 0x05]
        self.HANDSHAKE_RESPONSE = [0x05]  # 送料柜返回0x05表示握手成功
        
        # 状态和回调
        self.connected = False
        self.status_callback: Optional[Callable[[Dict], Coroutine]] = None
        self.query_callback: Optional[Callable[[], Coroutine]] = None
        self.mapping_query_callback: Optional[Callable[[], Coroutine]] = None
        self.mapping_response_callback: Optional[Callable[[Dict], Coroutine]] = None
        self.mapping_set_callback: Optional[Callable[[Dict], Coroutine]] = None
        self.reconnect_callback: Optional[Callable[[], Coroutine]] = None  # 重连成功回调
        self.rfid_callback: Optional[Callable[[Dict], Coroutine]] = None  # RFID数据回调
        
        # 异步任务和锁
        self.rx_task: Optional[asyncio.Task] = None
        self.auto_reconnect = True  # 启用自动重连
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()
        self.reconnect_lock = asyncio.Lock()
        
        # 自动重连
        self.auto_reconnect = True
        self.reconnect_interval = 5  # seconds
        
        # 心跳响应监控
        self.heartbeat_sent_time = 0
        self.heartbeat_response_received = False
        self.heartbeat_timeout = 2  # 心跳响应超时2秒
        self.heartbeat_interval = 3  # 心跳发送间隔3秒
    
    async def connect(self) -> bool:
        """
        连接到CAN总线并执行握手过程
        
        Returns:
            bool: 连接和握手是否成功
        """
        if self.connected:
            self.logger.info("已经连接到CAN总线")
            return True
            
        try:
            self.logger.info(f"正在连接到CAN总线 {self.interface}...")
            self.bus = can.interface.Bus(
                channel=self.interface,
                bustype='socketcan',
                bitrate=self.bitrate,
                receive_own_messages=False
            )
            self.logger.info(f"成功连接到CAN总线 {self.interface}")
            
            # 执行握手过程
            if not await self._perform_handshake():
                self.logger.error("握手过程失败")
                # 清理连接但保持auto_reconnect
                await self._cleanup_connection()
                return False
            
            self.connected = True

            # 启动后台任务
            self.rx_task = asyncio.create_task(self._receive_loop())
            self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            self.logger.info("CAN连接和握手完成")
            return True
        except Exception as e:
            self.logger.error(f"连接CAN总线失败: {str(e)}", exc_info=True)
            if self.bus:
                try:
                    self.bus.shutdown()
                except Exception:
                    pass
                self.bus = None
            return False
    
    async def _cleanup_connection(self):
        """清理连接但不修改auto_reconnect标志"""
        self.connected = False
        
        tasks_to_cancel = []
        if self.rx_task:
            self.rx_task.cancel()
            tasks_to_cancel.append(self.rx_task)
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            tasks_to_cancel.append(self.heartbeat_task)

        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        if self.bus:
            try:
                self.bus.shutdown()
            except Exception as e:
                self.logger.error(f"关闭CAN总线时发生错误: {str(e)}")
            self.bus = None
            
        self.logger.info("已清理CAN总线连接")
    
    async def disconnect(self):
        """断开CAN总线连接"""
        self.connected = False
        self.auto_reconnect = False # 在手动断开时禁用自动重连
        
        tasks_to_cancel = []
        if self.rx_task:
            self.rx_task.cancel()
            tasks_to_cancel.append(self.rx_task)
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            tasks_to_cancel.append(self.heartbeat_task)

        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        if self.bus:
            try:
                self.bus.shutdown()
            except Exception as e:
                self.logger.error(f"关闭CAN总线时发生错误: {str(e)}")
            self.bus = None
            
        self.logger.info("已断开CAN总线连接")
    
    async def reconnect(self):
        """断开并重新连接CAN总线"""
        if not self.auto_reconnect:
            self.logger.warning("自动重连已禁用，跳过重连过程。")
            return

        async with self.reconnect_lock:
            if self.connected:
                return

            self.logger.info("开始CAN总线重连过程...")
            
            # 停止现有任务
            if self.rx_task: self.rx_task.cancel()
            if self.heartbeat_task: self.heartbeat_task.cancel()
            
            # 循环尝试直到重连成功
            while self.auto_reconnect and not self.connected:
                if await self.connect():
                    self.logger.info("CAN总线重连成功！")
                    
                    # 调用重连成功回调
                    if self.reconnect_callback:
                        try:
                            await self.reconnect_callback()
                        except Exception as e:
                            self.logger.error(f"执行重连回调时发生错误: {e}", exc_info=True)
                    
                    # 重连成功后查询料管映射关系以便状态同步
                    try:
                        await asyncio.sleep(0.5)  # 短暂延迟确保连接稳定
                        if await self.query_feeder_mapping():
                            self.logger.info("重连后已发送料管映射查询命令")
                        else:
                            self.logger.warning("重连后发送料管映射查询命令失败")
                    except Exception as e:
                        self.logger.error(f"重连后发送料管映射查询时发生错误: {e}", exc_info=True)
                    
                    return
                else:
                    self.logger.warning(f"重连失败，将在 {self.reconnect_interval} 秒后重试。")
                    await asyncio.sleep(self.reconnect_interval)

    async def _perform_handshake(self) -> bool:
        """
        执行握手过程
        
        Returns:
            bool: 握手是否成功
        """
        try:
            # 发送握手消息
            handshake_msg = can.Message(
                arbitration_id=self.HANDSHAKE_SEND_ID,
                data=self.HANDSHAKE_DATA,
                is_extended_id=False
            )
            self.bus.send(handshake_msg)
            self.logger.info(f"已发送握手消息: ID=0x{self.HANDSHAKE_SEND_ID:03X}, 数据={[hex(x) for x in self.HANDSHAKE_DATA]}")
            
            # 等待握手响应，超时5秒
            reader = can.AsyncBufferedReader()
            notifier = can.Notifier(self.bus, [reader])
            
            try:
                # 使用asyncio.wait_for设置超时
                msg = await asyncio.wait_for(reader.get_message(), timeout=5.0)

                if msg and msg.arbitration_id == self.HANDSHAKE_RECEIVE_ID:
                    self.logger.debug(f"收到握手响应: ID=0x{msg.arbitration_id:03X}, 数据={[hex(x) for x in msg.data]}")
                    response_data = list(msg.data)
                    if response_data == self.HANDSHAKE_RESPONSE:
                        self.logger.info("收到正确的握手响应")
                        notifier.stop()
                        return True
                    else:
                        self.logger.error(f"收到错误的握手响应数据: {[hex(x) for x in response_data]}")
                else:
                    self.logger.error(f"收到非握手响应消息或无响应")
                
                notifier.stop()
                return False

            except asyncio.TimeoutError:
                self.logger.error("握手超时 - 5秒内未收到响应")
                notifier.stop()
                return False
            
        except can.CanError as e:
            self.logger.error(f"握手过程中发生CAN错误: {e}")
            return False
        except Exception as e:
            self.logger.error(f"握手过程发生错误: {str(e)}")
            return False
    
    async def _receive_loop(self):
        """接收消息循环，在独立异步任务中运行"""
        self.logger.info("异步接收任务已启动")
        
        while self.auto_reconnect and self.connected:
            try:
                # 使用非阻塞接收，超时时间短以保持响应性
                msg = self.bus.recv(timeout=0.1)
                
                if msg is None:
                    # 没有消息时短暂异步睡眠，让出控制权
                    await asyncio.sleep(0.01)
                    continue
                    
                if msg.arbitration_id == self.RECEIVE_ID:
                        self.logger.debug(f"收到消息: ID=0x{msg.arbitration_id:03X}, 数据={[hex(x) for x in msg.data]}")
                        
                        if not msg.data:
                            self.logger.warning("收到空数据帧")
                            continue

                        command = msg.data[0]

                        if command == self.CMD_QUERY_PRINTER_FILAMENT_STATUS and self.query_callback:
                            asyncio.create_task(self.query_callback())
                        elif command == self.CMD_SET_FEEDER_MAPPING and self.mapping_set_callback:
                             if len(msg.data) >= 4 and msg.data[3] == 0x00 and msg.data[1] < 2 and msg.data[2] < 2 and msg.data[1] != msg.data[2]:
                                mapping_data = {
                                    'left_tube': msg.data[1],
                                    'right_tube': msg.data[2],
                                    'status': msg.data[3]
                                }
                                asyncio.create_task(self.mapping_set_callback(mapping_data))
                        elif command in [self.CMD_RFID_RAW_DATA_NOTIFY, self.CMD_RFID_RAW_DATA_RESPONSE, 
                                       self.CMD_RFID_DATA_PACKET, self.CMD_RFID_DATA_END, self.CMD_RFID_READ_ERROR]:
                            # RFID相关消息
                            if self.rfid_callback:
                                rfid_data = {
                                    'command': command,
                                    'data': list(msg.data)
                                }
                                asyncio.create_task(self.rfid_callback(rfid_data))
                        else:
                            # 检查是否为心跳响应 (根据你的candump，响应格式为: 05 00 FA E2 7E)
                            if len(msg.data) >= 1 and msg.data[0] == 0x05:
                                self.logger.debug("收到心跳响应")
                                self.heartbeat_response_received = True
                            
                            if self.status_callback:
                                status_data = {
                                    'status': msg.data[0],
                                    'progress': msg.data[1] if len(msg.data) > 1 else 0,
                                    'error_code': msg.data[2] if len(msg.data) > 2 else 0,
                                    'raw_data': list(msg.data)
                                }
                                asyncio.create_task(self.status_callback(status_data))
            except can.CanError as e:
                self.logger.error(f"接收消息时发生CAN错误: {e}")
                self.connected = False
                
                # 记录更多诊断信息
                error_str = str(e).lower()
                if "no such device" in error_str:
                    self.logger.error("CAN设备已消失！可能是硬件断开或驱动问题")
                elif "network is down" in error_str:
                    self.logger.error("CAN网络已关闭！可能是接口被禁用")
                
                asyncio.create_task(self.reconnect())
                return
            except asyncio.CancelledError:
                self.logger.info("接收任务被取消")
                break
            except Exception as e:
                if self.connected:
                    self.logger.error(f"接收消息时发生未知错误: {str(e)}", exc_info=True)
                    self.connected = False
                    asyncio.create_task(self.reconnect())
                    return
        
        self.logger.info("接收任务已结束")
    
    async def _heartbeat_loop(self):
        """心跳消息循环，在独立异步任务中运行"""
        self.logger.info("异步心跳任务已启动")
        heartbeat_fail_count = 0
        max_heartbeat_failures = 2  # 连续失败2次后认为断开
        
        while True:
            try:
                if self.connected:
                    # 重置响应标志
                    self.heartbeat_response_received = False
                    import time
                    self.heartbeat_sent_time = time.time()
                    
                    # 发送心跳
                    success = await self.send_message(self.CMD_HEARTBEAT)
                    if not success:
                        heartbeat_fail_count += 1
                        self.logger.warning(f"心跳发送失败 ({heartbeat_fail_count}/{max_heartbeat_failures})")
                        # 发送失败，立即重试
                        await asyncio.sleep(0.1)  # 短暂延迟后重试
                    else:
                        # 发送成功，等待响应
                        await asyncio.sleep(self.heartbeat_timeout)
                        
                        if not self.heartbeat_response_received:
                            heartbeat_fail_count += 1
                            self.logger.warning(f"心跳未收到响应 ({heartbeat_fail_count}/{max_heartbeat_failures})")
                            # 未收到响应，立即发送下一个心跳
                        else:
                            # 收到响应，重置计数器，等待正常间隔
                            if heartbeat_fail_count > 0:
                                self.logger.info("心跳响应恢复正常")
                            heartbeat_fail_count = 0
                            # 正常情况下等待剩余时间（总共3秒间隔）
                            remaining_time = self.heartbeat_interval - self.heartbeat_timeout
                            if remaining_time > 0:
                                await asyncio.sleep(remaining_time)
                    
                    # 检查是否需要判定断开
                    if heartbeat_fail_count >= max_heartbeat_failures:
                        self.logger.error("连续心跳失败或无响应，判定CAN连接已断开")
                        self.connected = False
                        heartbeat_fail_count = 0
                        if not self.reconnect_lock.locked():
                            asyncio.create_task(self.reconnect())
                else:
                    # 未连接时等待
                    await asyncio.sleep(1)
                            
            except asyncio.CancelledError:
                self.logger.info("心跳任务被取消")
                break
            except Exception as e:
                self.logger.error(f"心跳任务异常: {str(e)}")
                if self.connected:
                    self.connected = False
                    if not self.reconnect_lock.locked():
                        asyncio.create_task(self.reconnect())
                await asyncio.sleep(1)  # 异常后短暂等待
        
        self.logger.info("心跳任务已结束")
    
    
    def set_status_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置状态回调函数
        
        Args:
            callback: 状态回调函数，接收一个状态数据字典
        """
        self.status_callback = callback
    
    def set_query_callback(self, callback: Callable[[], Coroutine]):
        """
        设置查询回调函数
        
        Args:
            callback: 查询回调函数，无参数
        """
        self.query_callback = callback
    
    def set_mapping_query_callback(self, callback: Callable[[], Coroutine]):
        """
        设置料管映射查询回调函数
        
        Args:
            callback: 料管映射查询回调函数，无参数
        """
        self.mapping_query_callback = callback
    
    def set_mapping_response_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置料管映射响应回调函数
        
        Args:
            callback: 料管映射响应回调函数，接收一个映射数据字典
        """
        self.mapping_response_callback = callback
    
    def set_mapping_set_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置料管映射设置回调函数（接收送料柜的设置命令）
        
        Args:
            callback: 料管映射设置回调函数，接收一个映射数据字典
        """
        self.mapping_set_callback = callback
    
    def set_reconnect_callback(self, callback: Callable[[], Coroutine]):
        """
        设置重连成功回调函数
        
        Args:
            callback: 重连成功回调函数
        """
        self.reconnect_callback = callback
    
    def set_rfid_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置RFID数据回调函数
        
        Args:
            callback: RFID数据回调函数，接收包含命令和数据的字典
        """
        self.rfid_callback = callback
    
    async def _send_with_retry(self, msg: 'can.Message', retries: int = 3, retry_delay: float = 0.05) -> bool:
        """
        带重试机制的发送方法

        Args:
            msg: can.Message 对象
            retries: 重试次数
            retry_delay: 重试间隔 (秒)

        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("CAN总线未连接，无法发送消息")
            if not self.reconnect_lock.locked():
                asyncio.create_task(self.reconnect())
            return False

        async with self.send_lock:
            last_exception = None
            for attempt in range(retries):
                try:
                    self.bus.send(msg)
                    return True
                except can.CanError as e:
                    last_exception = e
                    error_str = str(e).lower()
                    if "no such device" in error_str or "network is down" in error_str:
                        self.logger.error(f"发送失败，CAN设备或网络不可用: {e}")
                        self.connected = False
                        asyncio.create_task(self.reconnect())
                        return False
                    
                    self.logger.warning(f"发送消息时发生CAN错误 (尝试 {attempt + 1}/{retries}): {e}")
                    if attempt < retries - 1:
                        await asyncio.sleep(retry_delay)
                except Exception as e:
                    self.logger.error(f"发送消息时发生未知错误: {e}")
                    self.connected = False
                    asyncio.create_task(self.reconnect())
                    return False
        
        self.logger.error(f"发送消息失败，已达到最大重试次数。最后一次错误: {last_exception}")
        self.connected = False
        if not self.reconnect_lock.locked():
            asyncio.create_task(self.reconnect())
        return False

    async def send_message(self, cmd_type: int, extruder: int = 0) -> bool:
        """
        发送通用消息
        
        Args:
            cmd_type: 命令类型
            extruder: 挤出机编号
            
        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            data = [cmd_type, extruder, 0, 0, 0, 0, 0, 0]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )
            
            return await self._send_with_retry(msg)
            
        except Exception as e:
            self.logger.error(f"构建或发送消息时发生未知错误: {str(e)}")
            return False
    
    async def request_feed(self, tube_id: int = 0) -> bool:
        """
        请求送料
        
        Args:
            tube_id: 料管编号
            
        Returns:
            bool: 请求是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            # 格式: [CMD_ID, IS_VALID, TUBE_ID, ...]
            data = [self.CMD_REQUEST_FEED, 0x00, tube_id, 0x00, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送补料请求: 料管ID={tube_id}, 数据={[hex(x) for x in data]}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送补料请求时失败: {str(e)}")
            return False
    
    async def stop_feed(self, tube_id: int = 0) -> bool:
        """
        停止送料
        
        Args:
            tube_id: 料管编号
            
        Returns:
            bool: 停止请求是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            # 格式: [CMD_ID, IS_VALID, TUBE_ID, ...]
            data = [self.CMD_STOP_FEED, 0x00, tube_id, 0x00, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送停止送料请求: 料管ID={tube_id}, 数据={[hex(x) for x in data]}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送停止送料请求时失败: {str(e)}")
            return False
    
    async def query_status(self) -> bool:
        """
        查询送料柜状态
        
        Returns:
            bool: 查询是否成功
        """
        return await self.send_message(self.CMD_QUERY_STATUS)
    
    async def send_printer_error(self, error_code: int, extruder: int = 0) -> bool:
        """
        发送打印机错误状态
        
        Args:
            error_code: 错误码
            extruder: 挤出机编号
            
        Returns:
            bool: 发送是否成功
        """
        if error_code == self.ERROR_NONE:
            self.logger.warning("尝试发送无错误的错误状态，改为发送空闲状态")
            return await self.send_message(self.CMD_PRINTER_IDLE, extruder)
            
        return await self.send_message(self.CMD_PRINTER_ERROR, extruder)
    
    async def send_filament_status_response(self, is_valid: bool, status_bitmap: int) -> bool:
        """
        发送挤出机余料状态响应
        
        Args:
            is_valid (bool): 数据是否有效
            status_bitmap (int): 挤出机状态位图
            
        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            validity_byte = 0x00 if is_valid else 0x01
            data = [self.CMD_PRINTER_FILAMENT_STATUS_RESPONSE, validity_byte, status_bitmap, 0, 0, 0, 0, 0]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送挤出机余料状态响应: ID=0x{self.SEND_ID:03X}, 数据={[hex(x) for x in data]}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送挤出机余料状态响应时失败: {str(e)}")
            return False
    
    async def set_feeder_mapping(self, left_tube_extruder: int, right_tube_extruder: int) -> bool:
        """
        设置料管与挤出机对应关系
        
        Args:
            left_tube_extruder: 左料管对应的挤出机号
            right_tube_extruder: 右料管对应的挤出机号
            
        Returns:
            bool: 设置是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            data = [self.CMD_SET_FEEDER_MAPPING, left_tube_extruder, right_tube_extruder, 0x00, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送料管映射设置: 左料管->挤出机{left_tube_extruder}, 右料管->挤出机{right_tube_extruder}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送料管映射设置时失败: {str(e)}")
            return False
    
    async def query_feeder_mapping(self) -> bool:
        """
        查询料管与挤出机对应关系
        
        Returns:
            bool: 查询是否成功
        """
        return await self.send_message(self.CMD_QUERY_FEEDER_MAPPING)
    
    async def send_feeder_mapping_response(self, left_extruder: int, right_extruder: int, status: int = 0) -> bool:
        """
        发送料管映射响应
        
        Args:
            left_extruder: 左料管对应的挤出机编号
            right_extruder: 右料管对应的挤出机编号
            status: 状态码 (0=成功, 其他=错误)
            
        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            data = [self.CMD_FEEDER_MAPPING_RESPONSE, left_extruder, right_extruder, status, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送料管映射响应: 左料管->挤出机{left_extruder}, 右料管->挤出机{right_extruder}, 状态={status}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送料管映射响应时失败: {str(e)}")
            return False
    
    async def request_rfid_data(self, extruder_id: int) -> bool:
        """
        请求指定挤出机的RFID数据
        
        Args:
            extruder_id: 挤出机编号
            
        Returns:
            bool: 请求是否成功发送
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            data = [self.CMD_RFID_RAW_DATA_REQUEST, 0x00, extruder_id, 0x00, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送RFID数据请求: 挤出机{extruder_id}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送RFID数据请求时失败: {str(e)}")
            return False 