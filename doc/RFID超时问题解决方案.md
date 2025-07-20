# RFID超时问题解决方案

## 问题描述

日志显示RFID数据传输在收到起始包后超时：
```
2025-07-20 18:22:19 - 开始接收RFID数据: 挤出机0, 耗材通道1, 总包数37, 数据长度148字节
2025-07-20 18:22:25 - RFID错误: 挤出机0, 操作超时
```

## 问题分析

1. **症状**：
   - 收到RFID起始包（0x14命令）
   - 期望收到37个数据包
   - 实际一个数据包都没收到
   - 6秒后送料柜发送超时错误（0x19命令）

2. **可能原因**：
   - CAN消息处理逻辑问题
   - 送料柜等待确认信号
   - 数据包被错误处理

## 已实施的解决方案

### 1. 修复CAN消息处理逻辑

修改了 `can_communication.py` 中的消息处理：

```python
# 修复前：所有未知命令都进入status_callback
else:
    if self.status_callback:
        status_data = {...}
        asyncio.create_task(self.status_callback(status_data))

# 修复后：只有特定状态命令才进入status_callback
elif command in [self.STATUS_IDLE, self.STATUS_READY, ...]:
    if self.status_callback:
        status_data = {...}
        asyncio.create_task(self.status_callback(status_data))
else:
    # 未知命令只记录日志
    self.logger.debug(f"收到未知命令: 0x{command:02X}")
```

### 2. 增强调试日志

在关键位置添加了调试日志：

- CAN接收时记录RFID消息
- RFID解析器记录处理的每个消息
- 错误响应包含更多信息

### 3. 创建诊断工具

创建了 `diagnose_rfid.py` 诊断脚本，用于：
- 实时监控RFID消息
- 分析消息时序
- 识别传输中断位置

## 使用诊断工具

运行诊断脚本监控RFID通信：

```bash
sudo python3 diagnose_rfid.py
```

脚本会：
1. 监控60秒内的所有RFID消息
2. 显示每个消息的详细信息
3. 计算消息间隔时间
4. 识别未完成的传输会话

## 调试步骤

1. **启用DEBUG日志**：
   ```bash
   feeder_cabinet -v
   ```

2. **监控CAN原始数据**：
   ```bash
   # 在另一个终端运行
   candump can1 | grep -E "(10A|10B)"
   ```

3. **运行诊断脚本**：
   ```bash
   sudo python3 diagnose_rfid.py
   ```

4. **触发RFID读取**：
   在送料柜上插入耗材或手动触发RFID读取

5. **分析日志**：
   - 检查是否收到数据包（0x17命令）
   - 查看错误码详情
   - 分析消息时序

## 可能的后续问题

如果问题仍然存在，可能需要：

1. **检查送料柜固件**：
   - 确认数据包发送逻辑
   - 检查是否需要应答机制

2. **调整超时参数**：
   - 增加RFID传输超时时间
   - 调整数据包发送间隔

3. **协议确认**：
   - 确认是否需要发送确认消息
   - 验证协议版本兼容性

## 建议配置

在 `config.yaml` 中调整RFID配置：

```yaml
rfid:
  enabled: true
  auto_set_temperature: false
  data_dir: /home/mingda/printer_data/rfid
  transfer_timeout: 15.0  # 增加到15秒
  cleanup_interval: 30
```

## 预期结果

修复后应该看到：
1. 起始包后立即收到数据包
2. 数据包按序号递增
3. 最后收到结束包
4. 成功解析耗材信息

## 错误码说明

如果收到错误响应（0x19），错误码含义：
- 0x01: RFID读取失败
- 0x02: 无耗材或未检测到
- 0x03: 数据格式无效
- 0x04: 操作超时
- 0x05: 无挤出机映射
- 0x06: 系统忙

## 总结

RFID超时问题主要由CAN消息处理逻辑引起。通过修复消息分发逻辑和增强调试能力，应该能够正常接收RFID数据。如果问题持续，需要进一步分析送料柜端的实现。