#!/usr/bin/env python3
"""
双挤出头运动监控示例

此示例演示如何使用WebSocket订阅方式实时获取双挤出头的运动状态更新。
基于MINGDA送料柜系统中的KlipperMonitor类实现。
"""

import asyncio
import json
import logging
from typing import Dict, Any

# 导入送料柜系统的KlipperMonitor类
try:
    from feeder_cabinet.klipper_monitor import KlipperMonitor
    from feeder_cabinet.can_communication import FeederCabinetCAN
except ImportError:
    from src.feeder_cabinet.klipper_monitor import KlipperMonitor  
    from src.feeder_cabinet.can_communication import FeederCabinetCAN

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ExtruderMotionMonitor:
    """双挤出头运动监控器"""
    
    def __init__(self, moonraker_url: str = "http://localhost:7125"):
        """
        初始化监控器
        
        Args:
            moonraker_url: Moonraker API地址
        """
        # 创建一个空的CAN通信实例（仅用于KlipperMonitor初始化）
        self.can_comm = None  # 在此示例中不使用CAN通信
        
        # 双挤出头配置
        self.extruder_config = {
            'count': 2,
            'active': 0,
            'mapping': {0: 0, 1: 1}  # 挤出机到料管的映射
        }
        
        # 初始化KlipperMonitor
        self.klipper_monitor = KlipperMonitor(
            can_comm=self.can_comm,
            moonraker_url=moonraker_url,
            extruder_config=self.extruder_config
        )
        
        # 注册状态更新回调
        self.klipper_monitor.register_status_callback(self.handle_status_update)
        
        # 运动状态缓存
        self.last_motion_state = None
        self.last_active_extruder = None
        
    async def handle_status_update(self, status: Dict[str, Any]):
        """
        处理状态更新回调
        
        Args:
            status: 状态更新数据
        """
        # 检查是否包含运动报告
        if 'motion_report' in status:
            await self.handle_motion_update(status)
        
        # 检查工具头状态变化
        if 'toolhead' in status:
            await self.handle_toolhead_update(status['toolhead'])
            
        # 检查挤出机状态变化  
        if 'extruder' in status:
            await self.handle_extruder_update('extruder', status['extruder'])
            
        if 'extruder1' in status:
            await self.handle_extruder_update('extruder1', status['extruder1'])
    
    async def handle_motion_update(self, status: Dict[str, Any]):
        """处理运动状态更新"""
        motion_report = status['motion_report']
        extruder_velocity = motion_report.get('live_extruder_velocity', 0.0)
        live_position = motion_report.get('live_position', [])
        
        # 获取当前激活的挤出头
        active_extruder = status.get('toolhead', {}).get('extruder', 'extruder')
        
        # 判断运动状态
        if extruder_velocity > 0.0:
            motion_state = "进料"
        elif extruder_velocity < 0.0:
            motion_state = "退料"
        else:
            motion_state = "停止"
        
        # 检查状态变化
        if (self.last_motion_state != motion_state or 
            self.last_active_extruder != active_extruder):
            
            if motion_state != "停止":
                logger.info(f"📈 挤出机状态变化: {active_extruder} -> {motion_state} "
                          f"(速度: {extruder_velocity:.2f} mm/s)")
            else:
                logger.info(f"⏹️  挤出机停止: {active_extruder}")
            
            self.last_motion_state = motion_state
            self.last_active_extruder = active_extruder
        
        # 记录详细信息（仅调试级别）
        logger.debug(f"运动详情 - 挤出头: {active_extruder}, 速度: {extruder_velocity:.2f}, "
                    f"位置: {live_position}")
    
    async def handle_toolhead_update(self, toolhead_data: Dict[str, Any]):
        """处理工具头状态更新"""
        active_extruder = toolhead_data.get('extruder')
        if active_extruder and active_extruder != self.last_active_extruder:
            extruder_index = 0 if active_extruder == 'extruder' else 1
            logger.info(f"🔄 活动挤出头切换: {active_extruder} (索引: {extruder_index})")
    
    async def handle_extruder_update(self, extruder_name: str, extruder_data: Dict[str, Any]):
        """处理挤出机状态更新"""
        can_extrude = extruder_data.get('can_extrude', False)
        temperature = extruder_data.get('temperature', 0)
        target_temp = extruder_data.get('target', 0)
        
        logger.debug(f"{extruder_name}: 温度={temperature:.1f}°C (目标:{target_temp:.1f}°C), "
                    f"可挤出={can_extrude}")
    
    async def start_monitoring(self):
        """开始监控"""
        logger.info("🚀 启动双挤出头运动监控...")
        
        # 连接到Klipper
        if await self.klipper_monitor.connect():
            logger.info("✅ 已连接到Klipper/Moonraker")
            
            # 开始监控（不启用定时查询，仅使用WebSocket订阅）
            self.klipper_monitor.start_monitoring(interval=10.0)
            
            logger.info("👀 开始实时监控挤出机运动状态...")
            logger.info("💡 提示:")
            logger.info("   - 进料时显示正速度")
            logger.info("   - 退料时显示负速度") 
            logger.info("   - 停止时显示零速度")
            logger.info("   - 活动挤出头切换时会有通知")
            
            return True
        else:
            logger.error("❌ 无法连接到Klipper/Moonraker")
            return False
    
    async def stop_monitoring(self):
        """停止监控"""
        logger.info("🛑 停止监控...")
        await self.klipper_monitor.stop_monitoring()
        await self.klipper_monitor.disconnect()
        logger.info("✅ 监控已停止")
    
    def get_motion_status(self) -> Dict[str, Any]:
        """获取当前运动状态快照"""
        return self.klipper_monitor.get_extruder_motion_status()

async def main():
    """主函数"""
    # 创建监控器实例
    monitor = ExtruderMotionMonitor()
    
    try:
        # 启动监控
        if await monitor.start_monitoring():
            logger.info("监控已启动，按 Ctrl+C 停止...")
            
            # 定期输出状态信息
            while True:
                await asyncio.sleep(30)  # 每30秒输出一次状态摘要
                
                status = monitor.get_motion_status()
                logger.info(f"📊 状态摘要: 活动挤出头={status['active_extruder']}, "
                          f"运动状态={status['motion_state']}, "
                          f"速度={status['extruder_velocity']:.2f} mm/s")
        
    except KeyboardInterrupt:
        logger.info("🔄 收到中断信号，正在退出...")
    except Exception as e:
        logger.error(f"❌ 监控过程中发生错误: {e}")
    finally:
        await monitor.stop_monitoring()

if __name__ == "__main__":
    # 运行监控程序
    asyncio.run(main())