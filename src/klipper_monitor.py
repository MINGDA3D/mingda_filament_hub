import logging
import json
import threading
import time
import requests
from typing import Optional, Dict, Any
from can_communication import FeederCabinetCAN

class KlipperMonitor:
    def __init__(self, can_comm: FeederCabinetCAN):
        """
        初始化Klipper监听器
        
        Args:
            can_comm: CAN通信实例
        """
        self.can_comm = can_comm
        self.logger = logging.getLogger(__name__)
        self.is_running = False
        self.printer_state = "disconnected"
        self.base_url = "http://localhost:7125"  # Moonraker HTTP API地址
        
        # 状态映射
        self.state_map = {
            "printing": self.can_comm.CMD_PRINTING,
            "complete": self.can_comm.CMD_PRINT_COMPLETE,
            "paused": self.can_comm.CMD_PRINT_PAUSE,
            "cancelled": self.can_comm.CMD_PRINT_CANCEL,
            "standby": self.can_comm.CMD_PRINTER_IDLE
        }
        
    def _send_request(self, method: str, params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """
        发送请求到Moonraker
        
        Args:
            method: API方法名
            params: 请求参数
            
        Returns:
            Optional[Dict[str, Any]]: Moonraker响应数据
        """
        try:
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
                "id": int(time.time() * 1000)
            }
            
            response = requests.post(
                f"{self.base_url}/jsonrpc",
                json=request,
                timeout=5
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"请求失败: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            self.logger.error(f"发送Moonraker请求失败: {str(e)}")
            return None
            
    def _update_printer_state(self):
        """更新打印机状态并发送到CAN总线"""
        # 查询打印机状态
        response = self._send_request("printer.objects.query", {
            "objects": {
                "print_stats": None,
                "toolhead": None,
                "extruder": None,
                "webhooks": None
            }
        })
        
        if not response or "result" not in response:
            return
            
        status = response["result"]["status"]
        print_stats = status.get("print_stats", {})
        state = print_stats.get("state", "unknown")
        
        # 如果状态发生变化，发送CAN消息
        if state != self.printer_state:
            self.logger.info(f"打印机状态变化: {self.printer_state} -> {state}")
            self.printer_state = state
            
            if state in self.state_map:
                self.can_comm.send_message(self.state_map[state])
                
    def start_monitoring(self):
        """开始监听Klipper状态"""
        self.is_running = True
        self.logger.info("开始监听Klipper状态")
        
        try:
            # 订阅打印机状态更新
            subscribe_response = self._send_request("printer.objects.subscribe", {
                "objects": {
                    "print_stats": None,
                    "toolhead": None,
                    "extruder": None,
                    "webhooks": None
                },
                "response_template": {}
            })
            
            if not subscribe_response or "result" not in subscribe_response:
                self.logger.error("订阅打印机状态失败")
                return
                
            while self.is_running:
                # 接收异步状态更新
                response = self._send_request("printer.objects.query", {
                    "objects": {
                        "print_stats": None,
                        "toolhead": None,
                        "extruder": None,
                        "webhooks": None
                    }
                })
                
                if response and "result" in response:
                    status = response["result"]["status"]
                    print_stats = status.get("print_stats", {})
                    state = print_stats.get("state", "unknown")
                    
                    if state != self.printer_state:
                        self.logger.info(f"打印机状态变化: {self.printer_state} -> {state}")
                        self.printer_state = state
                        
                        if state in self.state_map:
                            self.can_comm.send_message(self.state_map[state])
                            
                time.sleep(1)  # 每秒更新一次状态
                
        except Exception as e:
            self.logger.error(f"监听Klipper状态时发生错误: {str(e)}")
            
    def stop_monitoring(self):
        """停止监听"""
        self.is_running = False
        self.logger.info("停止监听Klipper状态")
        
    def get_printer_status(self) -> Dict[str, Any]:
        """
        获取打印机详细状态
        
        Returns:
            Dict[str, Any]: 打印机状态信息
        """
        # 获取服务器信息
        server_info = self._send_request("server.info")
        if not server_info or "result" not in server_info:
            return {}
            
        # 获取打印机状态
        printer_status = self._send_request("printer.objects.query", {
            "objects": {
                "print_stats": None,
                "toolhead": None,
                "extruder": None,
                "webhooks": None,
                "gcode_macros": None
            }
        })
        
        if not printer_status or "result" not in printer_status:
            return {}
            
        return {
            "server_info": server_info["result"],
            "printer_status": printer_status["result"]["status"]
        }