#!/usr/bin/env python3
"""
调试启动脚本 - 用于诊断服务启动问题
"""

import sys
import os
import traceback

# 添加源码目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

print(f"Python版本: {sys.version}")
print(f"Python路径: {sys.executable}")
print(f"当前目录: {os.getcwd()}")
print(f"脚本目录: {os.path.dirname(__file__)}")
print(f"sys.path: {sys.path}")

try:
    print("\n尝试导入feeder_cabinet模块...")
    import feeder_cabinet
    print(f"成功导入feeder_cabinet，版本: {feeder_cabinet.__version__}")
    
    print("\n尝试导入各个子模块...")
    from feeder_cabinet import FeederCabinetApp, main
    print("成功导入FeederCabinetApp和main")
    
    from feeder_cabinet.log_manager import LogManager
    print("成功导入LogManager")
    
    from feeder_cabinet.can_communication import FeederCabinetCAN
    print("成功导入FeederCabinetCAN")
    
    from feeder_cabinet.klipper_monitor import KlipperMonitor
    print("成功导入KlipperMonitor")
    
    print("\n尝试创建LogManager实例...")
    log_manager = LogManager()
    print("成功创建LogManager实例")
    
    print("\n尝试加载配置...")
    config_path = "/home/mingda/feeder_cabinet_help/config/config.yaml"
    if os.path.exists(config_path):
        print(f"配置文件存在: {config_path}")
        # 尝试创建应用实例
        print("\n尝试创建FeederCabinetApp实例...")
        app = FeederCabinetApp(config_path=config_path)
        print("成功创建FeederCabinetApp实例")
    else:
        print(f"配置文件不存在: {config_path}")
    
    print("\n所有导入和初始化测试通过！")
    
except Exception as e:
    print(f"\n错误: {str(e)}")
    print(f"\n详细错误信息:")
    traceback.print_exc()
    sys.exit(1)