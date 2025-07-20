#!/usr/bin/env python3
"""
RFID通信诊断脚本

用于诊断RFID数据传输超时问题
"""

import asyncio
import can
import logging
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# RFID命令定义
CMD_RFID_RAW_DATA_NOTIFY = 0x14
CMD_RFID_DATA_PACKET = 0x17
CMD_RFID_DATA_END = 0x18
CMD_RFID_READ_ERROR = 0x19


class RFIDDiagnostic:
    def __init__(self, interface='can1'):
        self.interface = interface
        self.bus = None
        self.rfid_session = None
        
    def connect(self):
        """连接到CAN总线"""
        try:
            self.bus = can.interface.Bus(
                channel=self.interface,
                bustype='socketcan',
                bitrate=1000000
            )
            logger.info(f"已连接到CAN接口: {self.interface}")
            return True
        except Exception as e:
            logger.error(f"连接CAN失败: {e}")
            return False
            
    def monitor_rfid(self, duration=30):
        """监控RFID消息"""
        logger.info(f"开始监控RFID消息，持续 {duration} 秒...")
        logger.info("提示：请在送料柜上触发RFID读取")
        
        start_time = datetime.now()
        packet_count = 0
        rfid_messages = []
        
        while (datetime.now() - start_time).seconds < duration:
            msg = self.bus.recv(timeout=1.0)
            if msg:
                # 检查是否是来自送料柜的消息
                if msg.arbitration_id == 0x10B:  # 送料柜 -> 打印机
                    if len(msg.data) > 0:
                        cmd = msg.data[0]
                        
                        # 检查是否是RFID相关命令
                        if cmd in [CMD_RFID_RAW_DATA_NOTIFY, CMD_RFID_DATA_PACKET, 
                                  CMD_RFID_DATA_END, CMD_RFID_READ_ERROR]:
                            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                            hex_data = ' '.join([f'{b:02X}' for b in msg.data])
                            
                            logger.info(f"[{timestamp}] RFID消息: 0x{cmd:02X} - {hex_data}")
                            rfid_messages.append({
                                'time': timestamp,
                                'cmd': cmd,
                                'data': list(msg.data)
                            })
                            
                            # 解析消息
                            if cmd == CMD_RFID_RAW_DATA_NOTIFY:
                                seq = msg.data[1]
                                channel = msg.data[2]
                                total_packets = msg.data[3]
                                data_len = (msg.data[4] << 8) | msg.data[5]
                                extruder = msg.data[6]
                                source = msg.data[7]
                                
                                logger.info(f"  起始包: 序列号={seq}, 通道={channel}, "
                                          f"总包数={total_packets}, 数据长度={data_len}, "
                                          f"挤出机={extruder}, 数据源={'RFID' if source==0 else '手动'}")
                                
                                self.rfid_session = {
                                    'seq': seq,
                                    'total': total_packets,
                                    'received': 0,
                                    'start_time': datetime.now()
                                }
                                
                            elif cmd == CMD_RFID_DATA_PACKET:
                                seq = msg.data[1]
                                packet_num = msg.data[2]
                                valid_bytes = msg.data[3]
                                
                                logger.info(f"  数据包: 序列号={seq}, 包号={packet_num}, "
                                          f"有效字节={valid_bytes}")
                                
                                if self.rfid_session and self.rfid_session['seq'] == seq:
                                    self.rfid_session['received'] += 1
                                    packet_count += 1
                                    
                            elif cmd == CMD_RFID_DATA_END:
                                seq = msg.data[1]
                                total = msg.data[2]
                                checksum = (msg.data[3] << 8) | msg.data[4]
                                status = msg.data[5]
                                
                                logger.info(f"  结束包: 序列号={seq}, 总包数={total}, "
                                          f"校验和=0x{checksum:04X}, 状态={status}")
                                
                                if self.rfid_session and self.rfid_session['seq'] == seq:
                                    duration = (datetime.now() - self.rfid_session['start_time']).total_seconds()
                                    logger.info(f"  传输完成: 收到 {self.rfid_session['received']}/{self.rfid_session['total']} 包, "
                                              f"耗时 {duration:.2f} 秒")
                                    self.rfid_session = None
                                    
                            elif cmd == CMD_RFID_READ_ERROR:
                                seq = msg.data[1]
                                extruder = msg.data[2]
                                error_code = msg.data[3]
                                ext_error = msg.data[4]
                                
                                error_map = {
                                    0x01: "RFID读取失败",
                                    0x02: "无耗材或未检测到",
                                    0x03: "数据格式无效",
                                    0x04: "操作超时",
                                    0x05: "无挤出机映射",
                                    0x06: "系统忙"
                                }
                                
                                error_msg = error_map.get(error_code, f"未知错误(0x{error_code:02X})")
                                logger.error(f"  错误响应: 挤出机={extruder}, 错误={error_msg}, "
                                           f"扩展错误=0x{ext_error:02X}")
                                
        # 总结
        logger.info("\n=== RFID监控总结 ===")
        logger.info(f"监控时长: {duration} 秒")
        logger.info(f"收到RFID消息数: {len(rfid_messages)}")
        logger.info(f"收到数据包数: {packet_count}")
        
        if self.rfid_session:
            logger.warning(f"警告: 存在未完成的传输会话 - 序列号{self.rfid_session['seq']}, "
                         f"收到 {self.rfid_session['received']}/{self.rfid_session['total']} 包")
            
        # 分析消息间隔
        if len(rfid_messages) > 1:
            logger.info("\n消息时间分析:")
            for i in range(1, len(rfid_messages)):
                prev_time = datetime.strptime(rfid_messages[i-1]['time'], '%H:%M:%S.%f')
                curr_time = datetime.strptime(rfid_messages[i]['time'], '%H:%M:%S.%f')
                interval = (curr_time - prev_time).total_seconds() * 1000  # 毫秒
                
                cmd_name = {
                    0x14: "起始包",
                    0x17: "数据包",
                    0x18: "结束包",
                    0x19: "错误包"
                }.get(rfid_messages[i]['cmd'], f"未知(0x{rfid_messages[i]['cmd']:02X})")
                
                logger.info(f"  {rfid_messages[i-1]['time']} -> {rfid_messages[i]['time']}: "
                          f"{interval:.1f}ms ({cmd_name})")
                          
    def send_rfid_ack(self, sequence):
        """发送RFID确认消息（如果需要）"""
        # 注意：标准协议可能不需要确认，这里仅用于测试
        msg = can.Message(
            arbitration_id=0x10A,  # 打印机 -> 送料柜
            data=[0x20, sequence, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],  # 假设的确认命令
            is_extended_id=False
        )
        self.bus.send(msg)
        logger.info(f"发送确认消息: 序列号={sequence}")
        
    def close(self):
        """关闭CAN连接"""
        if self.bus:
            self.bus.shutdown()
            logger.info("CAN连接已关闭")


def main():
    """主函数"""
    diag = RFIDDiagnostic()
    
    if not diag.connect():
        return
        
    try:
        # 监控RFID消息
        diag.monitor_rfid(duration=60)  # 监控60秒
        
    except KeyboardInterrupt:
        logger.info("\n监控被用户中断")
    finally:
        diag.close()
        
    logger.info("\n诊断建议:")
    logger.info("1. 如果只收到起始包没有数据包，可能是:")
    logger.info("   - 送料柜在等待某种确认信号")
    logger.info("   - 送料柜端的发送逻辑有问题")
    logger.info("   - CAN总线存在通信问题")
    logger.info("2. 如果收到错误响应0x04(超时)，说明送料柜端也检测到了超时")
    logger.info("3. 检查数据包之间的时间间隔是否正常（应该约50ms）")
    logger.info("4. 使用 candump can1 | grep 10B 同时监控原始CAN数据")


if __name__ == "__main__":
    main()