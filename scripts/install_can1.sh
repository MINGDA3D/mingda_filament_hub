#!/bin/bash

# /etc/network/interfaces.d/can1 /usr/local/bin/can_rename.sh /etc/udev/rules.d/75-can-custom.rules

# file name
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
FILE_DIR=$SCRIPT_DIR/two_can
CAN_NAME=$FILE_DIR/can1
SH_NAME=$FILE_DIR/can_rename.sh
RU_NAME=$FILE_DIR/75-can-custom.rules

if [[ -f "$CAN_NAME" && -f "$SH_NAME" && -f "$RU_NAME" ]]; then
    echo "所有文件存在，继续执行"
else
    echo "错误：以下文件缺失："
    [[ ! -f "$CAN_NAME" ]] && echo "  - $CAN_NAME"
    [[ ! -f "$SH_NAME" ]] && echo "  - $SH_NAME"
    [[ ! -f "$RU_NAME" ]] && echo "  - $RU_NAME"
    exit 1
fi

# copy files
sudo cp $CAN_NAME /etc/network/interfaces.d/
sudo cp $SH_NAME /usr/local/bin/
sudo cp $RU_NAME /etc/udev/rules.d/

# restart service
sudo udevadm control --reload
sudo udevadm trigger
