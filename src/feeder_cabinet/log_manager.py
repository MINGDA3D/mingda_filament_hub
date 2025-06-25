"""
日志管理模块 - 提供完善的日志文件管理功能

此模块提供以下功能：
- 日志文件轮转（按大小和时间）
- 自动清理旧日志文件
- 多级别日志输出
- 线程安全的日志处理
"""

import os
import sys
import logging
import logging.handlers
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
import threading
import glob

class LogManager:
    """日志管理器类"""
    
    def __init__(self, 
                 app_name: str = "feeder_cabinet",
                 log_dir: str = "/home/mingda/printer_data/logs",
                 log_level: str = "INFO",
                 max_file_size: int = 10 * 1024 * 1024,  # 10MB
                 backup_count: int = 5,
                 max_age_days: int = 30,
                 console_output: bool = True):
        """
        初始化日志管理器
        
        Args:
            app_name: 应用名称，用于日志文件命名
            log_dir: 日志文件目录
            log_level: 日志级别
            max_file_size: 单个日志文件最大大小（字节）
            backup_count: 保留的日志文件数量
            max_age_days: 日志文件最大保留天数
            console_output: 是否输出到控制台
        """
        self.app_name = app_name
        self.log_dir = Path(log_dir)
        self.log_level = getattr(logging, log_level.upper())
        self.max_file_size = max_file_size
        self.backup_count = backup_count
        self.max_age_days = max_age_days
        self.console_output = console_output
        
        # 确保日志目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 日志文件路径
        self.log_file = self.log_dir / f"{app_name}.log"
        
        # 清理线程
        self.cleanup_thread = None
        self.cleanup_running = False
        self._cleanup_lock = threading.Lock()
        
    def setup_logger(self, logger_name: Optional[str] = None) -> logging.Logger:
        """
        设置并返回配置好的logger
        
        Args:
            logger_name: logger名称，默认使用app_name
            
        Returns:
            logging.Logger: 配置好的logger对象
        """
        if logger_name is None:
            logger_name = self.app_name
            
        logger = logging.getLogger(logger_name)
        logger.setLevel(self.log_level)
        
        # 移除现有的处理器
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # 创建格式化器
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - [%(levelname)s] - %(funcName)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 文件处理器 - 使用RotatingFileHandler
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(self.log_file),
            maxBytes=self.max_file_size,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(self.log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
        # 控制台处理器
        if self.console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(self.log_level)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        
        # 启动清理线程
        if not self.cleanup_running:
            self.start_cleanup_thread()
        
        return logger
    
    def get_child_logger(self, parent_logger: logging.Logger, child_name: str) -> logging.Logger:
        """
        获取子logger
        
        Args:
            parent_logger: 父logger
            child_name: 子logger名称
            
        Returns:
            logging.Logger: 子logger对象
        """
        return parent_logger.getChild(child_name)
    
    def update_log_level(self, logger: logging.Logger, level: str):
        """
        动态更新日志级别
        
        Args:
            logger: logger对象
            level: 新的日志级别
        """
        new_level = getattr(logging, level.upper())
        logger.setLevel(new_level)
        
        # 更新所有处理器的级别
        for handler in logger.handlers:
            handler.setLevel(new_level)
    
    def start_cleanup_thread(self):
        """启动日志清理线程"""
        self.cleanup_running = True
        self.cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="LogCleanup"
        )
        self.cleanup_thread.start()
    
    def stop_cleanup_thread(self):
        """停止日志清理线程"""
        self.cleanup_running = False
        if self.cleanup_thread and self.cleanup_thread.is_alive():
            self.cleanup_thread.join(timeout=5)
    
    def _cleanup_loop(self):
        """日志清理循环"""
        while self.cleanup_running:
            try:
                self.cleanup_old_logs()
                # 每小时检查一次
                threading.Event().wait(3600)
            except Exception as e:
                print(f"日志清理时发生错误: {e}")
                threading.Event().wait(300)  # 出错后等待5分钟
    
    def cleanup_old_logs(self):
        """清理过期的日志文件"""
        with self._cleanup_lock:
            try:
                cutoff_date = datetime.now() - timedelta(days=self.max_age_days)
                
                # 查找所有日志文件
                log_pattern = str(self.log_dir / f"{self.app_name}*.log*")
                log_files = glob.glob(log_pattern)
                
                for log_file in log_files:
                    try:
                        # 获取文件修改时间
                        mtime = os.path.getmtime(log_file)
                        file_date = datetime.fromtimestamp(mtime)
                        
                        # 如果文件过期，删除它
                        if file_date < cutoff_date:
                            os.remove(log_file)
                            print(f"已删除过期日志文件: {log_file}")
                    except Exception as e:
                        print(f"删除日志文件 {log_file} 时出错: {e}")
                        
            except Exception as e:
                print(f"清理日志文件时发生错误: {e}")
    
    def get_log_stats(self) -> Dict[str, Any]:
        """
        获取日志统计信息
        
        Returns:
            Dict: 包含日志文件数量、总大小等信息
        """
        stats = {
            'log_dir': str(self.log_dir),
            'files': [],
            'total_size': 0,
            'oldest_file': None,
            'newest_file': None
        }
        
        try:
            log_pattern = str(self.log_dir / f"{self.app_name}*.log*")
            log_files = glob.glob(log_pattern)
            
            for log_file in log_files:
                try:
                    file_stats = os.stat(log_file)
                    file_info = {
                        'path': log_file,
                        'size': file_stats.st_size,
                        'modified': datetime.fromtimestamp(file_stats.st_mtime).isoformat()
                    }
                    stats['files'].append(file_info)
                    stats['total_size'] += file_stats.st_size
                    
                    # 更新最旧和最新文件
                    if stats['oldest_file'] is None or file_stats.st_mtime < os.stat(stats['oldest_file']).st_mtime:
                        stats['oldest_file'] = log_file
                    if stats['newest_file'] is None or file_stats.st_mtime > os.stat(stats['newest_file']).st_mtime:
                        stats['newest_file'] = log_file
                        
                except Exception as e:
                    print(f"获取文件 {log_file} 统计信息时出错: {e}")
                    
        except Exception as e:
            print(f"获取日志统计信息时发生错误: {e}")
            
        return stats
    
    def archive_logs(self, archive_dir: Optional[str] = None) -> bool:
        """
        归档日志文件
        
        Args:
            archive_dir: 归档目录，默认为log_dir/archive
            
        Returns:
            bool: 归档是否成功
        """
        if archive_dir is None:
            archive_dir = self.log_dir / "archive"
        else:
            archive_dir = Path(archive_dir)
            
        try:
            # 创建归档目录
            archive_dir.mkdir(parents=True, exist_ok=True)
            
            # 创建带时间戳的子目录
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_subdir = archive_dir / timestamp
            archive_subdir.mkdir(parents=True, exist_ok=True)
            
            # 移动所有旧的日志文件到归档目录
            log_pattern = str(self.log_dir / f"{self.app_name}*.log.*")
            old_logs = glob.glob(log_pattern)
            
            for old_log in old_logs:
                try:
                    dest_path = archive_subdir / os.path.basename(old_log)
                    os.rename(old_log, dest_path)
                except Exception as e:
                    print(f"归档文件 {old_log} 时出错: {e}")
                    
            return True
            
        except Exception as e:
            print(f"归档日志文件时发生错误: {e}")
            return False
    
    def __del__(self):
        """析构函数，确保清理线程停止"""
        self.stop_cleanup_thread()


class MultiProcessLogManager(LogManager):
    """支持多进程的日志管理器"""
    
    def setup_logger(self, logger_name: Optional[str] = None) -> logging.Logger:
        """
        设置支持多进程的logger
        
        使用QueueHandler和QueueListener来处理多进程日志
        """
        if logger_name is None:
            logger_name = self.app_name
            
        logger = logging.getLogger(logger_name)
        logger.setLevel(self.log_level)
        
        # 移除现有的处理器
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # 创建格式化器
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - [%(levelname)s] - %(processName)s:%(process)d - %(funcName)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 使用WatchedFileHandler代替RotatingFileHandler以支持多进程
        file_handler = logging.handlers.WatchedFileHandler(
            filename=str(self.log_file),
            encoding='utf-8'
        )
        file_handler.setLevel(self.log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
        # 控制台处理器
        if self.console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(self.log_level)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        
        # 启动清理线程
        if not self.cleanup_running:
            self.start_cleanup_thread()
        
        return logger