import can
import logging
from typing import Optional, Callable

class FeederCabinetCAN:
    def __init__(self, interface: str = 'can1', bitrate: int = 1000000):
        """
        初始化CAN通信类
        
        Args:
            interface: CAN接口名称
            bitrate: CAN总线波特率
        """
        self.interface = interface
        self.bitrate = bitrate
        self.bus = None
        self.logger = logging.getLogger(__name__)
        
        # CAN消息ID定义
        self.SEND_ID = 0x10A  # 打印机 -> 送料柜
        self.RECEIVE_ID = 0x10B  # 送料柜 -> 打印机
        
        # 命令类型定义
        self.CMD_REQUEST_FEED = 0x01
        self.CMD_STOP_FEED = 0x02
        self.CMD_QUERY_STATUS = 0x03
        self.CMD_PRINTING = 0x04
        self.CMD_PRINT_COMPLETE = 0x05
        self.CMD_PRINT_PAUSE = 0x06
        self.CMD_PRINT_CANCEL = 0x07
        self.CMD_PRINTER_IDLE = 0x08
        self.CMD_PRINTER_ERROR = 0x09  # 新增：打印机错误状态命令
        
        # 状态码定义
        self.STATUS_IDLE = 0x00
        self.STATUS_READY = 0x01
        self.STATUS_FEEDING = 0x02
        self.STATUS_COMPLETE = 0x03
        self.STATUS_ERROR = 0x04
        
        # 错误码定义
        self.ERROR_NONE = 0x00
        self.ERROR_MECHANICAL = 0x01
        self.ERROR_MATERIAL_MISSING = 0x02
        self.ERROR_OTHER = 0x03
        self.ERROR_KLIPPER = 0x04  # 新增：Klipper错误
        self.ERROR_MOONRAKER = 0x05  # 新增：Moonraker错误
        self.ERROR_COMMUNICATION = 0x06  # 新增：通信错误
        
        self._status_callback = None
        
    def connect(self) -> bool:
        """
        连接到CAN总线
        
        Returns:
            bool: 连接是否成功
        """
        try:
            self.bus = can.interface.Bus(
                channel=self.interface,
                bustype='socketcan',
                bitrate=self.bitrate
            )
            self.logger.info(f"成功连接到CAN总线 {self.interface}")
            return True
        except Exception as e:
            self.logger.error(f"连接CAN总线失败: {str(e)}")
            return False
            
    def disconnect(self):
        """断开CAN总线连接"""
        if self.bus:
            self.bus.shutdown()
            self.logger.info("已断开CAN总线连接")
            
    def set_status_callback(self, callback: Callable):
        """
        设置状态回调函数
        
        Args:
            callback: 回调函数，接收状态信息的函数
        """
        self._status_callback = callback
        
    def send_message(self, command: int, extruder: int = 0) -> bool:
        """
        发送消息到送料柜
        
        Args:
            command: 命令类型
            extruder: 挤出头编号
            
        Returns:
            bool: 发送是否成功
        """
        if not self.bus:
            self.logger.error("CAN总线未连接")
            return False
            
        try:
            data = [command, extruder] + [0] * 6
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )
            self.bus.send(msg)
            self.logger.debug(f"发送消息: 命令={command}, 挤出头={extruder}")
            return True
        except Exception as e:
            self.logger.error(f"发送消息失败: {str(e)}")
            return False
            
    def request_feed(self, extruder: int = 0) -> bool:
        """
        请求补料
        
        Args:
            extruder: 挤出头编号
            
        Returns:
            bool: 请求是否成功
        """
        return self.send_message(self.CMD_REQUEST_FEED, extruder)
        
    def stop_feed(self, extruder: int = 0) -> bool:
        """
        停止补料
        
        Args:
            extruder: 挤出头编号
            
        Returns:
            bool: 请求是否成功
        """
        return self.send_message(self.CMD_STOP_FEED, extruder)
        
    def query_status(self, extruder: int = 0) -> bool:
        """
        查询状态
        
        Args:
            extruder: 挤出头编号
            
        Returns:
            bool: 请求是否成功
        """
        return self.send_message(self.CMD_QUERY_STATUS, extruder)
        
    def start_receiving(self):
        """开始接收消息"""
        if not self.bus:
            self.logger.error("CAN总线未连接")
            return
            
        try:
            while True:
                msg = self.bus.recv()
                if msg.arbitration_id == self.RECEIVE_ID:
                    self._process_message(msg)
        except Exception as e:
            self.logger.error(f"接收消息失败: {str(e)}")
            
    def _process_message(self, msg: can.Message):
        """
        处理接收到的消息
        
        Args:
            msg: CAN消息对象
        """
        status = msg.data[0]
        progress = msg.data[1]
        error_code = msg.data[2]
        
        if self._status_callback:
            self._status_callback({
                'status': status,
                'progress': progress,
                'error_code': error_code
            })
            
        self.logger.debug(f"收到消息: 状态={status}, 进度={progress}, 错误码={error_code}")
        
    def send_printer_error(self, error_code: int = 0x04, extruder: int = 0) -> bool:
        """
        发送打印机错误状态
        
        Args:
            error_code: 错误码
            extruder: 挤出头编号
            
        Returns:
            bool: 发送是否成功
        """
        return self.send_message(self.CMD_PRINTER_ERROR, extruder) 