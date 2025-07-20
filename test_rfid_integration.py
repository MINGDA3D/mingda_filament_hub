#!/usr/bin/env python3
"""
RFID集成测试脚本

用于测试RFID功能是否正确集成到主程序中
"""

import asyncio
import logging
import sys
from pathlib import Path

# 添加src目录到Python路径
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from feeder_cabinet.main import FeederCabinetApp
from feeder_cabinet.rfid_parser import CMD_RFID_RAW_DATA_NOTIFY, CMD_RFID_DATA_PACKET, CMD_RFID_DATA_END


async def simulate_rfid_data():
    """模拟RFID数据发送"""
    # 创建应用实例
    app = FeederCabinetApp("config/config.yaml")
    
    # 初始化应用
    if not app.init():
        print("应用初始化失败")
        return
        
    # 模拟RFID数据
    print("\n=== 模拟RFID数据接收 ===\n")
    
    # 1. 起始包
    start_msg = {
        'command': CMD_RFID_RAW_DATA_NOTIFY,
        'data': [
            CMD_RFID_RAW_DATA_NOTIFY,  # 命令
            0x01,  # 序列号
            0x00,  # 耗材通道0
            0x03,  # 总包数3
            0x00, 0x0C,  # 数据长度12字节
            0x00,  # 挤出机0
            0x00   # RFID读取
        ]
    }
    
    print("发送RFID起始包...")
    await app._handle_rfid_message(start_msg)
    await asyncio.sleep(0.1)
    
    # 2. 数据包1 - 版本和制造商开始
    data_packet1 = {
        'command': CMD_RFID_DATA_PACKET,
        'data': [
            CMD_RFID_DATA_PACKET,
            0x01,  # 序列号
            0x01,  # 包序号1
            0x04,  # 有效字节4
            0x01, 0x00,  # tag_version = 1
            0x4D, 0x44   # "MD" (制造商开始)
        ]
    }
    
    print("发送数据包1...")
    await app._handle_rfid_message(data_packet1)
    await asyncio.sleep(0.1)
    
    # 3. 数据包2 - 继续制造商
    data_packet2 = {
        'command': CMD_RFID_DATA_PACKET,
        'data': [
            CMD_RFID_DATA_PACKET,
            0x01,  # 序列号
            0x02,  # 包序号2
            0x04,  # 有效字节4
            0x00, 0x00,  # 填充
            0x00, 0x00   # 填充
        ]
    }
    
    print("发送数据包2...")
    await app._handle_rfid_message(data_packet2)
    await asyncio.sleep(0.1)
    
    # 4. 数据包3 - 材料名称
    data_packet3 = {
        'command': CMD_RFID_DATA_PACKET,
        'data': [
            CMD_RFID_DATA_PACKET,
            0x01,  # 序列号
            0x03,  # 包序号3
            0x04,  # 有效字节4
            0x50, 0x4C,  # "PL"
            0x41, 0x00   # "A\0"
        ]
    }
    
    print("发送数据包3...")
    await app._handle_rfid_message(data_packet3)
    await asyncio.sleep(0.1)
    
    # 5. 结束包
    checksum = 0x01 + 0x00 + 0x4D + 0x44 + 0x50 + 0x4C + 0x41  # 简单示例校验和
    end_msg = {
        'command': CMD_RFID_DATA_END,
        'data': [
            CMD_RFID_DATA_END,
            0x01,  # 序列号
            0x03,  # 总包数
            (checksum >> 8) & 0xFF,  # 校验和高字节
            checksum & 0xFF,  # 校验和低字节
            0x00,  # 成功状态
            0x00, 0x00  # 保留
        ]
    }
    
    print("发送结束包...")
    await app._handle_rfid_message(end_msg)
    
    # 等待处理完成
    await asyncio.sleep(1)
    
    # 检查缓存的数据
    if hasattr(app, '_rfid_data_cache') and 0 in app._rfid_data_cache:
        print("\n✅ RFID数据已成功缓存!")
        cached = app._rfid_data_cache[0]
        print(f"   时间戳: {cached['timestamp']}")
        print(f"   耗材通道: {cached['filament_id']}")
    else:
        print("\n❌ RFID数据未找到在缓存中")
        
    # 测试主动请求
    print("\n=== 测试主动请求RFID数据 ===\n")
    await app.request_rfid_data(0)
    
    # 清理
    await app.stop()


