# MINGDA Filament Hub

## 项目简介

MINGDA Filament Hub 是一个基于Klipper的3D打印机自动续料解决方案，通过CAN总线与料丝中心控制器通信，实现自动检测断料、请求送料、恢复打印的完整流程。系统支持多挤出头、多种错误处理，并提供状态监控和G-code命令控制。

### 主要功能

- **断料检测与自动续料**：检测到耗材用尽后自动暂停打印，请求送料，等待完成后恢复打印
- **实时状态监控**：监控打印机和料丝中心状态，及时处理状态变化
- **G-code命令集成**：提供G-code命令控制料丝中心的各种操作
- **错误处理与恢复**：支持多种错误类型处理和自动恢复
- **丰富的配置选项**：支持自定义CAN接口、刷新频率、日志级别等

## 系统需求

- **操作系统**：Debian 11或兼容系统
- **Python**：3.7或更高版本
- **依赖项**：`python-can`、`requests`、`pyyaml`、`websocket-client`、`websockets`、`aiohttp`
- **硬件**：具有CAN接口的控制板，如BTT Octopus、SKR等
- **固件**：Klipper与Moonraker

## 部署步骤

### 快速部署（推荐）

使用提供的安装脚本可以自动完成大部分配置：

```bash
# 克隆代码仓库
git clone https://github.com/your-username/mingda_filament_hub.git
cd mingda_filament_hub

# 运行安装脚本（需要root权限）
sudo scripts/install.sh
```

安装脚本会自动完成以下操作：
- 安装系统依赖和Python包
- 创建Python虚拟环境
- 配置CAN接口（如果相关文件存在）
- 复制配置文件到指定目录
- 创建并启动systemd服务

### 手动部署

如果需要手动部署或自定义安装，请按以下步骤操作：

#### 1. 安装依赖项

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv python3-yaml python3-can
```

#### 2. 创建虚拟环境并安装Python包

```bash
# 创建虚拟环境
python3 -m venv /home/mingda/mingda_filament_hub_venv

# 激活虚拟环境
source /home/mingda/mingda_filament_hub_venv/bin/activate

# 安装Python包
pip install --upgrade pip
pip install python-can requests pyyaml websocket-client websockets aiohttp

# 安装项目
pip install -e .

# 退出虚拟环境
deactivate
```

#### 3. 配置CAN接口

CAN接口配置文件位于`scripts/`目录下：
- `can1`: CAN接口配置文件
- `can_rename.sh`: CAN设备重命名脚本
- `75-can-custom.rules`: udev规则文件

复制这些文件到系统目录：

```bash
sudo cp scripts/can1 /etc/network/interfaces.d/
sudo cp scripts/can_rename.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/can_rename.sh
sudo cp scripts/75-can-custom.rules /etc/udev/rules.d/

# 重载udev规则
sudo udevadm control --reload
sudo udevadm trigger
```

#### 4. 配置应用程序

创建配置目录并复制配置文件：

```bash
# 创建配置目录
sudo mkdir -p /home/mingda/printer_data/config
sudo mkdir -p /home/mingda/printer_data/logs

# 复制配置文件
sudo cp config/config.yaml /home/mingda/printer_data/config/

# 设置权限
sudo chown -R mingda:mingda /home/mingda/printer_data/config
sudo chown -R mingda:mingda /home/mingda/printer_data/logs
```

根据需要编辑配置文件：

```bash
nano /home/mingda/printer_data/config/config.yaml
```

#### 5. 配置系统服务

创建systemd服务文件：

```bash
sudo tee /etc/systemd/system/mingda_filament_hub.service > /dev/null << EOF
[Unit]
Description=MINGDA Filament Hub System
After=network.target
After=klipper.service
After=moonraker.service

[Service]
Type=simple
User=mingda
ExecStart=/home/mingda/mingda_filament_hub_venv/bin/python /path/to/mingda_filament_hub/src/mingda_filament_hub/main.py -c /home/mingda/printer_data/config/config.yaml
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

# 重载systemd配置
sudo systemctl daemon-reload

