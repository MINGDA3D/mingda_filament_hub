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
from typing import Optional, Callable, Dict, List, Any, Union
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
        self.thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="can_comm_")
        
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
        self.seq_number = 0  # 消息序列号
        self.status_callback = None  # 状态回调函数
        
        # 接收消息线程
        self.rx_thread = None
        self.rx_running = False
        self.rx_queue = queue.Queue()
        
        # 心跳线程
        self.heartbeat_thread = None
        self.heartbeat_running = False
        
        # 锁，用于线程同步
        self.send_lock = threading.Lock()
    
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
            self.disconnect()
            return False
    
    def disconnect(self):
        """断开CAN总线连接"""
        self.rx_running = False
        self.heartbeat_running = False
        
        if self.rx_thread and self.rx_thread.is_alive():
            self.rx_thread.join(timeout=1.0)
            
        # 关闭线程池（允许已提交任务完成）
        self.thread_pool.shutdown(wait=False)
            
        if self.bus:
            try:
                self.bus.shutdown()
            except Exception as e:
                self.logger.error(f"关闭CAN总线时发生错误: {str(e)}")
            self.bus = None
            
        self.connected = False
        self.logger.info("已断开CAN总线连接")
        
        # 重新创建线程池，以便重新连接时使用
        self.thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="can_comm_")
    
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
                    
                    # 解析状态消息
                    if len(msg.data) >= 3:
                        status_data = {
                            'status': msg.data[0],    # 状态码
                            'progress': msg.data[1],  # 进度 (0-100)
                            'error_code': msg.data[2] # 错误码
                        }
                        
                        # 使用队列传递状态数据，避免阻塞接收线程
                        self.rx_queue.put(status_data)
                        
                        # 如果有状态回调函数，使用线程池调用
                        if self.status_callback:
                            self.thread_pool.submit(self._process_status, status_data)
            except Exception as e:
                if self.rx_running:  # 只在非主动停止时记录错误
                    self.logger.error(f"接收消息时发生错误: {str(e)}")
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
                time.sleep(30)  # 每30秒发送一次心跳
            except Exception as e:
                if self.heartbeat_running:  # 只在非主动停止时记录错误
                    self.logger.error(f"发送心跳消息时发生错误: {str(e)}")
                    time.sleep(1)  # 防止错误消息刷屏
        
        self.logger.info("心跳线程已结束")
    
    def _get_next_seq(self) -> int:
        """获取下一个序列号 (1-255)"""
        with self.send_lock:
            self.seq_number = (self.seq_number % 255) + 1
            return self.seq_number
    
    def set_status_callback(self, callback: Callable):
        """
        设置状态回调函数
        
        Args:
            callback: 状态回调函数，接收一个状态数据字典
        """
        self.status_callback = callback
    
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
            seq = self._get_next_seq()
            
            # 构建8字节消息，根据协议格式
            data = [cmd_type, seq, extruder, 0, 0, 0, 0, 0]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )
            
            with self.send_lock:
                self.bus.send(msg)
                
            self.logger.debug(f"已发送消息: ID=0x{self.SEND_ID:03X}, 命令={hex(cmd_type)}, 序列号={seq}")
            return True
            
        except Exception as e:
            self.logger.error(f"发送消息失败: {str(e)}")
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
    
    def __del__(self):
        """析构方法，确保资源被清理"""
        try:
            # 关闭线程池
            if hasattr(self, 'thread_pool'):
                self.thread_pool.shutdown(wait=False)
                
            # 关闭CAN总线
            if hasattr(self, 'bus') and self.bus:
                self.bus.shutdown()
        except Exception as e:
            # 析构方法中不应抛出异常
            pass 