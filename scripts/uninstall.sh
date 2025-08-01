#!/bin/bash
# MINGDA Filament Hub 卸载脚本

set -e

# 显示卸载信息
echo "==== MINGDA Filament Hub 卸载脚本 ===="
echo "该脚本将卸载 MINGDA Filament Hub 及其相关配置"
echo
echo "警告：此操作将删除服务、虚拟环境和相关配置"
echo "配置文件和日志文件将被保留以供备份"
echo

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
  echo "需要root权限才能进行卸载"
  echo "请使用 'sudo' 重新运行此脚本"
  exit 1
fi

# 确认卸载
read -p "确定要卸载 MINGDA Filament Hub 吗？(y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "卸载已取消"
    exit 0
fi

# 定义变量
SERVICE_NAME="mingda_filament_hub"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_DIR="/home/mingda/mingda_filament_hub_venv"
CONFIG_DIR="/home/mingda/printer_data/config"
LOG_DIR="/home/mingda/printer_data/logs"
HOME="/home/mingda"

echo "开始卸载..."

# 停止并禁用服务
echo "正在停止服务..."
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl stop "$SERVICE_NAME"
    echo "服务已停止"
fi

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    echo "服务已禁用"
fi

# 删除服务文件
if [ -f "$SERVICE_FILE" ]; then
    echo "正在删除服务文件..."
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    echo "服务文件已删除"
fi

# 删除虚拟环境
if [ -d "$VENV_DIR" ]; then
    echo "正在删除Python虚拟环境..."
    rm -rf "$VENV_DIR"
    echo "虚拟环境已删除"
fi

# 清理Moonraker配置
echo "正在清理Moonraker配置..."
function remove_moonraker_config() {
    local moonraker_configs regex
    regex="${HOME//\//\\/}\/([A-Za-z0-9_]+)\/config\/moonraker\.conf"
    moonraker_configs=$(find "${HOME}" -maxdepth 3 -type f -regextype posix-extended -regex "${regex}" | sort)

    for conf in ${moonraker_configs}; do
        if grep -q "^\[update_manager mingda_filament_hub\]" "${conf}"; then
            echo "正在从 ${conf} 中删除配置..."
            # 创建临时文件
            temp_file=$(mktemp)
            # 删除update_manager mingda_filament_hub段落
            awk '
                /^\[update_manager mingda_filament_hub\]/ {
                    skip = 1
                    next
                }
                /^\[/ && skip {
                    skip = 0
                }
                !skip {
                    print
                }
            ' "${conf}" > "${temp_file}"
            # 替换原文件
            mv "${temp_file}" "${conf}"
            chown mingda:mingda "${conf}"
        fi
    done
}

function remove_moonraker_service() {
    local moonraker_asvc regex
    regex="${HOME//\//\\/}\/([A-Za-z0-9_]+)\/moonraker\.asvc"
    moonraker_asvc=$(find "${HOME}" -maxdepth 3 -type f -regextype posix-extended -regex "${regex}" | sort)

    for conf in ${moonraker_asvc}; do
        if grep -q "^mingda_filament_hub$" "${conf}"; then
            echo "正在从 ${conf} 中删除服务..."
            # 删除mingda_filament_hub行
            sed -i '/^mingda_filament_hub$/d' "${conf}"
        fi
    done
}

remove_moonraker_config
remove_moonraker_service

# 询问是否删除配置和日志
echo
read -p "是否删除配置文件？(y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ -f "$CONFIG_DIR/config.yaml" ]; then
        echo "正在备份配置文件..."
        cp "$CONFIG_DIR/config.yaml" "$CONFIG_DIR/config.yaml.bak.$(date +%Y%m%d_%H%M%S)"
        rm -f "$CONFIG_DIR/config.yaml"
        echo "配置文件已删除（备份已保存）"
    fi
fi

read -p "是否删除日志文件？(y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ -f "$LOG_DIR/mingda_filament_hub.log" ]; then
        echo "正在备份日志文件..."
        cp "$LOG_DIR/mingda_filament_hub.log" "$LOG_DIR/mingda_filament_hub.log.bak.$(date +%Y%m%d_%H%M%S)"
        rm -f "$LOG_DIR/mingda_filament_hub.log"
        rm -f "$LOG_DIR/mingda_filament_hub.log.*"
        echo "日志文件已删除（备份已保存）"
    fi
fi

# 显示完成信息
echo
echo "==== 卸载完成 ===="
echo "MINGDA Filament Hub 已成功卸载"
echo
echo "以下内容已保留："
echo "- 项目源代码（如需删除请手动删除项目目录）"
echo "- CAN接口配置（如需删除请手动清理）"
if [ -f "$CONFIG_DIR/config.yaml.bak."* ]; then
    echo "- 配置文件备份: $CONFIG_DIR/config.yaml.bak.*"
fi
if [ -f "$LOG_DIR/mingda_filament_hub.log.bak."* ]; then
    echo "- 日志文件备份: $LOG_DIR/mingda_filament_hub.log.bak.*"
fi
echo
echo "如需完全删除，请手动删除项目目录"
echo

exit 0