# 启用并启动服务
sudo systemctl enable mingda_filament_hub
sudo systemctl start mingda_filament_hub
```

### 为Moonraker配置更新管理器

编辑Moonraker配置文件（通常为`~/printer_data/config/moonraker.conf`），添加：

```
[update_manager mingda_filament_hub]
type: git_repo
path: ~/mingda_filament_hub  # 实际安装路径
origin: https://github.com/your-username/mingda_filament_hub.git
primary_branch: main
managed_services: mingda_filament_hub
install_script: scripts/install.sh
```

### 添加Klipper宏

编辑你的Klipper配置文件（通常为`printer.cfg`），添加提供的G-code宏。你可以从`src/mingda_filament_hub/gcode_macros.py`中复制相关宏定义。

### 验证部署

部署完成后，请执行以下步骤验证系统是否正常工作：

1. **检查服务状态**：
   ```bash
   sudo systemctl status mingda_filament_hub
   ```
   
2. **查看日志**：
   ```bash
   # 查看实时日志
   sudo journalctl -u mingda_filament_hub -f
   
   # 查看应用日志
   tail -f /home/mingda/printer_data/logs/mingda_filament_hub.log
   ```

3. **验证CAN接口**：
   ```bash
   # 检查CAN接口状态
   ip link show can1
   
   # 监控CAN总线消息
   candump can1 | grep -E "(10A|10B)"
   ```

4. **测试通信**：
   在Klipper控制台中运行：
   ```
   QUERY_FILAMENT_HUB
   ```

## 使用方法

### 基本命令

系统安装后会自动运行。你可以使用以下命令手动控制：

```bash
# 启动服务
sudo systemctl start mingda_filament_hub

# 停止服务
sudo systemctl stop mingda_filament_hub

# 查看日志
sudo journalctl -u mingda_filament_hub -f

# 运行带有特定配置的实例
mingda_filament_hub -c /path/to/config.yaml

# 调试模式
mingda_filament_hub -v
```

### G-code命令

在打印过程中，你可以使用以下G-code命令：

- `START_FILAMENT_HUB EXTRUDER=0`：请求料丝中心给指定挤出机送料
- `QUERY_FILAMENT_HUB`：查询料丝中心状态
- `CANCEL_FILAMENT_HUB EXTRUDER=0`：取消正在进行的送料操作
- `ENABLE_FILAMENT_RUNOUT`：启用断料检测
- `DISABLE_FILAMENT_RUNOUT`：禁用断料检测

例如：
```
G-code控制台> START_FILAMENT_HUB EXTRUDER=0 FORCE=1
```

## 通信协议

本节定义了料丝中心与打印机（通过`mingda_filament_hub`服务）之间的CAN通信协议。



**实现说明:**

1.  料丝中心控制器发送CAN ID为 `0x10B` 的消息来发起查询。
2.  运行在打印机主机上的 `mingda_filament_hub` 服务通过 `can_communication.py` 模块监听此ID。
3.  收到查询后，服务通过 `klipper_monitor.py` 模块向Moonraker API请求断料传感器的状态。
4.  服务根据Klipper返回的状态构建响应数据包。
5.  服务通过 `can_communication.py` 模块发送ID为 `0x10A` 的响应消息给料丝中心。

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
  log_dir: /var/log/mingda_filament_hub  # 日志文件目录
```

## 故障排除

### 部署相关问题

1. **安装脚本执行失败**
   - 确保使用root权限运行：`sudo scripts/install.sh`
   - 检查系统是否满足所有依赖要求
   - 查看脚本输出的错误信息

2. **服务无法启动**
   - 检查Python虚拟环境是否正确创建
   - 验证配置文件路径是否正确
   - 查看服务日志：`sudo journalctl -u mingda_filament_hub -n 50`

3. **Python模块导入错误**
   - 确保虚拟环境中安装了所有依赖
   - 检查PYTHONPATH设置
   - 尝试重新安装：`sudo scripts/install.sh`

### 运行时问题

1. **CAN总线连接失败**
   - 检查CAN接口是否正确配置：`ip link show can1`
   - 确认硬件连接是否正确
   - 验证udev规则是否生效
   - 查看日志确认错误信息

2. **握手失败**
   - 确认料丝中心控制器已启动
   - 检查CAN总线速率是否匹配（默认1Mbps）
   - 检查料丝中心固件是否支持该握手协议
   - 使用`candump`监控CAN消息

3. **自动续料不工作**
   - 确认已启用断料检测
   - 检查Moonraker连接状态
   - 验证断料传感器配置
   - 查看日志确认是否有错误信息

4. **KlipperMonitor连接问题**
   - 检查Moonraker URL配置是否正确
   - 确认Moonraker服务正在运行
   - 验证网络连接和防火墙设置

### 日志文件

主要日志文件位于：

- 应用日志：`/home/mingda/printer_data/logs/mingda_filament_hub.log`
- Systemd日志：`journalctl -u mingda_filament_hub`
- CAN重命名日志：`/home/mingda/tmp/can_rename.log`

## 开发者信息

### 项目架构

- `can_communication.py`: CAN总线通信模块
- `klipper_monitor.py`: Klipper状态监控模块
- `gcode_macros.py`: G-code宏集成模块
- `main.py`: 应用程序主入口

### 调试模式

使用verbose模式运行以获取更多调试信息：

```bash
mingda_filament_hub -v
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
- GitHub Issues: [创建issue](https://github.com/your-username/mingda_filament_hub/issues)
- 邮箱: your-email@example.com 