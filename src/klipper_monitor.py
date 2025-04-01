import logging
import json
import threading
import time
import requests
import socket
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
        self.base_url = self._get_moonraker_url()  # 获取 Moonraker 地址
        
        # 状态映射
        self.state_map = {
            "printing": self.can_comm.CMD_PRINTING,
            "complete": self.can_comm.CMD_PRINT_COMPLETE,
            "paused": self.can_comm.CMD_PRINT_PAUSE,
            "cancelled": self.can_comm.CMD_PRINT_CANCEL,
            "standby": self.can_comm.CMD_PRINTER_IDLE,
            "error": self.can_comm.CMD_PRINTER_ERROR  # 新增：错误状态映射
        }
        
    def _get_moonraker_url(self) -> str:
        """
        获取本机 IP 地址作为 Moonraker 的 URL
        
        Returns:
            str: Moonraker 的 URL
        """
        try:
            # 创建一个临时 socket 连接来获取本机 IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return f"http://{ip}:7125"
        except Exception as e:
            self.logger.warning(f"无法获取本机 IP: {str(e)}")
            return "http://localhost:7125"
        
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
            url = f"{self.base_url}/{method}"
            self.logger.info(f"发送请求到: {url}")
            
            if method == "server/info":
                # 使用 GET 请求获取服务器信息
                self.logger.info("使用 GET 请求获取服务器信息")
                response = requests.get(url, timeout=5)
            else:
                # 其他请求使用 POST 和 JSON-RPC
                request = {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params or {},
                    "id": int(time.time() * 1000)
                }
                self.logger.info(f"使用 POST 请求，数据: {json.dumps(request, indent=2)}")
                response = requests.post(
                    f"{self.base_url}/jsonrpc",
                    json=request,
                    timeout=5
                )
            
            self.logger.info(f"响应状态码: {response.status_code}")
            self.logger.info(f"响应头: {dict(response.headers)}")
            self.logger.info(f"响应内容: {response.text}")
            
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
        self.logger.info("开始获取服务器信息...")
        server_info = self._send_request("server/info")
        if not server_info:
            self.logger.error("获取服务器信息失败")
            self.can_comm.send_printer_error(self.can_comm.ERROR_MOONRAKER)
            return {}
            
        self.logger.info(f"服务器信息: {json.dumps(server_info, indent=2)}")
        
        # 检查 Klipper 状态
        server_result = server_info.get("result", {})
        if server_result.get("klippy_state") == "error":
            self.logger.error("Klipper 处于错误状态")
            self.can_comm.send_printer_error(self.can_comm.ERROR_KLIPPER)
            return {
                "server_info": server_result,
                "printer_status": {
                    "print_stats": {"state": "error"},
                    "toolhead": {"position": [0, 0, 0, 0]},
                    "extruder": {"temperature": 0, "target": 0},
                    "webhooks": {"state": "error"}
                }
            }
            
        # 获取打印机状态
        self.logger.info("开始获取打印机状态...")
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
            self.logger.error("获取打印机状态失败")
            self.can_comm.send_printer_error(self.can_comm.ERROR_COMMUNICATION)
            return {
                "server_info": server_result,
                "printer_status": {
                    "print_stats": {"state": "error"},
                    "toolhead": {"position": [0, 0, 0, 0]},
                    "extruder": {"temperature": 0, "target": 0},
                    "webhooks": {"state": "error"}
                }
            }
            
        self.logger.info(f"打印机状态: {json.dumps(printer_status, indent=2)}")
            
        return {
            "server_info": server_result,
            "printer_status": printer_status["result"]["status"]
        }