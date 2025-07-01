"""
CAN通信模块 - 管理与送料柜之间的CAN通信

此模块提供与送料柜的CAN总线通信功能，包括：
- 初始化CAN连接和握手
- 发送命令和状态查询
- 接收和解析状态消息
- 错误处理和重连机制
"""

import can
import logging
import time
import threading
from typing import Optional, Callable, Dict, List, Any, Union, TYPE_CHECKING

if TYPE_CHECKING:
    import can
import queue
from concurrent.futures import ThreadPoolExecutor

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
    CMD_QUERY_PRINTER_FILAMENT_STATUS  = 0x0D       # 送料柜查询左右缓冲区对应打印机挤出机余料状态
    CMD_PRINTER_FILAMENT_STATUS_RESPONSE = 0x0E    # 送料柜左右缓冲区对应打印机挤出机余料状态响应
    CMD_SET_FEEDER_MAPPING             = 0x0F       # 设置料管与挤出机对应关系
    CMD_QUERY_FEEDER_MAPPING           = 0x10       # 查询料管与挤出机对应关系
    CMD_FEEDER_MAPPING_RESPONSE        = 0x11       # 料管与挤出机对应关系响应
    
    def __init__(self, interface: str = 'can0', bitrate: int = 1000000):
        """
        初始化CAN通信类
        
        Args:
            interface: CAN接口名称
            bitrate: CAN总线波特率
        """
        self.interface = interface
        self.bitrate = bitrate
        self.bus = None
        self.logger = logging.getLogger("feeder_cabinet.can")
        
        # 线程池
        self.thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="can_comm_")
        
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
        self.status_callback = None  # 状态回调函数
        self.query_callback = None   # 查询回调函数
        self.mapping_query_callback = None    # 料管映射查询回调函数
        self.mapping_response_callback = None # 料管映射响应回调函数
        self.mapping_set_callback = None      # 料管映射设置回调函数（接收送料柜的设置命令）
        
        # 接收消息线程
        self.rx_thread = None
        self.rx_running = False
        self.rx_queue = queue.Queue()
        
        # 心跳线程
        self.heartbeat_thread = None
        self.heartbeat_running = False
        
        # 锁，用于线程同步
        self.send_lock = threading.Lock()
        
        # 自动重连
        self.auto_reconnect = True
        self.reconnect_interval = 5  # seconds
        self.reconnect_lock = threading.Lock()
    
    def connect(self) -> bool:
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
                bitrate=self.bitrate
            )
            self.logger.info(f"成功连接到CAN总线 {self.interface}")
            
            # 执行握手过程
            if not self._perform_handshake():
                self.logger.error("握手过程失败")
                self.disconnect()
                return False
            
            # 启动接收线程
            self.rx_running = True
            self.rx_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self.rx_thread.start()
            
            # 启动心跳任务 (使用线程池)
            self.heartbeat_running = True
            self.thread_pool.submit(self._heartbeat_loop)
            
            self.connected = True
            self.logger.info("CAN连接和握手完成")
            return True
        except Exception as e:
            self.logger.error(f"连接CAN总线失败: {str(e)}")
            if self.bus:
                try:
                    self.bus.shutdown()
                except Exception:
                    pass
                self.bus = None
            return False
    
    def disconnect(self):
        """断开CAN总线连接"""
        self.connected = False
        self.rx_running = False
        self.heartbeat_running = False
        
        if self.rx_thread and self.rx_thread.is_alive():
            self.rx_thread.join(timeout=1.0)
            
        if self.bus:
            try:
                self.bus.shutdown()
            except Exception as e:
                self.logger.error(f"关闭CAN总线时发生错误: {str(e)}")
            self.bus = None
            
        self.logger.info("已断开CAN总线连接")
    
    def reconnect(self):
        """断开并重新连接CAN总线"""
        if not self.auto_reconnect:
            self.logger.warning("自动重连已禁用，跳过重连过程。")
            return

        # 使用锁确保只有一个线程在执行重连
        if not self.reconnect_lock.acquire(blocking=False):
            self.logger.info("重连已在进行中，跳过此次请求。")
            return
        
        try:
            self.logger.info("开始CAN总线重连过程...")
            
            # 首先断开现有连接
            self.disconnect()
            
            # 循环尝试直到重连成功
            while self.auto_reconnect:
                if self.connect():
                    self.logger.info("CAN总线重连成功！")
                    return
                else:
                    self.logger.warning(f"重连失败，将在 {self.reconnect_interval} 秒后重试。")
                    time.sleep(self.reconnect_interval)
        finally:
            self.reconnect_lock.release()

    def _perform_handshake(self) -> bool:
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
            
            # 等待握手响应
            start_time = time.time()
            while time.time() - start_time < 5:  # 等待5秒
                msg = self.bus.recv(timeout=0.1)
                if msg and msg.arbitration_id == self.HANDSHAKE_RECEIVE_ID:
                    self.logger.debug(f"收到握手响应: ID=0x{msg.arbitration_id:03X}, 数据={[hex(x) for x in msg.data]}")
                    # 将bytearray转换为列表进行比较
                    response_data = list(msg.data)
                    if response_data == self.HANDSHAKE_RESPONSE:
                        self.logger.info("收到正确的握手响应")
                        return True
                    else:
                        self.logger.error(f"收到错误的握手响应: {response_data}, 期望: {self.HANDSHAKE_RESPONSE}")
                        return False
                        
            self.logger.error("握手超时")
            return False
            
        except can.CanError as e:
            self.logger.error(f"握手过程中发生CAN错误: {e}")
            return False
        except Exception as e:
            self.logger.error(f"握手过程发生错误: {str(e)}")
            return False
    
    def _receive_loop(self):
        """接收消息循环，在独立线程中运行"""
        self.logger.info("接收线程已启动")
        
        while self.rx_running and self.bus:
            try:
                msg = self.bus.recv(timeout=0.1)
                if msg and msg.arbitration_id == self.RECEIVE_ID:
                    self.logger.debug(f"收到消息: ID=0x{msg.arbitration_id:03X}, 数据={[hex(x) for x in msg.data]}")
                    
                    if not msg.data:
                        self.logger.warning("收到空数据帧")
                        continue

                    command = msg.data[0]

                    # 检查是否为送料柜状态查询命令
                    if command == self.CMD_QUERY_PRINTER_FILAMENT_STATUS:
                        if self.query_callback:
                            self.thread_pool.submit(self.query_callback)
                    # 检查是否为料管映射查询命令
                    elif command == self.CMD_QUERY_FEEDER_MAPPING:
                        if self.mapping_query_callback:
                            self.thread_pool.submit(self.mapping_query_callback)
                    # 检查是否为设置料管映射命令（送料柜发送的）
                    elif command == self.CMD_SET_FEEDER_MAPPING:
                        if len(msg.data) >= 4 and msg.data[1] < 0x02 and msg.data[2] < 0x02:
                            mapping_data = {
                                'left_buffer': msg.data[1],   # 左缓冲区编号
                                'right_buffer': msg.data[2],  # 右缓冲区编号
                                'status': msg.data[3] if len(msg.data) > 3 else 0  # 状态字段
                            }
                            self.logger.info(f"收到设置料管映射命令: 左缓冲区={msg.data[1]}, 右缓冲区={msg.data[2]}, 状态={mapping_data['status']}")
                            if self.mapping_set_callback:
                                self.thread_pool.submit(self.mapping_set_callback, mapping_data)
                    # 检查是否为料管映射响应
                    elif command == self.CMD_FEEDER_MAPPING_RESPONSE:
                        if len(msg.data) >= 4:
                            mapping_data = {
                                'left_extruder': msg.data[1],
                                'right_extruder': msg.data[2],
                                'status': msg.data[3] if len(msg.data) > 3 else 0
                            }
                            self.logger.info(f"收到料管映射响应: 左挤出机={msg.data[1]}, 右挤出机={msg.data[2]}, 状态={mapping_data['status']}")
                            if self.mapping_response_callback:
                                self.thread_pool.submit(self.mapping_response_callback, mapping_data)
                    # 否则，视为普通状态消息
                    else:
                        # 将接收到的数据放入队列，即使数据不完整
                        status_data = {
                            'status': msg.data[0],
                            'progress': msg.data[1] if len(msg.data) > 1 else 0,
                            'error_code': msg.data[2] if len(msg.data) > 2 else 0,
                            'raw_data': list(msg.data)
                        }
                        
                        self.rx_queue.put(status_data)
                        
                        if self.status_callback:
                            # 异步处理状态更新
                            self.thread_pool.submit(self._process_status, status_data)
            except can.CanError as e:
                if self.rx_running:
                    self.logger.error(f"接收消息时发生CAN错误: {e}")
                    self.connected = False
                    self.thread_pool.submit(self.reconnect)
                    # 等待一段时间以避免在重连期间CPU空转
                    time.sleep(self.reconnect_interval)
            except Exception as e:
                if self.rx_running:  # 只在非主动停止时记录错误
                    self.logger.error(f"接收消息时发生错误: {str(e)}")
                    # 对于未知错误，也尝试重连
                    self.connected = False
                    self.thread_pool.submit(self.reconnect)
                    time.sleep(1)  # 防止错误消息刷屏
        
        self.logger.info("接收线程已结束")
    
    def _process_status(self, status_data: dict):
        """处理状态数据的回调，在线程池中运行"""
        try:
            self.status_callback(status_data)
        except Exception as e:
            self.logger.error(f"处理状态回调时发生错误: {str(e)}")

    def _process_receive_message(self, status_data: dict):
        """处理接收到的消息"""
        status_code = status_data.get('status')
        error_code = status_data.get('error_code')

        if status_code == self.STATUS_COMPLETE:
            self.logger.info("送料完成")
        elif status_code == self.STATUS_ERROR:
            self.logger.error(f"送料柜错误: {error_code}")
    
    def _heartbeat_loop(self):
        """心跳消息循环，在独立线程中运行"""
        self.logger.info("心跳线程已启动")
        
        while self.heartbeat_running and self.connected:
            try:
                self.send_message(self.CMD_HEARTBEAT)
                time.sleep(5)  # 每5秒发送一次心跳
            except Exception as e:
                if self.heartbeat_running:  # 只在非主动停止时记录错误
                    self.logger.error(f"发送心跳消息时发生错误: {str(e)}")
                    time.sleep(1)  # 防止错误消息刷屏
        
        self.logger.info("心跳线程已结束")
    
    
    def set_status_callback(self, callback: Callable):
        """
        设置状态回调函数
        
        Args:
            callback: 状态回调函数，接收一个状态数据字典
        """
        self.status_callback = callback
    
    def set_query_callback(self, callback: Callable):
        """
        设置查询回调函数
        
        Args:
            callback: 查询回调函数，无参数
        """
        self.query_callback = callback
    
    def set_mapping_query_callback(self, callback: Callable):
        """
        设置料管映射查询回调函数
        
        Args:
            callback: 料管映射查询回调函数，无参数
        """
        self.mapping_query_callback = callback
    
    def set_mapping_response_callback(self, callback: Callable):
        """
        设置料管映射响应回调函数
        
        Args:
            callback: 料管映射响应回调函数，接收一个映射数据字典
        """
        self.mapping_response_callback = callback
    
    def set_mapping_set_callback(self, callback: Callable):
        """
        设置料管映射设置回调函数（接收送料柜的设置命令）
        
        Args:
            callback: 料管映射设置回调函数，接收一个映射数据字典
        """
        self.mapping_set_callback = callback
    
    def _send_with_retry(self, msg: 'can.Message', retries: int = 3, retry_delay: float = 0.05) -> bool:
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
            # 只有在没有进行重连时才触发
            if not self.reconnect_lock.locked():
                self.thread_pool.submit(self.reconnect)
            return False

        with self.send_lock:
            last_exception = None
            for attempt in range(retries):
                try:
                    self.bus.send(msg)
                    # self.logger.debug(f"成功发送消息 (尝试 {attempt + 1})")
                    return True
                except can.CanError as e:
                    last_exception = e
                    error_str = str(e).lower()
                    # 如果设备不存在或网络关闭，则无需重试，立即失败并触发重连
                    if "no such device" in error_str or "network is down" in error_str:
                        self.logger.error(f"发送失败，CAN设备或网络不可用: {e}")
                        self.connected = False
                        self.thread_pool.submit(self.reconnect)
                        return False
                    
                    self.logger.warning(f"发送消息时发生CAN错误 (尝试 {attempt + 1}/{retries}): {e}")
                    if attempt < retries - 1:
                        time.sleep(retry_delay)
                except Exception as e:
                    self.logger.error(f"发送消息时发生未知错误: {e}")
                    self.connected = False
                    self.thread_pool.submit(self.reconnect)
                    return False
        
        self.logger.error(f"发送消息失败，已达到最大重试次数。最后一次错误: {last_exception}")
        self.connected = False
        if not self.reconnect_lock.locked():
            self.thread_pool.submit(self.reconnect)
        return False

    def send_message(self, cmd_type: int, extruder: int = 0) -> bool:
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
            # 构建8字节消息，根据协议格式（不再使用seq）
            data = [cmd_type, extruder, 0, 0, 0, 0, 0, 0]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )
            
            # 使用带重试的发送方法
            return self._send_with_retry(msg)
            
        except Exception as e:
            self.logger.error(f"构建或发送消息时发生未知错误: {str(e)}")
            return False
    
    def request_feed(self, extruder: int = 0) -> bool:
        """
        请求送料
        
        Args:
            extruder: 挤出机编号
            
        Returns:
            bool: 请求是否成功
        """
        return self.send_message(self.CMD_REQUEST_FEED, extruder)
    
    def stop_feed(self, extruder: int = 0) -> bool:
        """
        停止送料
        
        Args:
            extruder: 挤出机编号
            
        Returns:
            bool: 停止请求是否成功
        """
        return self.send_message(self.CMD_STOP_FEED, extruder)
    
    def query_status(self) -> bool:
        """
        查询送料柜状态
        
        Returns:
            bool: 查询是否成功
        """
        return self.send_message(self.CMD_QUERY_STATUS)
    
    def send_printer_error(self, error_code: int, extruder: int = 0) -> bool:
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
            return self.send_message(self.CMD_PRINTER_IDLE, extruder)
            
        return self.send_message(self.CMD_PRINTER_ERROR, extruder)
    
    def get_last_status(self) -> Optional[dict]:
        """
        获取最后一次状态更新
        
        Returns:
            Optional[dict]: 状态信息，如果未收到则返回None
        """
        try:
            # 非阻塞方式获取最新状态
            latest_status = None
            while not self.rx_queue.empty():
                latest_status = self.rx_queue.get_nowait()
                self.rx_queue.task_done()
            return latest_status
        except queue.Empty:
            return None
        except Exception as e:
            self.logger.error(f"获取状态时发生错误: {str(e)}")
            return None
    
    def send_filament_status_response(self, is_valid: bool, status_bitmap: int) -> bool:
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
            # 填充为8字节，后面补0
            data = [self.CMD_PRINTER_FILAMENT_STATUS_RESPONSE, validity_byte, status_bitmap, 0, 0, 0, 0, 0]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if self._send_with_retry(msg):
                thread_id = threading.get_ident()
                self.logger.info(f"已发送挤出机余料状态响应: ID=0x{self.SEND_ID:03X}, 数据={[hex(x) for x in data]}, 线程ID: {thread_id}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送挤出机余料状态响应时失败: {str(e)}")
            return False
    
    def set_feeder_mapping(self, left_tube_extruder: int, right_tube_extruder: int) -> bool:
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

            if self._send_with_retry(msg):
                self.logger.info(f"已发送料管映射设置: 左料管->挤出机{left_tube_extruder}, 右料管->挤出机{right_tube_extruder}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送料管映射设置时失败: {str(e)}")
            return False
    
    def query_feeder_mapping(self) -> bool:
        """
        查询料管与挤出机对应关系
        
        Returns:
            bool: 查询是否成功
        """
        return self.send_message(self.CMD_QUERY_FEEDER_MAPPING)
    
    def send_feeder_mapping_response(self, left_extruder: int, right_extruder: int, status: int = 0) -> bool:
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

            if self._send_with_retry(msg):
                self.logger.info(f"已发送料管映射响应: 左料管->挤出机{left_extruder}, 右料管->挤出机{right_extruder}, 状态={status}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送料管映射响应时失败: {str(e)}")
            return False
    
    def __del__(self):
        """析构方法，确保资源被清理"""
        self.auto_reconnect = False
        self.disconnect()
        # 关闭线程池
        self.thread_pool.shutdown(wait=True) 