# 送料柜自动续料系统

## 项目简介

送料柜自动续料系统是一个基于Klipper的3D打印机自动续料解决方案，通过CAN总线与送料柜控制器通信，实现自动检测断料、请求送料、恢复打印的完整流程。系统支持多挤出头、多种错误处理，并提供状态监控和G-code命令控制。

### 主要功能

- **断料检测与自动续料**：检测到耗材用尽后自动暂停打印，请求送料，等待完成后恢复打印
- **实时状态监控**：监控打印机和送料柜状态，及时处理状态变化
- **G-code命令集成**：提供G-code命令控制送料柜的各种操作
- **错误处理与恢复**：支持多种错误类型处理和自动恢复
- **丰富的配置选项**：支持自定义CAN接口、刷新频率、日志级别等

## 系统需求

- **操作系统**：Debian 11或兼容系统
- **Python**：3.7或更高版本
- **依赖项**：`python-can`、`requests`、`pyyaml`
- **硬件**：具有CAN接口的控制板，如BTT Octopus、SKR等
- **固件**：Klipper与Moonraker

## 安装步骤

### 1. 安装依赖项

```bash
sudo apt update
sudo apt install -y python3-pip python3-yaml python3-can
pip3 install python-can requests pyyaml
```

### 2. 配置CAN接口

```bash
# 加载CAN模块
sudo modprobe can
sudo modprobe can_raw

# 设置CAN接口（请根据实际硬件调整参数）
sudo ip link set can0 type can bitrate 1000000
sudo ip link set up can0
```

添加到系统启动项（/etc/network/interfaces）：

```
auto can0
iface can0 inet manual
    pre-up /sbin/ip link set $IFACE type can bitrate 1000000
    up /sbin/ifconfig $IFACE up
    down /sbin/ifconfig $IFACE down
```

### 3. 安装送料柜自动续料系统

```bash
# 克隆代码仓库
git clone https://github.com/your-username/feeder_cabinet.git
cd feeder_cabinet

# 安装
pip3 install -e .
```

### 4. 配置

创建配置目录和默认配置文件：

```bash
sudo mkdir -p /etc/feeder_cabinet
sudo cp config/config.yaml.example /etc/feeder_cabinet/config.yaml
```

根据需要编辑配置文件：

```bash
sudo nano /etc/feeder_cabinet/config.yaml
```

### 5. 为Moonraker创建自定义组件

编辑Moonraker配置文件（通常为`~/printer_data/config/moonraker.conf`），添加：

```
[update_manager feeder_cabinet]
type: git_repo
path: ~/feeder_cabinet  # 实际安装路径
origin: https://github.com/your-username/feeder_cabinet.git
primary_branch: main
managed_services: feeder_cabinet
install_script: scripts/install.sh
```

### 6. 配置系统服务

```bash
sudo cp scripts/feeder_cabinet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable feeder_cabinet
sudo systemctl start feeder_cabinet
```

### 7. 添加Klipper宏

编辑你的Klipper配置文件（通常为`printer.cfg`），添加提供的G-code宏。你可以从`src/feeder_cabinet/gcode_macros.py`中复制相关宏定义。

## 使用方法

### 基本命令

系统安装后会自动运行。你可以使用以下命令手动控制：

```bash
# 启动服务
sudo systemctl start feeder_cabinet

# 停止服务
sudo systemctl stop feeder_cabinet

# 查看日志
sudo journalctl -u feeder_cabinet -f

# 运行带有特定配置的实例
feeder_cabinet -c /path/to/config.yaml

# 调试模式
feeder_cabinet -v
```

### G-code命令

在打印过程中，你可以使用以下G-code命令：

- `START_FEEDER_CABINET EXTRUDER=0`：请求送料柜给指定挤出机送料
- `QUERY_FEEDER_CABINET`：查询送料柜状态
- `CANCEL_FEEDER_CABINET EXTRUDER=0`：取消正在进行的送料操作
- `ENABLE_FILAMENT_RUNOUT`：启用断料检测
- `DISABLE_FILAMENT_RUNOUT`：禁用断料检测

例如：
```
G-code控制台> START_FEEDER_CABINET EXTRUDER=0 FORCE=1
```

## 通信协议

