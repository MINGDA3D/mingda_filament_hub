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
import asyncio

try:
    from feeder_cabinet.can_communication import FeederCabinetCAN
    from feeder_cabinet.klipper_monitor import KlipperMonitor
    from feeder_cabinet.log_manager import LogManager
    from feeder_cabinet.state_manager import StateManager, SystemStateEnum
except ImportError:
    # 如果从包导入失败，尝试相对导入
    from .can_communication import FeederCabinetCAN
    from .klipper_monitor import KlipperMonitor
    from .log_manager import LogManager
    from .state_manager import StateManager, SystemStateEnum

# 配置默认参数
DEFAULT_CONFIG_PATH = "/home/mingda/feeder_cabinet_help/config/config.yaml"
DEFAULT_CAN_INTERFACE = "can1"
DEFAULT_CAN_BITRATE = 1000000
DEFAULT_MOONRAKER_URL = "http://localhost:7125"
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
        # 先初始化logger为None
        self.logger = None
        
        # 加载配置
        self.config = self._load_config(config_path)
        
        # 初始化日志管理器
        log_config = self.config.get('logging', {})
        self.log_manager = LogManager(
            app_name="feeder_cabinet",
            log_dir=log_config.get('log_dir', DEFAULT_LOG_DIR),
            log_level=log_config.get('level', DEFAULT_LOG_LEVEL),
            max_file_size=log_config.get('max_file_size', 10 * 1024 * 1024),  # 默认10MB
            backup_count=log_config.get('backup_count', 5),
            max_age_days=log_config.get('max_age_days', 30),
            console_output=log_config.get('console_output', True)
        )
        
        # 设置主logger
        self.logger = self.log_manager.setup_logger()
        
        # 初始化状态管理器
        self.state_manager = StateManager(self.log_manager.get_child_logger(self.logger, "state"))
        self.state_manager.set_state_change_callback(self._on_state_changed)
        
        # 组件实例
        self.can_comm = None
        self.klipper_monitor = None
        
        # 运行状态 (由state_manager替代)
        # self.running = False
        self.main_thread = None
        
        # 配置文件路径
        self.config_path = config_path
        
        # 断料传感器状态缓存
        self._last_filament_status = {}
        
        # 打印机状态缓存
        self._last_printer_state = None
    
    
    
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
                'active': 0,  # 默认活动挤出机
                # 喷头到送料柜料管的映射
                # 格式: extruder_index: tube_index
                # 示例 (左喷头 -> 右料管, 右喷头 -> 左料管):
                # mapping:
                #   0: 1
                #   1: 0
                'mapping': {
                    0: 0,
                    1: 1
                }
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
                        if self.logger:
                            self.logger.info(f"从 {config_path} 加载配置")
            except Exception as e:
                if self.logger:
                    self.logger.error(f"加载配置文件时发生错误: {str(e)}")
        else:
            if self.logger:
                self.logger.info("使用默认配置")
        
        # 记录关键配置项
        if self.logger:
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
    
    def _save_config(self, config_path: str = None) -> bool:
        """
        保存配置文件
        
        Args:
            config_path: 配置文件路径，如果为None则使用加载时的路径
            
        Returns:
            bool: 保存是否成功
        """
        if not config_path:
            config_path = self.config_path
            
        if not config_path:
            self.logger.error("未指定配置文件路径，无法保存配置")
            return False
            
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(self.config, f, default_flow_style=False, allow_unicode=True)
            self.logger.info(f"配置已保存到 {config_path}")
            return True
        except Exception as e:
            self.logger.error(f"保存配置文件时发生错误: {str(e)}")
            return False
    
    async def _handle_filament_status_query(self):
        """处理送料柜的挤出机余料状态查询请求。"""
        self.logger.info("收到挤出机余料状态查询请求")

        if not self.klipper_monitor:
            self.logger.error("KlipperMonitor 未初始化，无法查询状态")
            if self.can_comm:
                await self.can_comm.send_filament_status_response(is_valid=False, status_bitmap=0)
            return

        # 检查WebSocket连接状态
        if not self.klipper_monitor.ws_connected:
            self.logger.warning("Klipper WebSocket未连接，耗材状态无效")
            if self.can_comm:
                await self.can_comm.send_filament_status_response(is_valid=False, status_bitmap=0)
            return

        try:
            sensor_states = self.klipper_monitor.get_filament_status()
            extruder_mapping = self.config.get('extruders', {}).get('mapping', {})
            
            status_bitmap = 0
            
            for sensor_info in self.config.get('filament_runout', {}).get('sensors', []):
                sensor_name = sensor_info.get('name')
                extruder_index = sensor_info.get('extruder')

                if sensor_name is None or extruder_index is None:
                    continue
                
                if sensor_states.get(sensor_name, False):
                    tube_index = extruder_mapping.get(extruder_index)
                    if tube_index is not None:
                        status_bitmap |= (1 << tube_index)

            self.logger.info(f"查询到耗材状态，准备发送响应。Bitmap: {bin(status_bitmap)}")
            if self.can_comm:
                await self.can_comm.send_filament_status_response(is_valid=True, status_bitmap=status_bitmap)

        except Exception as e:
            self.logger.error(f"处理耗材状态查询时出错: {e}", exc_info=True)
            if self.can_comm:
                await self.can_comm.send_filament_status_response(is_valid=False, status_bitmap=0)
    
    async def _send_filament_status_notification(self):
        """主动发送余料状态通知给送料柜（断料传感器状态变化时调用）"""
        self.logger.info("主动发送余料状态通知给送料柜")
        
        if not self.klipper_monitor:
            self.logger.error("KlipperMonitor 未初始化，无法发送状态通知")
            return
            
        if not self.klipper_monitor.ws_connected:
            self.logger.warning("Klipper WebSocket未连接，无法发送状态通知")
            return
            
        if not self.can_comm or not self.can_comm.connected:
            self.logger.warning("CAN通信未连接，无法发送状态通知")
            return
            
        try:
            sensor_states = self.klipper_monitor.get_filament_status()
            extruder_mapping = self.config.get('extruders', {}).get('mapping', {})
            
            status_bitmap = 0
            
            for sensor_info in self.config.get('filament_runout', {}).get('sensors', []):
                sensor_name = sensor_info.get('name')
                extruder_index = sensor_info.get('extruder')
                
                if sensor_name is None or extruder_index is None:
                    continue
                    
                if sensor_states.get(sensor_name, False):
                    tube_index = extruder_mapping.get(extruder_index)
                    if tube_index is not None:
                        status_bitmap |= (1 << tube_index)
                        
            self.logger.info(f"主动通知余料状态。Bitmap: {bin(status_bitmap)}")
            await self.can_comm.send_filament_status_response(is_valid=True, status_bitmap=status_bitmap)
            
        except Exception as e:
            self.logger.error(f"发送余料状态通知时出错: {e}", exc_info=True)
    
    async def _check_can_interface_status(self):
        """检查CAN接口的系统状态"""
        import subprocess
        try:
            # 使用ip命令检查接口状态
            result = subprocess.run(['ip', 'link', 'show', 'can1'], 
                                  capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                self.logger.info(f"CAN接口状态: {result.stdout.strip()}")
                return True
            else:
                self.logger.error(f"CAN接口不存在: {result.stderr.strip()}")
                return False
        except Exception as e:
            self.logger.error(f"检查CAN接口状态失败: {e}")
            return False
    
    async def _send_printer_status_notification(self, status: str):
        """主动发送打印机状态通知给送料柜"""
        if not self.can_comm or not self.can_comm.connected:
            self.logger.warning("CAN通信未连接，无法发送打印状态通知")
            return
            
        try:
            # 映射Klipper状态到CAN命令
            status_mapping = {
                'printing': self.can_comm.CMD_PRINTING,
                'paused': self.can_comm.CMD_PRINT_PAUSE,
                'complete': self.can_comm.CMD_PRINT_COMPLETE,
                'cancelled': self.can_comm.CMD_PRINT_CANCEL,
                'standby': self.can_comm.CMD_PRINTER_IDLE,
                'ready': self.can_comm.CMD_PRINTER_IDLE,
                'error': self.can_comm.CMD_PRINTER_ERROR
            }
            
            cmd = status_mapping.get(status)
            if cmd is not None:
                # 如果是error命令，检查CAN接口状态
                if status == 'error':
                    self.logger.warning("即将发送error状态，先检查CAN接口")
                    await self._check_can_interface_status()
                
                self.logger.info(f"主动发送打印状态通知给送料柜: {status} -> CMD 0x{cmd:02X}")
                await self.can_comm.send_message(cmd)
                
                # 发送error后再次检查
                if status == 'error':
                    await asyncio.sleep(0.5)
                    self.logger.warning("已发送error状态，再次检查CAN接口")
                    await self._check_can_interface_status()
            else:
                self.logger.warning(f"未知的打印状态: {status}")
                
        except Exception as e:
            self.logger.error(f"发送打印状态通知时出错: {e}", exc_info=True)
    
    async def _handle_can_reconnect(self):
        """处理CAN重连成功事件"""
        self.logger.info("处理CAN重连成功事件")
        
        # 发送当前打印状态和余料状态
        try:
            if self.klipper_monitor and self.klipper_monitor.ws_connected:
                # 获取当前缓存的状态
                old_cached_state = self._last_printer_state
                printer_status = self.klipper_monitor.get_printer_status()
                klipper_cached_state = printer_status.get('printer_state') if printer_status else None
                
                self.logger.info(f"状态检查 - 本地缓存: {old_cached_state}, Klipper缓存: {klipper_cached_state}")
                
                # CAN重连后总是重新订阅，确保状态更新正常
                self.logger.info("CAN重连后重新订阅WebSocket以确保状态同步")
                await self.klipper_monitor.resubscribe_objects()
                # 等待状态更新
                await asyncio.sleep(1.0)
                
                # 获取最新的打印机状态
                printer_status = self.klipper_monitor.get_printer_status()
                if printer_status and 'printer_state' in printer_status:
                    current_state = printer_status['printer_state']
                    self.logger.info(f"CAN重连后发送最新打印机状态: {current_state}")
                    await self._send_printer_status_notification(current_state)
                    
                    # 更新缓存的打印机状态，确保后续状态变化能够被正确检测
                    self._last_printer_state = current_state
                    self.logger.info(f"更新打印机状态缓存: {current_state}")
                    
                    # 如果打印机已经从error恢复，同步系统状态
                    if current_state in ['standby', 'ready'] and self.state_manager.state == SystemStateEnum.ERROR:
                        self.logger.info("检测到打印机已从错误状态恢复，同步系统状态")
                        self.state_manager.transition_to(SystemStateEnum.IDLE)
                else:
                    self.logger.warning("CAN重连后无法获取打印机状态")
                    
                # 发送余料状态
                await self._send_filament_status_notification()
            else:
                self.logger.warning("Klipper未连接，跳过状态同步")
                
        except Exception as e:
            self.logger.error(f"处理CAN重连事件时发生错误: {e}", exc_info=True)
    
    async def _handle_feeder_mapping_set(self, mapping_data: Dict[str, Any]):
        """
        处理送料柜发送的料管映射设置命令
        
        Args:
            mapping_data: 映射数据字典，包含left_tube, right_tube, status
        """
        try:
            left_tube = mapping_data.get('left_tube', 0)
            right_tube = mapping_data.get('right_tube', 1)
            
            self.logger.info(f"收到料管映射设置命令: 左料管={left_tube}, 右料管={right_tube}")
            
            if 'extruders' not in self.config:
                self.config['extruders'] = {}
            
            self.config['extruders']['mapping'] = {
                0: left_tube,
                1: right_tube
            }
            
            save_success = self._save_config()
            
            if self.can_comm:
                status = 0 if save_success else 1
                await self.can_comm.send_feeder_mapping_response(left_tube, right_tube, status)
                
                # 映射设置成功后，重新发送余料状态以保持信息同步
                if save_success:
                    self.logger.info("料管映射设置成功，重新发送余料状态信息")
                    await self._send_filament_status_notification()
                
        except Exception as e:
            self.logger.error(f"处理料管映射设置时发生错误: {str(e)}", exc_info=True)
            if self.can_comm:
                await self.can_comm.send_feeder_mapping_response(0, 0, 1)
    
    async def _handle_klipper_status_update(self, status: Dict[str, Any]):
        """处理来自KlipperMonitor的状态更新，并驱动状态机"""
        try:
            # 记录收到的状态更新
            self.logger.debug(f"收到状态更新，包含的键: {list(status.keys())}")
            if 'print_stats' in status:
                self.logger.debug(f"收到Klipper状态更新: print_stats={status.get('print_stats', {}).get('state')}")
                self.logger.debug(f"当前缓存的打印机状态: {self._last_printer_state}")
            else:
                self.logger.debug("状态更新中没有print_stats")
            
            # 解析打印机状态
            if 'print_stats' in status:
                klipper_state = status['print_stats'].get('state')
                # 仅在状态实际改变时记录日志和转换
                if klipper_state:
                    if klipper_state != self._last_printer_state:
                        self.logger.info(f"检测到Klipper状态变化: {self._last_printer_state} -> {klipper_state}")
                        self._last_printer_state = klipper_state
                        
                        # 主动发送打印状态通知给送料柜
                        asyncio.create_task(self._send_printer_status_notification(klipper_state))
                        
                        if klipper_state == 'printing' and self.state_manager.state == SystemStateEnum.IDLE:
                            self.state_manager.transition_to(SystemStateEnum.PRINTING)
                        elif klipper_state == 'paused' and self.state_manager.state in [SystemStateEnum.PRINTING, SystemStateEnum.RESUMING, SystemStateEnum.RUNOUT]:
                            self.state_manager.transition_to(SystemStateEnum.PAUSED)
                        elif klipper_state in ['complete', 'cancelled'] and self.state_manager.state != SystemStateEnum.IDLE:
                            self.state_manager.transition_to(SystemStateEnum.IDLE)
                        elif klipper_state == 'error' and self.state_manager.state != SystemStateEnum.ERROR:
                            self.logger.warning("打印机进入错误状态，但保持CAN通信")
                            self.state_manager.transition_to(SystemStateEnum.ERROR, reason="Klipper reported an error")
                        elif klipper_state in ['standby', 'ready'] and self.state_manager.state == SystemStateEnum.ERROR:
                            # 从错误状态恢复
                            self.logger.info(f"检测到打印机从错误状态恢复: {klipper_state}")
                            self.logger.info(f"当前系统状态: {self.state_manager.state.name}")
                            self.state_manager.transition_to(SystemStateEnum.IDLE)
                            self.logger.info("系统状态已从ERROR转换为IDLE")
                    else:
                        self.logger.debug(f"Klipper状态未变化，保持为: {klipper_state}")

            # 解析断料传感器状态
            filament_status_changed = False
            
            # 检查所有断料传感器状态变化
            for sensor_obj_name in self.klipper_monitor.filament_sensor_objects:
                if sensor_obj_name in status and 'filament_detected' in status[sensor_obj_name]:
                    current_status = status[sensor_obj_name]['filament_detected']
                    # 检查状态是否变化
                    if not hasattr(self, '_last_filament_status'):
                        self._last_filament_status = {}
                    
                    last_status = self._last_filament_status.get(sensor_obj_name)
                    if last_status is None or last_status != current_status:
                        self._last_filament_status[sensor_obj_name] = current_status
                        filament_status_changed = True
                        self.logger.info(f"传感器 {sensor_obj_name} 状态变化: {last_status} -> {current_status}")
            
            # 如果断料传感器状态有变化，主动发送通知
            if filament_status_changed:
                asyncio.create_task(self._send_filament_status_notification())
            
            # 处理打印中的断料事件
            if self.state_manager.state == SystemStateEnum.PRINTING:
                # 获取当前活跃的挤出机
                current_active_extruder = None
                if 'toolhead' in status and 'extruder' in status['toolhead']:
                    active_extruder_name = status['toolhead']['extruder']
                    current_active_extruder = 0 if active_extruder_name == 'extruder' else 1
                    self.klipper_monitor.active_extruder = current_active_extruder
                else:
                    current_active_extruder = self.klipper_monitor.active_extruder
                
                # 首先检查活跃挤出机的断料状态
                runout_detected = False
                active_extruder_runout = False
                
                for i, sensor_obj_name in enumerate(self.klipper_monitor.filament_sensor_objects):
                    if sensor_obj_name in status and 'filament_detected' in status[sensor_obj_name]:
                        has_filament = status[sensor_obj_name]['filament_detected']
                        if not has_filament:
                            if i == current_active_extruder:
                                # 活跃挤出机断料，优先处理
                                self.logger.info(f"检测到活跃挤出机 {i} 的传感器 {sensor_obj_name} 断料事件。")
                                if not self.state_manager.is_state(SystemStateEnum.RUNOUT):
                                    self.state_manager.transition_to(SystemStateEnum.RUNOUT, extruder=i)
                                    active_extruder_runout = True
                                    break
                            else:
                                # 非活跃挤出机断料，记录但不处理
                                self.logger.info(f"检测到非活跃挤出机 {i} 的传感器 {sensor_obj_name} 断料，暂不处理。")
                                runout_detected = True
                
                # 如果活跃挤出机没有断料，但有其他挤出机断料，记录警告
                if runout_detected and not active_extruder_runout:
                    self.logger.warning(f"非活跃挤出机有断料，但当前活跃挤出机 {current_active_extruder} 正常，继续打印。")
                            
        except Exception as e:
            self.logger.error(f"处理Klipper状态更新时发生错误: {str(e)}", exc_info=True)
            # 确保即使出错也不会阻止后续的状态更新
            # 不要在这里转换到ERROR状态，以免影响正常的错误恢复流程

    async def _on_state_changed(self, old_state: SystemStateEnum, new_state: SystemStateEnum, payload: Dict[str, Any]):
        """当状态机状态改变时，执行相应的动作"""
        self.logger.info(f"State Change: {old_state.name} -> {new_state.name} | Payload: {payload}")
        try:
            if new_state == SystemStateEnum.RUNOUT:
                extruder = payload.get('extruder')
                self.logger.info(f"ACTION: 为挤出机 {extruder} 断料事件暂停打印。")
                if not await self.klipper_monitor.pause_print():
                    self.logger.error("ACTION FAILED: 暂停打印失败！进入错误状态。")
                    self.state_manager.transition_to(SystemStateEnum.ERROR, reason="Failed to pause print for runout")
            
            elif new_state == SystemStateEnum.PAUSED:
                if old_state in [SystemStateEnum.PRINTING, SystemStateEnum.RESUMING, SystemStateEnum.RUNOUT]:
                    extruder = self.state_manager.get_payload().get('extruder')
                    self.logger.info(f"ACTION: 为挤出机 {extruder} 请求补料。")
                    if not await self.can_comm.request_feed(extruder=extruder):
                         self.logger.error(f"ACTION FAILED: 为挤出机 {extruder} 请求补料失败！进入错误状态。")
                         self.state_manager.transition_to(SystemStateEnum.ERROR, reason=f"Failed to request feed for extruder {extruder}")
                    else:
                        self.logger.info(f"补料请求已发送，转换为FEEDING状态。")
                        self.state_manager.transition_to(SystemStateEnum.FEEDING, extruder=extruder)

            elif new_state == SystemStateEnum.ERROR:
                reason = payload.get('reason', 'Unknown error')
                self.logger.error(f"ACTION: 系统进入错误状态，原因: {reason}")
                await self.can_comm.send_printer_error(error_code=99)

        except Exception as e:
            self.logger.error(f"ACTION FAILED: 状态处理时发生严重错误: {e}", exc_info=True)
            self.state_manager.transition_to(SystemStateEnum.ERROR, reason=f"Exception in state handler: {e}")

    
    def init(self) -> bool:
        """
        初始化应用程序组件
        
        Returns:
            bool: 初始化是否成功
        """
        try:
            # 记录日志配置信息
            self.logger.info(f"日志文件目录: {self.log_manager.log_dir}")
            self.logger.info(f"日志文件: {self.log_manager.log_file}")
            self.logger.info(f"日志级别: {logging.getLevelName(self.log_manager.log_level)}")
            self.logger.info(f"日志轮转: 文件大小限制={self.log_manager.max_file_size/1024/1024}MB, 保留数量={self.log_manager.backup_count}")
            self.logger.info(f"日志自动清理: {self.log_manager.max_age_days}天")
            
            # 初始化CAN通信
            can_config = self.config['can']
            self.logger.info(f"初始化CAN通信，接口: {can_config['interface']}, 波特率: {can_config['bitrate']}")
            self.can_comm = FeederCabinetCAN(
                interface=can_config['interface'],
                bitrate=can_config['bitrate']
            )
            self.can_comm.set_query_callback(self._handle_filament_status_query)
            self.can_comm.set_mapping_set_callback(self._handle_feeder_mapping_set)
            self.can_comm.set_reconnect_callback(self._handle_can_reconnect)
            
            # 为CAN通信模块设置logger
            self.can_comm.logger = self.log_manager.get_child_logger(self.logger, "can")
            
            # 初始化Klipper监控器
            klipper_config = self.config['klipper']
            self.logger.info(f"初始化Klipper监控器，Moonraker URL: {klipper_config['moonraker_url']}")
            self.klipper_monitor = KlipperMonitor(
                can_comm=self.can_comm,
                moonraker_url=klipper_config['moonraker_url'],
                extruder_config=self.config.get('extruders', None)
            )
            self.klipper_monitor.register_status_callback(self._handle_klipper_status_update)
            
            # 为Klipper监控模块设置logger
            self.klipper_monitor.logger = self.log_manager.get_child_logger(self.logger, "klipper")
            
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
                self.klipper_monitor.auto_reconnect = True
                self.klipper_monitor.reconnect_interval = 5
            
            return True
        except Exception as e:
            self.logger.error(f"初始化应用程序时发生错误: {str(e)}", exc_info=True)
            self.state_manager.transition_to(SystemStateEnum.ERROR, error=str(e))
            return False
    
    async def _state_sync_check_task(self):
        """定期检查状态同步的任务"""
        self.logger.info("启动状态同步检查任务")
        check_interval = 60  # 每60秒检查一次
        
        while self.state_manager.state != SystemStateEnum.DISCONNECTED:
            try:
                await asyncio.sleep(check_interval)
                
                if self.klipper_monitor and self.klipper_monitor.ws_connected:
                    # 获取Klipper内部缓存的状态
                    printer_status = self.klipper_monitor.get_printer_status()
                    klipper_state = printer_status.get('printer_state') if printer_status else None
                    
                    # 如果两个缓存不一致，说明可能存在同步问题
                    if klipper_state and klipper_state != self._last_printer_state:
                        self.logger.warning(f"检测到状态不同步 - 本地: {self._last_printer_state}, Klipper: {klipper_state}")
                        
                        # 重新订阅并查询状态
                        await self.klipper_monitor.resubscribe_objects()
                        await asyncio.sleep(1.0)
                        
                        # 重新获取状态并发送
                        printer_status = self.klipper_monitor.get_printer_status()
                        if printer_status and 'printer_state' in printer_status:
                            current_state = printer_status['printer_state']
                            self.logger.info(f"状态同步后发送最新状态: {current_state}")
                            await self._send_printer_status_notification(current_state)
                            self._last_printer_state = current_state
                            
            except asyncio.CancelledError:
                self.logger.info("状态同步检查任务被取消")
                break
            except Exception as e:
                self.logger.error(f"状态同步检查任务异常: {str(e)}", exc_info=True)
                
        self.logger.info("状态同步检查任务结束")
    
    async def _can_reconnect_task(self):
        """CAN自动重连任务"""
        self.logger.info("启动CAN自动重连任务")
        reconnect_interval = 5  # 重连间隔5秒
        
        while self.state_manager.state != SystemStateEnum.DISCONNECTED:
            try:
                await asyncio.sleep(reconnect_interval)
                
                if not self.can_comm.connected:
                    self.logger.info(f"CAN重连任务：检测到断开，当前系统状态={self.state_manager.state.name}")
                    self.logger.info("尝试重新连接CAN总线...")
                    if await self.can_comm.connect():
                        self.logger.info("CAN总线重连成功")
                        
                        # 发送当前打印状态和余料状态
                        if self.klipper_monitor:
                            # 获取最新的打印机状态
                            printer_status = self.klipper_monitor.get_printer_status()
                            if printer_status and 'printer_state' in printer_status:
                                current_state = printer_status['printer_state']
                                self.logger.info(f"CAN重连后发送最新打印机状态: {current_state}")
                                await self._send_printer_status_notification(current_state)
                                
                                # 更新缓存的打印机状态，确保后续状态变化能够被正确检测
                                self._last_printer_state = current_state
                                self.logger.info(f"更新打印机状态缓存: {current_state}")
                            else:
                                self.logger.warning("CAN重连后无法获取打印机状态")
                        await self._send_filament_status_notification()
                        
                        break
                    else:
                        self.logger.warning(f"CAN总线重连失败，{reconnect_interval}秒后重试")
                else:
                    # 已经连接，退出重连任务
                    break
                    
            except asyncio.CancelledError:
                self.logger.info("CAN重连任务被取消")
                break
            except Exception as e:
                self.logger.error(f"CAN重连任务异常: {str(e)}", exc_info=True)
                await asyncio.sleep(reconnect_interval)
        
        self.logger.info("CAN自动重连任务结束")
            
    async def start(self) -> bool:
        """
        启动应用程序
        
        Returns:
            bool: 启动是否成功
        """
        if self.state_manager.state != SystemStateEnum.STARTING:
            self.logger.info(f"应用程序已经在运行中或处于非启动状态: {self.state_manager.state.name}")
            return True
            
        try:
            # 连接CAN总线
            if not await self.can_comm.connect():
                self.logger.error("初次连接CAN总线失败，将启动自动重连任务")
                # 启动CAN自动重连任务
                asyncio.create_task(self._can_reconnect_task())
            else:
                # CAN连接成功，发送初始状态
                self.logger.info("CAN连接成功，发送初始状态给送料柜")
                await self.can_comm.send_message(self.can_comm.CMD_PRINTER_IDLE)
                
            # 连接Klipper，但不将其视为致命错误
            if not await self.klipper_monitor.connect():
                self.logger.warning("初次连接Klipper失败，系统将在后台自动重连。")
                # 程序继续运行，依赖后台重连
            else:
                self.logger.info("成功连接到Klipper。")
                
                # 如果CAN已连接，发送当前打印状态和余料状态
                if self.can_comm and self.can_comm.connected:
                    # 等待一下让Klipper状态稳定
                    await asyncio.sleep(1)
                    
                    # 发送当前打印状态
                    printer_status = self.klipper_monitor.get_printer_status()
                    if printer_status and 'printer_state' in printer_status:
                        klipper_state = printer_status['printer_state']
                        self.logger.info(f"初始化时发送打印机状态: {klipper_state}")
                        await self._send_printer_status_notification(klipper_state)
                        # 更新缓存
                        self._last_printer_state = klipper_state
                    
                    # 发送余料状态
                    await self._send_filament_status_notification()

            # 启动Klipper监控
            update_interval = self.config['klipper']['update_interval']
            self.klipper_monitor.start_monitoring(interval=update_interval)
            
            # 启动状态同步检查任务
            asyncio.create_task(self._state_sync_check_task())
            
            # 标记为运行中
            self.state_manager.transition_to(SystemStateEnum.IDLE)
            self.logger.info("应用程序已启动，进入空闲状态")
            
            return True
        except Exception as e:
            self.logger.error(f"启动应用程序时发生错误: {str(e)}")
            self.state_manager.transition_to(SystemStateEnum.ERROR, error=str(e))
            await self.stop()
            return False
    
    async def stop(self):
        """停止应用程序"""
        self.logger.info("正在停止应用程序...")
        self.state_manager.transition_to(SystemStateEnum.DISCONNECTED)
        
        try:
            if self.klipper_monitor:
                self.logger.info("正在断开Klipper监控器...")
                await self.klipper_monitor.disconnect()
                
            if self.can_comm:
                self.logger.info("正在断开CAN通信...")
                await self.can_comm.disconnect()

            self.logger.info("应用程序已成功停止。")
        except Exception as e:
            self.logger.error(f"停止应用程序时发生错误: {str(e)}", exc_info=True)
    
    async def run(self):
        """运行应用程序"""
        if not self.init():
            self.logger.error("初始化失败，退出程序")
            return
            
        if not await self.start():
            self.logger.error("启动失败，退出程序")
            return
            
        # 保持程序运行
        try:
            while self.state_manager.state != SystemStateEnum.DISCONNECTED:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.logger.info("接收到终止信号，正在停止...")
        finally:
            await self.stop()

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
    parser.add_argument(
        "--log-stats",
        help="显示日志统计信息并退出",
        action="store_true"
    )
    parser.add_argument(
        "--archive-logs",
        help="归档旧日志文件",
        action="store_true"
    )
    
    return parser.parse_args()

def main():
    """主函数"""
    try:
        # 最早的错误捕获 - 先创建一个简单的日志记录器
        import traceback
        
        # 创建临时日志记录器以便调试启动问题
        temp_logger = logging.getLogger("feeder_cabinet.startup")
        temp_logger.setLevel(logging.DEBUG)
        
        # 添加控制台和文件处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)
        temp_logger.addHandler(console_handler)
        
        # 尝试创建日志目录并添加文件处理器
        try:
            os.makedirs(DEFAULT_LOG_DIR, exist_ok=True)
            file_handler = logging.FileHandler(os.path.join(DEFAULT_LOG_DIR, 'feeder_cabinet_startup.log'))
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(console_formatter)
            temp_logger.addHandler(file_handler)
        except Exception as e:
            temp_logger.error(f"无法创建日志文件: {e}")
        
        temp_logger.info("启动 feeder_cabinet 主程序...")
        
        args = parse_args()
        temp_logger.info(f"命令行参数: {args}")
        
        # 创建应用程序实例
        temp_logger.info(f"加载配置文件: {args.config}")
        app = FeederCabinetApp(config_path=args.config)
        temp_logger.info("应用程序实例创建成功")
        
    except Exception as e:
        # 如果无法创建应用，至少输出错误到标准输出
        error_msg = f"创建应用程序实例失败: {str(e)}\n{traceback.format_exc()}"
        if 'temp_logger' in locals():
            temp_logger.error(error_msg)
        else:
            print(error_msg, file=sys.stderr)
        sys.exit(1)
    
    # 如果指定了详细输出，设置日志级别为DEBUG
    if args.verbose:
        app.log_manager.update_log_level(app.logger, "DEBUG")
    
    # 如果显示日志统计
    if args.log_stats:
        stats = app.log_manager.get_log_stats()
        app.logger.info("日志统计信息：")
        app.logger.info(f"  日志目录: {stats['log_dir']}")
        app.logger.info(f"  日志文件数: {len(stats['files'])}")
        app.logger.info(f"  总大小: {stats['total_size']/1024/1024:.2f} MB")
        if stats['oldest_file']:
            app.logger.info(f"  最旧文件: {os.path.basename(stats['oldest_file'])}")
        if stats['newest_file']:
            app.logger.info(f"  最新文件: {os.path.basename(stats['newest_file'])}")
        for file_info in stats['files']:
            app.logger.info(f"    - {os.path.basename(file_info['path'])}: {file_info['size']/1024:.2f} KB")
        return
    
    # 如果归档日志
    if args.archive_logs:
        if app.log_manager.archive_logs():
            app.logger.info("日志归档成功")
        else:
            app.logger.error("日志归档失败")
        return
    
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
    try:
        asyncio.run(app.run())
    except Exception as e:
        app.logger.error(f"运行时发生错误: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main() 