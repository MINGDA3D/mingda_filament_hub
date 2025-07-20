# RFID耗材信息集成指南

## 概述

本指南介绍如何在送料柜自动续料系统中集成RFID耗材信息接收和解析功能。系统能够接收从送料柜发送的OpenTag格式RFID数据，并解析出耗材的详细信息。

## 核心组件

### 1. RFID数据解析器 (`rfid_parser.py`)

负责解析从送料柜接收的RFID数据：
- 处理分包传输协议
- 重组完整数据
- 解析OpenTag格式
- 管理传输会话

### 2. CAN通信模块更新 (`can_communication.py`)

添加了RFID相关命令和回调支持：
- RFID命令定义（0x14-0x19）
- RFID消息回调机制
- 请求RFID数据方法

## 集成步骤

### 1. 在主程序中添加RFID支持

```python
# 在 FeederCabinetApp.__init__ 中添加
from feeder_cabinet.rfid_parser import RFIDDataParser

# 初始化RFID解析器
self.rfid_parser = RFIDDataParser()
```

### 2. 设置RFID消息回调

```python
# 在 initialize_components 方法中添加
async def initialize_components(self):
    # ... 现有代码 ...
    
    # 设置RFID回调
    self.can_comm.set_rfid_callback(self._handle_rfid_message)
```

### 3. 实现RFID消息处理

```python
async def _handle_rfid_message(self, data: dict):
    """处理RFID CAN消息"""
    try:
        # 提取CAN数据
        can_data = bytes(data['data'])
        
        # 使用解析器处理
        result = self.rfid_parser.handle_rfid_message(can_data)
        
        if result:
            if result['type'] == 'rfid_complete':
                # 处理完整的耗材数据
                await self._process_filament_data(
                    result['extruder_id'],
                    result['filament_id'], 
                    result['data']
                )
            elif result['type'] == 'rfid_error':
                self.logger.error(f"RFID错误: {result.get('error_msg')}")
                
    except Exception as e:
        self.logger.error(f"处理RFID消息错误: {e}")

async def _process_filament_data(self, extruder_id, filament_id, data):
    """处理解析后的耗材数据"""
    self.logger.info(f"收到挤出机{extruder_id}的耗材信息:")
    self.logger.info(f"  材料: {data.material_name}")
    self.logger.info(f"  颜色: {data.color_name}")
    self.logger.info(f"  制造商: {data.manufacturer}")
    self.logger.info(f"  打印温度: {data.print_temp}°C")
    self.logger.info(f"  热床温度: {data.bed_temp}°C")
    
    # TODO: 根据需要添加更多处理逻辑
    # - 更新数据库
    # - 通知Web界面
    # - 自动设置打印参数
```

### 4. 添加定期清理任务

```python
# 在主循环中添加清理任务
async def run_async(self):
    # ... 现有代码 ...
    
    # 启动RFID会话清理任务
    cleanup_task = asyncio.create_task(self._cleanup_rfid_sessions())
    
    try:
        # ... 主循环 ...
    finally:
        cleanup_task.cancel()

async def _cleanup_rfid_sessions(self):
    """定期清理超时的RFID传输会话"""
    while True:
        try:
            await asyncio.sleep(30)  # 每30秒清理一次
            self.rfid_parser.cleanup_expired_sessions()
        except asyncio.CancelledError:
            break
```

## 使用场景

### 1. 被动接收模式（推荐）

送料柜在以下情况会主动发送RFID数据：
- 检测到新耗材插入
- RFID标签读取成功
- 手动更新耗材信息

系统自动接收并处理这些数据。

### 2. 主动查询模式

需要时可主动请求特定挤出机的RFID数据：

```python
# 请求挤出机0的RFID数据
await self.can_comm.request_rfid_data(0)
```

## 数据处理建议

### 1. 数据存储

建议将接收到的耗材信息存储到本地数据库或文件：

```python
def save_filament_info(self, extruder_id, data):
    """保存耗材信息到文件"""
    info = {
        'timestamp': datetime.now().isoformat(),
        'extruder_id': extruder_id,
        'manufacturer': data.manufacturer,
        'material': data.material_name,
        'color': data.color_name,
        'print_temp': data.print_temp,
        'bed_temp': data.bed_temp,
        # ... 其他字段
    }
    
    # 保存到JSON文件
    with open(f'filament_{extruder_id}.json', 'w') as f:
        json.dump(info, f, indent=2)
```

### 2. 自动参数设置

可以根据耗材信息自动设置打印参数：

```python
async def apply_filament_settings(self, data):
    """应用耗材设置到打印机"""
    # 设置挤出温度
    gcode = f"M104 S{data.print_temp}"
    await self.klipper_monitor.execute_gcode(gcode)
    
    # 设置热床温度  
    gcode = f"M140 S{data.bed_temp}"
    await self.klipper_monitor.execute_gcode(gcode)
```

### 3. Web界面集成

通过WebSocket或API将耗材信息推送到Web界面：

```python
async def notify_web_interface(self, extruder_id, data):
    """通知Web界面更新耗材信息"""
    payload = {
        'type': 'filament_update',
        'extruder': extruder_id,
        'material': data.material_name,
        'color': data.color_name,
        'manufacturer': data.manufacturer
    }
    
    # 发送到Moonraker或自定义WebSocket
    await self.send_to_web(payload)
```

## 错误处理

系统会自动处理以下错误情况：
- 数据包丢失或损坏
- 校验和错误
- 传输超时（10秒）
- 无效的数据格式

错误信息会记录在日志中，方便调试。

## 调试建议

1. **启用详细日志**：
   ```bash
   feeder_cabinet -v  # 启用verbose模式
   ```

2. **监控CAN消息**：
   ```bash
   candump can1 | grep -E "(10A|10B)"
   ```

3. **检查RFID消息**：
   查看日志中的RFID相关信息，确认数据接收和解析是否正常。

## 注意事项

1. **字节序**：OpenTag数据使用小端格式，解析器已正确处理
2. **字符编码**：字符串使用UTF-8编码
3. **数据完整性**：使用校验和验证数据完整性
4. **内存管理**：自动清理超时的传输会话，避免内存泄漏

## 扩展功能

可以基于RFID数据实现更多功能：
- 耗材使用统计
- 自动订购提醒
- 打印质量优化
- 材料兼容性检查
- 打印历史记录

## 总结

RFID功能的集成使得送料柜系统能够自动识别和管理不同的耗材，提高了自动化程度和用户体验。通过合理的数据处理和应用，可以实现更智能的3D打印管理。