本节定义了送料柜与打印机（通过`feeder_cabinet`服务）之间的CAN通信协议。

### 查询挤出机余料状态

此命令用于送料柜查询打印机左右挤出机是否有料。

*   **请求 (送料柜 -> 打印机)**
    *   **命令**: `QUERY_PRINTER_FILAMENT_STATUS` 0x0D
    *   **CAN ID**: `0x10B` (示例ID, 可配置)
    *   **数据 (Payload)**: (1字节)。该命令查询所有配置的挤出机状态。
    *   **示例**: 10B#0D

*   **响应 (打印机 -> 送料柜)**
    *   **命令**: `PRINTER_FILAMENT_STATUS_RESPONSE` 0x0E
    *   **CAN ID**: `0x10A` (示例ID, 可配置)
    *   **数据 (Payload)**: (3字节) 命令号+有效位+状态数据
        *   `有效位`: 数据有效：0x00，数据无效：0x01。   
        *   `状态数据`: 挤出机状态。每一位对应一个挤出机是否有料。
            *   Bit 0: 送料柜左缓冲区对应的挤出机 (Extruder 0或者Extruder1，须看config.yaml如何设置) 状态 (1: 有料, 0: 无料)
            *   Bit 1: 送料柜右缓冲区对应的挤出机 (Extruder 0或者Extruder 1，须看config.yaml如何设置) 状态 (1: 有料, 0: 无料)
        *   **示例**: 10A#0E0001

**实现说明:**

1.  送料柜控制器发送CAN ID为 `0x10B` 的消息来发起查询。
2.  运行在打印机主机上的 `feeder_cabinet` 服务通过 `can_communication.py` 模块监听此ID。
3.  收到查询后，服务通过 `klipper_monitor.py` 模块向Moonraker API请求断料传感器的状态。
4.  服务根据Klipper返回的状态构建响应数据包。
5.  服务通过 `can_communication.py` 模块发送ID为 `0x10A` 的响应消息给送料柜。

## 配置选项

配置文件（`config.yaml`）包含以下主要选项：

```yaml
# CAN通信配置
can:
  interface: can0         # CAN接口名称
  bitrate: 1000000        # 波特率

# Klipper/Moonraker连接配置
klipper:
  moonraker_url: http://localhost:7125  # Moonraker API URL
  update_interval: 5.0     # 状态更新间隔（秒）

# 断料检测配置
filament_runout:
  enabled: true           # 是否启用断料检测
  sensor_pin: null        # 传感器引脚（可选）

# 日志配置
logging:
  level: INFO            # 日志级别 (DEBUG, INFO, WARNING, ERROR)
  log_dir: /var/log/feeder_cabinet  # 日志文件目录
```

## 故障排除

### 常见问题

1. **CAN总线连接失败**
   - 检查CAN接口是否正确配置
   - 确认硬件连接是否正确
   - 查看日志确认错误信息

2. **握手失败**
   - 确认送料柜控制器已启动
   - 检查CAN总线速率是否匹配
   - 检查送料柜固件是否支持该握手协议

3. **自动续料不工作**
   - 确认已启用断料检测
   - 检查打印机状态是否正确传递
   - 查看日志确认是否有错误信息

### 日志文件

主要日志文件位于：

- 系统日志：`/var/log/feeder_cabinet/feeder_cabinet.log`
- Systemd日志：`journalctl -u feeder_cabinet`

## 开发者信息

### 项目架构

- `can_communication.py`: CAN总线通信模块
- `klipper_monitor.py`: Klipper状态监控模块
- `gcode_macros.py`: G-code宏集成模块
- `main.py`: 应用程序主入口

### 调试模式

使用verbose模式运行以获取更多调试信息：

```bash
feeder_cabinet -v
```

### 贡献指南

1. Fork本仓库
2. 创建功能分支: `git checkout -b my-new-feature`
3. 提交更改: `git commit -am 'Add some feature'`
4. 推送到分支: `git push origin my-new-feature`
5. 提交Pull Request

## 许可证

本项目采用MIT许可证 - 详见[LICENSE](LICENSE)文件。

## 联系方式

如有问题或建议，请通过以下方式联系：
- GitHub Issues: [创建issue](https://github.com/your-username/feeder_cabinet/issues)
- 邮箱: your-email@example.com 