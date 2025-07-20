"""
RFID数据解析模块 - 解析从送料柜接收的RFID耗材信息

此模块提供OpenTag格式RFID数据的解析功能，包括：
- 分包数据接收和重组
- OpenTag数据结构解析
- 数据完整性校验
- 错误处理
"""

import struct
import logging
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


# CAN命令定义
CMD_RFID_RAW_DATA_NOTIFY = 0x14    # 主动通知RFID原始数据（起始包）
CMD_RFID_RAW_DATA_REQUEST = 0x15   # 请求RFID原始数据
CMD_RFID_RAW_DATA_RESPONSE = 0x16  # RFID原始数据响应（起始包）
CMD_RFID_DATA_PACKET = 0x17        # RFID数据包
CMD_RFID_DATA_END = 0x18           # RFID数据传输结束
CMD_RFID_READ_ERROR = 0x19         # RFID读取错误

# 错误码定义
RFID_ERR_READ_FAIL = 0x01      # RFID读取失败
RFID_ERR_NO_FILAMENT = 0x02    # 无耗材或未检测到
RFID_ERR_INVALID_DATA = 0x03   # 数据格式无效
RFID_ERR_TIMEOUT = 0x04         # 操作超时
RFID_ERR_NO_MAPPING = 0x05     # 无挤出机映射
RFID_ERR_BUSY = 0x06            # 系统忙，无法处理


@dataclass
class OpenTagFilamentData:
    """OpenTag耗材数据结构"""
    # 必需数据
    tag_version: int = 0
    manufacturer: str = ""
    material_name: str = ""
    color_name: str = ""
    diameter_target: int = 1750      # μm
    weight_nominal: int = 1000       # g
    print_temp: int = 210            # °C
    bed_temp: int = 60               # °C
    density: int = 1240              # μg/cm³
    
    # 可选数据
    serial_number: str = ""
    manufacture_date: Optional[datetime] = None
    spool_core_diameter: Optional[int] = None
    mfi: Optional[int] = None
    tolerance_measured: Optional[int] = None
    additional_data_url: str = ""
    empty_spool_weight: Optional[int] = None    # g
    filament_weight_measured: Optional[int] = None  # g
    filament_length_measured: Optional[int] = None  # m
    transmission_distance: Optional[int] = None
    color_hex: Optional[int] = None
    max_dry_temp: Optional[int] = None  # °C


@dataclass
class RFIDTransferSession:
    """RFID数据传输会话"""
    sequence: int
    extruder_id: int
    filament_id: int
    total_packets: int
    data_length: int
    data_source: int  # 0=RFID读取, 1=手动输入
    received_packets: Dict[int, bytes] = field(default_factory=dict)
    start_time: float = 0
    complete: bool = False
    

