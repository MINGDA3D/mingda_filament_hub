#!/usr/bin/env python3
# lancaigang261019 任务#460 配套: CAN 命令字双侧一致性自检
#
# 为什么有这个东西: 261019 F01 缺 case CMD_PRINT_PAUSE(0x06) 导致暂停态永久同步失效,
# 这类"枚举型分发器漏一项"的缺陷不会报错、只会在现场静默降级, 靠人眼对协议表不可靠。
# 本脚本把"定义表 vs 接收分发 switch"做差集, 改协议后必跑。
#
# 用法: python3 scripts/protocol_audit.py
# 退出码: 0=无致命遗漏(可能含提示), 1=发现接收侧缺口
import re, os, sys

FW = os.path.expanduser(
    '~/lancaigang-mingda-RD/mingda-md-f01-送料柜-260922/auto_feed_dry_box/auto_feed_dry_box')
HUB = os.path.expanduser(
    '~/lancaigang-mingda-RD/mingda-600D-机器拉取-2609261508-120-filament_hub/'
    'mingda_filament_hub/src/feeder_cabinet')

# 这些命令 F01 只发不收, 不要求接收分支
F01_TX_ONLY = {
    'CMD_RFID_RAW_DATA_NOTIFY', 'CMD_RFID_RAW_DATA_RESPONSE', 'CMD_RFID_DATA_PACKET',
    'CMD_RFID_DATA_END', 'CMD_RFID_READ_ERROR', 'CMD_FILAMENT_OUT_NOTIFY',
    'CMD_BUFFER_FILAMENT_STATUS_RESP', 'CMD_LOW_WEIGHT_NOTIFY',
    'CMD_TEMP_HUMIDITY_RESP', 'CMD_WEIGHT_SENSOR_FAULT',
}

# 这些命令 hub 只发不收
HUB_TX_ONLY = {
    'CMD_PRINTER_FILAMENT_STATUS_RESPONSE', 'CMD_FEEDER_MAPPING_RESPONSE',
    'CMD_RETRACT_FILAMENT', 'CMD_EXTRUDE_FILAMENT', 'CMD_RFID_RAW_DATA_REQUEST',
    'CMD_LOAD_FILAMENT', 'CMD_UNLOAD_FILAMENT', 'CMD_HEARTBEAT',
    'CMD_PRINTING', 'CMD_PRINT_COMPLETE', 'CMD_PRINT_PAUSE', 'CMD_PRINT_CANCEL',
    'CMD_PRINTER_IDLE', 'CMD_PRINTER_ERROR', 'CMD_REQUEST_FEED', 'CMD_STOP_FEED',
    'CMD_SET_FEEDER_MAPPING', 'CMD_QUERY_FEEDER_MAPPING', 'CMD_SET_MOTOR_CURRENT',
}

def die(msg):
    print('!! ' + msg)
    sys.exit(2)

def defs_from(path, pattern):
    if not os.path.exists(path):
        die('找不到文件: ' + path)
    src = open(path, encoding='utf-8', errors='ignore').read()
    d = {}
    for m in re.finditer(pattern, src, re.M):
        d[int(m.group(2), 16)] = m.group(1)
    return d, src

f01_defs, _ = defs_from(
    os.path.join(FW, 'Core/Inc/canserial.h'),
    r'#define\s+(CMD_[A-Z0-9_]+|PRINTER_[A-Z0-9_]+)\s+(0x[0-9A-Fa-f]+)')
# 分发 switch 在 .c 里, 定义表在 .h 里, 两者分属不同文件
f01_cpath = os.path.join(FW, 'Core/Src/canserial.c')
if not os.path.exists(f01_cpath):
    die('找不到文件: ' + f01_cpath)
f01_src = open(f01_cpath, encoding='utf-8', errors='ignore').read()

i = f01_src.find('static void handle_valid_message')
if i < 0:
    die('F01 找不到 handle_valid_message(函数名改了? 需同步改本脚本)')
j = f01_src.find('\n}', i)
f01_cases = set(re.findall(r'case\s+(CMD_[A-Z0-9_]+|PRINTER_[A-Z0-9_]+)\s*:', f01_src[i:j]))

hub_defs, hub_src = defs_from(
    os.path.join(HUB, 'can_communication.py'),
    r'^\s+(CMD_[A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+)')

hi = hub_src.find('async def _receive_loop')
if hi < 0:
    die('hub 找不到 _receive_loop')
hj = hub_src.find('\n    async def ', hi + 10)
hk = hub_src.find('\n    def ', hi + 10)
ends = [x for x in (hj, hk) if x > 0]
hub_seg = hub_src[hi:min(ends) if ends else len(hub_src)]
hub_recv = set(re.findall(r'self\.(CMD_[A-Z0-9_]+)', hub_seg))

fail = 0
print('=== F01 接收侧 (can serial.h 定义 %d 个) ===' % len(f01_defs))
for v, n in sorted(f01_defs.items()):
    if n in F01_TX_ONLY:
        continue
    if n not in f01_cases:
        print('  !! 0x%02X %-34s 定义了但 handle_valid_message 无分支' % (v, n))
        fail = 1
if not fail:
    print('  OK 所有接收类命令均有分支')

print()
print('=== hub 接收侧 (can_communication.py 定义 %d 个) ===' % len(hub_defs))
hub_missing = 0
for v, n in sorted(hub_defs.items()):
    if n in HUB_TX_ONLY:
        continue
    if n not in hub_recv:
        print('  !! 0x%02X %-34s 定义了但 _receive_loop 无分支' % (v, n))
        hub_missing += 1

print()
print('=== 双侧命令字数值差异 ===')
diff = 0
for k in sorted(set(f01_defs) | set(hub_defs)):
    a, b = f01_defs.get(k), hub_defs.get(k)
    if a != b:
        print('  !! 0x%02X  F01=%-32s hub=%s' % (k, a, b))
        diff = 1
if not diff:
    print('  OK 同值命令字命名/数值一致')

if hub_missing:
    print('\n提示: hub 缺分支的命令若为 F01 主动上报, 属静默丢弃(不崩但数据白传), 需评估是否消费。')
print('\nRESULT: %s' % ('FAIL' if fail else 'PASS'))
sys.exit(1 if fail else 0)
