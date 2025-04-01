import logging
import json
import time
from can_communication import FeederCabinetCAN
from klipper_monitor import KlipperMonitor
import threading

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def print_status(status):
    """打印服务器和打印机状态信息"""
    server_info = status.get("server_info", {})
    printer_status = status.get("printer_status", {})
    
    print("\n服务器信息:")
    print(f"Klipper 连接状态: {'已连接' if server_info.get('klippy_connected') else '未连接'}")
    print(f"Klipper 状态: {server_info.get('klippy_state', 'unknown')}")
    print(f"Moonraker 版本: {server_info.get('moonraker_version', 'unknown')}")
    print(f"API 版本: {server_info.get('api_version_string', 'unknown')}")
    
    if server_info.get('warnings'):
        print("\n警告信息:")
        for warning in server_info['warnings']:
            print(f"- {warning}")
    
    print("\n打印机状态:")
    print_stats = printer_status.get("print_stats", {})
    print(f"打印状态: {print_stats.get('state', 'unknown')}")
    print(f"文件名: {print_stats.get('filename', 'none')}")
    print(f"总打印时间: {print_stats.get('total_duration', 0):.1f}秒")
    print(f"打印进度: {print_stats.get('progress', 0)*100:.1f}%")
    
    toolhead = printer_status.get("toolhead", {})
    print(f"工具头位置: {toolhead.get('position', [0, 0, 0, 0])}")
    
    extruder = printer_status.get("extruder", {})
    print(f"挤出机温度: {extruder.get('temperature', 0):.1f}°C / {extruder.get('target', 0):.1f}°C")
    
    if print_stats.get('state') == 'error':
        print("\n警告: 打印机处于错误状态!")
        print("请检查:")
        print("1. 打印机是否已正确连接")
        print("2. 打印机固件是否正常运行")
        print("3. 是否有错误日志")
    
    print("\n" + "="*50)

def main():
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    try:
        # 创建 CAN 通信实例
        can_comm = FeederCabinetCAN()
        can_comm.connect()
        
        # 创建 Klipper 监听器
        monitor = KlipperMonitor(can_comm)
        
        print("获取打印机初始状态...")
        status = monitor.get_printer_status()
        print_status(status)
        
        # 如果打印机处于错误状态，不启动监控
        if status.get("server_info", {}).get("klippy_state") == "error":
            print("\n由于打印机处于错误状态，程序退出")
            return
            
        print("\n开始监控打印机状态...")
        print("按 Ctrl+C 退出")
        
        # 启动监控线程
        monitor_thread = threading.Thread(target=monitor.start_monitoring)
        monitor_thread.daemon = True
        monitor_thread.start()
        
        # 主循环
        while True:
            status = monitor.get_printer_status()
            print_status(status)
            time.sleep(5)  # 每5秒更新一次状态
            
    except KeyboardInterrupt:
        print("\n正在停止监控...")
        monitor.stop_monitoring()
    except Exception as e:
        print(f"\n发生错误: {str(e)}")
    finally:
        can_comm.disconnect()
        print("已断开 CAN 总线连接")

if __name__ == "__main__":
    main() 