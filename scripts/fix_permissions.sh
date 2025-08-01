#!/bin/bash
# 修复脚本文件权限和格式

echo "修复脚本文件权限和格式..."

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 转换所有 shell 脚本为 Unix 格式并设置可执行权限
find "$PROJECT_DIR" -name "*.sh" -type f | while read -r file; do
    echo "处理: $file"
    # 转换为 Unix 格式（如果有 dos2unix 命令）
    if command -v dos2unix &> /dev/null; then
        dos2unix "$file" 2>/dev/null
    fi
    # 设置可执行权限
    chmod +x "$file"
done

# 转换 Python 启动脚本
for file in "$PROJECT_DIR"/start_*.py "$PROJECT_DIR"/debug_*.py "$PROJECT_DIR"/diagnose_*.py "$PROJECT_DIR"/test_*.py; do
    if [ -f "$file" ]; then
        echo "处理: $file"
        if command -v dos2unix &> /dev/null; then
            dos2unix "$file" 2>/dev/null
        fi
        chmod +x "$file"
    fi
done

# 转换示例文件
find "$PROJECT_DIR" -name "*.example" -type f | while read -r file; do
    echo "处理: $file"
    if command -v dos2unix &> /dev/null; then
        dos2unix "$file" 2>/dev/null
    fi
done

echo "完成！"