async def test_rfid_parser():
    """测试RFID解析器功能"""
    from feeder_cabinet.rfid_parser import RFIDDataParser
    
    print("\n=== 测试RFID解析器 ===\n")
    
    parser = RFIDDataParser()
    
    # 测试数据 - 简化的OpenTag数据
    test_data = bytearray()
    test_data.extend([0x01, 0x00])  # version = 1
    test_data.extend(b'MINGDA\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')  # manufacturer (16 bytes)
    test_data.extend(b'PLA\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')  # material (16 bytes)
    test_data.extend(b'White\x00' + b'\x00' * 26)  # color (32 bytes)
    test_data.extend([0xD6, 0x06])  # diameter = 1750
    test_data.extend([0xE8, 0x03])  # weight = 1000
    test_data.extend([0xD2, 0x00])  # print_temp = 210
    test_data.extend([0x3C, 0x00])  # bed_temp = 60
    test_data.extend([0xD8, 0x04])  # density = 1240
    
    # 模拟完整的传输过程
    # 起始包
    start_data = bytes([
        CMD_RFID_RAW_DATA_NOTIFY,
        0x01,  # 序列号
        0x00,  # 耗材通道
        20,    # 总包数
        (len(test_data) >> 8) & 0xFF,
        len(test_data) & 0xFF,
        0x00,  # 挤出机0
        0x00   # RFID读取
    ])
    
    result = parser.handle_rfid_message(start_data)
    print(f"起始包结果: {result}")
    
    # 发送数据包
    for i in range(20):
        offset = i * 4
        remaining = len(test_data) - offset
        valid_bytes = min(4, remaining)
        
        if valid_bytes <= 0:
            break
            
        packet_data = bytearray([
            CMD_RFID_DATA_PACKET,
            0x01,  # 序列号
            i + 1,  # 包序号
            valid_bytes
        ])
        
        # 添加数据
        for j in range(valid_bytes):
            packet_data.append(test_data[offset + j])
            
        # 填充到8字节
        while len(packet_data) < 8:
            packet_data.append(0x00)
            
        result = parser.handle_rfid_message(bytes(packet_data))
        if result:
            print(f"数据包{i+1}结果: {result}")
    
    # 计算校验和
    checksum = sum(test_data) & 0xFFFF
    
    # 结束包
    end_data = bytes([
        CMD_RFID_DATA_END,
        0x01,  # 序列号
        20,    # 总包数
        (checksum >> 8) & 0xFF,
        checksum & 0xFF,
        0x00,  # 成功
        0x00, 0x00
    ])
    
    result = parser.handle_rfid_message(end_data)
    print(f"\n结束包结果: {result}")
    
    if result and result['type'] == 'rfid_complete':
        data = result['data']
        print("\n解析的耗材数据:")
        print(f"  制造商: {data.manufacturer}")
        print(f"  材料: {data.material_name}")
        print(f"  颜色: {data.color_name}")
        print(f"  直径: {data.diameter_target/1000:.2f} mm")
        print(f"  重量: {data.weight_nominal} g")
        print(f"  打印温度: {data.print_temp}°C")
        print(f"  热床温度: {data.bed_temp}°C")


async def main():
    """主测试函数"""
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("RFID集成测试开始...\n")
    
    # 先测试解析器
    await test_rfid_parser()
    
    # 再测试集成
    await simulate_rfid_data()
    
    print("\nRFID集成测试完成!")


if __name__ == "__main__":
    asyncio.run(main())