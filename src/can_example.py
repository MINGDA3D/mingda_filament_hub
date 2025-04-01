import logging
import time
from can_communication import FeederCabinetCAN

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def status_callback(status_data):
    """
    状态回调函数
    
    Args:
        status_data: 包含状态信息的字典
    """
    status = status_data['status']
    progress = status_data['progress']
    error_code = status_data['error_code']
    
    # 状态码映射
    status_map = {
        0x00: "空闲",
        0x01: "就绪",
        0x02: "送料中",
        0x03: "完成",
        0x04: "错误"
    }
    
    # 错误码映射
    error_map = {
        0x00: "无错误",
        0x01: "机械错误",
        0x02: "材料缺失",
        0x03: "其他错误",
        0x04: "Klipper错误",
        0x05: "Moonraker错误",
        0x06: "通信错误"
    }
    
    logger.info(f"状态: {status_map.get(status, '未知')}")
    logger.info(f"进度: {progress}%")
    logger.info(f"错误: {error_map.get(error_code, '未知')}")

def main():
    """
    主函数
    """
    try:
        # 创建CAN通信实例
        can_comm = FeederCabinetCAN(interface='can1', bitrate=1000000)
        
        # 连接CAN总线（包含握手过程）
        if not can_comm.connect():
            logger.error("CAN连接失败")
            return
            
        # 设置状态回调函数
        can_comm.set_status_callback(status_callback)
        
        # 查询初始状态
        logger.info("查询初始状态...")
        can_comm.query_status()
        
        # 模拟打印过程
        logger.info("开始模拟打印过程...")
        
        # 发送打印开始命令
        logger.info("发送打印开始命令")
        can_comm.send_message(can_comm.CMD_PRINTING)
        
        # 模拟送料请求
        logger.info("请求送料")
        can_comm.request_feed()
        
        # 等待一段时间
        time.sleep(5)
        
        # 停止送料
        logger.info("停止送料")
        can_comm.stop_feed()
        
        # 发送打印完成命令
        logger.info("发送打印完成命令")
        can_comm.send_message(can_comm.CMD_PRINT_COMPLETE)
        
        # 等待一段时间
        time.sleep(2)
        
        # 发送打印机空闲命令
        logger.info("发送打印机空闲命令")
        can_comm.send_message(can_comm.CMD_PRINTER_IDLE)
        
        # 模拟错误情况
        logger.info("模拟发送错误状态")
        can_comm.send_printer_error(can_comm.ERROR_KLIPPER)
        
        # 等待一段时间
        time.sleep(5)
        
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"发生错误: {str(e)}")
    finally:
        # 断开CAN连接
        can_comm.disconnect()
        logger.info("程序结束")

if __name__ == "__main__":
    main() 