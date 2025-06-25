#!/usr/bin/env python3
"""
送料柜自动续料系统启动脚本
用于正确设置Python路径并启动主程序
"""

import sys
import os

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# 导入并启动主程序
from feeder_cabinet.main import main

if __name__ == "__main__":
    main()