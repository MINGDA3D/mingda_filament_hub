import logging
from can_communication import FeederCabinetCAN
import time
import threading

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
    
    # 连接到CAN总线
    if not can_comm.connect():
        print("连接CAN总线失败")
        return
        
    # 设置状态回调函数
    can_comm.set_status_callback(status_callback)
    
    # 在单独的线程中启动消息接收
    receive_thread = threading.Thread(target=can_comm.start_receiving)
    receive_thread.daemon = True
    receive_thread.start()
    
    try:
        # 示例：发送补料请求
        print("发送补料请求...")
        can_comm.request_feed(extruder=0)
        
        # 等待一段时间
        time.sleep(5)
        
        # 查询状态
        print("\n查询状态...")
        can_comm.query_status(extruder=0)
        
        # 等待一段时间
        time.sleep(5)
        
        # 停止补料
        print("\n停止补料...")
        can_comm.stop_feed(extruder=0)
        
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    finally:
        # 断开CAN总线连接
        can_comm.disconnect()

if __name__ == "__main__":
    main() 