#!/bin/bash

LOG_PATH="/home/mingda/tmp/can_rename.log"
if [ -f $LOG_PATH ] # 判断文件是否存在
then
    size=$(ls -l $LOG_PATH | awk '{print $5}') # 获取文件的字节数
    if [ $size -gt 100*1024 ] # 判断是否大于100kb
    then
        rm $LOG_PATH # 删除文件
    fi
fi

if [ $# -lt 2 ]; then
    #echo "错误：至少需要2个参数" >&2
    #echo "用法：$0 <当前设备名> <目标设备名> [临时设备名]" >&2
    echo "Error, at least 2 parameters are required" >> $LOG_PATH
    exit 1
fi

# 目标名称（例如从 udev 环境变量获取）
CURRENT_NAME="$1"
TARGET_NAME="$2"  # 替换为你的目标名称
TEMP_NAME="temp0"
REC_FLAG=0

echo "*** 1:$CURRENT_NAME 2:$TARGET_NAME***" >> $LOG_PATH
echo $(date) >> $LOG_PATH

if [ $1 == $2 ]; then
    ifdown "$TARGET_NAME"
    ifup   "$TARGET_NAME"
    echo "Finish: The same." >> $LOG_PATH
    exit 0
fi

# 检查目标名称是否已存在
if ip link show "$TARGET_NAME" &>/dev/null; then
    # 如果存在，重命名为临时名称
    #ip link set "$TARGET_NAME" down
    ifdown "$TARGET_NAME"
    ip link set "$TARGET_NAME" name "$TEMP_NAME"
    #ip link set "$TEMP_NAME" up
    REC_FLAG=1
else
    echo "No $TARGET_NAME, just name it" >> $LOG_PATH
fi

# 将当前设备重命名为目标名称（由 udev 传递的设备名）
#ip link set "$CURRENT_NAME" down
ifdown "$CURRENT_NAME"
ip link set "$CURRENT_NAME" name "$TARGET_NAME"
ifup "$TARGET_NAME"

if [ $REC_FLAG -eq 1 ]; then
    ip link set "$TEMP_NAME" name "$CURRENT_NAME"
    ifup "$CURRENT_NAME"
    echo "Finish: $CURRENT_NAME <==> $TARGET_NAME" >> $LOG_PATH
else
    echo "Finish: $CURRENT_NAME ===> $TARGET_NAME" >> $LOG_PATH
fi

exit 0
