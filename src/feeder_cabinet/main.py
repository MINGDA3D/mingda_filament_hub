#!/usr/bin/env python3
"""
自动续料系统主程序

此模块提供应用程序的入口点，包括：
- 参数解析
- 配置加载
- 日志设置
- 系统初始化和启动
"""

import os
import sys
import time
import logging
import argparse
import threading
import json
import yaml
from typing import Dict, Any, Optional
import requests
from concurrent.futures import ThreadPoolExecutor

from .can_communication import FeederCabinetCAN
from .klipper_monitor import KlipperMonitor

# 配置默认参数
DEFAULT_CONFIG_PATH = "/home/mingda/feeder_cabinet_help/config/config.yaml"
DEFAULT_CAN_INTERFACE = "can1"
DEFAULT_CAN_BITRATE = 1000000
DEFAULT_MOONRAKER_URL = "http://192.168.86.200:7125"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_UPDATE_INTERVAL = 5.0
DEFAULT_LOG_DIR = "/home/mingda/printer_data/logs"

class FeederCabinetApp:
    """自动续料系统应用程序类"""
    
    def __init__(self, config_path: str = None):
        """
        初始化应用程序
        
        Args:
            config_path: 配置文件路径
        """
        # 初始化记录器
        self.logger = self._setup_logging("feeder_cabinet", "INFO")
        
        # 加载配置
        self.config = self._load_config(config_path)
        
        # 应用配置后更新日志级别
        log_level = self.config.get('logging', {}).get('level', DEFAULT_LOG_LEVEL)
        self._update_log_level(log_level)
        
        # 组件实例
        self.can_comm = None
        self.klipper_monitor = None
        
        # 运行状态
        self.running = False
        self.main_thread = None
        
        # 线程池
        self.thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feeder_cabinet_")
    
    def _setup_logging(self, logger_name: str, log_level: str) -> logging.Logger:
        """
        设置日志记录器
        
        Args:
            logger_name: 记录器名称
            log_level: 日志级别
            
        Returns:
            logging.Logger: 配置好的记录器
        """
        logger = logging.getLogger(logger_name)
        logger.setLevel(getattr(logging, log_level))
        
        # 创建控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, log_level))
        
        # 设置日志格式
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        console_handler.setFormatter(formatter)
        
        # 添加处理器
        logger.addHandler(console_handler)
        
        return logger
    
    def _update_log_level(self, log_level: str):
        """
        更新日志级别
        
        Args:
            log_level: 日志级别
        """
        level = getattr(logging, log_level)
        self.logger.setLevel(level)
        for handler in self.logger.handlers:
            handler.setLevel(level)
            
        # 更新根记录器
        root_logger = logging.getLogger("feeder_cabinet")
        root_logger.setLevel(level)
        for handler in root_logger.handlers:
            handler.setLevel(level)
    
    def _load_config(self, config_path: str = None) -> Dict[str, Any]:
        """
        加载配置文件
        
        Args:
            config_path: 配置文件路径
            
        Returns:
            Dict: 配置字典
        """
        # 默认配置
        config = {
            'can': {
                'interface': DEFAULT_CAN_INTERFACE,
                'bitrate': DEFAULT_CAN_BITRATE
            },
            'klipper': {
                'moonraker_url': DEFAULT_MOONRAKER_URL,
                'update_interval': DEFAULT_UPDATE_INTERVAL
            },
            'logging': {
                'level': DEFAULT_LOG_LEVEL,
                'log_dir': DEFAULT_LOG_DIR
            },
            'filament_runout': {
                'enabled': True,
                'sensors': [
                    {'name': 'Filament_Sensor0', 'extruder': 0},
                    {'name': 'Filament_Sensor1', 'extruder': 1}
                ]
            },
            'extruders': {
                'count': 2,  # 默认支持双挤出机
                'active': 0  # 默认活动挤出机
            }
        }
        
        # 如果指定了配置文件，尝试加载
        if config_path:
            try:
                with open(config_path, 'r') as f:
                    user_config = yaml.safe_load(f)
                    if user_config:
                        # 递归更新配置
                        self._update_config(config, user_config)
                        self.logger.info(f"从 {config_path} 加载配置")
            except Exception as e:
                self.logger.error(f"加载配置文件时发生错误: {str(e)}")
        else:
            self.logger.info("使用默认配置")
        
        # 记录关键配置项
        self.logger.info(f"CAN接口: {config['can']['interface']}, 波特率: {config['can']['bitrate']}")
        self.logger.info(f"Moonraker URL: {config['klipper']['moonraker_url']}")
        self.logger.info(f"挤出机数量: {config['extruders']['count']}")
        self.logger.info(f"断料检测: {'启用' if config['filament_runout']['enabled'] else '禁用'}")
        if config['filament_runout']['enabled']:
            for sensor in config['filament_runout']['sensors']:
                self.logger.info(f"断料传感器: {sensor['name']} 用于挤出机 {sensor['extruder']}")
        
        return config
    
    def _update_config(self, config: Dict, updates: Dict):
        """
        递归更新配置字典
        
        Args:
            config: 原配置字典
            updates: 更新配置字典
        """
        for key, value in updates.items():
            if isinstance(value, dict) and key in config and isinstance(config[key], dict):
                self._update_config(config[key], value)
            else:
                config[key] = value
    
    def _setup_file_logging(self):
        """设置文件日志"""
        log_dir = self.config['logging']['log_dir']
        log_level = self.config['logging']['level']
        
        # 确保日志目录存在
        os.makedirs(log_dir, exist_ok=True)
        
        # 日志文件路径
        log_file = os.path.join(log_dir, 'feeder_cabinet.log')
        
        # 创建文件处理器
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(getattr(logging, log_level))
        
        # 设置日志格式
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(formatter)
        
        # 添加处理器到根记录器
        root_logger = logging.getLogger("feeder_cabinet")
        root_logger.addHandler(file_handler)
        
        self.logger.info(f"日志文件: {log_file}")
    
    def init(self) -> bool:
        """
        初始化应用程序组件
        
        Returns:
            bool: 初始化是否成功
        """
        try:
            # 设置文件日志
            self._setup_file_logging()
            
            # 初始化CAN通信
            can_config = self.config['can']
            self.logger.info(f"初始化CAN通信，接口: {can_config['interface']}, 波特率: {can_config['bitrate']}")
            self.can_comm = FeederCabinetCAN(
                interface=can_config['interface'],
                bitrate=can_config['bitrate']
            )
            
            # 初始化Klipper监控器
            klipper_config = self.config['klipper']
            self.logger.info(f"初始化Klipper监控器，Moonraker URL: {klipper_config['moonraker_url']}")
            self.klipper_monitor = KlipperMonitor(
                can_comm=self.can_comm,
                moonraker_url=klipper_config['moonraker_url'],
                extruder_config=self.config.get('extruders', None)
            )
            
            # 设置默认活跃挤出机
            extruder_config = self.config.get('extruders', {})
            active_extruder = extruder_config.get('active', 0)
            if active_extruder in [0, 1]:
                self.klipper_monitor.active_extruder = active_extruder
                if active_extruder == 0:
                    self.klipper_monitor.toolhead_info['extruder'] = 'extruder'
                else:
                    self.klipper_monitor.toolhead_info['extruder'] = 'extruder1'
                self.logger.info(f"设置默认活跃挤出机: {active_extruder}")
            
            # 配置断料检测
            filament_config = self.config['filament_runout']
            if filament_config.get('enabled', True):
                # 获取传感器配置
                sensor_names = []
                for sensor in filament_config.get('sensors', []):
                    name = sensor.get('name')
                    if name:
                        sensor_names.append(name)
                
                # 设置传感器名称
                if sensor_names:
                    self.klipper_monitor.filament_sensor_names = sensor_names
                    self.logger.info(f"配置断料传感器: {sensor_names}")
                
                # 启用断料检测
                self.klipper_monitor.enable_filament_runout_detection()
                
                # 配置自动重连
                self.klipper_monitor.enable_auto_reconnect(
                    enable=True,
                    max_attempts=10,
                    interval=5
                )
            
            return True
        except Exception as e:
            self.logger.error(f"初始化应用程序时发生错误: {str(e)}")
            return False
            
    def start(self) -> bool:
        """
        启动应用程序
        
        Returns:
            bool: 启动是否成功
        """
        if self.running:
            self.logger.info("应用程序已经在运行中")
            return True
            
        try:
            # 连接CAN总线
            if not self.can_comm.connect():
                self.logger.error("连接CAN总线失败")
                return False
                
            # 连接Klipper
            if not self.klipper_monitor.connect():
                self.logger.error("连接Klipper失败")
                self.can_comm.disconnect()
                return False
                
            # 启动Klipper监控
            update_interval = self.config['klipper']['update_interval']
            self.klipper_monitor.start_monitoring(interval=update_interval)
            
            # 标记为运行中
            self.running = True
            self.logger.info("应用程序已启动")
            
            return True
        except Exception as e:
            self.logger.error(f"启动应用程序时发生错误: {str(e)}")
            self.stop()
            return False
    
    def stop(self):
        """停止应用程序"""
        try:
            if self.klipper_monitor:
                self.klipper_monitor.stop_monitoring()
                self.klipper_monitor.disconnect()
                
            if self.can_comm:
                self.can_comm.disconnect()
                
            self.running = False
            self.logger.info("应用程序已停止")
        except Exception as e:
            self.logger.error(f"停止应用程序时发生错误: {str(e)}")
    
    def run(self):
        """运行应用程序"""
        if not self.init():
            self.logger.error("初始化失败，退出程序")
            return
            
        if not self.start():
            self.logger.error("启动失败，退出程序")
            return
            
        # 保持程序运行
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("接收到终止信号，正在停止...")
        finally:
            self.stop()

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="自动续料系统")
    parser.add_argument(
        "-c", "--config",
        help=f"配置文件路径 (默认: {DEFAULT_CONFIG_PATH})",
        default=DEFAULT_CONFIG_PATH
    )
    parser.add_argument(
        "-v", "--verbose",
        help="增加输出详细程度",
        action="store_true"
    )
    parser.add_argument(
        "--check-config",
        help="只检查配置文件是否有效",
        action="store_true"
    )
    parser.add_argument(
        "--dry-run",
        help="初始化但不启动系统",
        action="store_true"
    )
    
    return parser.parse_args()

def main():
    """主函数"""
    args = parse_args()
    
    # 创建应用程序实例
    app = FeederCabinetApp(config_path=args.config)
    
    # 如果指定了详细输出，设置日志级别为DEBUG
    if args.verbose:
        app._update_log_level("DEBUG")
    
    # 如果只检查配置
    if args.check_config:
        app.logger.info("配置检查完成")
        return
    
    # 如果是试运行模式
    if args.dry_run:
        if app.init():
            app.logger.info("初始化成功，试运行模式，不启动系统")
        else:
            app.logger.error("初始化失败")
        return
    
    # 正常运行
    app.run()

if __name__ == "__main__":
    main() 