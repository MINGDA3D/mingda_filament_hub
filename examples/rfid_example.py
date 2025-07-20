#!/usr/bin/env python3
"""
RFID数据接收示例程序

演示如何在送料柜系统中接收和解析RFID耗材信息
"""

import asyncio
import logging
import sys
from pathlib import Path

# 添加src目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from feeder_cabinet.can_communication import FeederCabinetCAN
from feeder_cabinet.rfid_parser import RFIDDataParser, OpenTagFilamentData


class RFIDExample:
    """RFID数据接收示例类"""
    
    def __init__(self):
        self.can = FeederCabinetCAN(interface='can1', bitrate=1000000)
        self.rfid_parser = RFIDDataParser()
        self.logger = logging.getLogger(__name__)
        
    async def setup(self):
        """初始化并连接CAN总线"""
        # 设置RFID数据回调
        self.can.set_rfid_callback(self.handle_rfid_message)
        
        # 连接CAN总线
        if await self.can.connect():
            self.logger.info("CAN总线连接成功")
            return True
        else:
            self.logger.error("CAN总线连接失败")
            return False
            
    async def handle_rfid_message(self, data: dict):
        """处理接收到的RFID CAN消息"""
        try:
            # 从CAN消息中提取数据
            can_data = bytes(data['data'])
            
            # 使用RFID解析器处理消息
            result = self.rfid_parser.handle_rfid_message(can_data)
            
            if result:
                if result['type'] == 'rfid_start':
                    self.logger.info(f"开始接收RFID数据: 挤出机{result['extruder_id']}, "
                                   f"耗材通道{result['filament_id']}, "
                                   f"数据源: {result['data_source']}")
                    
                elif result['type'] == 'rfid_packet':
                    self.logger.debug(f"接收RFID数据包 {result['packet_num']}/{result['total_packets']}")
                    
                elif result['type'] == 'rfid_complete':
                    self.logger.info("RFID数据接收完成!")
                    await self.process_filament_data(result['extruder_id'], 
                                                   result['filament_id'], 
                                                   result['data'])
                    
                elif result['type'] == 'rfid_error':
                    self.logger.error(f"RFID错误: {result.get('error_msg', result.get('error'))}")
                    
        except Exception as e:
            self.logger.error(f"处理RFID消息时发生错误: {e}", exc_info=True)
            
    async def process_filament_data(self, extruder_id: int, filament_id: int, 
                                  data: OpenTagFilamentData):
        """处理解析后的耗材数据"""
        self.logger.info("=" * 60)
        self.logger.info(f"挤出机 {extruder_id} 耗材信息 (通道 {filament_id}):")
        self.logger.info("=" * 60)
        
        # 基本信息
        self.logger.info(f"制造商: {data.manufacturer}")
        self.logger.info(f"材料类型: {data.material_name}")
        self.logger.info(f"颜色: {data.color_name}")
        
        # 规格参数
        self.logger.info(f"直径: {data.diameter_target/1000:.2f} mm")
        self.logger.info(f"标称重量: {data.weight_nominal} g")
        self.logger.info(f"密度: {data.density/1000:.3f} g/cm³")
        
        # 温度参数
        self.logger.info(f"打印温度: {data.print_temp}°C")
        self.logger.info(f"热床温度: {data.bed_temp}°C")
        
        # 可选参数
        if data.serial_number:
            self.logger.info(f"序列号: {data.serial_number}")
            
        if data.empty_spool_weight is not None:
            self.logger.info(f"空线轴重量: {data.empty_spool_weight} g")
            
        if data.filament_weight_measured is not None:
            self.logger.info(f"实测耗材重量: {data.filament_weight_measured} g")
            
        if data.filament_length_measured is not None:
            self.logger.info(f"实测耗材长度: {data.filament_length_measured} m")
            
        if data.max_dry_temp is not None:
            self.logger.info(f"最大干燥温度: {data.max_dry_temp}°C")
            
        if data.color_hex is not None:
            self.logger.info(f"颜色值: #{data.color_hex:06X}")
            
        self.logger.info("=" * 60)
        
        # 这里可以添加更多的数据处理逻辑，比如：
        # - 更新数据库
        # - 发送到Web界面
        # - 触发自动温度设置
        # - 等等
        
    async def request_rfid_data(self, extruder_id: int):
        """主动请求指定挤出机的RFID数据"""
        self.logger.info(f"请求挤出机 {extruder_id} 的RFID数据...")
        
        if await self.can.request_rfid_data(extruder_id):
            self.logger.info("RFID数据请求已发送")
        else:
            self.logger.error("RFID数据请求发送失败")
            
    async def cleanup_expired_sessions(self):
        """定期清理超时的RFID传输会话"""
        while True:
            try:
                await asyncio.sleep(30)  # 每30秒清理一次
                self.rfid_parser.cleanup_expired_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"清理超时会话时发生错误: {e}")
                
    async def run(self):
        """运行示例程序"""
        if not await self.setup():
            return
            
        # 启动清理任务
        cleanup_task = asyncio.create_task(self.cleanup_expired_sessions())
        
        try:
            # 示例：等待5秒后请求挤出机0的RFID数据
            await asyncio.sleep(5)
            await self.request_rfid_data(0)
            
            # 持续运行，等待RFID数据
            self.logger.info("等待接收RFID数据...")
            await asyncio.Event().wait()  # 永久等待
            
        except KeyboardInterrupt:
            self.logger.info("程序被用户中断")
        finally:
            cleanup_task.cancel()
            await self.can.disconnect()
            

async def main():
    """主函数"""
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 创建并运行示例
    example = RFIDExample()
    await example.run()


if __name__ == "__main__":
    asyncio.run(main())