class RFIDDataParser:
    """RFID数据解析器"""
    
    def __init__(self):
        self.active_sessions: Dict[int, RFIDTransferSession] = {}
        self.completed_data: Dict[int, OpenTagFilamentData] = {}  # 按挤出机ID存储
        
    def handle_rfid_message(self, data: bytes) -> Optional[Dict]:
        """处理RFID相关的CAN消息
        
        Args:
            data: CAN消息数据（8字节）
            
        Returns:
            解析结果字典，包含消息类型和相关数据
        """
        if len(data) < 8:
            logger.error(f"RFID消息长度不足: {len(data)} 字节")
            return None
            
        cmd = data[0]
        
        if cmd == CMD_RFID_RAW_DATA_NOTIFY:
            return self._handle_notify_start(data)
        elif cmd == CMD_RFID_RAW_DATA_RESPONSE:
            return self._handle_response_start(data)
        elif cmd == CMD_RFID_DATA_PACKET:
            return self._handle_data_packet(data)
        elif cmd == CMD_RFID_DATA_END:
            return self._handle_data_end(data)
        elif cmd == CMD_RFID_READ_ERROR:
            return self._handle_error(data)
        else:
            return None
            
    def _handle_notify_start(self, data: bytes) -> Dict:
        """处理主动通知起始包"""
        sequence = data[1]
        filament_id = data[2]
        total_packets = data[3]
        data_length = (data[4] << 8) | data[5]
        extruder_id = data[6]
        data_source = data[7]
        
        # 创建新的传输会话
        session = RFIDTransferSession(
            sequence=sequence,
            extruder_id=extruder_id,
            filament_id=filament_id,
            total_packets=total_packets,
            data_length=data_length,
            data_source=data_source,
            start_time=datetime.now().timestamp()
        )
        self.active_sessions[sequence] = session
        
        logger.info(f"开始接收RFID数据: 挤出机{extruder_id}, 耗材通道{filament_id}, "
                   f"总包数{total_packets}, 数据长度{data_length}字节")
        
        return {
            'type': 'rfid_start',
            'extruder_id': extruder_id,
            'filament_id': filament_id,
            'data_source': 'rfid' if data_source == 0 else 'manual'
        }
        
    def _handle_response_start(self, data: bytes) -> Dict:
        """处理查询响应起始包"""
        # 与通知包类似，但字段顺序略有不同
        sequence = data[1]
        extruder_id = data[2]
        total_packets = data[3]
        data_length = (data[4] << 8) | data[5]
        filament_id = data[6]
        data_source = data[7]
        
        session = RFIDTransferSession(
            sequence=sequence,
            extruder_id=extruder_id,
            filament_id=filament_id,
            total_packets=total_packets,
            data_length=data_length,
            data_source=data_source,
            start_time=datetime.now().timestamp()
        )
        self.active_sessions[sequence] = session
        
        return {
            'type': 'rfid_response_start',
            'extruder_id': extruder_id,
            'filament_id': filament_id
        }
        
    def _handle_data_packet(self, data: bytes) -> Optional[Dict]:
        """处理数据包"""
        sequence = data[1]
        packet_num = data[2]
        valid_bytes = data[3]
        packet_data = data[4:4+valid_bytes]
        
        session = self.active_sessions.get(sequence)
        if not session:
            logger.warning(f"收到未知序列号的数据包: {sequence}")
            return None
            
        # 存储数据包
        session.received_packets[packet_num] = packet_data
        
        logger.debug(f"接收数据包 {packet_num}/{session.total_packets}, "
                    f"有效字节数: {valid_bytes}")
        
        return {
            'type': 'rfid_packet',
            'packet_num': packet_num,
            'total_packets': session.total_packets
        }
        
    def _handle_data_end(self, data: bytes) -> Optional[Dict]:
        """处理传输结束包"""
        sequence = data[1]
        total_packets = data[2]
        checksum = (data[3] << 8) | data[4]
        status = data[5]
        
        session = self.active_sessions.get(sequence)
        if not session:
            logger.warning(f"收到未知序列号的结束包: {sequence}")
            return None
            
        # 重组数据
        raw_data = self._reassemble_data(session)
        if raw_data is None:
            logger.error(f"数据重组失败: 序列号{sequence}")
            return {'type': 'rfid_error', 'error': 'reassemble_failed'}
            
        # 验证校验和
        calc_checksum = sum(raw_data) & 0xFFFF
        if calc_checksum != checksum:
            logger.error(f"校验和错误: 期望{checksum}, 实际{calc_checksum}")
            return {'type': 'rfid_error', 'error': 'checksum_failed'}
            
        # 解析OpenTag数据
        try:
            filament_data = self._parse_opentag_data(raw_data)
            self.completed_data[session.extruder_id] = filament_data
            
            logger.info(f"成功接收RFID数据: 挤出机{session.extruder_id}, "
                       f"材料: {filament_data.material_name}, "
                       f"颜色: {filament_data.color_name}")
            
            # 清理会话
            del self.active_sessions[sequence]
            
            return {
                'type': 'rfid_complete',
                'extruder_id': session.extruder_id,
                'filament_id': session.filament_id,
                'data': filament_data
            }
            
        except Exception as e:
            logger.error(f"解析OpenTag数据失败: {e}")
            return {'type': 'rfid_error', 'error': 'parse_failed'}
            
    def _handle_error(self, data: bytes) -> Dict:
        """处理错误响应"""
        sequence = data[1]
        extruder_id = data[2]
        error_code = data[3]
        ext_error = data[4]
        
        error_map = {
            RFID_ERR_READ_FAIL: "RFID读取失败",
            RFID_ERR_NO_FILAMENT: "无耗材或未检测到",
            RFID_ERR_INVALID_DATA: "数据格式无效",
            RFID_ERR_TIMEOUT: "操作超时",
            RFID_ERR_NO_MAPPING: "无挤出机映射",
            RFID_ERR_BUSY: "系统忙"
        }
        
        error_msg = error_map.get(error_code, f"未知错误: {error_code}")
        logger.error(f"RFID错误: 挤出机{extruder_id}, {error_msg}")
        
        return {
            'type': 'rfid_error',
            'extruder_id': extruder_id,
            'error_code': error_code,
            'error_msg': error_msg
        }
        
    def _reassemble_data(self, session: RFIDTransferSession) -> Optional[bytes]:
        """重组分包数据"""
        if len(session.received_packets) != session.total_packets:
            logger.error(f"数据包不完整: 收到{len(session.received_packets)}, "
                        f"期望{session.total_packets}")
            return None
            
        # 按包序号排序并拼接
        data = bytearray()
        for i in range(1, session.total_packets + 1):
            if i not in session.received_packets:
                logger.error(f"缺少数据包: {i}")
                return None
            data.extend(session.received_packets[i])
            
        # 截取到实际数据长度
        return bytes(data[:session.data_length])
        
    def _parse_opentag_data(self, data: bytes) -> OpenTagFilamentData:
        """解析OpenTag格式数据
        
        注意：所有多字节数值使用小端格式
        """
        if len(data) < 89:  # 最小必需字段长度
            raise ValueError(f"数据长度不足: {len(data)} 字节")
            
        filament = OpenTagFilamentData()
        
        # 解析必需字段
        offset = 0
        filament.tag_version = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        
        filament.manufacturer = self._extract_string(data, offset, 16)
        offset += 16
        
        filament.material_name = self._extract_string(data, offset, 16)
        offset += 16
        
        filament.color_name = self._extract_string(data, offset, 32)
        offset += 32
        
        filament.diameter_target = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        
        filament.weight_nominal = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        
        filament.print_temp = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        
        filament.bed_temp = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        
        filament.density = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        
        # 解析可选字段（如果数据长度足够）
        if len(data) > offset:
            filament.serial_number = self._extract_string(data, offset, 16)
            offset += 16
            
        if len(data) > offset + 8:
            manufacture_date = struct.unpack_from('<I', data, offset)[0]
            manufacture_time = struct.unpack_from('<I', data, offset + 4)[0]
            if manufacture_date != 0xFFFFFFFF:
                # 转换Unix时间戳
                filament.manufacture_date = datetime.fromtimestamp(manufacture_date)
            offset += 8
            
        if len(data) > offset:
            filament.spool_core_diameter = data[offset]
            if filament.spool_core_diameter == 0xFF:
                filament.spool_core_diameter = None
            offset += 1
            
        if len(data) > offset:
            filament.mfi = data[offset]
            if filament.mfi == 0xFF:
                filament.mfi = None
            offset += 1
            
        if len(data) > offset:
            filament.tolerance_measured = data[offset]
            if filament.tolerance_measured == 0xFF:
                filament.tolerance_measured = None
            offset += 1
            
        if len(data) > offset + 32:
            filament.additional_data_url = self._extract_string(data, offset, 32)
            offset += 32
            
        if len(data) > offset + 2:
            filament.empty_spool_weight = struct.unpack_from('<H', data, offset)[0]
            if filament.empty_spool_weight == 0xFFFF:
                filament.empty_spool_weight = None
            offset += 2
            
        if len(data) > offset + 2:
            filament.filament_weight_measured = struct.unpack_from('<H', data, offset)[0]
            if filament.filament_weight_measured == 0xFFFF:
                filament.filament_weight_measured = None
            offset += 2
            
        if len(data) > offset + 2:
            filament.filament_length_measured = struct.unpack_from('<H', data, offset)[0]
            if filament.filament_length_measured == 0xFFFF:
                filament.filament_length_measured = None
            offset += 2
            
        if len(data) > offset + 2:
            filament.transmission_distance = struct.unpack_from('<H', data, offset)[0]
            if filament.transmission_distance == 0xFFFF:
                filament.transmission_distance = None
            offset += 2
            
        if len(data) > offset + 4:
            filament.color_hex = struct.unpack_from('<I', data, offset)[0]
            if filament.color_hex == 0xFFFFFFFF:
                filament.color_hex = None
            offset += 4
            
        if len(data) > offset:
            filament.max_dry_temp = data[offset]
            if filament.max_dry_temp == 0xFF:
                filament.max_dry_temp = None
                
        return filament
        
    def _extract_string(self, data: bytes, offset: int, length: int) -> str:
        """从数据中提取字符串"""
        try:
            string_data = data[offset:offset+length]
            # 查找NULL终止符
            null_pos = string_data.find(b'\x00')
            if null_pos != -1:
                string_data = string_data[:null_pos]
            return string_data.decode('utf-8', errors='ignore').strip()
        except:
            return ""
            
    def get_filament_data(self, extruder_id: int) -> Optional[OpenTagFilamentData]:
        """获取指定挤出机的耗材数据"""
        return self.completed_data.get(extruder_id)
        
    def request_rfid_data(self, extruder_id: int) -> bytes:
        """生成请求RFID数据的CAN消息"""
        msg = bytearray(8)
        msg[0] = CMD_RFID_RAW_DATA_REQUEST
        msg[1] = 0  # 序列号（应由CAN模块管理）
        msg[2] = extruder_id
        return bytes(msg)
        
    def cleanup_expired_sessions(self, timeout: float = 10.0):
        """清理超时的传输会话"""
        current_time = datetime.now().timestamp()
        expired = []
        
        for seq, session in self.active_sessions.items():
            if current_time - session.start_time > timeout:
                expired.append(seq)
                
        for seq in expired:
            logger.warning(f"清理超时的RFID传输会话: 序列号{seq}")
            del self.active_sessions[seq]


# 使用示例
if __name__ == "__main__":
    # 设置日志
    logging.basicConfig(level=logging.DEBUG)
    
    # 创建解析器
    parser = RFIDDataParser()
    
    # 模拟接收数据
    # 起始包
    start_msg = bytes([
        CMD_RFID_RAW_DATA_NOTIFY,  # 命令
        0x01,  # 序列号
        0x00,  # 耗材通道0
        0x05,  # 总包数5
        0x00, 0x94,  # 数据长度148字节
        0x00,  # 挤出机0
        0x00   # RFID读取
    ])
    
    result = parser.handle_rfid_message(start_msg)
    print(f"起始包结果: {result}")
    
    # 数据包示例
    data_packet = bytes([
        CMD_RFID_DATA_PACKET,
        0x01,  # 序列号
        0x01,  # 包序号1
        0x04,  # 有效字节4
        0x01, 0x00,  # tag_version = 1
        0x4D, 0x69   # "Mi" (manufacturer开始)
    ])
    
    result = parser.handle_rfid_message(data_packet)
    print(f"数据包结果: {result}")