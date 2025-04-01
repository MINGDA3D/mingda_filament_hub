import logging
import json
import time
from can_communication import FeederCabinetCAN
from klipper_monitor import KlipperMonitor
import threading

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def print_status(status):
    """打印状态信息"""
    if not status:
        print("\n" + "="*50)
        print("错误: 无法获取打印机状态")
        print("请检查:")
        print("1. Moonraker服务是否正在运行")
        print("2. 打印机是否已连接")
        print("3. 网络连接是否正常")
        print("="*50 + "\n")
        return
        
    print("\n" + "="*50)
    print("服务器信息:")
    server_info = status.get("server_info", {})
    print(f"Klipper连接状态: {server_info.get('klippy_connected', 'unknown')}")
    print(f"Klipper状态: {server_info.get('klippy_state', 'unknown')}")
    print(f"Moonraker版本: {server_info.get('moonraker_version', 'unknown')}")
    print(f"API版本: {server_info.get('api_version_string', 'unknown')}")
    
    print("\n打印机状态:")
    printer_status = status.get("printer_status", {})
    
    # 打印状态
    print_stats = printer_status.get("print_stats", {})
    print(f"打印状态: {print_stats.get('state', 'unknown')}")
    print(f"文件名: {print_stats.get('filename', 'none')}")
    print(f"总打印时间: {print_stats.get('total_duration', 0):.1f}秒")
    print(f"打印进度: {print_stats.get('progress', 0):.1f}%")
    
    # 工具头状态
    toolhead = printer_status.get("toolhead", {})
    print(f"\n工具头位置: X:{toolhead.get('position', [0,0,0,0])[0]:.1f} Y:{toolhead.get('position', [0,0,0,0])[1]:.1f} Z:{toolhead.get('position', [0,0,0,0])[2]:.1f}")
    
    # 挤出机状态
    extruder = printer_status.get("extruder", {})
    print(f"挤出机温度: {extruder.get('temperature', 0):.1f}°C / {extruder.get('target', 0):.1f}°C")
    
    print("="*50 + "\n")

def main():
    # 创建CAN通信实例
    can_comm = FeederCabinetCAN()
    if not can_comm.connect():
        print("连接CAN总线失败")
        return
        
    # 创建Klipper监听器
    monitor = KlipperMonitor(can_comm)
    
    try:
        # 获取初始状态
        print("获取打印机初始状态...")
        status = monitor.get_printer_status()
        print_status(status)
        
        if not status:
            print("无法获取打印机状态，程序退出")
            return
            
        # 开始监控
        print("开始监控打印机状态...")
        monitor_thread = threading.Thread(target=monitor.start_monitoring)
        monitor_thread.daemon = True
        monitor_thread.start()
        
        # 持续监控状态变化
        while True:
            status = monitor.get_printer_status()
            print_status(status)
            time.sleep(5)  # 每5秒更新一次状态
            
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    finally:
        # 停止监控并断开连接
        monitor.stop_monitoring()
        can_comm.disconnect()

if __name__ == "__main__":
    main() 