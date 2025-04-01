import logging
from can_communication import FeederCabinetCAN
from klipper_monitor import KlipperMonitor
import threading
import time
import json

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def status_callback(status_data):
    """状态回调函数"""
    status_map = {
        0x00: "空闲",
        0x01: "准备送料",
        0x02: "送料中",
        0x03: "送料完成",
        0x04: "送料失败"
    }
    
    error_map = {
        0x00: "无错误",
        0x01: "机械故障",
        0x02: "耗材缺失",
        0x03: "其他错误"
    }
    
    status = status_map.get(status_data['status'], "未知状态")
    error = error_map.get(status_data['error_code'], "未知错误")
    
    print(f"状态: {status}")
    print(f"进度: {status_data['progress']}%")
    if status_data['error_code'] != 0x00:
        print(f"错误: {error}")

def main():
    # 创建CAN通信实例
    can_comm = FeederCabinetCAN()
    if not can_comm.connect():
        print("连接CAN总线失败")
        return
        
    # 设置状态回调函数
    can_comm.set_status_callback(status_callback)
    
    # 创建Klipper监听器
    monitor = KlipperMonitor(can_comm)
    
    # 在单独的线程中启动CAN消息接收
    receive_thread = threading.Thread(target=can_comm.start_receiving)
    receive_thread.daemon = True
    receive_thread.start()
    
    # 在单独的线程中启动Klipper状态监听
    monitor_thread = threading.Thread(target=monitor.start_monitoring)
    monitor_thread.daemon = True
    monitor_thread.start()
    
    try:
        # 获取打印机状态
        status = monitor.get_printer_status()
        print("\n打印机状态:")
        print(json.dumps(status, indent=2, ensure_ascii=False))
        
        # 保持主程序运行
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    finally:
        # 停止监听并断开连接
        monitor.stop_monitoring()
        can_comm.disconnect()

if __name__ == "__main__":
    main() 