# RFID功能使用说明

## 概述

送料柜自动续料系统已集成RFID耗材识别功能，能够自动接收和解析从送料柜发送的RFID耗材信息。

## 功能特点

1. **自动接收**：送料柜在以下情况会自动发送RFID数据：
   - 检测到新耗材插入
   - RFID标签读取成功
   - 手动更新耗材信息

2. **完整解析**：支持OpenTag标准的所有字段，包括：
   - 基本信息：制造商、材料类型、颜色
   - 规格参数：直径、重量、密度
   - 温度参数：打印温度、热床温度
   - 其他信息：序列号、生产日期等

3. **数据存储**：耗材信息自动保存到本地文件，便于查询和管理

4. **温度控制**：可配置自动应用耗材的温度设置

## 配置说明

在 `config/config.yaml` 中添加RFID配置：

```yaml
# RFID配置
rfid:
  # 是否启用RFID功能
  enabled: true
  
  # 是否自动设置耗材温度
  auto_set_temperature: false
  
  # RFID数据保存目录
  data_dir: /home/mingda/printer_data/rfid
  
  # RFID传输超时时间（秒）
  transfer_timeout: 10.0
  
  # 会话清理间隔（秒）
  cleanup_interval: 30
```

## 使用方法

### 1. 查看耗材信息

RFID数据接收后会在日志中显示：

```
2024-01-01 12:00:00 - feeder_cabinet - INFO - ============================================================
2024-01-01 12:00:00 - feeder_cabinet - INFO - 挤出机 0 耗材信息 (通道 0):
2024-01-01 12:00:00 - feeder_cabinet - INFO - ============================================================
2024-01-01 12:00:00 - feeder_cabinet - INFO - 制造商: MINGDA
2024-01-01 12:00:00 - feeder_cabinet - INFO - 材料类型: PLA
2024-01-01 12:00:00 - feeder_cabinet - INFO - 颜色: White
2024-01-01 12:00:00 - feeder_cabinet - INFO - 直径: 1.75 mm
2024-01-01 12:00:00 - feeder_cabinet - INFO - 标称重量: 1000 g
2024-01-01 12:00:00 - feeder_cabinet - INFO - 打印温度: 210°C
2024-01-01 12:00:00 - feeder_cabinet - INFO - 热床温度: 60°C
```

### 2. 查看保存的耗材数据

耗材信息保存在配置的数据目录中：

```bash
# 查看挤出机0的耗材信息
cat /home/mingda/printer_data/rfid/filament_extruder_0.json
```

输出示例：
```json
{
  "timestamp": "2024-01-01 12:00:00",
  "extruder_id": 0,
  "manufacturer": "MINGDA",
  "material": "PLA",
  "color": "White",
  "diameter": 1.75,
  "weight_nominal": 1000,
  "density": 1.24,
  "print_temp": 210,
  "bed_temp": 60,
  "serial_number": "MD202401010001"
}
```

### 3. 自动温度设置

如果启用了 `auto_set_temperature`，系统会自动应用耗材的温度设置：

```yaml
rfid:
  auto_set_temperature: true
```

当接收到RFID数据后，系统会自动执行：
- 设置对应挤出机的温度
- 设置热床温度（仅挤出机0）

### 4. 主动请求RFID数据

虽然系统主要依赖被动接收，但也可以通过修改代码主动请求：

```python
# 在需要的地方调用
await app.request_rfid_data(extruder_id)
```

## 故障排查

### 1. 未收到RFID数据

检查：
- CAN通信是否正常连接
- 送料柜是否成功读取RFID标签
- 查看日志中是否有RFID相关错误

### 2. 数据解析错误

可能原因：
- RFID标签数据格式不符合OpenTag标准
- 数据传输过程中出现错误
- 查看日志中的详细错误信息

### 3. 温度未自动设置

检查：
- 配置中 `auto_set_temperature` 是否为 `true`
- Klipper是否正常连接
- 打印机是否处于可接受命令的状态

## 日志级别

调试RFID功能时，建议使用DEBUG日志级别：

```yaml
logging:
  level: DEBUG
```

或使用命令行参数：
```bash
feeder_cabinet -v
```

## 扩展开发

### 1. 添加数据处理

在 `_process_filament_data` 方法中添加自定义处理：

```python
async def _process_filament_data(self, extruder_id, filament_id, data):
    # 现有处理...
    
    # 添加自定义处理
    # 例如：发送到数据库
    await self.save_to_database(extruder_id, data)
    
    # 例如：通知Web界面
    await self.notify_web_ui(extruder_id, data)
```

### 2. 添加新的命令

如需支持新的RFID相关命令，在 `can_communication.py` 中添加：

```python
CMD_RFID_CUSTOM = 0x1A  # 自定义命令

# 在接收循环中处理
elif command == self.CMD_RFID_CUSTOM:
    # 处理自定义命令
```

## 注意事项

1. RFID数据使用小端字节序
2. 字符串字段以NULL结尾
3. 可选字段未定义时值为0xFF/0xFFFF/0xFFFFFFFF
4. 数据传输采用分包机制，每包最多4字节有效数据
5. 传输超时默认为10秒，可在配置中调整

## 总结

RFID功能的集成使得送料柜系统能够自动识别耗材信息，提高了自动化程度。通过合理配置和使用，可以实现耗材的智能管理和参数自动设置。