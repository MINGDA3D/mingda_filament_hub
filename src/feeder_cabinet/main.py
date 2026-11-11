#!/usr/bin/env python3
"""
自动续料系统主程序

此模块提供应用程序的入口点，包括：
- 参数解析
- 配置加载
- 日志设置
- 系统初始化和启动
"""

import os
import sys
import time
import logging
import argparse
import threading
import json
import shutil
import yaml
from typing import Dict, Any, Optional
import requests
import asyncio

try:
    from feeder_cabinet.can_communication import FeederCabinetCAN
    from feeder_cabinet.klipper_monitor import KlipperMonitor
    from feeder_cabinet.log_manager import LogManager
    from feeder_cabinet.state_manager import StateManager, SystemStateEnum
    from feeder_cabinet.rfid_parser import RFIDDataParser, OpenTagFilamentData
    from feeder_cabinet.spoolman_client import SpoolmanClient
    from feeder_cabinet import feed_ack_contract as ack_contract
except ImportError:
    # 如果从包导入失败，尝试相对导入
    from .can_communication import FeederCabinetCAN
    from .klipper_monitor import KlipperMonitor
    from .log_manager import LogManager
    from .state_manager import StateManager, SystemStateEnum
    from .rfid_parser import RFIDDataParser, OpenTagFilamentData
    from .spoolman_client import SpoolmanClient
    from . import feed_ack_contract as ack_contract

# 配置默认参数
DEFAULT_CONFIG_PATH = "/home/mingda/feeder_cabinet_help/config/config.yaml"
DEFAULT_CAN_INTERFACE = "can1"
DEFAULT_CAN_BITRATE = 1000000
DEFAULT_MOONRAKER_URL = "http://localhost:7125"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_UPDATE_INTERVAL = 5.0
DEFAULT_LOG_DIR = "/home/mingda/printer_data/logs"

# ============ 任务#286 审计#282漏洞修复常量（hub侧·qoder-expert） ============
# V1/V2/V3/V5 静默死等族兜底: FEEDING态超时阈值(秒)。
# F05侧INIT_FEED推8米料最长300s超时, hub取5min(300s)不误伤正常长续料;
# 超时后回PAUSED由PAUSED轮询自动重走编排重发0x01(F05侧分流⑤粘滞态正好接住,#262闭环补全)
FEEDING_TIMEOUT_SECONDS = 300.0
# FEEDING超时防重试风暴: 连续超时重试上限, 达到后放弃自动重试保持PAUSED+ERROR日志等人工
# (避免F05侧真ERROR如无兼容候选时无限循环重试刷屏; F05无候选ERROR其侧有弹窗提示)
FEEDING_MAX_TIMEOUT_RETRIES = 3

# ============ 任务#462/#463: 缓冲器"假有料"自愈 + 回执契约收敛 ============
# 根因(261019 双侧日志直证, 非推测): 剪断点在缓冲器下游时, 缓冲内残料成为"死料"(上游已断,
#   永远不会被拉走), 右缓冲缺料开关(no_fil_status / GPIO NO_FIL2)被死料顶着长期报"有料"——
#   实测 f01_watch_1019_102003.log 中 R(no=0) 从剪断起卡了 4分45秒, 直到人工捅缓冲器才变 1。
#   F01 据此走 check_trigger_conditions 的 trigger=2(缓冲有料) 分支 -> 回 IDLE,
#   hub 原逻辑对回执零动作、死等 FEEDING_TIMEOUT_SECONDS(300s), 现场表现为"续料完全没反应"。
# 对策: 确认"未受理"后补发 0x1E 退料把死料退出缓冲 -> 开关触发 -> 重发 0x01, 最多 3 轮。
#
# ★ 261019 任务#463 B(机制收敛): 判定逻辑全部搬到 feed_ack_contract.py(契约唯一真值源),
#   本文件只留时序参数。收敛后的唯一决策链见该模块 docstring, 一句话:
#   [回执分类] -> [非失败统一挂 10s 裁决] -> [0x03 探测真实状态] -> [仅"曾受理且此刻 IDLE"才自愈]。
#   本轮退休的机制: BUFFER_CLEAR_CONFIRM_SECONDS(60s 观察窗) —— 职责被 10s 裁决完全覆盖且更快;
#   "收到 0x00 立即退料"与"只认 0x02 一帧"两个错误前提同时作废。
BUFFER_CLEAR_MAX_ROUNDS = 3
# 0x01 回执后多久补发一次 0x03 查询做裁决(秒)。取值依据: 真机 F01 从受理到回落 IDLE 仅 373ms,
#   正常进料全程 2~60s; 10s 既跨过"瞬时置位"窗口, 又远早于被退休的 60s 观察窗。
FEED_VERDICT_DELAY_SECONDS = 10.0
# 裁决探测等回执的最长时长(秒)。超时=放弃本次探测, 交回 300s 超时兜底
#   (必须清掉探测标志, 否则会误吞后续真回执)。
FEED_VERDICT_REPLY_TIMEOUT_SECONDS = 15.0
# 每轮重发 0x01 后等待回执的时长(秒): F01 受理会回 STATUS_FEEDING 并取消本任务, 故正常情况
# 会被提前打断; 8s 仅为"未受理"时进入下一轮的间隔, 不参与成功路径判定。
BUFFER_CLEAR_ACK_WAIT_SECONDS = 8.0

# lancaigang261017 任务#447: PAUSE 在途最长等待(秒)。
# 背景(2026-10-17 09:40-09:57 事故): Klipper 阻塞在 START_PRINT 的 M191/M109 等温流程约 14 分钟,
# 期间低重通知每 30s 重入一次 _auto_change_filament, 每次都往队列里塞一条 PAUSE + UNLOAD,
# 共堆了 4 条 PAUSE + 3 条 UNLOAD; 等温结束队列集中放行, 第一条 PAUSE 在 T1 换头后把打印打断。
# 守卫逻辑: 已下发但尚未确认生效的 PAUSE 在途期间, 不再下发第二条; 超过本时限视为该 PAUSE
# 已丢失(仍可能迟到生效), 允许重新下发避免功能死锁。正常情况 PAUSE 秒级生效, 本值只兜底异常。
PAUSE_PENDING_MAX_WAIT = 300.0

# lancaigang261017 任务#447(用户261017追加定策): 首次进料前"一律缓发PAUSE"的兜底上限(秒)。
# 背景: 261017 09:40:58 那次低重通知落在 START_PRINT 的升温/调平阶段(尚未挤出第一段料),
# PAUSE 被排进已阻塞的队列, 等温结束才迟到生效, 打印在 T1 换头后被莫名打断。
# 首次进料前(含升温/调平/网床/归位等开场流程)一律不发 PAUSE, 等 F05 侧 WAIT_CUT 30s 重发
# 0x1D, 首次进料后立即受理, PAUSE 当场生效而不是迟到生效。
# 本上限仅兜底 _extruder_fed_seen 永不为 True 的异常场景(超时后仍允许下发, 且下发前还有
# probe_gcode_queue 队列探测兜底, 不会把 PAUSE 塞进阻塞队列)。
LOW_WEIGHT_FED_DEFER_MAX = 1800.0
# 首次进料前缓发的节流日志间隔(秒): 0x1D 每 30s 重发, 不节流会在长升温期刷屏
LOW_WEIGHT_DEFER_LOG_INTERVAL = 60.0

# lancaigang261017 任务#447: RUNOUT暂停在队列阻塞期的重试参数。
# 背景(261017 10:44:48 实测): 打印中Klipper阻塞在长命令时, RUNOUT路径发PAUSE回执超时,
# 旧代码直接置ERROR("Failed to pause print for runout"), 而那条PAUSE最终还是迟到生效——
# 既把人卡死又没修好时机。新策略: 阻塞期间不硬发PAUSE、不置ERROR, 后台重试直到队列畅通。
RUNOUT_PAUSE_RETRY_INTERVAL = 10.0
# 重试总时限(秒): 超过仍未暂停成功才置ERROR(30min, 覆盖升降温+调平+换头等最长开场链路)
RUNOUT_PAUSE_RETRY_TIMEOUT = 1800.0

# lancaigang261017 任务#447b(用户261017投诉"低重自动续料没成功"): PAUSE 回执超时后的补确认窗(秒)。
# 背景(261017 10:55 实测): PAUSE 宏 = PLAY_WARN_SOUND + M400 + PAUSE_BASE + 停车 + 抬Z + 状态灯,
# is_paused 在宏中段(实测6s)就已翻 True, 但 jsonrpc 应答要等整个宏跑完(实测>10s)。
# 旧逻辑 10s 回执超时直接判 failed 中止换料 -> 打印机其实已暂停, 却停在中途;
# 后续 0x1D 又因"打印机不在打印状态(paused)"被整条丢弃, 低重自动续料彻底卡死等人工。
# 补确认: 回执超时/被拒后不立即认失败, 再等本窗轮询 is_paused; 确认为真=迟到确认, 按 ok 继续编排。
PAUSE_ACK_RECHECK_SECONDS = 10.0
# 失败后"暂停迟到生效"的自救窗(秒): 编排已中止但打印机事后确实进入暂停(且暂停是hub本机发起的),
# 在该窗口内允许下一次 0x1D 直接恢复换料编排(跳过步骤1重复PAUSE), 避免永久卡在PAUSED等人工。
PAUSE_ABORT_RECOVER_WINDOW = 600.0

# lancaigang261017 任务#447c: "PAUSE回执超时中止"标记的落盘文件名(存于日志目录)。
# 背景(261017 11:05 实测): 该标记原只在内存(_pause_abort_at), hub 进程一重启就丢;
# 而重启后 hub 会跟随 klipper 同步为 PAUSED, 后续 0x1D 又因"不在打印状态"被丢弃,
# 槽已 0g 却无人换料。落盘后: 重启仍能在自救窗内接续编排, 不需人工干预。
PAUSE_ABORT_STATE_FILENAME = 'pause_abort_state.json'

# lancaigang261017 运行态持久化(用户261017定策: hub 部署/重启后要能恢复到重启前的状态机与数据值)。
# 背景: 部署会重启进程, 内存里的状态机/会话标志全部归零, 只剩 start() 按 Klipper 现状粗对齐,
# 导致: 失败锁解除后重复自动续料、低重借道标志丢失使后续 RUNOUT 走捷径、FEEDING 计时丢失
# 使超时兜底重新计时、在途PAUSE守卫丢失使重启后又发一条PAUSE、FEEDING态退回PAUSED后可能
# 重走清管编排造成双退料。故把"同一打印任务内"的运行态定期落盘, 重启后校验任务身份一致才恢复。
RUNTIME_STATE_FILENAME = 'hub_runtime_state.json'
# 快照结构版本(字段语义变更时+1, 旧版本快照启动即丢弃, 不做迁移)
RUNTIME_STATE_VERSION = 1

# lancaigang261017 任务#447b: "首次进料"判定速度下限(mm/s)。
# 原判据 live_extruder_velocity > 0.0 会被噪声级微正速度(实测日志打印"速度: 0.00")误置位,
# 导致开场缓发门禁在升温/调平期就被提前解除(261017 11:03 重启即出现该现象)。
# 真实挤出速度 >= 0.5mm/s, 取 0.1 作下限: 既滤掉噪声又不误伤首段真实进料。
FED_SEEN_MIN_VELOCITY = 0.1

# lancaigang 260918 20:52:35 任务#296 P0-1改动1: FEEDING态最短驻留防护窗(治#295环4)
# 背景: runout清管UNLOAD退残料时线尾回带假触发打印头传感器"有料"(stale帧出现在0x01后
# 0-5s), 旧版FEEDING轮询直接读当前帧filament_detected无最短驻留要求, 首帧stale"有料"
# 4-5秒即转RESUMING→空管打印。数值依据: 新料推送到打印头传感器物理最短数十秒(管长
# 数米), 20s窗稳定覆盖stale窗口(0-5s)且不误伤真实恢复(FEEDING前置条件是传感器无料,
# 物理上不可能20s内真实恢复, 低重/非低重两链路统一受益零影响)
FEEDING_MIN_HOLD_SECONDS = 20.0
# 任务#296改动1: 驻留窗内stale有料的节流日志间隔(防每轮轮询刷屏)
FEEDING_HOLD_LOG_INTERVAL = 10.0
# lancaigang261014214500 qoder-agent-f01-leader 任务#437(B): RUNOUT边沿去抖(治打印头sensor1
# 受卡料/碎屑污染时的单帧假"无料"误暂停)。旧逻辑打印中活跃传感器单帧无料即 PRINTING->RUNOUT->
# PAUSED; 改为"持续无料>=本阈值才确认RUNOUT"。真断料为持续无料(不会自愈), 取3s可滤掉<=3s 的
# 抖动/瞬时污染(FEEDING轮询周期5s, 真断料最迟下一轮查询确认, 不丢保护只延后)。仅改暂停入口,
# 不改变已暂停后的恢复链路(恢复端由A'交叉核对把关)。
RUNOUT_DEBOUNCE_SECONDS = 3.0
# lancaigang261014224500 qoder-agent-f01-leader 任务#438(冗余校验): 退料(挤出机退出线尾+料柜收回)是上位机
# 主动下发的动作, 打印头"知道"退了料——退料完成后 filament_switch_sensor 物理上必然持续无料>=本阈值。
# 以该事实作传感器可信度的确定性锚点: 退料后若活跃传感器仍报有料/抖动不达窗, 即"传感器与机械事实矛盾"
# (线材断在打印头开关内卡住), 拒绝自动恢复打印(退料收尾处_arm_retract_empty_check武装,
# _update_retract_empty_check每帧累计, _recovery_evidence_ok作恢复端总门)。
RETRACT_EMPTY_VERIFY_SECONDS = 10.0
# lancaigang261014233000 qoder-agent-f01-leader 任务#439(超时机制): 退料后"确认稳定无料"的窗口时限。
# 退料后须在此期限内取得"稳定无料>=RETRACT_EMPTY_VERIFY_SECONDS"的确认; 超时即判退料失败——
# 因线材可能无法拉出(卡死在打印头内/机械卡滞), 不能无限等待, 必须超时后停止自动恢复转人工处理。
RETRACT_EMPTY_VERIFY_TIMEOUT_SECONDS = 60.0
# lancaigang261014233000 任务#439(抖动判别): 确认窗内 有料<->无料 真实翻转次数上限。超过即判
# "传感器持续抖动"(非真实无料), 与"稳定无料"区分对待, 不认可为退料到位证据。
RETRACT_EMPTY_MAX_TOGGLES = 3
STUCK_RECOVER_SECONDS = 30.0  # lancaigang260922 任务#388: 停滞自愈观察窗(秒)
# lancaigang261012 任务#411: 恢复打印闸门常量——治261012 20:25:00"F01旧盘退料中打印头自己恢复打印"。
# 原逻辑只凭【传感器有料】单一信号即RESUME; 而0x01回执恒为IDLE(料柜从无FEEDING/COMPLETE回执),
# 线尾回带/毛刺假有料即可恢复→空管打印。闸门做冗余校验: G1无编排在跑 / G2挤出机非退料 /
# G3传感器有料持续; 拦截达上限判本轮续料失败(lancaigang261025103309 任务#563: 原兜底放行会在料/温未确认时空打)。
RESUME_SENSOR_HOLD_SECONDS = 5.0        # G3: 传感器需连续有料的秒数(滤1~2s毛刺)
RESUME_RETRACT_VEL_THRESHOLD = -0.5     # G2: 视为"正在退料"的速度阈值(mm/s)
RESUME_MOTION_FRESH_SECONDS = 3.0       # G2: 运动数据新鲜度窗口(秒)
RESUME_GATE_MAX_BLOCK_SECONDS = 300.0   # 拦截上限, 拦满判本轮续料失败(lancaigang261025103309 任务#563: 原超时兜底放行; 与FEEDING超时同量级)
# lancaigang261024115600 任务#554-copy108-hotfix3: 108 现场 11:55:44 续料到料后仍报
# "挤出机仍在退料"，实测 motion_report.live_extruder_velocity=-8.88e-16(浮点负零噪声)，
# 而 _copy_heads_ready 用 "< 0" 判定退料 -> 恢复门永远不放行、打印停在 PAUSED。
# 机械静止时 Klipper 会给出极小负数，真实退料实测 <= -0.34mm/s，故取 -0.05mm/s 作物理噪声边界。
COPY_RETRACT_VEL_THRESHOLD = -0.05  # mm/s, 低于此值才视为"正在退料"
# lancaigang261024104414 任务#554: FEEDING 内 0x1D 重发的在途查询限频窗口(秒)。
# 对端 WAIT_CUT 每 30s 重发一次，这里取 20s，保证同一次重发只补一次 0x03 查询。
FEEDING_DUP_PROBE_INTERVAL_SECONDS = 20.0

RESUME_GATE_LOG_INTERVAL = 10.0         # 拦截节流日志间隔(防刷屏)
# lancaigang261025103309 任务#563: 暂停来源窗。断料暂停时传感器先于 print_stats 变化(Klipper 断料暂停延时/
# Moonraker 分帧), 人工暂停时传感器有料。只认"暂停边沿前 PRE 秒 ~ 后 POST 秒内参与打印的头由有料转无料"为断料暂停;
# POST 只覆盖分帧, 人工暂停后才拔料/退料不在窗内, 不得被当断料自动续料+恢复(恢复即空打)。
PAUSE_EDGE_RUNOUT_PRE_SECONDS = 5.0
PAUSE_EDGE_RUNOUT_POST_SECONDS = 2.0
PAUSE_SOURCE_GATE_LOG_INTERVAL = 60.0   # 来源门拦截节流日志间隔
# lancaigang261024114500 任务#554-copy108-hotfix2: Moonraker 把 print_stats 与 pause_resume
# 分帧推送(108实测: 11:40:27 状态帧变paused, 11:40:28 才 is_paused=True)。状态帧会立即触发
# 复制续料, 此刻 pause_resume_is_paused 仍为 False, 旧判定"未确认同单真实暂停"直接终止并
# 置失败锁 → 现场表现为"复制打印头1无料暂停但不自动续料"。改为先等暂停确认窗(只等在途帧),
# 超时仅保持 PAUSED 等人工, 不写失败锁。
COPY_PAUSE_CONFIRM_TIMEOUT_SECONDS = 10.0
# lancaigang261024143000 任务#554-copy108-hotfix4: 108 现场 13:56:52 自动续料"双头共同预挤
# 100mm"执行了整整10.3s(恰好100mm@600mm/min), 但打印头1全程没有吐料, 打印仍被RESUME。
# 三条可证伪缺陷: (1) 预挤脚本未复核 extruder1->extruder 挤出队列同步, 等待期被 T0/T1 宏
# (先把 extruder1 SYNC 到 extruder1 且不恢复)打断时, G1 E 只作用主头 -> 看起来"吐料的是头0";
# (2) _send_gcode 只等 jsonrpc 回执, Klipper 运行期拒绝(挤出行程超限/越界/低温)不回传 error,
# 命令被拒也被当成预挤成功; (3) COPY 与 MIRROR 共用同一套事务, 日志/依据却只写"复制"。
# 定策: 总行程不变(避免暂停期多挤造成料点鼓包), 拆成多段逐段复核, 任一异常立即停在PAUSED。
COPY_PURGE_TOTAL_MM = 100.0      # 双头预挤总行程(与旧版一致, 不增加耗材与鼓包风险)
COPY_PURGE_SEGMENTS = 2          # 分段数: 段间复核模式/同步/运行期报错
COPY_PURGE_FEEDRATE = 600.0      # mm/min, 与旧版 F600 一致
# lancaigang261031165500 任务#580: 纯runout停稳后先吐净打印头残料，再允许请求新盘。
RUNOUT_RESIDUE_PURGE_TOTAL_MM = 50.0
RUNOUT_RESIDUE_PURGE_SEGMENT_MM = 25.0  # 两段避开Klipper单次挤出行程上限
RUNOUT_RESIDUE_PURGE_MIN_ACTUAL_MM = 45.0
RUNOUT_RESIDUE_PURGE_MIN_TEMP = 175.0
RUNOUT_RESIDUE_PURGE_POLLS = 40         # 每次间隔0.5秒，最长20秒
# lancaigang261104093930 任务#588: 默认FRO暂停等人工取料，只轮询真实打印头传感器，不驱动挤出。
FRO_MANUAL_POLL_INTERVAL = 0.5
# lancaigang261025100000 任务#554-copy108-hotfix10: 退料时由"本次低重的那一个打印头"先反转。
# hotfix9(261024) 已证明送料柜 0x1E 单侧抽线时打印头必须一起退, 否则就是送料柜硬拽线材;
# 但 hotfix9 只退 3mm 且两头同步(extruder1 SYNC 到 extruder), 实测(108 16:22:47)脚本
# 确实被受理, 线却仍在热端被锚住, 用户现场看到"打印头0和打印头1都不退料"。
# 根因: (1) 3mm 远小于挤出机齿轮到喷嘴的熔融段长度, 退不出锚定区;
#       (2) 低重只发生在一条料路上, 另一侧料是好的, 同步反转会白退好料。
# 定策: 单次 25mm、单事务上限 50mm(欠料由 100mm 预挤补回); 退料前 SYNC_EXTRUDER_MOTION
# 先把 extruder1 解绑到自身队列, 再 ACTIVATE_EXTRUDER 只驱动本次低重的那个头。
# 不走 T0/T1 宏: 两个宏在 virtual_sdcard 有文件(paused 也算)时直接返回
# "Cannot change mode while printing!", 暂停期根本切不了头。
COPY_RETRACT_ASSIST_MM = 25.0         # mm, 单次反转量(须越过热端熔融段)
COPY_RETRACT_ASSIST_FEEDRATE = 300.0  # mm/min, 慢速反转, 减小对线材的冲击
COPY_RETRACT_ASSIST_MAX_MM = 50.0     # mm, 单次续料事务内反转总量上限(由预挤补回)
COPY_RETRACT_ASSIST_MIN_TEMP = 170.0  # ℃, 低于此温度不反转(冷态硬拉必断)
# lancaigang261024175000 任务#554-copy108-hotfix13: 单头反转后等活跃挤出机/挤出队列归位上限。
# 25mm@F300(=5mm/s) 实测 5.36s, 取 8s 留状态帧推送余量; 超时即按归属门中止(不盲发 0x01)。
COPY_RETRACT_OWNER_BACK_TIMEOUT_SECONDS = 8.0

# lancaigang261024163000 任务#554-copy108-hotfix6: COPY/MIRROR 双头加热看护。
# 108现场(261024 14:23:32 idle timeout -> 14:38:10 外部RESUME): printer_client.cfg 的 PAUSE 只记
# printer[toolhead.extruder].target(IDEX 下恒为 T0), RESUME 只发 M109 S{temp}(不带 T 编号),
# 实测 T0 34.6->234.3 而 T1 全程 36.0/0.0 -> 打印头1 恢复打印后在冷态挤料。
# 口径: 只补"本单曾热过/对侧仍在加热"的一侧, 两侧同时降温(END_PRINT/idle_timeout)一律不动;
# 没有任何已知目标时绝不瞎猜, 宁可如实失败也不冷挤。
COPY_HEATER_GUARD_MIN_KNOWN_TARGET = 175.0   # 认定为"曾有真实加热目标"的下限(与预挤硬门同源)
COPY_HEATER_GUARD_LOW_TEMP_MARGIN = 20.0     # 当前温度比目标低这么多才算"真冷"
COPY_HEATER_GUARD_INTERVAL_SECONDS = 10.0    # 同一侧补发节流
COPY_HEATER_WAIT_MAX_SECONDS = 300.0         # 续料事务内等双头到温的上限(超时如实失败, 不硬挤)


# lancaigang261013221433 任务#435: 收窄#433"进料会话窗口"→"hub刚RESUME后短窗反弹"自愈门限。
# 背景(#433缺陷, 261013 21:58:54实测): 旧#433按"最近FEED_SESSION_GRACE_SECONDS=300s内有过进料/
# 换料/清管"来判"外部自动暂停", 但该启发式无法区分"外部自动暂停"与"人工暂停"——用户在续料后
# (距上次进料仅36s)人工暂停, 仍被#388在30s后强制RESUME, 违背#388"人工暂停永不自愈"承诺。
# 定策(用户261013拍板): 仅当"hub自己刚发出过RESUME、打印机却在短窗内又弹回paused"(即本次恢复
# 没生效, 疑似外部干扰/竞态)才自愈; 距hub上次RESUME超出本窗的暂停一律尊重(含人工暂停)。
HUB_RESUME_BOUNCE_WINDOW_SECONDS = 20.0

# lancaigang261019 任务#459: CAN断连时续料请求(0x01)发不出去, 旧逻辑直接 transition ERROR 且
# ERROR 唯一自动出口是 Klipper 回 ready/standby(261019 09:21:25 实测: CAN 断开期间请求续料失败
# 进 ERROR, 09:29:46 CAN 恢复后仍卡 ERROR 直到人工干预)。改为:
#   1) 未连接导致的 request_feed 失败只"挂起"该 0x01, 不进 ERROR;
#   2) CAN 恢复后只补发 0x01(不重走已完成的退料编排);
#   3) 已进 ERROR 且原因属 CAN 类时, CAN 连通且打印确未在跑, 观察窗后自愈回 IDLE。
CAN_PENDING_FEED_TIMEOUT_SECONDS = 900.0    # 挂起补发最长等待(秒), 超时丢弃转人工
CAN_PENDING_FEED_RETRY_INTERVAL = 5.0       # 挂起补发最小重试间隔(秒), 防轮询高频重发
ERROR_CAN_SELFHEAL_OBSERVE_SECONDS = 30.0   # CAN已恢复但仍ERROR的观察窗(秒), 到点自愈回IDLE
# lancaigang261014100043 任务#436(发现1·A方案: 仅纠正注释, 不改逻辑):
# 已知死支/自限: 本窗(20s) < STUCK_RECOVER_SECONDS(30s), 故"仅靠_recent_hub_resume()单独武装"的
# #388自愈永远无法达成30s阈值——计时器从短窗内起算, 到点前窗口已失效→命中:1553条件的else→
# :1588每帧清_stuck_recover_since。当前实际生效的自愈路径=编排过(_pause_orchestrated)+有料,
# 短窗反弹这一路为冗余保险尚未生效。离线harness已断言此死支(resume_calls==0)。
# 若将来需让短窗反弹独立生效, 须令 窗口W >= STUCK_RECOVER_SECONDS+观察间隔(>=~35s),
# 并重验"hub RESUME后短窗内人工暂停"(靠_hub_resume_at回printing时清空:1371保护)不被误伤。


class FeederCabinetApp:
    """自动续料系统应用程序类"""
    
    def __init__(self, config_path: str = None):
        """
        初始化应用程序
        
        Args:
            config_path: 配置文件路径
        """
        # 先初始化logger为None
        self.logger = None
        
        # 加载配置
        self.config = self._load_config(config_path)
        
        # 初始化日志管理器
        log_config = self.config.get('logging', {})
        self.log_manager = LogManager(
            app_name="feeder_cabinet",
            log_dir=log_config.get('log_dir', DEFAULT_LOG_DIR),
            log_level=log_config.get('level', DEFAULT_LOG_LEVEL),
            max_file_size=log_config.get('max_file_size', 10 * 1024 * 1024),  # 默认10MB
            backup_count=log_config.get('backup_count', 5),
            max_age_days=log_config.get('max_age_days', 30),
            console_output=log_config.get('console_output', True)
        )
        
        # 设置主logger
        self.logger = self.log_manager.setup_logger()
        
        # 初始化状态管理器
        self.state_manager = StateManager(self.log_manager.get_child_logger(self.logger, "state"))
        self.state_manager.set_state_change_callback(self._on_state_changed)
        
        # 组件实例
        self.can_comm = None
        self.klipper_monitor = None
        self.rfid_parser = None  # RFID解析器
        self.spoolman_client = None  # Spoolman客户端
        # lancaigang261030233801 任务#575: 展示快照与自然RFID缓存分离，仅内存保存当前槽态
        self._slot_count = 0
        self._slot_rows = {}
        self._slot_active = [0xFF, 0xFF]
        self._slot_pending_info = {}
        self._slot_status_received = set()
        self._slot_info_blocked = set()
        
        # 运行状态 (由state_manager替代)
        # self.running = False
        self.main_thread = None
        
        # 配置文件路径
        self.config_path = config_path
        
        # 断料传感器状态缓存
        self._last_filament_status = {}
        
        # 打印机状态缓存
        self._last_printer_state = None
        
        # lancaigang 260915 201650 治本修复主方案A-2(单T1打印开场误暂停): 新增进料运动门禁状态,
        # 按挤出机ID记录是否见过实际进料运动(速度>0), 首次进料前视为开场流程(等温/归位/网床/换头占位),
        # 断料只记日志不转RUNOUT不发PAUSE; 打印开始时不清除(中途恢复打印: 之前进过料的头继续受保护)
        self._extruder_fed_seen = {0: False, 1: False}
        
        # 任务#278 H2: 换料/清管编排互斥标志——低重链路(_auto_change_filament)与
        # RUNOUT清管续料编排(_runout_clear_and_feed)共用，防止双编排并发导致
        # 双UNLOAD宏/双0x1E/双0x01叠加执行
        # 任务#286 V10: 维持全局单标志结构(hub单状态机架构不支持真并发编排: UNLOAD宏
        # 作用于toolhead当前活跃侧无侧参数, 分侧标志会让左右两编排并发跑导致双UNLOAD宏
        # 叠加+状态机payload互相覆盖)。另加_refill_orchestration_owner记录当前编排占用的
        # 挤出机侧, 被拒时日志分侧可观测; 对侧0x1D不排队但F05侧WAIT_CUT 30s重发0x1D已兜底收敛
        self._refill_orchestration_busy = False
        self._refill_orchestration_owner = None  # 当前编排占用的挤出机ID(0/1), 仅日志可观测用
        # lancaigang261022103000 fixflow：失败终止在途任务；代次防止合法解锁复活旧发送。
        self._refill_orchestration_task = None
        # lancaigang261104093930 任务#588: 活跃槽FRO人工清料等待唯一任务；会话归属_refill_context，不另建状态机。
        self._fro_purge_task = None
        self._fro_purge_generation = 0
        # lancaigang261023120000 任务#540：独立保存恢复任务引用，取消时可中断温度/挤出/发送等待。
        # 不能复用退料busy，否则恢复出口G1会挡住自身；它不改变低重/runout分流。
        self._resume_task = None
        self._refill_failure_epoch = 0
        # lancaigang261024113000 任务#554-copy108：双头共享状态机，保存本次分管续料归属直到打印恢复。
        self._copy_refill_context = None
        # lancaigang261024104414 任务#554: 机械owner会在两趟间释放，事务归属必须跨两趟保存。
        self._refill_context = None
        self._failure_stop_task = None
        self._error_probe_task = None
        
        # 任务#286 V1/V2/V3/V5: FEEDING超时兜底状态(静默死等族修复)
        self._feeding_started_at = None        # FEEDING态进入时刻(time.monotonic), 离开时清除
        self._feeding_timeout_retry_count = 0  # FEEDING超时连续重试计数(传感器恢复/新打印时清零)
        self._feeding_giveup_logged = False    # 超时重试放弃后只记一次ERROR日志的防刷屏标志
        # lancaigang261015 任务#442: 自动续料失败锁——打印头无料暂停后F01(F05)匹配失败
        # (0x03回执报错: status=ERROR或error_code!=0, 尤其"无兼容候选/材料缺失")即判定本次
        # 自动续料失败并置位; 置位后两个自动续料触发入口(PAUSED轮询/PAUSED状态迁移动作)均拦截,
        # 即使之后插入料盘/传感器变化也不再自动启动续料; 仅新一轮打印或回到IDLE时解除。
        # (解锁见_on_state_changed: new_state∈[PRINTING,IDLE])
        self._auto_feed_failed = False

        # lancaigang261019 任务#459: CAN断连导致 0x01 发不出去的挂起记录
        # {'tube_id': int, 'extruder': int, 'at': monotonic, 'last_try': monotonic, 'wall': time}
        # CAN恢复后只补发0x01(退料编排已执行完, 不重走), 防止旧逻辑一步进ERROR锁死。
        self._pending_feed_after_can = None
        # 任务#459: ERROR态下"CAN已连通"的持续起点(monotonic), ERROR自愈看门狗用; None=未武装
        self._error_can_since = None
                
        # lancaigang 260918 20:52:35 任务#296 P0-1改动1: FEEDING最短驻留防护窗状态——
        # 进入FEEDING时置为 now+FEEDING_MIN_HOLD_SECONDS, 窗内传感器"有料"不转RESUMING
        # (防护UNLOAD回带stale假有料, #295环4); 离开FEEDING时清除。判定优先于超时判定
        self._feeding_min_hold_until = None
        # 任务#296改动1: 驻留窗未满时节流日志的上次记录时刻(每FEEDING_HOLD_LOG_INTERVAL秒一条)
        self._feeding_hold_log_at = 0.0
                
        # lancaigang 260918 20:52:35 任务#296 P0-3改动2: 低重会话级标志——本打印会话内是否
        # 受理过F05低重0x1D通知(受理成功置位于_handle_low_weight_notify门禁通过后, 纯通知
        # 丢弃不算"见过"); printing开始时与_extruder_fed_seen同批复位。
        # lancaigang261015175319 qoder-agent-f01-leader 用户261015定策: 撤销#296场景分流后的
        # 细粒度分流由下方 _low_weight_refill_pending(事件级)承担; 本会话级标志保留仅作会话内
        # 低重受理可观测性, 不再直接用作分流判据。
        self._low_weight_seen_this_print = False
        # lancaigang261016 qoder-agent-f01-leader 任务#446: 低重链路续料"待恢复借道"事件级标志。
        # 根因(见记忆"hub runout续料分流标志会话级污染"): 原分流判据 is_non_low_weight =
        # not _low_weight_seen_this_print 是会话级——本打印会话内只要受理过任一次活跃挤出机
        # 低重0x1D 就永久置位, 导致此后该会话内所有纯runout续料被误判为"低重会话"走完整退料
        # 序列(旧料盘三段0x1E分段退料 + 打印头UNLOAD线尾回带假有料 #295)。改用本标志后: 仅低重
        # 链路续料"在途待恢复"时才走完整序列; 已恢复后的独立runout一律跳过退料。
        # 置位: _auto_change_filament 步骤5成功发0x01后(低重链路已退料完, 待恢复闭环借道
        #       _runout_clear_and_feed置FEEDING);
        # 复位: printing开始(新会话, 与_low_weight_seen_this_print同批) 或 恢复成功转RESUMING。
        self._low_weight_refill_pending = False
        # lancaigang260922 任务#388: 停滞自愈(断料暂停死锁根治)
        self._pause_orchestrated = False
        self._stuck_recover_since = None
        # lancaigang261017 任务#447: PAUSE 在途守卫时间戳(monotonic); None=当前无在途PAUSE。
        # 背景(261017 09:40-09:57 事故): Klipper 阻塞在 START_PRINT 等温流程时, hub 连发 4 条 PAUSE
        # 全部堆在队列里, 等温结束后集中放行, 打印在 T1 换头后被迟到 PAUSE 打断。
        # 置位: pause_print 未获受理 或 受理后 wait_paused 超时未确认生效;
        # 清除: 检测到 pause_resume.is_paused=True 或 在途超过 PAUSE_PENDING_MAX_WAIT 秒。
        # 在途期间 _handle_low_weight_notify 丢弃新的低重通知, 避免多份换料编排叠加。
        self._pause_pending_since = None
        # lancaigang261017 任务#447: 首次进料前缓发 PAUSE 的节流日志时刻(monotonic)
        self._low_weight_defer_log_at = 0.0
        # lancaigang261017 任务#447b: 上次低重换料因"PAUSE 回执超时"中止的时刻(monotonic); None=无。
        # 用途: 若事后打印机确实进入暂停(迟到生效), 在 PAUSE_ABORT_RECOVER_WINDOW 内允许 0x1D
        # 直接恢复换料编排(跳过步骤1重复PAUSE), 防止中止后永久卡 PAUSED 等人工(261017 10:55 事故)。
        self._pause_abort_at = None
        # lancaigang261017 任务#447c: 同一标记的墙钟时刻/所属挤出机, 用于跨进程重启自救(落盘复用)
        self._pause_abort_wall = None
        self._pause_abort_extruder = None
        # 启动时若发现上次进程遗留的中止标记(未过期), 载入并恢复自救能力
        self._load_pause_abort_marker()
        # lancaigang261017 运行态持久化: 快照节流签名/启动载入(只载入, 待连上Klipper校验同一单后再应用)
        self._runtime_saved_at = 0.0
        self._runtime_save_sig = None
        self._runtime_snapshot = None
        self._load_runtime_state()
        # lancaigang261013221433 任务#435: hub最近一次成功发出RESUME的时刻(monotonic); None=本会话未发过
        # (收窄#433: 由"进料会话300s窗"改为"刚RESUME后短窗", 仅自愈本次恢复没生效的反弹暂停)
        self._hub_resume_at = None
        self._last_status_heartbeat = 0.0
        # lancaigang261024163000 任务#554-copy108-hotfix6: 本单各打印头最近已知加热目标
        # (按job切单重置) + 补发节流时刻; 外部把单侧目标清零后据此补 M104 T{i}。
        self._head_target_snapshot = {}
        self._head_guard_job = None
        self._head_guard_at = {}
                
        # 任务#286 V6: F05 NACK(0x1E被拒等)连续计数, 每次新编排开始时清零
        self._f05_nack_count = 0
        # 任务#549(用户261023现场 槽1/槽6): 最近一次 F05 NACK 的原因码, 仅用于"按段判定是否继续盲发0x1E"。
        # retract_filament 的返回值只代表命令已发出, F05 的拒绝走异步 _handle_f05_nack, 必须靠它才能分清。
        self._last_f05_nack_reason = None
        # lancaigang261019 任务#462: 缓冲器"假有料"死锁自愈状态(根因见 BUFFER_CLEAR_MAX_ROUNDS 注释)。
        # 全局单实例: 换料/清管编排本就串行, 不做分侧并发, 避免双 0x1E/双 0x01 叠加。
        self._buffer_clear_task = None      # 当前"清缓冲+重发"任务; None/已结束=空闲
        self._buffer_clear_round = 0        # 本次已执行轮次(仅日志可观测)
        # 任务#463 B: 0x01 回执后的统一裁决(替代 R2 的"只探测 0x02"与 #462 的"收到 0x00 就退料")。
        # 任何非失败回执都挂一次 10s 后的 0x03 探测, 由探测到的真实状态决定动作,
        # 完整决策链见 feed_ack_contract.py 模块 docstring。全局单实例。
        self._feed_verdict_buffer = None          # 正在裁决的缓冲区; None=无在途裁决
        self._feed_verdict_task = None            # 裁决任务句柄
        self._feed_verdict_saw_accepted = False   # 本次事务是否收到过 0x02(区分"曾受理"与"从未受理")
        self._feed_verdict_probe_seq = None       # 已发出但未回执的探测 seq; None=无在途探测

        # lancaigang261012 任务#411: 恢复闸门状态
        self._sensor_fil_since = {}     # {sensor_obj_name: 连续有料起始时刻} (G3数据源)
        self._gate_block_since = None   # 闸门首次拦截时刻(None=当前未在拦截)
        self._gate_log_at = 0.0         # 拦截节流日志基准
        # lancaigang261025103309 任务#563: 暂停来源门(断料暂停才自动续料, 人工暂停等人工)
        self._pause_edge_at = None      # 最近一次 Klipper printing->paused 边沿时刻; 其它边沿清 None
        self._sensor_absent_at = {}     # {sensor_obj_name: 最近一次 有料->无料 翻转时刻}
        self._source_gate_log_at = 0.0  # 来源门拦截节流日志基准
        
        # RFID数据缓存
        self._rfid_data_cache = {}  # 按挤出机ID存储的耗材信息
    
    
    
    def _load_config(self, config_path: str = None) -> Dict[str, Any]:
        """
        加载配置文件
        
        Args:
            config_path: 配置文件路径
            
        Returns:
            Dict: 配置字典
        """
        # 默认配置
        config = {
            'can': {
                'interface': DEFAULT_CAN_INTERFACE,
                'bitrate': DEFAULT_CAN_BITRATE
            },
            'klipper': {
                'moonraker_url': DEFAULT_MOONRAKER_URL,
                'update_interval': DEFAULT_UPDATE_INTERVAL
            },
            'logging': {
                'level': DEFAULT_LOG_LEVEL,
                'log_dir': DEFAULT_LOG_DIR
            },
            'filament_runout': {
                'enabled': True,
                'sensors': [
                    {'name': 'Filament_Sensor0', 'extruder': 0},
                    {'name': 'Filament_Sensor1', 'extruder': 1}
                ]
            },
            'extruders': {
                'count': 2,  # 默认支持双挤出机
                'active': 0,  # 默认活动挤出机
                # 喷头到送料柜料管的映射
                # 格式: extruder_index: tube_index
                # 示例 (左喷头 -> 右料管, 右喷头 -> 左料管):
                # mapping:
                #   0: 1
                #   1: 0
                'mapping': {
                    0: 0,
                    1: 1
                }
            }
        }
        
        # 如果指定了配置文件，尝试加载
        if config_path:
            try:
                with open(config_path, 'r') as f:
                    user_config = yaml.safe_load(f)
                    if user_config:
                        # 递归更新配置
                        self._update_config(config, user_config)
                        if self.logger:
                            self.logger.info(f"从 {config_path} 加载配置")
            except Exception as e:
                # lancaigang261017 任务#456: 解析失败必须留下现场, 且要把「已降级」喊出来。
                # 原实现只打一条 error 就静默回退默认配置, 而后续任意一次 _save_config
                # (例如 F01 重连补发 0x0F 映射) 会把默认值写回磁盘, 原内容被永久覆盖,
                # 现场无从比对。此处只复制不移动, 不改变控制流与返回值。
                if self.logger:
                    self.logger.error(
                        f"加载配置文件时发生错误: {str(e)}; 已回退默认配置"
                        f"(映射按直连 0:0/1:1 处理), 请检查 {config_path}"
                    )
                try:
                    if os.path.exists(config_path) and os.path.getsize(config_path) > 0:
                        corrupt_path = f"{config_path}.corrupt_{time.strftime('%y%m%d%H%M%S')}"
                        shutil.copy2(config_path, corrupt_path)
                        if self.logger:
                            self.logger.error(f"已将疑似损坏的配置文件另存为 {corrupt_path}")
                except Exception as copy_err:
                    if self.logger:
                        self.logger.error(f"备份损坏配置文件失败: {copy_err}")
        else:
            if self.logger:
                self.logger.info("使用默认配置")
        
        # 记录关键配置项
        if self.logger:
            self.logger.info(f"CAN接口: {config['can']['interface']}, 波特率: {config['can']['bitrate']}")
            self.logger.info(f"Moonraker URL: {config['klipper']['moonraker_url']}")
            self.logger.info(f"挤出机数量: {config['extruders']['count']}")
            self.logger.info(f"断料检测: {'启用' if config['filament_runout']['enabled'] else '禁用'}")
            if config['filament_runout']['enabled']:
                for sensor in config['filament_runout']['sensors']:
                    self.logger.info(f"断料传感器: {sensor['name']} 用于挤出机 {sensor['extruder']}")
        
        return config
    
    def _update_config(self, config: Dict, updates: Dict):
        """
        递归更新配置字典
        
        Args:
            config: 原配置字典
            updates: 更新配置字典
        """
        for key, value in updates.items():
            if isinstance(value, dict) and key in config and isinstance(config[key], dict):
                self._update_config(config[key], value)
            else:
                config[key] = value
    
    def _save_config(self, config_path: str = None) -> bool:
        """
        保存配置文件
        
        Args:
            config_path: 配置文件路径，如果为None则使用加载时的路径
            
        Returns:
            bool: 保存是否成功
        """
        if not config_path:
            config_path = self.config_path
            
        if not config_path:
            self.logger.error("未指定配置文件路径，无法保存配置")
            return False
            
        # lancaigang261017 任务#456: 原子写。原实现直接 open(config_path,'w') 截断后 dump,
        # 在「已截断、dump 未写完」的窗口内断电或被 kill(部署时 systemctl restart 同样命中),
        # 会留下 0 字节或半截 yaml; 下次启动 _load_config 解析失败 -> 静默回退默认配置 ->
        # 下一次保存把默认值固化写回 -> 原配置永久丢失且无告警。
        # 改为 写同目录临时文件 -> fsync 落盘 -> os.replace 原子替换: 任意时刻磁盘上
        # 只存在「完整旧版本」或「完整新版本」, 不存在中间态。同目录保证 replace 是原子重命名。
        tmp_path = config_path + ".tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(self.config, f, default_flow_style=False, allow_unicode=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)
            self.logger.info(f"配置已保存到 {config_path}")
            return True
        except Exception as e:
            self.logger.error(f"保存配置文件时发生错误: {str(e)}")
            # 清理残留临时文件, 避免下次被误读; config_path 本身未被动过(仍为上一个完整版本)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                    self.logger.warning(f"已清理残留临时文件: {tmp_path}")
            except OSError as clean_err:
                self.logger.warning(f"清理临时文件失败: {clean_err}")
            return False
    
    async def _handle_filament_status_query(self):
        """处理送料柜的挤出机余料状态查询请求。"""
        self.logger.info("收到挤出机余料状态查询请求")

        if not self.klipper_monitor:
            self.logger.error("KlipperMonitor 未初始化，无法查询状态")
            if self.can_comm:
                await self.can_comm.send_filament_status_response(is_valid=False, status_bitmap=0)
            return

        # 检查WebSocket连接状态
        if not self.klipper_monitor.ws_connected:
            self.logger.warning("Klipper WebSocket未连接，耗材状态无效")
            if self.can_comm:
                await self.can_comm.send_filament_status_response(is_valid=False, status_bitmap=0)
            return

        try:
            sensor_states = self.klipper_monitor.get_filament_status()
            extruder_mapping = self.config.get('extruders', {}).get('mapping', {})
            
            status_bitmap = 0
            
            for sensor_info in self.config.get('filament_runout', {}).get('sensors', []):
                sensor_name = sensor_info.get('name')
                extruder_index = sensor_info.get('extruder')

                if sensor_name is None or extruder_index is None:
                    continue
                
                if sensor_states.get(sensor_name, False):
                    tube_index = extruder_mapping.get(extruder_index)
                    if tube_index is not None:
                        status_bitmap |= (1 << tube_index)

            self.logger.info(f"查询到耗材状态，准备发送响应。Bitmap: {bin(status_bitmap)}")
            if self.can_comm:
                await self.can_comm.send_filament_status_response(is_valid=True, status_bitmap=status_bitmap)

        except Exception as e:
            self.logger.error(f"处理耗材状态查询时出错: {e}", exc_info=True)
            if self.can_comm:
                await self.can_comm.send_filament_status_response(is_valid=False, status_bitmap=0)
    
    async def _send_filament_status_notification(self):
        """主动发送余料状态通知给送料柜（断料传感器状态变化时调用）"""
        self.logger.info("主动发送余料状态通知给送料柜")
        
        if not self.klipper_monitor:
            self.logger.error("KlipperMonitor 未初始化，无法发送状态通知")
            return
            
        if not self.klipper_monitor.ws_connected:
            self.logger.warning("Klipper WebSocket未连接，无法发送状态通知")
            return
            
        if not self.can_comm or not self.can_comm.connected:
            self.logger.warning("CAN通信未连接，无法发送状态通知")
            return
            
        try:
            sensor_states = self.klipper_monitor.get_filament_status()
            extruder_mapping = self.config.get('extruders', {}).get('mapping', {})
            
            status_bitmap = 0
            
            for sensor_info in self.config.get('filament_runout', {}).get('sensors', []):
                sensor_name = sensor_info.get('name')
                extruder_index = sensor_info.get('extruder')
                
                if sensor_name is None or extruder_index is None:
                    continue
                    
                if sensor_states.get(sensor_name, False):
                    tube_index = extruder_mapping.get(extruder_index)
                    if tube_index is not None:
                        status_bitmap |= (1 << tube_index)
                        
            self.logger.info(f"主动通知余料状态。Bitmap: {bin(status_bitmap)}")
            await self.can_comm.send_filament_status_response(is_valid=True, status_bitmap=status_bitmap)
            
        except Exception as e:
            self.logger.error(f"发送余料状态通知时出错: {e}", exc_info=True)
    
    async def _check_can_interface_status(self):
        """检查CAN接口的系统状态"""
        import subprocess
        try:
            # 使用ip命令检查接口状态
            result = subprocess.run(['ip', 'link', 'show', 'can1'], 
                                  capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                self.logger.info(f"CAN接口状态: {result.stdout.strip()}")
                return True
            else:
                self.logger.error(f"CAN接口不存在: {result.stderr.strip()}")
                return False
        except Exception as e:
            self.logger.error(f"检查CAN接口状态失败: {e}")
            return False
    
    async def _send_printer_status_notification(self, status: str, quiet: bool = False):
        """主动发送打印机状态通知给送料柜

        quiet=True 时只打 debug 日志(供周期心跳使用, 避免每15s刷一条 info)。
        """
        if not self.can_comm or not self.can_comm.connected:
            self.logger.warning("CAN通信未连接，无法发送打印状态通知")
            return
            
        try:
            # 映射Klipper状态到CAN命令
            status_mapping = {
                'printing': self.can_comm.CMD_PRINTING,
                'paused': self.can_comm.CMD_PRINT_PAUSE,
                'complete': self.can_comm.CMD_PRINT_COMPLETE,
                'cancelled': self.can_comm.CMD_PRINT_CANCEL,
                'standby': self.can_comm.CMD_PRINTER_IDLE,
                'ready': self.can_comm.CMD_PRINTER_IDLE,
                'error': self.can_comm.CMD_PRINTER_ERROR
            }
            
            cmd = status_mapping.get(status)
            if cmd is not None:
                # 如果是error命令，检查CAN接口状态
                if status == 'error':
                    self.logger.warning("即将发送error状态，先检查CAN接口")
                    await self._check_can_interface_status()
                
                if quiet:
                    self.logger.debug(f"周期心跳: 补发打印状态 {status} -> CMD 0x{cmd:02X}")
                else:
                    self.logger.info(f"主动发送打印状态通知给送料柜: {status} -> CMD 0x{cmd:02X}")
                await self.can_comm.send_message(cmd)
                
                # 发送error后再次检查
                if status == 'error':
                    await asyncio.sleep(0.5)
                    self.logger.warning("已发送error状态，再次检查CAN接口")
                    await self._check_can_interface_status()
            else:
                self.logger.warning(f"未知的打印状态: {status}")
                
        except Exception as e:
            self.logger.error(f"发送打印状态通知时出错: {e}", exc_info=True)

    async def _resend_printer_status_to_f01(self, reason: str):
        """F01 上线/重启后补发一次当前打印状态(lancaigang261017135122 任务#452)

        缺口(真机日志直证, 非推测): F01 重启后打印态一直停在默认 IDLE(st=8),
        Weight_CheckLowWeight 的第一道门 `lwchk skip=print cn=1 st=8` 直接 return,
        低重 0x1D 永不发出 -> 低重无法触发自动续料。
        hub 原有的"CAN重连后重发状态"分支在 F01 单侧重启时不会触发(hub 不感知断线),
        故以映射查询应答/映射设置命令(F01 上线必发)作为信号补发。

        lancaigang261019222000 任务#481: 同一信号必须【先补发余料位图 0x0E】。
        缺口(真机直证 261019): hub 21:36:04/21:36:21 两次主动通知 Bitmap: 0b11,
        F01 于 21:36:42(烧录后)重启, 这两帧全部丢失; 本函数只补打印态 0x04,
        于是 F01 g_printer_left/right_has_filament 恒为 0 -> #478/#480 的
        "打印头有料"硬门把补料开关每 100ms 强制关 0(21:38:33 sw-chg autofeed L/R
        1->0 caller=FeedModule_Process, 用户开上 253ms 即被关), 且开机 flash 恢复的
        af 也保不住; 表现为用户报障"T0/T1 都有线材, 两个开关却开不起来"。
        语义: F01 上线 = 状态全量对齐 —— 先补事实(位图), 再补打印态(0x04),
        打印态跃迁时 #473 便能看到正确的 has_filament 门。
        """
        try:
            await self._send_filament_status_notification()
            state = getattr(self, '_last_printer_state', None)
            if not state and self.klipper_monitor:
                printer_status = self.klipper_monitor.get_printer_status()
                state = printer_status.get('printer_state') if printer_status else None
            if not state:
                self.logger.warning(f"F01上线({reason}): 无可用打印态, 跳过状态补发")
                return
            self.logger.info(f"F01上线({reason}): 补发当前打印状态 {state}, 防F01停在IDLE导致低重通知不发")
            await self._send_printer_status_notification(state)
        except Exception as e:
            self.logger.warning(f"F01上线补发打印状态异常: {e}", exc_info=True)

    def _resolve_klipper_real_state(self) -> Optional[str]:
        """lancaigang261019 任务#460: 取 Klipper 侧真实打印态。

        优先实时订阅缓存(klipper_monitor.get_printer_status), 读不到时退回
        _last_printer_state。只返回 _send_printer_status_notification 有 CAN 映射的
        状态, 避免把 'shutdown'/'unknown' 之类的值直接填进去。
        """
        valid = ('printing', 'paused', 'complete', 'cancelled',
                 'standby', 'ready', 'error')
        try:
            if self.klipper_monitor and self.klipper_monitor.ws_connected:
                printer_status = self.klipper_monitor.get_printer_status()
                if printer_status:
                    st = printer_status.get('printer_state')
                    if st in valid:
                        return st
                    if st:
                        self.logger.debug(f"任务#460: Klipper状态'{st}'无CAN映射, 不用于补发")
        except Exception as e:
            self.logger.warning(f"任务#460: 读取Klipper真实状态失败: {e}")
        st = getattr(self, '_last_printer_state', None)
        return st if st in valid else None

    async def _f01_status_heartbeat_task(self):
        """lancaigang261019 任务#460: 周期性向 F01 重发 Klipper 真实打印状态。

        缺口(261019 真机日志直证): F01 的 g_printer_status 只由状态帧写入,
        canserial_is_printing() 没有超时恢复; hub 一旦停在 ERROR/FEEDING/RESUMING
        等「不再发状态帧」的路径, F01 就永久保留最后一帧。
        本次事故: hub 10:03:00 因 RESUME 回执超时进 ERROR, F01 lwst st=9 持续
        7分26秒, Weight_CheckLowWeight 首门 `lwchk skip=print st=9` 直接 return,
        低重 0x1D 全静默, 槽4 去皮后 5 分钟无任何续料动作。
        本任务只补发真实状态, 不改 hub 状态机、不做任何业务动作, 与既有
        状态变化发送互不冲突(幂等帧)。
        """
        interval = 15
        self.logger.info(f"启动F01打印状态心跳任务(每{interval}s补发, 防F01状态漂移)")
        while self.state_manager.state != SystemStateEnum.DISCONNECTED:
            try:
                await asyncio.sleep(interval)
                if not (self.can_comm and self.can_comm.connected):
                    continue
                state = self._resolve_klipper_real_state()
                if not state:
                    continue
                prev = getattr(self, '_hb_last_state', None)
                changed = (state != prev)
                await self._send_printer_status_notification(state, quiet=not changed)
                if changed:
                    self.logger.info(f"任务#460: 心跳状态变化 {prev} -> {state} "
                                     f"(hub={self.state_manager.state.name})")
                    self._hb_last_state = state
            except asyncio.CancelledError:
                self.logger.info("F01打印状态心跳任务被取消")
                break
            except Exception as e:
                self.logger.error(f"F01打印状态心跳任务异常: {e}", exc_info=True)
        self.logger.info("F01打印状态心跳任务结束")
    
    async def _get_extruder_temperature(self, extruder: int) -> Optional[float]:
        """
        获取挤出机温度
        
        Args:
            extruder: 挤出机编号
            
        Returns:
            float: 挤出机温度，如果获取失败返回None
        """
        try:
            if not self.klipper_monitor or not self.klipper_monitor.ws_connected:
                self.logger.warning("Klipper未连接，无法获取温度")
                return None
                
            # 获取打印机状态
            printer_status = self.klipper_monitor.get_printer_status()
            
            # 根据挤出机编号获取温度
            if extruder == 0:
                extruder_info = printer_status.get('extruder', {})
            elif extruder == 1:
                extruder_info = printer_status.get('extruder1', {})
            else:
                self.logger.warning(f"不支持的挤出机编号: {extruder}")
                return None
                
            temperature = extruder_info.get('temperature')
            if temperature is not None:
                self.logger.debug(f"挤出机 {extruder} 温度: {temperature:.1f}°C")
                return float(temperature)
            else:
                self.logger.warning(f"无法获取挤出机 {extruder} 温度信息")
                return None
                
        except Exception as e:
            self.logger.error(f"获取挤出机 {extruder} 温度时发生错误: {e}", exc_info=True)
            return None
    
    async def _extrude_filament(self, extruder: int, amount: float) -> bool:
        """
        挤出料丝
        
        Args:
            extruder: 挤出机编号
            amount: 挤出量（mm）
            
        Returns:
            bool: 挤出是否成功
        """
        try:
            if not self.klipper_monitor or not self.klipper_monitor.ws_connected:
                self.logger.error("Klipper未连接，无法挤出料丝")
                return False
                
            # lancaigang261025103309 任务#563: G1 E 只驱动当前活跃挤出机; 预挤目标不是活跃头时挤出的是另一侧,
            # 目标头料是否到喷嘴无从确认; 暂停中也不切头(切头宏会移动/改温), 拒绝预挤走失败分支。
            _active = getattr(self.klipper_monitor, 'active_extruder', None)
            if extruder is not None and _active != extruder:
                self.logger.error(f"任务#563: 预挤目标挤出机{extruder}不是当前活跃挤出机{_active}, 暂停中不切头, 拒绝预挤")
                return False
            # lancaigang261025103309 任务#563: 暂停时 gcode 若处于 M82 绝对挤出, "G1 E100" 是绝对坐标(可能倒抽/零挤出/超长挤出);
            # 显式 M83 并用 SAVE/RESTORE_GCODE_STATE 包住还原模式/速度/E 基准(与 _copy_purge_script 同范式)。
            gcode = ("SAVE_GCODE_STATE NAME=HUB_REFILL_PURGE\nM83\n"
                     f"G1 E{amount} F600\nRESTORE_GCODE_STATE NAME=HUB_REFILL_PURGE")  # 以10mm/s的速度挤出

                
            self.logger.info(f"发送挤出命令: {gcode}")
            
            # 发送G代码
            if await self.klipper_monitor.execute_gcode(gcode):
                # 等待挤出完成，根据挤出量和速度估算时间
                wait_time = amount / 10.0 + 2.0  # 10mm/s + 2秒缓冲
                self.logger.info(f"挤出命令已发送，等待 {wait_time:.1f}秒 让挤出完成...")
                await asyncio.sleep(wait_time)
                
                # 发送相对挤出模式重置命令
                await self.klipper_monitor.execute_gcode("G92 E0")  # 重置挤出机位置
                
                return True
            else:
                self.logger.error("发送挤出G代码失败")
                return False
                
        except Exception as e:
            self.logger.error(f"挤出料丝时发生错误: {e}", exc_info=True)
            return False

    async def _purge_runout_residue(self, extruder: int) -> bool:
        """纯runout暂停后实际吐料50mm；命令受理不等于残料已吐完。"""
        km = self.klipper_monitor
        if not km or not getattr(km, 'ws_connected', False):
            self.logger.error("runout残料吐料拒绝: Klipper未连接")
            return False
        if getattr(km, 'active_extruder', None) != extruder:
            self.logger.error("runout残料吐料拒绝: 目标头%s不是当前活跃头%s",
                              extruder, getattr(km, 'active_extruder', None))
            return False
        temperature = await self._get_extruder_temperature(extruder)
        if temperature is None or temperature < RUNOUT_RESIDUE_PURGE_MIN_TEMP:
            self.logger.error("runout残料吐料拒绝: 挤出机%s温度%s低于%s°C",
                              extruder, temperature, RUNOUT_RESIDUE_PURGE_MIN_TEMP)
            return False
        start_e = await km.get_gcode_e_position()
        if start_e is None:
            self.logger.error("runout残料吐料拒绝: 无法读取E坐标基准")
            return False

        state_name = 'HUB_RUNOUT_RESIDUE_PURGE'
        segment_count = int(RUNOUT_RESIDUE_PURGE_TOTAL_MM / RUNOUT_RESIDUE_PURGE_SEGMENT_MM)
        moves = '\n'.join(
            'G1 E%.1f F600\nM400' % RUNOUT_RESIDUE_PURGE_SEGMENT_MM
            for _ in range(segment_count))
        script = ('SAVE_GCODE_STATE NAME=%s\nM83\n%s' % (state_name, moves))
        actual = None
        accepted = False
        restored = False
        self.logger.info("runout残料吐料开始: 挤出机=%s 目标=%.0fmm(%d段) E基准=%.3f",
                         extruder, RUNOUT_RESIDUE_PURGE_TOTAL_MM, segment_count, start_e)
        try:
            accepted = await km.execute_gcode(script)
            if not accepted:
                self.logger.error("runout残料吐料指令未被受理")
            else:
                for attempt in range(RUNOUT_RESIDUE_PURGE_POLLS):
                    current_e = await km.get_gcode_e_position()
                    if current_e is not None:
                        actual = current_e - start_e
                        if actual >= RUNOUT_RESIDUE_PURGE_MIN_ACTUAL_MM:
                            break
                    if attempt + 1 < RUNOUT_RESIDUE_PURGE_POLLS:
                        await asyncio.sleep(0.5)
        except Exception as e:
            self.logger.error("runout残料吐料执行异常: %s", e, exc_info=True)
        finally:
            # 即使命令回执异常也可能已部分执行；必须尝试恢复原挤出模式和速度。
            try:
                restored = await km.execute_gcode('RESTORE_GCODE_STATE NAME=%s' % state_name)
            except Exception as e:
                self.logger.error("runout残料吐料恢复G-code状态异常: %s", e, exc_info=True)
        if not restored:
            self.logger.error("runout残料吐料失败: 原G-code状态未确认恢复")
            return False
        if getattr(km, 'active_extruder', None) != extruder:
            self.logger.error("runout残料吐料失败: 期间活跃挤出机发生变化")
            return False
        if not accepted or actual is None or actual < RUNOUT_RESIDUE_PURGE_MIN_ACTUAL_MM:
            self.logger.error("runout残料吐料失败: 实际E位移=%s，要求至少%.1fmm，保持暂停",
                              '不可读' if actual is None else '%.3fmm' % actual,
                              RUNOUT_RESIDUE_PURGE_MIN_ACTUAL_MM)
            return False
        self.logger.info("runout残料吐料确认完成: 挤出机=%s 实际E位移=%.3fmm，允许请求续料",
                         extruder, actual)
        return True

    async def _handle_klipper_disconnect(self):
        """处理Klipper断开连接事件"""
        self.logger.info("处理Klipper断开连接事件，清除状态缓存")
        if getattr(self, '_copy_refill_context', None):
            self._apply_feed_failure(4, 0, self._copy_refill_context.get('tube'), 0, '复制续料连接断开')
        fro_context = getattr(self, '_refill_context', None)
        if (fro_context and fro_context.get('fro_session') and not self._auto_feed_failed
                and self.state_manager.state != SystemStateEnum.FEEDING):
            self._fail_current_refill("FRO人工清料等待/续料交接期间Klipper连接断开", FeederCabinetCAN.ERROR_KLIPPER)
        # 清除主程序的状态缓存，确保重连后能正确检测状态变化
        self._last_printer_state = None
        # 清除断料传感器状态缓存
        self._last_filament_status = {}
        self.logger.info("已清除主程序打印机状态和断料传感器状态缓存")
    
    async def _handle_can_reconnect(self):
        """处理CAN重连成功事件"""
        self.logger.info("处理CAN重连成功事件")
        
        # 发送当前打印状态和余料状态
        try:
            if self.klipper_monitor and self.klipper_monitor.ws_connected:
                # 获取当前缓存的状态
                old_cached_state = self._last_printer_state
                printer_status = self.klipper_monitor.get_printer_status()
                klipper_cached_state = printer_status.get('printer_state') if printer_status else None
                
                self.logger.info(f"状态检查 - 本地缓存: {old_cached_state}, Klipper缓存: {klipper_cached_state}")
                
                # CAN重连后总是重新订阅，确保状态更新正常
                self.logger.info("CAN重连后重新订阅WebSocket以确保状态同步")
                await self.klipper_monitor.resubscribe_objects()
                # 等待状态更新
                await asyncio.sleep(1.0)
                
                # 获取最新的打印机状态
                printer_status = self.klipper_monitor.get_printer_status()
                if printer_status and 'printer_state' in printer_status:
                    current_state = printer_status['printer_state']
                    self.logger.info(f"CAN重连后发送最新打印机状态: {current_state}")
                    await self._send_printer_status_notification(current_state)
                    
                    # 更新缓存的打印机状态，确保后续状态变化能够被正确检测
                    self._last_printer_state = current_state
                    self.logger.info(f"更新打印机状态缓存: {current_state}")

                    # lancaigang261017 任务#448: 重连回填缓存会吞掉"paused 边沿"。
                    # 背景(261017 14:39~14:44 实测): 打印中人为暂停后 CAN 恰好重连, 本处把
                    # _last_printer_state 直接回填成 paused, 而状态机仍停在 PRINTING —— 此后
                    # 不再有 printing->paused 的边沿, 状态机永久与 Klipper 分叉(:1785 的对齐规则
                    # 再也走不到)。分叉后断料时 PRINTING->RUNOUT 分支看到 is_paused=True 便判定
                    # "无需再次暂停", 又无 RUNOUT->PAUSED 边沿, 清管续料编排永不触发(实测卡 7 分钟
                    # 后被取消转 IDLE)。此处按 :1785 同一规则补齐状态对齐。
                    if current_state == 'paused' and self.state_manager.state in [
                            SystemStateEnum.PRINTING, SystemStateEnum.RESUMING]:
                        _act = self.klipper_monitor.active_extruder if self.klipper_monitor else 0
                        self.logger.warning(f"CAN重连状态对齐: Klipper已paused而hub仍为"
                                            f"{self.state_manager.state.name}, 同步转PAUSED(活跃挤出机{_act}), "
                                            f"防状态分叉致断料后无RUNOUT->PAUSED边沿而不续料(任务#448)")
                        self.state_manager.transition_to(SystemStateEnum.PAUSED, extruder=_act)
                    
                    # 如果打印机已经从error恢复，同步系统状态
                    if current_state in ['standby', 'ready'] and self.state_manager.state == SystemStateEnum.ERROR:
                        self.logger.info("检测到打印机已从错误状态恢复，同步系统状态")
                        self.state_manager.transition_to(SystemStateEnum.IDLE)
                else:
                    self.logger.warning("CAN重连后无法获取打印机状态")
                    
                # 发送余料状态
                await self._send_filament_status_notification()
            else:
                self.logger.warning("Klipper未连接，跳过状态同步")

            # lancaigang261019 任务#459: CAN已恢复——先尝试解除CAN类ERROR锁死, 再补发挂起的0x01
            await self._maybe_selfheal_can_error()
            await self._try_flush_pending_feed("CAN重连回调")
                
        except Exception as e:
            self.logger.error(f"处理CAN重连事件时发生错误: {e}", exc_info=True)
    
    async def _handle_feeder_mapping_set(self, mapping_data: Dict[str, Any]):
        """
        处理送料柜发送的料管映射设置命令(0x0F)

        协议语义(F01 canserial.c send_feeder_mapping_to_printer):
            data[1] = 左料管(料管0)对应的挤出机号
            data[2] = 右料管(料管1)对应的挤出机号
        本地配置语义相反: extruders.mapping[挤出机] = 料管, 此处必须做一次反向构造。
        旧代码直接 mapping={0:data[1], 1:data[2]} 把方向写反了, 仅在2x2交换时因互为逆
        而数值恰好相同; 一旦出现非对称组合就会错, 此处按真义修正。

        Args:
            mapping_data: 映射数据字典，包含left_tube, right_tube, status
        """
        try:
            left_extruder = mapping_data.get('left_tube', 0)
            right_extruder = mapping_data.get('right_tube', 1)

            self.logger.info(f"收到料管映射设置命令: 左料管->挤出机{left_extruder}, 右料管->挤出机{right_extruder}")

            if left_extruder not in (0, 1) or right_extruder not in (0, 1) or left_extruder == right_extruder:
                # 非对称映射(两料管指向同一挤出机)物理上不成立: 拒绝并回失败状态,
                # 保持本地原映射不变, 避免hub与F01两侧状态静默分叉且现场无感知。
                self.logger.warning(
                    f"拒绝非法料管映射(左右料管指向同一挤出机): 左={left_extruder}, 右={right_extruder}, "
                    f"保持原映射={self.config.get('extruders', {}).get('mapping')}"
                )
                if self.can_comm:
                    await self.can_comm.send_feeder_mapping_response(left_extruder, right_extruder, 1)
                return

            if 'extruders' not in self.config:
                self.config['extruders'] = {}

            # 料管->挤出机 反构为 挤出机->料管
            new_mapping = {
                left_extruder: 0,
                right_extruder: 1
            }

            # lancaigang261017: F01 每次 CAN 重连/心跳恢复都会补发一次 0x0F, 内容未变时
            # 不重复写盘/不重发 0x0E, 只回响应, 避免无意义刷 SD 卡。
            raw_current = self.config.get('extruders', {}).get('mapping', {})
            try:
                current_mapping = {int(k): int(v) for k, v in raw_current.items()}
            except (TypeError, ValueError):
                current_mapping = {}
            if current_mapping == new_mapping:
                self.logger.info(f"料管映射未变化(重复下发, 已跳过保存): {new_mapping}")
                if self.can_comm:
                    await self.can_comm.send_feeder_mapping_response(left_extruder, right_extruder, 0)
                return

            self.config['extruders']['mapping'] = new_mapping
            
            save_success = self._save_config()
            
            if self.can_comm:
                status = 0 if save_success else 1
                await self.can_comm.send_feeder_mapping_response(left_extruder, right_extruder, status)
                
                # 映射设置成功后，重新发送余料状态以保持信息同步
                if save_success:
                    self.logger.info("料管映射设置成功，重新发送余料状态信息")
                    await self._send_filament_status_notification()
                    # 任务#452: F01重连后补发 0x0F 也是上线信号, 同步补发打印态
                    await self._resend_printer_status_to_f01("F01重连补发映射")
                
        except Exception as e:
            self.logger.error(f"处理料管映射设置时发生错误: {str(e)}", exc_info=True)
            if self.can_comm:
                await self.can_comm.send_feeder_mapping_response(0, 0, 1)

    async def _handle_feeder_mapping_response(self, mapping_data: Dict[str, Any]):
        """
        处理送料柜对 0x10 映射查询的应答(应答命令字仍为0x10, 见F01 canserial.c process_query_feeder_mapping)

        用途: hub 启动/CAN重连后的映射对齐。F01 是映射的唯一权威(用户在其屏上点选交换),
        若 hub 停机期间 F01 改过映射, 0x0F 会因 CAN 未连接被 F01 直接丢弃(canserial.c 的
        g_conn_status 门禁且无重发), 只有这一次查询应答能把差异拉回来。
        """
        try:
            left_extruder = mapping_data.get('left_tube')
            right_extruder = mapping_data.get('right_tube')
            status = mapping_data.get('status', 0)

            self.logger.info(
                f"收到料管映射查询应答: 左料管->挤出机{left_extruder}, "
                f"右料管->挤出机{right_extruder}, 状态={status}"
            )

            if status != 0 or left_extruder not in (0, 1) or right_extruder not in (0, 1) \
                    or left_extruder == right_extruder:
                self.logger.warning(f"料管映射应答无效(状态={status}), 保持本地映射不变")
                return

            f01_mapping = {left_extruder: 0, right_extruder: 1}
            raw_local = self.config.get('extruders', {}).get('mapping', {})
            try:
                local_mapping = {int(k): int(v) for k, v in raw_local.items()}
            except (TypeError, ValueError):
                local_mapping = {}

            if local_mapping == f01_mapping:
                self.logger.info(f"料管映射对齐一致(本地=F01): {f01_mapping}")
            else:
                self.logger.warning(
                    f"料管映射不一致, 按F01回填(hub停机期间F01可能改过左右管交换): "
                    f"本地={local_mapping}, F01={f01_mapping}"
                )
                if 'extruders' not in self.config:
                    self.config['extruders'] = {}
                self.config['extruders']['mapping'] = f01_mapping

                if self._save_config():
                    self.logger.info(f"料管映射已按F01回填并保存: {f01_mapping}")
                else:
                    self.logger.error("料管映射回填后保存配置失败")

                # 映射变化后立即重发余料位图, 让F01左右仓状态随新映射刷新
                await self._send_filament_status_notification()

            # 任务#452: 映射应答是 F01 上线(hub重连/F01重启)的可靠信号,
            # 借此补发打印态, 否则 F01 停在 st=IDLE 时低重通知链整条失效。
            await self._resend_printer_status_to_f01("映射对齐完成")
        except Exception as e:
            self.logger.error(f"处理料管映射查询应答时发生错误: {e}", exc_info=True)
    
    async def _handle_rfid_message(self, data: dict):
        """
        处理RFID CAN消息
        
        Args:
            data: 包含命令和数据的字典
        """
        try:
            # 从CAN消息中提取数据
            can_data = bytes(data['data'])
            
            # 使用RFID解析器处理消息
            result = self.rfid_parser.handle_rfid_message(can_data)
            
            if result:
                if result['type'] == 'rfid_start':
                    self.logger.info(f"开始接收RFID数据: 挤出机{result['extruder_id']}, "
                                   f"耗材通道{result['filament_id']}, "
                                   f"数据源: {result['data_source']}")
                    
                elif result['type'] == 'rfid_packet':
                    self.logger.debug(f"接收RFID数据包 {result['packet_num']}/{result['total_packets']}")
                    
                elif result['type'] == 'rfid_complete':
                    self.logger.info("RFID数据接收完成!")
                    # lancaigang261030233801 任务#575: source=3只进展示链，自然source=0原路径不变
                    if result.get('data_source') == 3:
                        await self._process_slot_filament_snapshot(
                            result['filament_id'], result['data'])
                    else:
                        await self._process_filament_data(result['extruder_id'],
                                                       result['filament_id'],
                                                       result['data'])
                    
                elif result['type'] == 'rfid_error':
                    self.logger.error(f"RFID错误: {result.get('error_msg', result.get('error'))}")
                    
        except Exception as e:
            self.logger.error(f"处理RFID消息时发生错误: {e}", exc_info=True)

    def _ensure_slot_snapshot_state(self):
        # __new__测试与正式初始化共用同一惰性边界，不引入额外持久化或业务状态。
        if not hasattr(self, '_slot_count'):
            self._slot_count = 0
            self._slot_rows = {}
            self._slot_active = [0xFF, 0xFF]
            self._slot_pending_info = {}
            self._slot_status_received = set()
            self._slot_info_blocked = set()

    def _get_slot_sync_lock(self):
        lock = getattr(self, '_slot_sync_lock', None)
        if lock is None:
            lock = asyncio.Lock()
            self._slot_sync_lock = lock
        return lock

    @staticmethod
    def _empty_slot_info():
        return {
            'manufacturer': '', 'material': '', 'color': '', 'color_hex': '', 'uid': '',
        }

    @classmethod
    def _empty_slot_row(cls, slot):
        row = {
            'slot': slot, 'net_weight': None, 'weight_valid': False,
            'fault_code': 0, 'info_state': 0,
        }
        row.update(cls._empty_slot_info())
        return row

    @staticmethod
    def _slot_info_fields(data):
        return {
            'manufacturer': data.manufacturer or '',
            'material': data.material_name or '',
            'color': data.color_name or '',
            'color_hex': (f'{data.color_hex:06X}'
                          if data.color_hex is not None else ''),
            'uid': data.serial_number or '',
        }

    def _slot_snapshot_payload(self):
        slots = []
        for slot in range(self._slot_count):
            row = dict(self._slot_rows[slot])
            row['active'] = slot in self._slot_active
            slots.append(row)
        return {
            'slot_count': self._slot_count,
            'slots': slots,
        }

    async def _sync_slot_snapshot(self):
        if not self.spoolman_client or not self._slot_count:
            return
        try:
            # 调用方持锁到HTTP完成，防止旧全量快照晚于新快照覆盖Moonraker。
            await self.spoolman_client.sync_slot_snapshot(self._slot_snapshot_payload())
        except Exception as e:
            self.logger.error(f"同步槽位展示快照失败: {e}")

    async def _handle_slot_status(self, data: dict):
        """lancaigang261030233801 任务#575: 合并轻量槽态，非法帧不改变既有展示。"""
        self._ensure_slot_snapshot_state()
        slot = data.get('slot')
        count = data.get('slot_count')
        weight = data.get('net_weight')
        weight_valid = data.get('weight_valid')
        fault_code = data.get('fault_code')
        info_state = data.get('info_state')
        if (type(slot) is not int or type(count) is not int
                or not 1 <= count <= 64 or not 0 <= slot < count
                or type(weight) is not int or not 0 <= weight <= 0xFFFF
                or weight_valid not in (0, 1) or info_state not in (0, 1, 2)
                or type(fault_code) is not int or not 0 <= fault_code <= 0xFF):
            self.logger.warning(f"拒绝非法槽位状态: {data}")
            return

        async with self._get_slot_sync_lock():
            if count != self._slot_count:
                self._slot_count = count
                for cache in (self._slot_rows, self._slot_pending_info):
                    for old_slot in [key for key in cache if key >= count]:
                        cache.pop(old_slot, None)
                self._slot_status_received = {
                    key for key in self._slot_status_received if key < count
                }
                self._slot_info_blocked = {
                    key for key in self._slot_info_blocked if key < count
                }
                for buffer in range(2):
                    if (self._slot_active[buffer] != 0xFF
                            and self._slot_active[buffer] >= count):
                        self._slot_active[buffer] = 0xFF
            for index in range(count):
                self._slot_rows.setdefault(index, self._empty_slot_row(index))

            row = self._slot_rows[slot]
            previous_info_state = row['info_state']
            row.update({
                'net_weight': weight if weight_valid else None,
                'weight_valid': bool(weight_valid),
                'fault_code': fault_code,
                'info_state': info_state,
            })
            self._slot_status_received.add(slot)
            if info_state == 0:
                self._slot_pending_info.pop(slot, None)
                self._slot_info_blocked.add(slot)
                row.update(self._empty_slot_info())
            else:
                self._slot_info_blocked.discard(slot)
                if previous_info_state != info_state:
                    row.update(self._empty_slot_info())
                pending = self._slot_pending_info.get(slot)
                if pending is not None:
                    row.update(pending)
            await self._sync_slot_snapshot()

    async def _handle_slot_active(self, data: dict):
        """合并单侧真实活跃槽，仅更新内存展示快照。"""
        self._ensure_slot_snapshot_state()
        buffer = data.get('buffer')
        count = data.get('slot_count')
        active_slot = data.get('active_slot')
        if (type(buffer) is not int or buffer not in (0, 1)
                or type(count) is not int or not 1 <= count <= 64
                or type(active_slot) is not int
                or not (active_slot == 0xFF or 0 <= active_slot < count)):
            self.logger.warning(f"拒绝非法活跃槽状态: {data}")
            return

        async with self._get_slot_sync_lock():
            if count != self._slot_count:
                self.logger.warning(f"忽略跨轮活跃槽状态: {data}")
                return
            self._slot_active[buffer] = active_slot
            await self._sync_slot_snapshot()

    async def _process_slot_filament_snapshot(self, slot: int, data: OpenTagFilamentData):
        """lancaigang261030233801 任务#575: 仅保存展示字段，不建档、不写余量、不切换active。"""
        self._ensure_slot_snapshot_state()
        if type(slot) is not int or not 0 <= slot < 64 or not isinstance(data, OpenTagFilamentData):
            self.logger.warning(f"拒绝非法槽位耗材快照: slot={slot}")
            return
        async with self._get_slot_sync_lock():
            if (self._slot_count and slot >= self._slot_count) or slot in self._slot_info_blocked:
                return
            fields = self._slot_info_fields(data)
            self._slot_pending_info[slot] = fields
            if slot not in self._slot_status_received:
                return
            row = self._slot_rows.get(slot)
            if row is None or row['info_state'] == 0:
                return
            row.update(fields)
            await self._sync_slot_snapshot()
    
    async def _handle_low_weight_notify(self, data: dict):
        """
        处理低重量通知

        Args:
            data: 包含低重量信息的字典
        """
        try:
            filament_id = data.get('filament_id')
            buffer_id = data.get('buffer_id')
            net_weight = data.get('net_weight')
            material_type = data.get('material_type', 0x00)
            material_name = data.get('material_name', '未知')

            # 从buffer_id反向查找对应的extruder_id
            extruder_mapping = self.config.get('extruders', {}).get('mapping', {})
            extruder_id = None
            for ext_id, buf_id in extruder_mapping.items():
                if buf_id == buffer_id:
                    extruder_id = ext_id
                    break

            if extruder_id is None:
                self.logger.warning(f"无法从缓冲区ID {buffer_id} 找到对应的挤出机映射关系，忽略低重量通知")
                return

            self.logger.warning(f"处理低重量通知: 耗材通道={filament_id}, 缓冲区={'左' if buffer_id == 0 else '右'}({buffer_id}), "
                              f"对应挤出机={extruder_id}, 净重量={net_weight}g, 耗材类型={material_name}")

            # 检查是否处于打印状态
            if not self.klipper_monitor or not self.klipper_monitor.ws_connected:
                self.logger.warning("Klipper未连接，无法处理低重量通知")
                return

            printer_status = self.klipper_monitor.get_printer_status()
            if not printer_status:
                self.logger.warning("无法获取打印机状态")
                return

            printer_state = printer_status.get('printer_state', 'unknown')
            active_extruder = printer_status.get('active_extruder', 0)

            self.logger.info(f"当前打印机状态: {printer_state}, 活跃挤出机: {active_extruder}")

            # lancaigang261023010000 qoder-agent-f05-leader #539: 先核既有管路归属，再执行
            # FEEDING自愈/PAUSE在途等副作用，防对侧低重复位当前侧；不缓存旧事件补投。
            # lancaigang261024160000 任务#554-copy108-hotfix5: 归属判据由"与单值 active_extruder 相等"
            # 升级为"是否属于本次参与打印的头集合"。Klipper toolhead.extruder 是单值, COPY/MIRROR 下
            # 恒为 extruder(T0)、active_extruder 恒 0, 但同时两个头都在打印; 旧判据会把另一侧
            # (槽8→tube1→extruder1)的真实低重整条吞掉(108现场 14:14:34 连续4帧"不是活跃挤出机(0)，忽略")。
            _print_heads = self._printing_extruders()
            if extruder_id not in _print_heads:
                self.logger.info(f"缓冲区{buffer_id}对应挤出机{extruder_id}不在参与打印的头集合{_print_heads}中，忽略低重量通知")
                return

            # lancaigang261023120000 任务#540：先过失败/互斥门，拒绝通知不得复位共享事务。
            if (self._auto_feed_failed or getattr(self, '_refill_orchestration_busy', False)
                    or self._fro_purge_busy()):
                self.logger.info("低重通知暂不受理: 失败锁定或已有续料编排，不改本轮状态")
                return
            # lancaigang261024104414 任务#554: 同一事务内的0x1D重发只吸收，不重做切料退料；
            # 无当前事务归属(FEEDING超时残留/重启)仍走既有低重自愈，保留首趟PAUSED→第二趟FEEDING。
            _txn = getattr(self, '_refill_context', None)
            # 任务#554(计划「hub：同一事务的0x1D重发只吸收/查询，不再借FEEDING低重通知
            # 重启完整切料退料编排」)：FEEDING 内收到的 0x1D 是同一趟的重发。
            # 若本侧具备 0x03 在途查询能力(任务#463 R2 query_feed_state)，就按"吸收+限频查询"处理：
            # 不重做切料/退料、不改 FEEDING 期限，只补一次状态查询留证；
            # 没有该能力时无法区分"重发"与"真卡死"，只能沿用 #540 的 FEEDING 卡住自愈。
            if (self.state_manager.state == SystemStateEnum.FEEDING
                    and (self._refill_context_is_current(_txn)
                         or callable(getattr(self.can_comm, 'query_feed_state', None)))):
                self.logger.info(f"吸收在途低重重发(管={buffer_id})，保留原FEEDING期限(任务#554)")
                self._probe_feeding_duplicate(buffer_id)
                return
            feeding_context = None
            # 只有当buffer_id对应的extruder_id是活跃挤出机时才暂停打印
            if printer_state != 'printing':
                # lancaigang261017 任务#447b(用户261017投诉): 上次低重换料因PAUSE回执超时中止,
                # 但暂停事后确实生效(迟到确认)时不能把后续 0x1D 全部丢弃——否则打印机停在 PAUSED、
                # 槽已 0g, 自动续料彻底卡死等人工(261017 10:55 事故)。此窗口内直接恢复编排。
                _abort_age = self._pause_abort_age()
                # lancaigang261023120000 任务#540：0x1D可能是重发，不证明已经换了新盘。
                # 自愈只申请交接当前FEEDING轮次；真正复位/退料在协程取得互斥后执行。
                _feeding_stuck = (
                    printer_state == 'paused'
                    and self.state_manager.state == SystemStateEnum.FEEDING
                    and not getattr(self, '_refill_orchestration_busy', False)
                    and not self._fro_purge_busy()
                )
                _can_recover = _feeding_stuck or (
                    printer_state == 'paused'
                    and _abort_age is not None
                    and _abort_age < PAUSE_ABORT_RECOVER_WINDOW
                    and getattr(self, '_pause_abort_extruder', None) in (None, extruder_id)
                    and self.state_manager.state == SystemStateEnum.PAUSED
                    and not getattr(self, '_refill_orchestration_busy', False)
                    and not self._fro_purge_busy()
                )
                if not _can_recover:
                    self.logger.info(f"打印机不在打印状态({printer_state})，忽略低重量通知")
                    return
                if _feeding_stuck:
                    feeding_context = (self.state_manager.get_payload(), self._refill_failure_epoch)
                    self.logger.warning(f"FEEDING低重自愈申请: 通道={filament_id}, 缓冲区={buffer_id}, "
                                        f"净重={net_weight}g；待核轮次并交接旧送料任务(任务#540)")
                else:
                    self.logger.warning(f"低重恢复: 上次编排因PAUSE回执超时中止已{_abort_age:.0f}s, "
                                        f"打印机现为paused且状态机为PAUSED, 判定暂停已迟到生效, "
                                        f"恢复低重换料编排(跳过步骤1重复PAUSE, 任务#447b/c)")
            # lancaigang 260916 12:30:07 任务#188辅加固: 0x1D低重通知开场20秒宽限(与RUNOUT宽限同款),
            # 开场期buffer_id->挤出机映射可能误匹配active_extruder(T1开场活跃头尚未切换),
            # 宽限内只记日志不触发暂停换料; 0x1D有30秒重发机制, 宽限后下一轮重发照常触发不丢功能
            # lancaigang261014205813 qoder-agent-f01-leader: 用户定策"低重应马上暂停", 收紧开场宽限——
            # 仅当活跃挤出机【尚未发生首次进料运动】(_extruder_fed_seen=False, 仍处开场未稳期)才拦;
            # 打印中一旦有进料运动(映射已稳定), 0x1D立即受理, 不再被丢弃后等下一轮重发(消除"暂停被推迟~30s")。
            # 判据与RUNOUT路径(:1486 _fed_seen门禁)对齐, 补上低重路径此前缺失的这一门禁。
            # lancaigang261017 任务#447(用户261017定策): 取消 20s 上限——首次进料前一律缓发 PAUSE。
            # 根因: 升温(M191/M109)、调平(QUAD_GANTRY_LEVEL/网床)、归位等开场流程期间 gcode 队列
            # 被长命令阻塞, 此时下发 PAUSE 只会排队, 等阻塞结束才迟到生效, 打印在任意时刻被打断。
            # 首次进料后映射已稳定且队列已畅通, 0x1D(30s 重发)立即受理, 不丢功能只正确定时。
            _ps = getattr(self, '_printing_started_at', None)
            _fed = self._extruder_fed_seen.get(active_extruder, False)
            # _ps 缺失时按已超上限处理, 防开场标志异常导致永久缓发(下发前仍有队列探测兜底)
            _elapsed = (time.monotonic() - _ps) if _ps is not None else LOW_WEIGHT_FED_DEFER_MAX
            if not _fed and _elapsed < LOW_WEIGHT_FED_DEFER_MAX:
                _now = time.monotonic()
                if _now - self._low_weight_defer_log_at >= LOW_WEIGHT_DEFER_LOG_INTERVAL:
                    self._low_weight_defer_log_at = _now
                    self.logger.info(f"低重通知在开场阶段暂不处理(活跃挤出机{active_extruder}尚未首次进料, "
                                     f"升温/调平/归位期间队列可能阻塞, 此刻发PAUSE只会迟到生效): "
                                     f"耗材通道={filament_id}, 对应挤出机={extruder_id}, 已打印{_elapsed:.0f}s; "
                                     f"F05侧30s重发0x1D, 首次进料后立即受理")
                return
            if not _fed:
                self.logger.warning(f"低重通知: 活跃挤出机{active_extruder}打印{_elapsed:.0f}s仍无首次进料, "
                                    f"超过缓发上限{LOW_WEIGHT_FED_DEFER_MAX:.0f}s, 放行本次下发(由队列探测兜底)")

            # lancaigang261017 任务#447: PAUSE 在途守卫(见 PAUSE_PENDING_MAX_WAIT 常量注释)。
            # 上一条 PAUSE 未确认生效前不再下发新的, 否则每条低重通知都会往 Klipper 积压队列里
            # 塞一条迟到 PAUSE, 等温/长命令结束后集中放行, 打印会在任意时刻被莫名打断。
            # F05 侧 WAIT_CUT 30s 重发 0x1D 会持续重试, 所以这里丢弃不丢功能。
            if self._pause_pending_since is not None:
                if self.klipper_monitor.pause_resume_is_paused:
                    self.logger.info("PAUSE在途守卫解除: 上一条PAUSE已确认生效(pause_resume.is_paused=True)")
                    self._pause_pending_since = None
                elif time.monotonic() - self._pause_pending_since < PAUSE_PENDING_MAX_WAIT:
                    self.logger.warning(f"低重通知丢弃: 上一条PAUSE仍在途({time.monotonic() - self._pause_pending_since:.1f}s未确认生效), "
                                        f"不再重复下发(任务#447); 耗材通道={filament_id}")
                    return
                else:
                    self.logger.warning(f"PAUSE在途已超过{PAUSE_PENDING_MAX_WAIT:.0f}s仍未生效, 放弃等待并允许重新下发(任务#447)")
                    self._pause_pending_since = None

            # lancaigang261024160000 任务#554-copy108-hotfix5: 双车模式下打印头此刻仍有料是正常态
            # (旧盘还剩55g); 若该头已经断料则属runout事件, 交复制纯runout事务, 免得同一事件被两条
            # 链路各暂停一次、或把"打印头无料"当低重去退料(空管)。
            if self._copy_print_mode():
                _head_has = self._active_sensor_has_filament(extruder_id)
                if _head_has is not True:
                    self.logger.info(f"复制/镜像打印头{extruder_id}当前"
                                     f"{'无料(转断料场景)' if _head_has is False else '传感器状态未知'}, "
                                     f"低重通知不在此受理(交复制纯runout事务/等下一轮0x1D重发)")
                    return

            # 确认是参与打印的头且在打印中，开始自动换料流程
            self.logger.warning(f"检测到参与打印的挤出机{extruder_id}(缓冲区{buffer_id})的耗材重量过低({net_weight}g)，开始自动换料流程")

            # lancaigang 260918 20:52:35 任务#296 P0-3改动2: 低重会话级标志置位——
            # 受理成功(全部门禁通过走到此处)才置位, 纯通知丢弃(未打印/开场宽限/对侧/
            # 映射失败等早退路径)不算"见过"。供_runout_clear_and_feed场景分流:
            # 本会话后续的runout编排走完整原序列(与现状行为一致, 低重链路隔离的关键)
            # lancaigang261023120000 任务#540：标志随协程真实受理置位，不让迟到任务污染分流。

            # 启动换料任务
            if self._copy_print_mode():
                # lancaigang261024160000 任务#554-copy108-hotfix5: 双车模式不能走 _auto_change_filament——
                # 其 UNLOAD_FILAMENT 作用在 toolhead.extruder(T0) 的 E 轴, 而 extruder1 被 SYNC 到同一运动
                # 队列, 一次退料会把另一侧打印头也退空(另一侧无续料=空打)。改走复制低重续料事务:
                # 单侧 0x1E 退料到该头传感器无料 → 0x01 → 双头分段预挤 → 恢复。
                self.logger.warning(f"[COPY-REFILL] 双车模式低重(挤出机={extruder_id}, 缓冲区={buffer_id}, "
                                    f"净重={net_weight}g): 走复制低重续料事务(单侧退料+双头预挤)")
                asyncio.create_task(self._copy_low_weight_refill(extruder_id, buffer_id, net_weight))
            else:
                asyncio.create_task(self._auto_change_filament(
                    extruder_id, buffer_id, net_weight, feeding_context=feeding_context))

        except Exception as e:
            self.logger.error(f"处理低重量通知时发生错误: {e}", exc_info=True)

    async def _auto_change_filament(self, extruder_id: int, buffer_id: int, net_weight: int,
                                    feeding_context=None):
        """
        自动换料流程

        Args:
            extruder_id: 挤出机ID
            buffer_id: 缓冲区ID
            net_weight: 当前净重量
        """
        # 任务#278 H2: 编排互斥入口——与RUNOUT清管编排共用标志，防止低重换料与
        # runout清管双编排并发(双UNLOAD宏/双0x1E/双0x01)。顺带修复旧版双0x1D
        # 重复触发时并发跑两份_auto_change_filament的隐患(旧版仅靠FEEDING标志粗互斥)。
        # 任务#286 V10: 维持全局单标志(单状态机不支持真并发编排, 理由见
        # _runout_clear_and_feed互斥入口注释), 加owner分侧日志可观测:
        # 对侧低重0x1D被延后时有明确日志口径, 不排队但F05侧WAIT_CUT 30s重发0x1D兜底。
        resume_task = getattr(self, '_resume_task', None)
        if (getattr(self, '_refill_orchestration_busy', False) or self._fro_purge_busy()
                or (resume_task is not None and not resume_task.done())):
            self.logger.warning(f"已有换料/清管编排进行中(占用侧: 挤出机{getattr(self, '_refill_orchestration_owner', None)}), "
                                f"本次挤出机{extruder_id}侧低重自动换料触发延后（防双编排并发; "
                                f"F05侧WAIT_CUT 30s重发0x1D兜底, 宽限后重触发收敛）")
            return
        # lancaigang261023120000 任务#540：普通迟到任务不能借FEEDING入口重退料。
        # payload对象由每次真实状态迁移更换，配合已有失败代次区分旧自愈申请，无新增全局闩锁。
        if feeding_context is not None:
            old_payload, old_epoch = feeding_context
            if (self.state_manager.state != SystemStateEnum.FEEDING
                    or self.state_manager.get_payload() is not old_payload
                    or not self._refill_attempt_is_current(old_epoch)
                    or old_payload.get('extruder', extruder_id) != extruder_id
                    or not self.klipper_monitor.pause_resume_is_paused
                    or self.klipper_monitor.active_extruder != extruder_id
                    or self.config.get('extruders', {}).get('mapping', {}).get(extruder_id) != buffer_id):
                self.logger.info("低重自愈申请已过期或归属改变，不退料、不接续新轮次")
                return
        elif self._auto_feed_failed or self.state_manager.state not in (
                SystemStateEnum.PRINTING, SystemStateEnum.PAUSED, SystemStateEnum.RUNOUT):
            self.logger.info(f"低重换料编排未启动: 失败锁={self._auto_feed_failed}, 状态={self.state_manager.state.name}")
            return
        context = self._begin_refill_context(extruder_id, buffer_id)
        if context is None:
            return
        self._refill_orchestration_busy = True
        self._refill_orchestration_owner = extruder_id
        self._refill_orchestration_task = asyncio.current_task()
        failure_epoch = self._refill_failure_epoch
        try:
            if feeding_context is not None:
                # lancaigang261023120000 任务#540：持锁交接，先撤旧裁决/清缓冲并离开FEEDING，
                # 再等旧任务退出；保留超时累计。旧FEEDING不能被后段误认作本轮已发0x01。
                old_tasks = self._cancel_refill_tasks("低重自愈交接")
                self.state_manager.transition_to(SystemStateEnum.PAUSED, extruder=extruder_id)
                if old_tasks:
                    await asyncio.gather(*old_tasks, return_exceptions=True)
                if (not self._refill_attempt_is_current(failure_epoch)
                        or self.state_manager.state != SystemStateEnum.PAUSED):
                    return
                self.logger.info(f"低重自愈交接完成: 挤出机={extruder_id}, 缓冲区={buffer_id}, 进入PAUSED首趟编排")
            self._low_weight_seen_this_print = True
            self._clear_pause_abort_marker("新编排启动")  # 任务#447b/c: 清掉"上次中止待自救"内存+落盘标记
            self._f05_nack_count = 0  # 任务#286 V6: 新编排开始, 清零F05 NACK连续计数
            self._last_f05_nack_reason = None  # 任务#549: 同步清零"最近NACK原因", 防上一轮编排的原因码串到本轮
            # lancaigang261016 任务#446(用户261016定策): 新一次续料编排起点清初始中间态(full=False,
            # 保留承重墙分流标志 _low_weight_refill_pending 供低重链路两趟借道判定)。
            self._reset_auto_refill_state("低重自动换料编排开始", full=False)
            # lancaigang261016 任务#446: 低重链路编排一旦启动即标记"待恢复借道"(放入口而非步骤5,
            # 以覆盖步骤4提前return/FEEDING等各路径); 其后 PAUSED 轮询/状态迁移第二次触发
            # _runout_clear_and_feed 时据此走完整序列(承重墙恢复闭环不变); 恢复成功(转RESUMING/
            # printing新会话)时复位, 使后续独立runout跳过退料。
            self._low_weight_refill_pending = True
            self.logger.info(f"=== 开始自动换料流程 ===")
            self.logger.info(f"挤出机: {extruder_id}, 缓冲区: {buffer_id}, 剩余重量: {net_weight}g")

            # 步骤1: 暂停打印
            self.logger.info("步骤1: 暂停打印")
            if self.klipper_monitor.pause_resume_is_paused:
                # 任务#447b: 迟到的低重恢复路径(打印机已暂停, 编排从暂停态接续), 不再重发 PAUSE,
                # 否则 PAUSE 宏虽会因 is_paused 短路, 但白占一次回执等待, 且污染在途守卫。
                self.logger.info("打印机已是暂停态(pause_resume.is_paused=True), 跳过步骤1的PAUSE下发(任务#447b)")
                self._pause_pending_since = None
            else:
                # lancaigang261017 任务#447: 统一走带队列探测与生效确认的守卫式暂停:
                # blocked=队列被升温/调平/长宏阻塞(未下发PAUSE, 等F05 30s重发0x1D再试);
                # failed=PAUSE未被受理/未确认生效(可能迟到生效, 置在途守卫防重复下发, 绝不继续发UNLOAD)。
                _pr = await self._pause_print_guarded(f"低重换料(挤出机{extruder_id})步骤1")
                if _pr == 'blocked':
                    self.logger.warning("gcode队列阻塞, 本次换料编排中止(未下发PAUSE), 等下一轮0x1D重试(任务#447)")
                    return
                if _pr == 'failed':
                    # 任务#447b: 记下中止时刻, 若暂停事后迟到生效, 下一次 0x1D 可据此恢复编排,
                    # 不再永久卡在 PAUSED(261017 10:55 实测: 中止后 4 条 0x1D 全被丢弃)。
                    # 任务#447c: 同时落盘, 使 hub 重启后仍能自救(重启会跟随klipper同步为PAUSED)。
                    self._save_pause_abort_marker(extruder_id, buffer_id)
                    self.logger.error("暂停打印未获确认, 中止本次换料流程, 不再下发UNLOAD_FILAMENT(任务#447); "
                                      "若暂停迟到生效, 下一次低重通知将直接接续编排(任务#447b/c)")
                    return

            # 使用M400等待所有计划的移动完成
            await self.klipper_monitor.execute_gcode("M400")
            self.logger.info("暂停移动已完成")

            # lancaigang261016 qoder-agent-f01-leader 任务#445: 用户261016反馈——10s停稳窗太短,
            # 打印头还没移动到暂停位置, F01槽就同步开始退料(现象: 暂停移动过程中槽已开始退料)。
            # 根因: _send_gcode走WebSocket printer.gcode.script发完即返回, 上方M400并不真正等待
            # 移动完成(即发即走), 故"停稳窗"是唯一等待; 打印头park移动耗时可能>10s。用户拍板10s→30s。
            self.logger.info("等待30秒确保打印机完全暂停并停稳(10s→30s, 用户261016定策)...")
            await asyncio.sleep(30.0)
            if not self._refill_attempt_is_current(failure_epoch):
                return
            self.logger.info("等待完成，打印机已完全停稳")

            # 步骤2: 检查并启动UNLOAD_FILAMENT宏
            # UNLOAD_FILAMENT宏执行流程分析：
            # - 阶段1 (前8秒): 预准备(+5mm) + 塑形反复进退10次，净后退约5mm
            # - 阶段2 (后5秒): 后退50mm
            # 总计：净后退55mm，总用时约13秒

            self.logger.info("步骤2: 检查并启动UNLOAD_FILAMENT宏")
            if not await self.klipper_monitor.check_gcode_macro_exists("UNLOAD_FILAMENT"):
                self.logger.error("未找到UNLOAD_FILAMENT宏，中止换料流程")
                return

            # 启动UNLOAD_FILAMENT宏（异步执行，不等待完成）
            self.logger.info("启动UNLOAD_FILAMENT宏（塑形+后退50mm）")
            if not await self.klipper_monitor.execute_gcode("UNLOAD_FILAMENT"):
                self.logger.error("执行UNLOAD_FILAMENT宏失败，中止换料流程")
                return

            self.logger.info("UNLOAD_FILAMENT宏已启动")

            # 步骤3: 等待塑形阶段，然后送料柜同步退料
            self.logger.info("步骤3: 等待挤出机塑形阶段完成（约7.5秒），然后送料柜同步退料")

            # 等待塑形阶段完成（约7.5秒）
            # 此时挤出机在做复杂的前进后退动作形成尖角
            self.logger.info("等待挤出机塑形阶段（约7.5秒）...")
            await asyncio.sleep(2.5)

            if not await self.can_comm.retract_filament(buffer_id, distance=3, speed=0):
                self.logger.error("送料柜退料命令发送失败，中止换料流程")
                return

            await asyncio.sleep(5.5)
            # 送料柜大幅后退，与挤出机的100mm后退同步
            # 挤出机会后退100mm，送料柜也退100mm，保持同步
            self.logger.info("送料柜开始后退100mm，与挤出机同步退料")
            if not await self.can_comm.retract_filament(buffer_id, distance=4, speed=0):
                self.logger.error("送料柜退料命令发送失败，中止换料流程")
                return

            # 档位4=50mm, 速度0=10mm/s, 用时5秒
            self.logger.info("送料柜退料命令已发送 (50mm, 10mm/s，预计5秒)")

            # 等待送料柜退料完成（5秒）+ 额外缓冲时间
            self.logger.info("等待送料柜退料完成...")
            await asyncio.sleep(5.5)

            self.logger.info("送料柜开始后退100mm，与挤出机同步退料")
            if not await self.can_comm.retract_filament(buffer_id, distance=4, speed=5):
                self.logger.error("送料柜退料命令发送失败，中止换料流程")
                return
            # 等待挤出机的所有移动完成
            await self.klipper_monitor.execute_gcode("M400")
            self.logger.info("同步退料完成：挤出机已退50mm，送料柜已退50mm")
            # lancaigang261014224500 qoder-agent-f01-leader 任务#438: 武装"退料后持续无料"冗余校验
            self._arm_retract_empty_check()

            # 步骤4: 等待并确认传感器检测到断料
            self.logger.info("步骤4: 等待并确认传感器检测到断料")
            sensor_name = f"Filament_Sensor{extruder_id}"
            sensor_obj_name = f"filament_switch_sensor {sensor_name}"
            max_retries = 3
            retract_distance = 2  # 档位2 = 10mm
            retract_speed = 0     # 档位0 = 10mm/s

            # lancaigang 261020 14:20:34 任务#438观测: 退料确认门禁留给结论标记(纯日志, 不改判定)。
            # 旧日志只有'有料'WARNING与末次'强行继续'WARNING, 事后无法区分本轮到过
            # "确认通过/重试耗尽强行放行"哪条分支, 也无法与后续0x01的seq对齐时间线。
            _confirm_started_at = time.monotonic()
            _confirm_verdict = 'PENDING'
            self.logger.info(
                f"[RETRACT-CHECK] 退料确认门禁启动: 缓冲区={buffer_id} 挤出机={extruder_id} "
                f"传感器={sensor_name} 最大补退={max_retries}次(每次10mm@10mm/s) "
                f"初始缓存状态={'有料' if self._last_filament_status.get(sensor_obj_name, True) else '无料'} "
                f"#439校验={'已判失败' if getattr(self, '_retract_verify_failed', False) else ('已取得稳定无料' if getattr(self, '_retract_empty_ok', False) else '计时中')}")

            # 等待传感器状态稳定（退料后传感器可能需要时间响应）
            self.logger.info("等待传感器状态稳定（2秒）...")
            await asyncio.sleep(2.0)

            # 检查传感器是否检测到断料
            for retry in range(max_retries + 1):  # +1 是第一次检查
                # 检查系统状态：如果已经转换到FEEDING，说明Klipper监控器已经检测到断料并触发了补料
                if self.state_manager.state == SystemStateEnum.FEEDING:
                    self.logger.info("✓ 检测到系统已转换到FEEDING状态，Klipper监控器已触发补料")
                    self.logger.info("退料流程已完成，补料流程已由Klipper监控器启动，退出换料流程")
                    return

                # 获取传感器状态（使用本地缓存_last_filament_status，key是完整对象名）
                has_filament = self._last_filament_status.get(sensor_obj_name, True)

                self.logger.info(f"传感器 {sensor_name} 状态检查 (第{retry + 1}次): {'有料' if has_filament else '无料'}")

                if not has_filament:
                    _confirm_verdict = 'CONFIRMED_EMPTY'
                    self.logger.info(f"✓ 传感器确认断料（无料），退料成功 [RETRACT-CHECK] verdict=CONFIRMED_EMPTY "
                                     f"第{retry + 1}次检查通过, 耗时{time.monotonic() - _confirm_started_at:.1f}s")
                    break

                # 第一次检查时就发现有料
                if retry == 0:
                    self.logger.warning(f"⚠ 传感器仍检测到有料，说明退料不完全")

                # 仍有料，尝试补充退料（最多3次）
                if retry < max_retries:
                    self.logger.warning(f"执行第 {retry + 1}/{max_retries} 次补充退料 (10mm, 10mm/s)")
                    if await self.can_comm.retract_filament(buffer_id, retract_distance, retract_speed):
                        self.logger.info("补充退料命令已发送，等待完成（1.5秒）")
                        await asyncio.sleep(1.5)  # 等待10mm退料完成
                        # lancaigang 261020 14:20:34 任务#438观测: 补退完成即打点, 便于算出每次补退的
                        # 有效位移与传感器响应延迟(固定10mm是否不足, 靠这条日志量化)
                        self.logger.info(f"[RETRACT-CHECK] 第{retry + 1}/{max_retries}次补充退料已完成(10mm@10mm/s), "
                                         f"进入第{retry + 2}次复检")

                    else:
                        self.logger.error("发送补充退料命令失败")
                        break
            else:
                # lancaigang260912: 重试耗尽仍有料——仅警告，不中止，强行继续续料
                # lancaigang 261020 14:20:34 任务#438观测: 标记强行放行分支及当时的#439校验状态,
                # 该分支意味着"传感器与机械事实矛盾"仍下发续料, 需在日志里可直接检出
                _confirm_verdict = 'FORCED_CONTINUE'
                self.logger.warning(
                    f"⚠ 重试{max_retries}次后传感器仍检测到有料，但强行继续续料流程 "
                    f"[RETRACT-CHECK] verdict=FORCED_CONTINUE 缓冲区={buffer_id} 挤出机={extruder_id} "
                    f"退料总量=同步100mm+补退{max_retries}x10mm 检查耗时={time.monotonic() - _confirm_started_at:.1f}s "
                    f"#439校验={'已判失败' if getattr(self, '_retract_verify_failed', False) else ('已取得稳定无料' if getattr(self, '_retract_empty_ok', False) else '计时中')}")

            # 步骤5: 发送续料命令（不再因传感器状态阻断）
            # lancaigang 261020 14:20:34 任务#438观测: 步骤4->5 之间输出确认结论, 与后续seq/回执/裁决日志对齐,
            # 使"确认未通过仍下发"这一放行决策在时间线上可定位(不放行时留INFO, 放行时升WARNING便于检索)
            _confirm_elapsed = time.monotonic() - _confirm_started_at
            _confirm_msg = (f"[RETRACT-CHECK] 步骤4结论: verdict={_confirm_verdict} 缓冲区={buffer_id} "
                            f"挤出机={extruder_id} 耗时={_confirm_elapsed:.1f}s -> 进入步骤5下发续料")
            if _confirm_verdict == 'FORCED_CONTINUE':
                self.logger.warning(_confirm_msg)
            else:
                self.logger.info(_confirm_msg)
            self.logger.info("步骤5: 发送续料命令")

            # 检查系统状态：如果已经转换到FEEDING，说明补料已经触发，不需要再发送
            if self.state_manager.state == SystemStateEnum.FEEDING:
                self.logger.info("✓ 系统已在FEEDING状态，补料流程已启动，无需重复发送")
                self.logger.info("=== 自动换料流程完成（补料已由监控器触发） ===")
                return

            if not self._refill_attempt_is_current(failure_epoch):
                return
            # 发送续料命令
            # lancaigang 261020 14:20:34 任务#438观测: 标注请求来源(req_origin), 用于识别"同一次缺料
            # 被PAUSED编排与PAUSED轮询各发一次0x01"的重复发令(两次来源名不同即可对照)
            self.logger.info(f"发送续料命令到送料柜... (req_origin=lowweight_auto_change 缓冲区={buffer_id} "
                             f"挤出机={extruder_id} 退料确认={_confirm_verdict} 当前状态={self.state_manager.state.name})")
            feed_sent = await self.can_comm.request_feed(tube_id=buffer_id)
            if not self._refill_attempt_is_current(failure_epoch):
                return
            if feed_sent:
                # ★★★【承重墙·删除/重构前必读·任务#286】★★★
                # 低重链路此处发0x01后【刻意不转FEEDING保持PAUSED】:
                # 恢复闭环借道: PAUSED轮询(检测到活跃挤出机缺料)→第二次触发
                # _runout_clear_and_feed→它置FEEDING→新料到头→FEEDING轮询转RESUMING→resume。
                # 此PAUSED保持是低重链路与RUNOUT链路共用的恢复承重墙(Quentin3审计#282结论),
                # 严禁在此处直接transition_to(FEEDING)——会跳过第二次编排的清管步骤且与
                # PAUSED轮询触发路径冲突。任务#286的FEEDING超时兜底只作用于转FEEDING之后,
                # 本链路由PAUSED轮询+第二次编排天然兜底, 不受超时改动影响。
                self.logger.info(f"✓ 续料命令已发送到缓冲区{buffer_id} (req_origin=lowweight_auto_change, "
                                 f"状态={self.state_manager.state.name}; 非FEEDING态按设计不挂裁决, "
                                 f"恢复依赖PAUSED轮询触发二次清管编排)")
                self.logger.info("等待送料柜完成续料，完成后将自动恢复打印")
                self.logger.info("=== 自动换料流程完成（等待续料） ===")
                return
            else:
                self.logger.error("✗ 发送续料命令失败，换料流程失败")
                self.logger.error("打印保持暂停状态，请检查送料柜连接")
                return

        except Exception as e:
            self.logger.error(f"自动换料流程发生错误: {e}", exc_info=True)
            self.logger.error("换料流程失败，打印保持暂停状态")
        finally:
            # 任务#278 H2: 无论成功/失败/提前return，都释放编排互斥标志
            self._refill_orchestration_task = None
            self._refill_orchestration_owner = None
            self._refill_orchestration_busy = False

    async def _handle_filament_runout(self, data: dict):
        """FRO开关分流：true等待打印头RUNOUT；false立即暂停并等待人工取净残料。"""
        try:
            filament_id = data.get('filament_id')
            buffer_id = data.get('buffer_id')
            status = data.get('status')
            material_name = data.get('material_name', '未知')
            extruder_mapping = self.config.get('extruders', {}).get('mapping', {})
            extruder_id = next((ext for ext, tube in extruder_mapping.items() if tube == buffer_id), None)
            self.logger.info(f"处理断料通知: 耗材通道={filament_id}, 缓冲区={buffer_id}, "
                             f"对应挤出机={extruder_id}, 状态={status}, 耗材类型={material_name}")
            if not self.klipper_monitor or not self.klipper_monitor.ws_connected:
                self.logger.warning("Klipper未连接，断料通知仅记录")
                return
            printer_status = self.klipper_monitor.get_printer_status()
            if not printer_status:
                self.logger.warning("无法获取打印机状态，断料通知仅记录")
                return
            active_extruder = printer_status.get('active_extruder', 0)
            printer_state = printer_status.get('printer_state', 'unknown')
            if printer_state != 'printing' or extruder_id != active_extruder:
                self.logger.info(f"记录断料通知(不动作): 活跃挤出机={active_extruder}, "
                                 f"断料挤出机={extruder_id}, 打印状态={printer_state}")
                return
            if self._copy_print_mode():
                self.logger.info("COPY/MIRROR收到FRO无料，仅保留既有通知/复制续料路径")
                return
            # lancaigang261104093930 任务#588: true只恢复旧纯runout链，绝不启用已撤销的强制清料。
            if self.config.get('fro_auto_refill', False) is True:
                self.logger.info("fro_auto_refill=true: FRO仅记录，继续打印；等待打印头RUNOUT后执行普通50mm清料续料")
                return

            context = getattr(self, '_refill_context', None)
            if context is not None and context.get('fro_session') and self._refill_context_is_current(context):
                task = getattr(self, '_fro_purge_task', None)
                if not self._auto_feed_failed and (task is None or task.done()):
                    self._fail_current_refill("FRO会话异常结束且尚未交接，重复通知不重启人工清料等待")
                else:
                    self.logger.info("吸收同一FRO会话的重复0x1A，保留原人工清料等待任务")
                return
            if self._auto_feed_failed or self._refill_motion_busy():
                self.logger.info("FRO通知暂不受理: 失败锁定或已有机械编排，不改本轮状态")
                return

            has_filament = self._active_sensor_has_filament(extruder_id)
            self._pause_orchestrated = True
            context = self._begin_refill_context(extruder_id, buffer_id)
            if context is None:
                self._pause_orchestrated = False
                return
            self._fro_purge_generation = getattr(self, '_fro_purge_generation', 0) + 1
            sensor_objects = getattr(self.klipper_monitor, 'filament_sensor_objects', [])
            context.update(
                fro_session=True,
                fro_generation=self._fro_purge_generation,
                fro_started_at=time.monotonic(),
                fro_phase='pause',
                fro_precleared=False,
                fro_initial_absent=has_filament is False,
                fro_sensor=(sensor_objects[extruder_id]
                            if extruder_id < len(sensor_objects) else None),
            )
            # 先发布任务引用再转RUNOUT，通用状态动作看到归属后不会重复暂停。
            self._fro_purge_task = asyncio.create_task(self._fro_manual_clear_and_feed(context))
            self.state_manager.transition_to(SystemStateEnum.RUNOUT, extruder=extruder_id)
            self._save_runtime_state("FRO暂停等待人工清料会话开始", force=True)
        except Exception as e:
            self.logger.error(f"处理断料通知时发生错误: {e}", exc_info=True)

    async def _fro_manual_clear_and_feed(self, context):
        """暂停后零挤出等待人工；真实无料边沿连续3秒才交接既有续料并跳过50mm。"""
        task = asyncio.current_task()
        km = self.klipper_monitor
        extruder = context['extruder']
        sensor_obj_name = context['fro_sensor']

        def current():
            return (getattr(self, '_fro_purge_task', None) is task
                    and context.get('fro_generation') == getattr(self, '_fro_purge_generation', 0)
                    and self._refill_context_is_current(context)
                    and self.state_manager.state in (SystemStateEnum.RUNOUT, SystemStateEnum.PAUSED)
                    and not self._copy_print_mode())

        def absent_ready():
            has_filament = self._active_sensor_has_filament(extruder)
            if has_filament is None:
                raise RuntimeError("打印头传感器状态未知，保持暂停等人工")
            absent_at = (getattr(self, '_sensor_absent_at', {}) or {}).get(sensor_obj_name)
            valid_edge = (absent_at is not None
                          and (context.get('fro_initial_absent')
                               or absent_at >= context['fro_started_at']))
            return (has_filament is False and valid_edge
                    and time.monotonic() - absent_at >= RUNOUT_DEBOUNCE_SECONDS)

        try:
            context['fro_phase'] = 'pause'
            await self._pause_for_runout_with_retry(extruder)
            if not current():
                return
            if not km.pause_resume_is_paused:
                raise RuntimeError("Klipper未确认暂停，保持人工处理")
            if self.state_manager.state == SystemStateEnum.RUNOUT:
                self.state_manager.transition_to(SystemStateEnum.PAUSED, extruder=extruder)
            if not current() or self.state_manager.state != SystemStateEnum.PAUSED:
                raise RuntimeError("Hub未确认PAUSED，保持人工处理")
            if not await km.execute_gcode("M400"):
                raise RuntimeError("暂停后M400未被受理")
            if not current():
                return
            await asyncio.sleep(30.0)
            if not current():
                return
            if self._active_sensor_has_filament(extruder) is None:
                raise RuntimeError("停稳后打印头传感器状态未知，保持暂停等人工")

            context['fro_phase'] = 'manual_wait'
            self.logger.warning("FRO已暂停：请人工取净料管残料；Hub不会自动吐料，打印头真实无料稳定3秒后将自动续料")
            self._save_runtime_state("FRO暂停等待人工取净料管残料", force=True)
            while True:
                if not current():
                    return
                if absent_ready():
                    break
                has_filament = self._active_sensor_has_filament(extruder)
                if has_filament is False:
                    absent_at = (getattr(self, '_sensor_absent_at', {}) or {}).get(sensor_obj_name)
                    valid_edge = (absent_at is not None
                                  and (context.get('fro_initial_absent')
                                       or absent_at >= context['fro_started_at']))
                    if not valid_edge:
                        raise RuntimeError("当前无料但缺少真实有料→无料边沿，禁止直接续料")
                await asyncio.sleep(FRO_MANUAL_POLL_INTERVAL)

            context['fro_precleared'] = True
            context['fro_phase'] = 'handoff'
            self._session_vacancy_seen = True
            self._save_runtime_state("FRO人工清料确认待交接", force=True)
            await self._runout_clear_and_feed(extruder)
            if self._refill_context is context and self.state_manager.state == SystemStateEnum.FEEDING:
                context['fro_phase'] = 'feeding'
                self._save_runtime_state("FRO人工清料已交接FEEDING", force=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if (getattr(self, '_fro_purge_task', None) is task
                    and self._refill_context_is_current(context)):
                self._fail_current_refill(f"FRO人工清料等待失败: {e}", FeederCabinetCAN.ERROR_KLIPPER)
            else:
                self.logger.info(f"FRO旧会话异常收口(不动作): {e}")
        finally:
            if getattr(self, '_fro_purge_task', None) is task:
                self._fro_purge_task = None
        
    async def _handle_weight_sensor_fault(self, data: dict):
        """
        任务#454: 处理 F01 称重故障通知(0x22, 纯通知).

        F01 侧判据(只读 raw/tare, 与净重无关): 活跃槽 raw 连续无效 >=60s 且打印中,
        60s 限频重发。故障码 1=raw未就绪/读失败 2=raw越健康窗 3=未去皮。
        去皮后的 0g、取下料盘的 0g 都是 raw 健康的正常状态, F01 不会上报。

        动作定策(与 0x1A 纯通知同源): 只记录 + WARNING 告警, 不改状态机、
        不暂停打印、不发起换料 —— 该槽净重不可信期间 F01 已抑制 0x1D 低重上报,
        真断料由 F01 0x1A + hub RUNOUT 链兜底。
        """
        try:
            filament_id = data.get('filament_id')
            buffer_id = data.get('buffer_id')
            fault_code = data.get('fault_code')
            fault_name = data.get('fault_name', '未定义')
            material_name = data.get('material_name', '未知')
            _side = '左' if buffer_id == 0 else '右'
            self.logger.warning(
                f"称重故障(0x22纯通知): 耗材通道={filament_id}, 缓冲区={_side}({buffer_id}), "
                f"原因={fault_name}(code={fault_code}), 耗材类型={material_name}; "
                f"该槽净重不可信(0x1D低重已抑制), 仅记录不暂停, 真断料由0x1A/RUNOUT兜底")
            self._last_weight_fault = {
                'filament_id': filament_id,
                'buffer_id': buffer_id,
                'fault_code': fault_code,
                'fault_name': fault_name,
                'material_name': material_name,
                'timestamp': time.time()
            }
        except Exception as e:
            self.logger.error(f"处理称重故障通知时发生错误: {e}", exc_info=True)

    async def _handle_f05_nack(self, data: dict):
        """
        任务#286 V6: 处理F05的NACK回帧(0x1E等hub命令被F05拒绝)
            
        背景: 旧版hub三段0x1E盲发无回执, F05拒绝hub不知情(审计#282 V6, hub侧原L1327
        docstring自述)。F05侧任务#284对0x1E被拒加NACK回帧(功能码NACK_FUNC_CODE=0x1F,
        任务#288对拍定稿: F05实际发裸格式[0x1F,被拒cmd,原因码,侧别,保留x4],
        can_communication.py裸分支已提取侧别buffer_id, 0xA5带帧头分支保留兼容框架)。
                    
        处理策略:
        - 收到即WARN日志记录"命令0xXX被X侧拒绝原因X"(可观测性, 含侧别定位)
        - 不中断编排: 设计内时序F05在途拒绝无害(F05可能在忙补料状态机, hub时序等待+
          传感器确认循环兜底, 编排继续走)
        - 连续NACK>=3次记ERROR(提示排查F05侧持续拒绝的根因), 同样不中断编排
        """
        try:
            rejected_cmd = data.get('rejected_cmd', 0x00)
            reason_code = data.get('reason_code', 0x00)
            buffer_id = data.get('buffer_id', None)  # 任务#288对拍: 侧别0=左/1=右(framed格式/异常短帧无此键取None)
            frame_format = data.get('format', 'unknown')
            copy_context = getattr(self, '_copy_refill_context', None)
            if copy_context:
                if (rejected_cmd == 0x01 and buffer_id == copy_context.get('tube')
                        and self._copy_refill_is_current(copy_context)):
                    self._apply_feed_failure(4, reason_code, buffer_id, 0, '复制续料NACK')
                return
            self._f05_nack_count += 1
            # 任务#549: 供 _runout_clear_and_feed 的三段0x1E逐段判定(0x01=运动锁窗口外→F05已自行退料)
            self._last_f05_nack_reason = reason_code
                        
            # 任务#288对拍定稿: 原因码对齐F05侧(6c3da82已烧录)canserial.c实际定义——
            # 1=运动锁窗口外(自动流程/P0闸门生效), 2=电机忙(非空闲); 未知码原样透传显示
            reason_map = {
                0x01: "运动锁窗口外(自动流程/P0闸门生效)",
                0x02: "电机忙(非空闲)",
            }
            reason_desc = reason_map.get(reason_code, f"未知原因码(F05侧未定义, 原样透传0x{reason_code:02X})")
            # 侧别描述: None=帧内无侧别(framed/短帧), 0=左/1=右(F05裸格式定稿), 其他值防御性原样显示
            side_desc = "未知侧" if buffer_id is None else ("左" if buffer_id == 0 else "右" if buffer_id == 1 else f"侧{buffer_id}(非法值)")
                        
            self.logger.warning(f"收到F05 NACK(帧格式: {frame_format}, {side_desc}侧): 命令0x{rejected_cmd:02X}被F05拒绝, "
                                f"原因码0x{reason_code:02X}({reason_desc}), 本次编排内连续NACK={self._f05_nack_count}")
                        
            if self._f05_nack_count >= 3:
                self.logger.error(f"F05连续NACK已达{self._f05_nack_count}次(>=3), {side_desc}侧0x1E/0x01可能被F05持续拒绝, "
                                  f"请排查F05侧状态(运动锁/电机忙); 编排按设计继续(时序等待+传感器确认兜底)")
            # 刻意不中断编排: 见docstring处理策略(设计内时序F05在途拒绝无害)
        except Exception as e:
            self.logger.error(f"处理F05 NACK时发生错误: {e}", exc_info=True)
    
    async def _handle_feed_ack(self, data: dict):
        """
        lancaigang 260918 20:52:35 任务#296: 处理 F05 对 0x01 补料请求的回执帧。

        背景(#295): hub 曾对 0x01 盲发全程盲——回执通道物理存在无人消费。

        ★ 261019 任务#463 B(机制收敛): 本方法的判定逻辑全部下沉到 feed_ack_contract.py,
          这里只做"分类 -> 分发动作", 不再各处分叉 if。
          [1] 先识别探测回执(本轮 0x03 探测的响应, 反映 F01 当下真实状态)
          [2] 再对 0x01 回执调 classify_feed_ack(): 失败->失败锁 / 其余->挂 10s 统一裁决
          为什么不能直接用首帧判定: 0x00 可能是"中间态默认值"(旧固件), 0x02 可能是
          "刚置位后 373ms 就回落"(真机 11:14 直证) —— 两者都必须等探测才能定论。
        """
        try:
            seq = data.get('seq')
            status = data.get('status', 0x00)
            buffer_id = data.get('buffer_id', 0)
            error_code = data.get('error_code', 0)

            copy_context = getattr(self, '_copy_refill_context', None)
            if copy_context:
                # 纯runout不借用旧清缓冲裁决；只接收当前物理管和序号的失败。
                if (buffer_id == copy_context.get('tube') and seq == copy_context.get('seq')
                        and self._copy_refill_is_current(copy_context)):
                    self.logger.info(f"[COPY-REFILL] 回执 tube={buffer_id} seq={seq} status={status} error={error_code}")
                    if ack_contract.classify_feed_ack(status, error_code) == ack_contract.ACK_FAILED:
                        self._apply_feed_failure(status, error_code, buffer_id, seq, '复制续料回执')
                return

            # lancaigang261024104414 任务#554: CAN匹配seq+tube后仍须核当前事务，主动ERROR只触发新鲜查询。
            context = getattr(self, '_refill_context', None)
            if not self._refill_context_is_current(context) or buffer_id != context['tube']:
                return
            if data.get('unsolicited'):
                # lancaigang261025180000 任务#554-hotfix12(日志铁律): F05 主动 ERROR 帧是"本轮续料失败"的唯一
                # 首因边沿, 旧实现在这里一行日志都没有 —— 108 实测(17:23:30 打印头0)现场只能靠紧随其后的
                # 0x03 探测回执反推"是谁在什么时候报了错", 根本无法区分 F05 真失败与 hub 自己误判。
                task = getattr(self, '_error_probe_task', None)
                _probe_busy = task is not None and not task.done()
                _now = time.monotonic()
                if _now - getattr(self, '_unsolicited_error_log_at', 0.0) >= 5.0:
                    self._unsolicited_error_log_at = _now
                    self.logger.warning(
                        f"收到F05主动ERROR帧(续料失败首因边沿): status=0x{status:02X}, "
                        f"error=0x{error_code:02X}({self._error_code_name(error_code)}), seq={seq}, 管={buffer_id}, "
                        f"挤出机={context['extruder']}, 判定=新鲜0x03探测, 探测在途={_probe_busy}")
                if status == FeederCabinetCAN.STATUS_ERROR and not _probe_busy:
                    self._error_probe_task = asyncio.create_task(self._confirm_unsolicited_error(context))
                return
            if data.get('context') is not context or data.get('request_cmd') != 0x01:
                self.logger.debug("忽略无当前请求归属的续料回执")
                return

            # ===== [1]-[3] 0x01 回执: 查契约 -> 分发动作 =====
            action = ack_contract.classify_feed_ack(status, error_code)
            if action == ack_contract.ACK_FAILED:
                self._apply_feed_failure(status, error_code, buffer_id, seq, "0x01回执")
            elif action == ack_contract.ACK_ACCEPTED_PENDING:
                # 任务#462: 一旦受理就停掉"清缓冲+重发", 避免在续料已在途时再叠一层 0x1E。
                if self._buffer_clear_task and not self._buffer_clear_task.done():
                    self.logger.info(f"任务#462: F01已受理续料(seq={seq}), 停止缓冲清空重试(已执行{self._buffer_clear_round}轮)")
                    self._buffer_clear_task.cancel()
                    # 引用由该任务finally释放，避免迟到finally清掉新任务。
                self.logger.info(f"0x01回执: F05已受理续料(seq={seq}, 缓冲区={buffer_id}), 续料事务在途; "
                                 f"但0x02不代表持续(真机373ms即回落) -> 挂{FEED_VERDICT_DELAY_SECONDS:.0f}s后裁决")
                self._arm_feed_verdict(buffer_id, saw_accepted=True)
            elif action == ack_contract.ACK_NOT_ACCEPTED:
                # 任务#463 B: 不再"立即退料"(旧固件 0x00 可能是中间态默认值, 会退掉已送达的料),
                # 也不再"死等300s"(新固件下 0x00 确实表示未受理) —— 统一交探测裁决。
                self.logger.warning(f"0x01回执: F05当下不在续料事务中(0x00)(seq={seq}, 缓冲区={buffer_id}) -> "
                                    f"挂{FEED_VERDICT_DELAY_SECONDS:.0f}s后裁决(探测真实状态再决定是否自愈)")
                self._arm_feed_verdict(buffer_id, saw_accepted=False)
            else:
                self.logger.info(f"0x01回执: 未分类状态status=0x{status:02X}(seq={seq}, 缓冲区={buffer_id}, "
                                 f"error=0x{error_code:02X}), 仅记录不动作(契约未定义)")
            # 除失败锁外, 本方法不改状态机: 状态推进由裁决/超时兜底负责。
        except Exception as e:
            self.logger.error(f"处理0x01回执时发生错误: {e}", exc_info=True)

    def _arm_feed_verdict(self, buffer_id: int, saw_accepted: bool):
        """任务#463 B [3]: 给本次续料事务挂一次统一裁决(10s 后向 F01 问真实状态)。

        为什么不在回执当场下结论: 真机 261019 两次反证——
          · 10:46 旧固件对【已成功】的续料也回 0x00(中间态落 default) -> 当场退料会退掉好料;
          · 11:14 新固件回 0x02 后 373ms 就回落 IDLE(缓冲有料/本侧料盘 FRO 无料) -> 当场信 0x02 会死等。
        故 0x00 与 0x02 一视同仁: 都只当"待裁决", 由 10s 后的 0x03 探测定论。

        全局单实例: 已有在途裁决时只"升级受理标志"(0x02 是受理证据, 不会被后来的 0x00 抵消),
        不重复挂任务, 避免双探测/双自愈。
        """
        if self._auto_feed_failed or self.state_manager.state != SystemStateEnum.FEEDING:
            self.logger.info(f"任务#463: 不在FEEDING({self.state_manager.state.name}), 不挂裁决(缓冲区={buffer_id})")
            return
        if self._feed_verdict_task is not None and not self._feed_verdict_task.done():
            self._feed_verdict_saw_accepted = self._feed_verdict_saw_accepted or saw_accepted
            self.logger.info(f"任务#463: 已有在途裁决, 只更新受理标志={self._feed_verdict_saw_accepted}(缓冲区={buffer_id})")
            return
        self._feed_verdict_buffer = buffer_id
        self._feed_verdict_saw_accepted = saw_accepted
        self._feed_verdict_task = asyncio.create_task(self._feed_verdict(buffer_id))

    async def _feed_verdict(self, buffer_id: int):
        """任务#463 B [4]: 等 FEED_VERDICT_DELAY_SECONDS 后补发一次 0x03, 拿 F01 当下真实状态。

        时间参数依据: F01 从受理到回落 IDLE 仅 373ms, 正常进料全程 2~60s;
        10s 既跨过"瞬时置位"窗口, 又比被退休的 60s 观察窗快得多。
        无回执/发送失败 -> 放弃本次探测, 交回 300s 超时兜底(必须清标志, 忌误吞后续真回执)。
        """
        try:
            await asyncio.sleep(FEED_VERDICT_DELAY_SECONDS)
            if self.state_manager.state != SystemStateEnum.FEEDING:
                self.logger.info(f"任务#463: 裁决无需进行, 已离开FEEDING({self.state_manager.state.name})(缓冲区={buffer_id})")
                return
            probe_seq = await self.can_comm.query_feed_state(buffer_id)
            if not probe_seq:
                self.logger.warning(f"任务#463: 状态查询发送失败, 放弃本次裁决, 交回FEEDING超时兜底(缓冲区={buffer_id})")
                return
            self._feed_verdict_probe_seq = probe_seq
            self.logger.info(f"任务#463: 已发裁决探测(seq={probe_seq}, 缓冲区={buffer_id}), 等 F01 回当下状态")
            await asyncio.sleep(FEED_VERDICT_REPLY_TIMEOUT_SECONDS)
            if self._feed_verdict_probe_seq == probe_seq:
                self.logger.warning(f"任务#463: 探测{FEED_VERDICT_REPLY_TIMEOUT_SECONDS:.0f}s无回执, 放弃本次裁决, "
                                    f"交回FEEDING超时兜底(缓冲区={buffer_id})")
                self._feed_verdict_probe_seq = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(f"任务#463: 裁决异常: {e}", exc_info=True)
        finally:
            self._feed_verdict_task = None
            self._feed_verdict_buffer = None
            self._feed_verdict_saw_accepted = False

    def _apply_verdict(self, status: int, error_code: int, buffer_id: int, probe_seq: int):
        """任务#463 B [5]: 用探测到的真实状态裁决动作(HEAL/PROGRESS/FAIL/OBSERVE)。

        HEAL 的充要条件: 【曾受理(收到过 0x02)】且【10s 后已不在续料事务里】。
          - 新固件正常进料中 -> 探测回 0x02 -> PROGRESS, 不动;
          - 新固件已完成 -> 探测回 0x03 -> PROGRESS, 不动;
          - 死锁(缓冲有料/料盘无料) -> 0x02 后回落 0x00 -> HEAL, 进唯一自愈路径;
          - 旧固件(从未回 0x02) -> 探测回 0x00 也只会 OBSERVE, 不退料(兼容安全网)。
        """
        if getattr(self, '_copy_refill_context', None):
            return
        saw_accepted = self._feed_verdict_saw_accepted
        verdict = ack_contract.decide_verdict(status, error_code, saw_accepted)
        self.logger.info(f"任务#463: 裁决回执 status=0x{status:02X} error=0x{error_code:02X} "
                         f"曾受理={saw_accepted} -> {verdict}(缓冲区={buffer_id}, seq={probe_seq})")
        if verdict == ack_contract.VERDICT_HEAL:
            if (not self._auto_feed_failed and self.state_manager.state == SystemStateEnum.FEEDING
                    and (self._buffer_clear_task is None or self._buffer_clear_task.done())):
                self.logger.warning(f"任务#463: F01 曾受理但{FEED_VERDICT_DELAY_SECONDS:.0f}s后已不在续料"
                                    f"(缓冲有料/本侧料盘无料) -> 清缓冲自愈(唯一自愈路径)")
                self._buffer_clear_task = asyncio.create_task(self._clear_buffer_and_refeed(buffer_id))
            else:
                self.logger.warning("任务#463: 判定需清缓冲自愈, 但状态已离开FEEDING或已有清缓冲在途, 跳过")
        elif verdict == ack_contract.VERDICT_FAIL:
            self._apply_feed_failure(status, error_code, buffer_id, probe_seq, "裁决探测")
        elif verdict == ack_contract.VERDICT_PROGRESS:
            self.logger.info("任务#463: F01 确在续料/已完成, 不动")
        else:
            self.logger.warning(f"任务#463: 裁决信息不足(曾受理={saw_accepted}), 只记录不动作, "
                                f"交回FEEDING {FEEDING_TIMEOUT_SECONDS:.0f}s超时兜底")

    # lancaigang261024104414 任务#554: 首因/原管保存在现有失败锁所属context，不建立平行状态机。
    @staticmethod
    def _current_async_task():
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _begin_refill_context(self, extruder, tube):
        # 只核本侧映射归属; 是否活跃由各入口(_handle_low_weight_notify/_runout)先行把关,
        # 回执匹配阶段再由 _refill_context_is_current 复核 active_extruder。
        if (self._auto_feed_failed or extruder not in (0, 1) or tube not in (0, 1)
                or self.config.get('extruders', {}).get('mapping', {}).get(extruder) != tube):
            self.logger.warning(f"任务#554-hotfix11: 续料事务上下文无法建立(失败锁={self._auto_feed_failed}, "
                                f"挤出机={extruder}, 料管={tube}, "
                                f"映射={self.config.get('extruders', {}).get('mapping', {})})")
            return None
        context = getattr(self, '_refill_context', None)
        if context is not None:
            if self._refill_context_is_current(context):
                return context
            # lancaigang261025173000 任务#554-hotfix11: 陈旧事务上下文必须当场丢弃重建, 不能直接 return None。
            # 旧实现只在失败锁/映射不符时打日志, 走到这里返回 None 全程静默; 而 _refill_context 从创建到
            # 进程结束都没有任何清零点(只有 __init__), epoch 却在进入 PRINTING/IDLE/ERROR 时 +1。
            # 于是"上一次续料的事务上下文"在下一轮打印里永远不等于当前 epoch -> 每次 PAUSED 缺料轮询都
            # 静默 return -> 自动续料彻底不动。108 实测(16:59:18 剪断槽4): 传感器已无料、Klipper 已 PAUSED、
            # hub 每 5 秒打一条"触发补料请求", 之后没有任何编排输出, 3 分钟毫无动作。
            self.logger.info(f"任务#554-hotfix11: 丢弃陈旧续料事务上下文(挤出机={context.get('extruder')}, "
                             f"料管={context.get('tube')}, epoch={context.get('epoch')}->{self._refill_failure_epoch}, "
                             f"当前活跃挤出机={getattr(self.klipper_monitor, 'active_extruder', None)}), 按本次缺料重建")
            self._refill_context = None
        context = {'extruder': extruder, 'tube': tube, 'epoch': self._refill_failure_epoch,
                   'job_key': self._job_key_from_print_stats(
                       getattr(self.klipper_monitor, 'print_stats', {}) or {})}
        self._refill_context = context
        self._buffer_clear_round = 0
        return context

    def _refill_context_is_current(self, context):
        if (context is None or context is not getattr(self, '_refill_context', None)
                or self._auto_feed_failed or context['epoch'] != self._refill_failure_epoch):
            return False
        return (self.klipper_monitor.active_extruder == context['extruder']
                and self.config.get('extruders', {}).get('mapping', {}).get(context['extruder']) == context['tube']
                and self.state_manager.state in (SystemStateEnum.PRINTING, SystemStateEnum.PAUSED,
                    SystemStateEnum.RUNOUT, SystemStateEnum.FEEDING, SystemStateEnum.RESUMING))

    def _probe_feeding_duplicate(self, tube: int):
        """任务#554: FEEDING 内重发只补一次定向查询留证，限频避免每 30s 重发打满 CAN。"""
        now = time.monotonic()
        last = getattr(self, '_feeding_dup_probe_at', None)
        if last is not None and now - last < FEEDING_DUP_PROBE_INTERVAL_SECONDS:
            return
        self._feeding_dup_probe_at = now
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._run_feeding_duplicate_probe(tube))

    async def _run_feeding_duplicate_probe(self, tube: int):
        try:
            seq = await self.can_comm.query_feed_state(tube)
            self.logger.info(f"在途重发查询已发出(管={tube}, seq={seq})，仅留证不重启编排(任务#554)")
        except Exception as e:
            self.logger.info(f"在途重发查询失败(管={tube}): {e}")

    def _fail_current_refill(self, source, error_code=FeederCabinetCAN.ERROR_OTHER):
        context = getattr(self, '_refill_context', None)
        if context is None:
            extruder = self.klipper_monitor.active_extruder
            tube = self.config.get('extruders', {}).get('mapping', {}).get(extruder)
            context = self._begin_refill_context(extruder, tube)
        if context is not None:
            self._apply_feed_failure(FeederCabinetCAN.STATUS_ERROR, error_code, context['tube'], 0, source)

    @staticmethod
    def _error_code_name(code):
        """F05 回执 error_code 的中文语义(与 F05 协议同名常量), 只用于日志定性, 不参与判定。"""
        return {0x00: '无错误', 0x01: '机械错误', 0x02: '材料缺失(无可换新料盘/新盘无料)',
                0x03: '其他错误', 0x04: 'Klipper错误', 0x05: 'Moonraker错误',
                0x06: '通信错误'}.get(code, '未知错误码')

    def _apply_feed_failure(self, status: int, error_code: int, buffer_id: int, seq: int, source: str):
        """已核归属的失败同步封口；停止确认独立于被取消的业务任务。"""
        context = getattr(self, '_refill_context', None)
        if context is None:
            # 任务#554-copy108: COPY 双头纯runout尚未并入统一事务, 单独走兼容收口;
            # 否则合并后复制链路失败既不锁也不停, 会退回"反复重投"。
            copy_context = getattr(self, '_copy_refill_context', None)
            if copy_context is not None and not self._auto_feed_failed:
                self._apply_copy_feed_failure(status, error_code, buffer_id, seq, source)
            return
        if context['tube'] != buffer_id or self._auto_feed_failed:
            return
        self._auto_feed_failed = True
        self._refill_failure_epoch += 1
        context.update(error=error_code or FeederCabinetCAN.ERROR_OTHER, source=source,
                       failure_seq=seq, stop_confirmed=False)
        self._pending_feed_after_can = None
        self._feed_verdict_probe_seq = None
        self.can_comm.invalidate_feed_requests(buffer_id)
        pending = self._cancel_refill_tasks(source, include_orchestration=True)
        self._feeding_giveup_logged = True
        self._pause_orchestrated = False
        self._stuck_recover_since = None
        self._hub_resume_at = None
        self.logger.error(f"{source}: 本轮续料失败锁定(status=0x{status:02X}, "
                          f"error=0x{context['error']:02X}({self._error_code_name(context['error'])}), "
                          f"seq={seq}, 管={buffer_id}, 挤出机={context['extruder']}); 停止并等待打印侧人工恢复")
        self.state_manager.transition_to(SystemStateEnum.PAUSED, extruder=context['extruder'])
        self._save_runtime_state("续料失败首因", force=True)
        self._ensure_failure_stop(pending)

    def _apply_copy_feed_failure(self, status: int, error_code: int, buffer_id: int, seq: int, source: str):
        """任务#554-copy108: 复制纯runout失败收口(任务#442语义), 失败即停并锁定等人工。

        #554 的统一事务 _refill_context 只覆盖共享状态机的单侧编排; COPY 双头序列仍在
        _copy_refill_context 上运行, 故保留独立入口, 避免合并后复制链路失败被静默丢弃。
        """
        copy_context = getattr(self, '_copy_refill_context', None)
        if self._auto_feed_failed or copy_context is None:
            return
        _err_name = {FeederCabinetCAN.ERROR_MECHANICAL: "机械错误",
                     FeederCabinetCAN.ERROR_MATERIAL_MISSING: "材料缺失(如无兼容候选)",
                     }.get(error_code, f"其他错误(0x{error_code:02X})")
        self._auto_feed_failed = True
        self._refill_failure_epoch = getattr(self, '_refill_failure_epoch', 0) + 1
        copy_context['interrupted'] = True
        copy_context['error'] = error_code or FeederCabinetCAN.ERROR_OTHER
        # lancaigang261024191500 任务#554-copy108-hotfix14: 这里刻意不清 _copy_refill_context。
        # 该对象仍被编排 finally 用于收口(RESUMING→PAUSED 兜底靠 `is context` 身份比对),
        # 提前清会让失败态卡在 RESUMING(回归 test_copy_output_failure_is_terminal 红)。
        # 改为在 _copy_low_weight_refill 门口按 interrupted/complete 判活:
        # 已终止事务不再拦后续低重(108 实测 19:09:13/19:09:43 丢两次槽1低重)。
        self._pending_feed_after_can = None
        self._feed_verdict_probe_seq = None
        current_task = asyncio.current_task()
        for name in ('_refill_orchestration_task', '_buffer_clear_task', '_feed_verdict_task', '_resume_task'):
            task = getattr(self, name, None)
            if task is not None and task is not current_task and not task.done():
                task.cancel()
                self.logger.info(f"自动续料失败：终止在途任务 {name}，禁止旧轮次续发")
        self._feeding_giveup_logged = True
        self._pause_orchestrated = False
        self._reset_auto_refill_state("复制续料失败锁定", full=True)
        if seq == 0 and buffer_id is None:
            # hotfix13: 本地编排中止(退料/归属/预挤)没有设备回执, 旧日志把它写成
            # "COPY补料请求失败(status=0x04, error=0x00, seq=0, 缓冲区=None)", 现场会误判成
            # F05 报了错。这里分开表述, 设备回执只在实际拿到 seq/buffer 时才打。
            self.logger.error(f"{source}: 本地中止(无F05回执, 非设备报错) "
                              f"-> 判定本次自动续料失败并锁定(任务#442/#554)")
        else:
            self.logger.error(f"{source}: COPY补料请求失败(status=0x{status:02X}, "
                              f"error=0x{error_code:02X}({_err_name}), "
                              f"seq={seq}, 缓冲区={buffer_id}) -> 判定本次自动续料失败并锁定(任务#442/#554)")
        if self.state_manager.state == SystemStateEnum.FEEDING:
            self.state_manager.transition_to(SystemStateEnum.PAUSED,
                                             extruder=self.klipper_monitor.active_extruder if self.klipper_monitor else 0)
        self._save_runtime_state("复制续料失败首因", force=True)

    def _ensure_failure_stop(self, pending=()):
        context = getattr(self, '_refill_context', None)
        if not self._auto_feed_failed or context is None:
            return
        task = getattr(self, '_failure_stop_task', None)
        if task is not None and not task.done():
            return
        capability = (self.can_comm._connection_generation, self.can_comm.f05_protocol_version)
        if context.get('stop_attempt') == capability:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 同步状态采样可先锁失败；下一次事件循环再收口。
        context['stop_attempt'] = capability
        self._failure_stop_task = asyncio.create_task(
            self._stop_failed_refill(context, self._refill_failure_epoch, pending))

    async def _stop_failed_refill(self, context, epoch, pending):
        def valid():
            return (self._auto_feed_failed and self._refill_context is context
                    and self._refill_failure_epoch == epoch)

        def confirmed(reply):
            return (reply is not None and reply.get('buffer_id') == context['tube']
                    and reply.get('status') == FeederCabinetCAN.STATUS_ERROR)

        try:
            if not valid():
                return
            if (self.can_comm.f05_protocol_version or 0) < 3:
                self.logger.warning(f"管{context['tube']}停止未确认：F05协议V3能力未知或不支持")
                return
            reply = await self.can_comm.terminate_feed(
                context['tube'], context['error'], context=context, valid=valid)
            if not valid():
                return
            if not confirmed(reply):
                reply = await self.can_comm.query_feed_state_response(
                    context['tube'], context=context, valid=valid)
                if not valid():
                    return
            context['stop_confirmed'] = confirmed(reply)
            self.logger.warning(f"管{context['tube']}失败收口："
                                f"{'终止回执已确认' if context['stop_confirmed'] else '停止未确认'}，保持暂停，不重新续料")
            self._save_runtime_state("失败停止结果", force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(f"失败停止未确认: {exc}", exc_info=True)
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self._failure_stop_task is self._current_async_task():
                self._failure_stop_task = None

    async def _confirm_unsolicited_error(self, context):
        try:
            valid = lambda: self._refill_context_is_current(context)
            reply = await self.can_comm.query_feed_state_response(
                context['tube'], context=context, valid=valid)
            if (valid() and reply is not None and reply.get('context') is context
                    and reply.get('buffer_id') == context['tube']
                    and ack_contract.classify_feed_ack(reply['status'], reply['error_code']) == ack_contract.ACK_FAILED):
                self._apply_feed_failure(reply['status'], reply['error_code'], context['tube'],
                                         reply['seq'], "主动ERROR新鲜查询确认")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(f"主动ERROR查询未确认，保持当前暂停/期限: {exc}")
        finally:
            if self._error_probe_task is self._current_async_task():
                self._error_probe_task = None

    async def _clear_buffer_and_refeed(self, buffer_id: int):
        """任务#462: 排除缓冲器"假有料"后重发续料请求(任务#463 B 起为唯一自愈路径)。

        为什么需要(261019 双侧日志直证, 非推测):
          剪断点在缓冲器下游时, 缓冲内残留一段"死料"——上游已断, 它永远不会被拉走,
          却一直顶住缓冲缺料开关(GPIO NO_FIL2 -> drybox.no_fil_status), 使 F01 长期判
          "缓冲区有料", 对 0x01 回 IDLE(0x00) 且不换盘。实测该状态持续 4分45秒,
          人工捅一下缓冲器就立即变无料并续料成功 —— 证明卡点就在这段死料。

        动作序列(复用既有 0x1E, 不新增协议):
          0x1E 50mm@10mm/s -> 等 5.5s -> 0x1E 50mm@60mm/s -> 等 5.5s
          -> 0x1E 20mm@10mm/s -> 等 2s -> 重发 0x01 -> 等回执 8s
        最多 BUFFER_CLEAR_MAX_ROUNDS 轮; 任一轮 F01 受理(回 STATUS_FEEDING)会被
        _handle_feed_ack 立即 cancel 本任务。全部失败则交回 FEEDING 300s 超时兜底。

        影响面:
          - 唯一入口: _apply_verdict() 的 VERDICT_HEAL(已用 0x03 探测确认"曾受理但当下不在续料");
            不再由回执当场触发, 故本方法自身不再需要观察窗(旧 60s 窗已退休);
          - 全局单实例, 不破现有编排串行不变量;
          - 退掉的是缓冲内残料(该场景下本就是死料), 不会退走已进打印头的料。
        """
        self._buffer_clear_round = 0
        try:
            # 入口再确认一次(裁决与任务调度之间可能差几毫秒), 避免在续料已推进时误退料。
            if self._auto_feed_failed or self.state_manager.state != SystemStateEnum.FEEDING:
                self.logger.info(f"任务#463: 进入清缓冲时状态已离开FEEDING({self.state_manager.state.name}), "
                                 f"放弃自愈(缓冲区={buffer_id})")
                return
            for round_no in range(1, BUFFER_CLEAR_MAX_ROUNDS + 1):
                if self.state_manager.state != SystemStateEnum.FEEDING:
                    self.logger.info(f"任务#462: 状态已离开FEEDING({self.state_manager.state.name}), 中止清缓冲(第{round_no}轮前)")
                    return
                self._buffer_clear_round = round_no
                self.logger.warning(f"任务#462: 第{round_no}/{BUFFER_CLEAR_MAX_ROUNDS}轮清缓冲: "
                                    f"0x1E退料(缓冲区={buffer_id}) 排除死料顶住缺料开关")
                if not await self.can_comm.retract_filament(buffer_id, distance=4, speed=0):
                    self.logger.warning("任务#462: 0x1E(50mm@10mm/s)发送失败, 本轮继续")
                await asyncio.sleep(5.5)
                if not await self.can_comm.retract_filament(buffer_id, distance=4, speed=5):
                    self.logger.warning("任务#462: 0x1E(50mm@60mm/s)发送失败, 本轮继续")
                await asyncio.sleep(5.5)
                # 末段小退料: 让残料彻底脱离开关行程(实测人工捅一下即可恢复, 相当于这段位移)
                if not await self.can_comm.retract_filament(buffer_id, distance=3, speed=0):
                    self.logger.warning("任务#462: 0x1E(20mm@10mm/s)发送失败, 本轮继续")
                await asyncio.sleep(2.0)
                if self.state_manager.state != SystemStateEnum.FEEDING:
                    self.logger.info(f"任务#462: 状态已离开FEEDING({self.state_manager.state.name}), 中止清缓冲(第{round_no}轮退料后)")
                    return
                self.logger.info(f"任务#462: 第{round_no}轮清缓冲完成, 重发0x01")
                if not await self.can_comm.request_feed(buffer_id):
                    self.logger.error("任务#462: 重发0x01失败")
                # 等回执: F01 受理会回 STATUS_FEEDING 并在 _handle_feed_ack 中 cancel 本任务
                await asyncio.sleep(BUFFER_CLEAR_ACK_WAIT_SECONDS)
            self.logger.error(f"任务#462: 清缓冲+重发已重试{BUFFER_CLEAR_MAX_ROUNDS}轮仍未受理, "
                              f"交回FEEDING {FEEDING_TIMEOUT_SECONDS:.0f}s超时兜底(缓冲开关可能未复位或存在其他机械卡滞)")
        except asyncio.CancelledError:
            self.logger.info("任务#462: 清缓冲任务被取消(F01已受理续料或状态已切换)")
            raise
        except Exception as e:
            self.logger.error(f"任务#462: 清缓冲+重发异常: {e}", exc_info=True)

    async def _process_filament_data(self, extruder_id: int, filament_id: int,
                                  data: OpenTagFilamentData):
        """
        处理解析后的耗材数据
        
        Args:
            extruder_id: 挤出机ID
            filament_id: 耗材通道ID
            data: OpenTag格式的耗材数据
        """
        try:
            self.logger.info("=" * 60)
            self.logger.info(f"挤出机 {extruder_id} 耗材信息 (通道 {filament_id}):")
            self.logger.info("=" * 60)
            
            # 基本信息
            self.logger.info(f"制造商: {data.manufacturer}")
            self.logger.info(f"材料类型: {data.material_name}")
            self.logger.info(f"颜色: {data.color_name}")
            
            # 规格参数
            self.logger.info(f"直径: {data.diameter_target/1000:.2f} mm")
            self.logger.info(f"标称重量: {data.weight_nominal} g")
            self.logger.info(f"密度: {data.density/1000:.3f} g/cm³")
            
            # 温度参数
            self.logger.info(f"打印温度: {data.print_temp}°C")
            self.logger.info(f"热床温度: {data.bed_temp}°C")
            
            # 可选参数
            if data.serial_number:
                self.logger.info(f"序列号: {data.serial_number}")
                
            if data.empty_spool_weight is not None:
                self.logger.info(f"空线轴重量: {data.empty_spool_weight} g")
                
            if data.filament_weight_measured is not None:
                self.logger.info(f"实测耗材重量: {data.filament_weight_measured} g")
                
            if data.filament_length_measured is not None:
                self.logger.info(f"实测耗材长度: {data.filament_length_measured} m")
                
            if data.max_dry_temp is not None:
                self.logger.info(f"最大干燥温度: {data.max_dry_temp}°C")
                
            if data.color_hex is not None:
                self.logger.info(f"颜色值: #{data.color_hex:06X}")
                
            self.logger.info("=" * 60)
            
            # 缓存耗材数据
            self._rfid_data_cache[extruder_id] = {
                'timestamp': time.time(),
                'filament_id': filament_id,
                'data': data
            }
            
            # 保存耗材信息到文件（可选）
            await self._save_filament_info(extruder_id, data)
            
            # 同步到Spoolman
            if (self.spoolman_client and 
                self.config.get('spoolman', {}).get('auto_sync_rfid', True)):
                await self._sync_to_spoolman(extruder_id, data)
            
            # 如果配置了自动设置温度，应用耗材温度设置
            if self.config.get('rfid', {}).get('auto_set_temperature', False):
                await self._apply_filament_temperature(extruder_id, data)
                
        except Exception as e:
            self.logger.error(f"处理耗材数据时发生错误: {e}", exc_info=True)
    
    async def _save_filament_info(self, extruder_id: int, data: OpenTagFilamentData):
        """
        保存耗材信息到文件
        
        Args:
            extruder_id: 挤出机ID
            data: 耗材数据
        """
        try:
            # 确保目录存在
            data_dir = self.config.get('rfid', {}).get('data_dir', '/home/mingda/printer_data/rfid')
            os.makedirs(data_dir, exist_ok=True)
            
            # 构建文件路径
            filename = os.path.join(data_dir, f'filament_extruder_{extruder_id}.json')
            
            # 准备数据
            info = {
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'extruder_id': extruder_id,
                'manufacturer': data.manufacturer,
                'material': data.material_name,
                'color': data.color_name,
                'diameter': data.diameter_target / 1000,  # 转换为mm
                'weight_nominal': data.weight_nominal,
                'density': data.density / 1000,  # 转换为g/cm³
                'print_temp': data.print_temp,
                'bed_temp': data.bed_temp,
                'serial_number': data.serial_number,
                'empty_spool_weight': data.empty_spool_weight,
                'max_dry_temp': data.max_dry_temp
            }
            
            # 保存到文件
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(info, f, indent=2, ensure_ascii=False)
                
            self.logger.info(f"耗材信息已保存到: {filename}")
            
        except Exception as e:
            self.logger.error(f"保存耗材信息时发生错误: {e}", exc_info=True)
    
    async def _apply_filament_temperature(self, extruder_id: int, data: OpenTagFilamentData):
        """
        应用耗材温度设置到打印机
        
        Args:
            extruder_id: 挤出机ID
            data: 耗材数据
        """
        try:
            if not self.klipper_monitor or not self.klipper_monitor.ws_connected:
                self.logger.warning("Klipper未连接，无法应用温度设置")
                return
                
            # 设置挤出机温度
            if extruder_id == 0:
                gcode = f"M104 T0 S{data.print_temp}"
            else:
                gcode = f"M104 T1 S{data.print_temp}"
            
            await self.klipper_monitor.execute_gcode(gcode)
            self.logger.info(f"已设置挤出机{extruder_id}温度为 {data.print_temp}°C")
            
            # 设置热床温度（仅对挤出机0设置）
            if extruder_id == 0:
                gcode = f"M140 S{data.bed_temp}"
                await self.klipper_monitor.execute_gcode(gcode)
                self.logger.info(f"已设置热床温度为 {data.bed_temp}°C")
                
        except Exception as e:
            self.logger.error(f"应用温度设置时发生错误: {e}", exc_info=True)
    
    async def request_rfid_data(self, extruder_id: int):
        """
        主动请求指定挤出机的RFID数据
        
        Args:
            extruder_id: 挤出机ID
        """
        try:
            if not self.can_comm or not self.can_comm.connected:
                self.logger.warning("CAN未连接，无法请求RFID数据")
                return
                
            self.logger.info(f"请求挤出机 {extruder_id} 的RFID数据...")
            
            if await self.can_comm.request_rfid_data(extruder_id):
                self.logger.info("RFID数据请求已发送")
            else:
                self.logger.error("RFID数据请求发送失败")
                
        except Exception as e:
            self.logger.error(f"请求RFID数据时发生错误: {e}", exc_info=True)
    
    async def _sync_to_spoolman(self, extruder_id: int, data: OpenTagFilamentData):
        """
        将耗材数据同步到Spoolman
        
        Args:
            extruder_id: 挤出机ID
            data: 耗材数据
        """
        try:
            self.logger.info(f"开始同步挤出机 {extruder_id} 的耗材数据到Spoolman...")
            
            # 获取重试配置
            retry_count = self.config.get('spoolman', {}).get('retry_count', 3)
            retry_interval = self.config.get('spoolman', {}).get('retry_interval', 5.0)
            
            # 尝试同步
            for attempt in range(retry_count):
                try:
                    async with self.spoolman_client:
                        result = await self.spoolman_client.sync_rfid_to_spoolman(
                            data, extruder_id=extruder_id
                        )
                        
                    self.logger.info(f"成功同步到Spoolman: 卷轴ID={result['spool_id']}, "
                                   f"耗材={result['filament_name']}, "
                                   f"序列号={result['serial_number']}")
                    
                    # 保存Spoolman ID映射
                    mapping_file = os.path.join(
                        self.config.get('rfid', {}).get('data_dir', '/home/mingda/printer_data/rfid'),
                        f'spoolman_mapping_extruder_{extruder_id}.json'
                    )
                    
                    with open(mapping_file, 'w', encoding='utf-8') as f:
                        json.dump(result, f, indent=2, ensure_ascii=False)
                        
                    return
                    
                except Exception as e:
                    self.logger.warning(f"同步到Spoolman失败 (尝试 {attempt + 1}/{retry_count}): {e}")
                    if attempt < retry_count - 1:
                        await asyncio.sleep(retry_interval)
                        
            self.logger.error("同步到Spoolman最终失败")
            
        except Exception as e:
            self.logger.error(f"同步到Spoolman时发生错误: {e}", exc_info=True)
    
    async def _cleanup_rfid_sessions(self):
        """定期清理超时的RFID传输会话"""
        cleanup_interval = 60  # 每60秒清理一次
        
        while self.state_manager.state != SystemStateEnum.DISCONNECTED:
            try:
                await asyncio.sleep(cleanup_interval)
                
                if self.rfid_parser:
                    self.rfid_parser.cleanup_expired_sessions()
                    self.logger.debug("已执行RFID会话清理")
                    
            except asyncio.CancelledError:
                self.logger.info("RFID会话清理任务被取消")
                break
            except Exception as e:
                self.logger.error(f"清理RFID会话时发生错误: {e}", exc_info=True)
                
        self.logger.info("RFID会话清理任务结束")
    
    async def _handle_klipper_status_update(self, status: Dict[str, Any]):
        """处理来自KlipperMonitor的状态更新，并驱动状态机"""
        try:
            # lancaigang261019 任务#459: ERROR自愈看门狗(每帧检查, 不依赖状态变化边沿)
            await self._maybe_selfheal_can_error()

            # lancaigang261019113900 任务#463: 增量帧缺 print_stats 时用 monitor 缓存的权威值补一帧。
            # 为什么必须在入口补(261019 11:28-11:33 真机直证): Moonraker 只推变化字段, 心跳帧里
            # klippy 长期为 None; 而失败锁解锁/PRINTING 同步全靠 print_stats.state 的变化边沿 ->
            # 打印恢复了 hub 仍卡 PAUSED+锁死, 后续断料被静默拦截, 现场就是"续料没反应"。
            # 补齐后下游逻辑完全不变(仍有变化边沿判定, 不会重复触发), 也不新增任何状态。
            if 'print_stats' not in status and self.klipper_monitor is not None:
                _auth_ks = getattr(self.klipper_monitor, 'printer_state', None)
                if _auth_ks:
                    status = dict(status)
                    status['print_stats'] = {'state': _auth_ks}

            # 记录收到的状态更新
            self.logger.debug(f"收到状态更新，包含的键: {list(status.keys())}")
            if 'print_stats' in status:
                self.logger.debug(f"收到Klipper状态更新: print_stats={status.get('print_stats', {}).get('state')}")
                self.logger.debug(f"当前缓存的打印机状态: {self._last_printer_state}")
            else:
                self.logger.debug("状态更新中没有print_stats")
            
            # 首先更新当前活跃的挤出机，确保后续逻辑使用最新的活跃挤出机信息
            if 'toolhead' in status and 'extruder' in status['toolhead']:
                active_extruder_name = status['toolhead']['extruder']
                new_active_extruder = 0 if active_extruder_name == 'extruder' else 1
                if self.klipper_monitor.active_extruder != new_active_extruder:
                    self.logger.info(f"[IMPORTANT] 活跃挤出机切换: {self.klipper_monitor.active_extruder} -> {new_active_extruder} (名称: {active_extruder_name})")
                    # lancaigang 260915 201652 治本修复辅方案A-1(开场误暂停): 打印中换头重置20秒宽限期,
                    # 作为主方案(首次进料门禁)的兜底, 防换头瞬间新活跃头料道状态未稳/传感器竞态误判断料
                    if self.state_manager.state == SystemStateEnum.PRINTING:
                        self._printing_started_at = time.monotonic()
                        self._grace_runout_logged = False
                        self.logger.info("打印中发生换头, 重新进入20秒断料判定宽限期(防换头竞态误暂停)")
                self.klipper_monitor.active_extruder = new_active_extruder
                self.logger.debug(f"[DEBUG] 更新活跃挤出机: active_extruder_name={active_extruder_name}, active_extruder={new_active_extruder}")

            # lancaigang261017 部署/重启同步: 启动时 moonraker 未就绪导致任务身份缺失、快照暂未应用时,
            # 首帧状态就绪后补应用一次(应用成功即释放快照; 任务不一致则在内部丢弃)。
            if getattr(self, '_runtime_snapshot', None) and status.get('print_stats'):
                if self._apply_runtime_state(self.klipper_monitor.get_printer_status()):
                    self.logger.info("首帧状态就绪, 已补应用运行态快照")
            
            # lancaigang 260915 201655 治本修复主方案A-2(开场误暂停): 从motion_report检测活跃挤出机进料运动
            # (live_extruder_velocity>0正向E轴运动, 口径与KlipperMonitor._handle_extruder_motion_update一致), 置位进料门禁
            if 'motion_report' in status:
                _motion_data = status.get('motion_report') or {}
                _extruder_velocity = _motion_data.get('live_extruder_velocity', 0.0)
                # 任务#447b: 加最小速度下限, 防噪声级微正速度(0.00x mm/s)把"已进料"提前置位,
                # 否则开场缓发 PAUSE 的门禁形同虚设(升温/调平期又会下发 PAUSE 并迟到生效)。
                if _extruder_velocity > FED_SEEN_MIN_VELOCITY:
                    _fed_idx = self.klipper_monitor.active_extruder
                    if not self._extruder_fed_seen.get(_fed_idx, False):
                        self._extruder_fed_seen[_fed_idx] = True
                        self.logger.info(f"挤出机 {_fed_idx} 首次检测到进料运动(速度: {_extruder_velocity:.2f} mm/s), 首次进料前开场流程宽限解除, 断料保护完全生效")
            
            # 解析打印机状态
            # lancaigang261013213856 任务#433: 预置klipper_state防#388自愈块UnboundLocalError
            # (Klipper增量帧无print_stats时该变量未赋值, 自愈块引用抛异常致兜底二次失效)
            klipper_state = None
            if 'print_stats' in status:
                klipper_state = status['print_stats'].get('state')
                # 仅在状态实际改变时记录日志和转换
                if klipper_state:
                    if klipper_state != self._last_printer_state:
                        self.logger.info(f"检测到Klipper状态变化: {self._last_printer_state} -> {klipper_state}")
                        # lancaigang261025103309 任务#563: 只记真实 printing->paused 边沿(重连/重启回填无边沿=来源不明);
                        # 边沿前已转无料的头(断料先于暂停到达)此刻认定断料暂停, 其后分帧到达的由传感器段补认。
                        self._pause_edge_at = (time.monotonic() if (self._last_printer_state == 'printing'
                                                                    and klipper_state == 'paused') else None)
                        self._note_pause_edge_vacancy('暂停边沿')
                        self._last_printer_state = klipper_state
                        
                        # 主动发送打印状态通知给送料柜
                        asyncio.create_task(self._send_printer_status_notification(klipper_state))
                        # lancaigang261015 qoder-agent-f01-leader 任务#442(修订): 失败锁解除时机修正
                        # 检测到打印恢复/新一轮打印(klipper=printing)即解除锁并复位重试计数。
                        # 背景: 失败锁把 hub 压到 PAUSED, 而 hub 回 PRINTING 仅认 {IDLE,RESUMING},
                        # 本流程永远够不到 → 锁粘死使后续断料被静默跳过("没反应")。插盘但不打印仍不解锁。
                        if klipper_state == 'printing' and self._auto_feed_failed:
                            self.logger.info("自动续料失败锁解除(检测到打印恢复 printing, 任务#442)")
                            self._auto_feed_failed = False
                            self._feeding_giveup_logged = False
                            self._feeding_timeout_retry_count = 0
                            # lancaigang261024200000 任务#554-copy108-hotfix16: 解锁=上一轮编排已作废,
                            # 低重借道标志必须同帧复位。它是会话级承重墙(只在进入新打印时复位),
                            # 而失败锁在场时 _reset_auto_refill_state 首行即 return 从不清它, 于是
                            # "失败锁已解除但 pending 仍 True"这一组合会让回切/纯runout 判据永久不放行,
                            # 反而把 hotfix15 想消灭的盲区重新焊死。
                            if getattr(self, '_low_weight_refill_pending', False):
                                self._low_weight_refill_pending = False
                                self.logger.info("失败锁解除同帧复位低重借道标志(_low_weight_refill_pending, 任务#554-hotfix16)")
                            # lancaigang261024191500 任务#554-copy108-hotfix14: 解锁必须同帧回切状态机。
                            # 具体回切由下方统一的 _can_restore_printing 分支完成(hotfix15 合并),
                            # 避免两处判据分叉。
                        
                        # lancaigang261024193500 任务#554-copy108-hotfix15: 回 PRINTING 白名单补 PAUSED。
                        # hotfix14 只在"失败锁解除"分支里纠偏, 覆盖不到无锁场景: 108 实测 19:24:25
                        # Klipper paused->printing(人为恢复, 失败锁=False) 时不进解锁分支, PAUSED 仍被
                        # 白名单挡住, hub 与设备分叉 30s(19:24:25~19:24:55), 靠低重事件才自然推进;
                        # 无事件时则退化为任务#443 两周期去抖(19:08:32→19:10:01 的 89s 盲区)。
                        # 放行必须排除在途编排: 低重/runout 编排中途 hub 自己发过 PAUSE, 此时 Klipper
                        # 报 printing 只是 PAUSE 未生效的残帧, 回切会与编排打架, 且 _on_state_changed
                        # 进入 PRINTING 还会顺手清掉 _copy_refill_context。
                        # lancaigang261024200000 任务#554-copy108-hotfix16: 判据统一收口到
                        # _copy_refill_ctx_live(), 并把 _low_weight_refill_pending 从"在途"判据里撤下。
                        # 该标志是会话级承重墙(仅新打印复位), 失败锁在场时不会被清, 解锁后仍为 True
                        # 会把回切永久挡死; 真正的在途由 _refill_motion_busy() 与存活事务对象覆盖。
                        _can_restore_printing = (
                            self.state_manager.state in [SystemStateEnum.IDLE, SystemStateEnum.RESUMING]
                            or (self.state_manager.state == SystemStateEnum.PAUSED
                                and not self._refill_motion_busy()
                                and not self._copy_refill_ctx_live()))
                        if klipper_state == 'printing' and _can_restore_printing:
                            if self.state_manager.state == SystemStateEnum.PAUSED:
                                self.logger.info("状态回切纠偏: PAUSED -> PRINTING(设备已printing, 无在途编排/低重, 任务#554-hotfix15)")
                            self.state_manager.transition_to(SystemStateEnum.PRINTING)
                            # 260915修复(开场挤出头切换竞态误暂停): 记录打印开始时刻
                            self._printing_started_at = time.monotonic()
                            self._grace_runout_logged = False
                            self._prefeed_runout_logged = False  # lancaigang 260915 201658 首次进料宽限日志只记一次的标志复位
                            # lancaigang 260916 12:30:07 任务#188主修复: 复位首次进料门禁, 防跨打印任务残留True
                            # 导致开场"首次进料前不判RUNOUT"门禁失效(T1开场20秒宽限一过即误PAUSE)
                            self._extruder_fed_seen = {0: False, 1: False}
                            # lancaigang261014214500 qoder-agent-f01-leader 任务#437(A'+B): 新打印
                            # 会话复位"真实缺料证据"与RUNOUT去抖计时(新会话新基准, 防跨任务残留)
                            self._session_vacancy_seen = False
                            self._runout_absent_since = {}
                            # lancaigang261014224500 任务#438: 复位退料空料冗余校验(新打印会话新基准)
                            self._retract_cmd_at = None
                            self._retract_empty_ok = False
                            self._retract_empty_since = None
                            # lancaigang261015104408 任务#441: 退料轮次撤下时同步清失败，防跨会话残留。
                            self._retract_verify_failed = False
                            self._retract_verify_timeout_logged = False
                            # lancaigang 260918 20:52:35 任务#296 P0-3改动2: 低重会话级标志复位——
                            # 新打印任务新会话(与_extruder_fed_seen同批同生命周期范式);
                            # 若不复位, 上一次打印的低重标志会残留到本次打印, 导致本次
                            # 纯runout场景误走完整退料序列(违背用户定策)
                            self._low_weight_seen_this_print = False
                            # lancaigang261016 任务#446: 低重续料"待恢复借道"标志随新会话一并复位
                            self._low_weight_refill_pending = False
                            self._pause_orchestrated = False; self._stuck_recover_since = None  # 任务#388
                            # 任务#447c: 新打印会话已恢复打印, 之前"暂停回执超时中止"的自救标记彻底作废
                            self._clear_pause_abort_marker("打印恢复进入新会话")
                            # lancaigang261013221433 任务#435: 恢复已生效(已回printing)→撤下"刚RESUME"武装,
                            # 仅保留"发出RESUME后短窗内没走到printing(真反弹)"的少量重试, 消除残留误捕窗
                            self._hub_resume_at = None
                            # lancaigang261025103309 任务#563: 暂停已结束, 闸门拦截计时归零(原不复位: 上次暂停的拦截时长串入下次, 提前触发拦满)
                            self._gate_block_since = None
                            self.logger.info("打印开始, 断料判定进入20秒宽限期(防开场挤出头未切换竞态)")
                        elif klipper_state == 'paused' and self.state_manager.state in [SystemStateEnum.IDLE, SystemStateEnum.PRINTING, SystemStateEnum.RESUMING, SystemStateEnum.RUNOUT]:
                            # 总是传递当前活跃挤出机信息，以便在PAUSED状态处理时能正确检查断料状态
                            active_extruder = self.klipper_monitor.active_extruder if self.klipper_monitor else 0
                            self.state_manager.transition_to(SystemStateEnum.PAUSED, extruder=active_extruder)
                        elif klipper_state in ['complete', 'cancelled'] and self.state_manager.state != SystemStateEnum.IDLE:
                            # 任务#459: 打印已结束, 挂起的0x01补发请求作废(防CAN恢复后对新任务误发续料)
                            if self._pending_feed_after_can:
                                self.logger.warning(f"打印已结束({klipper_state}), 丢弃挂起的续料补发请求(tube={self._pending_feed_after_can.get('tube_id')}, 任务#459)")
                                self._pending_feed_after_can = None
                            self.state_manager.transition_to(SystemStateEnum.IDLE)
                        elif klipper_state == 'error' and self.state_manager.state != SystemStateEnum.ERROR:
                            self.logger.warning("打印机进入错误状态，但保持CAN通信")
                            self.state_manager.transition_to(SystemStateEnum.ERROR, reason="Klipper reported an error")
                        elif klipper_state in ['standby', 'ready'] and self.state_manager.state == SystemStateEnum.ERROR:
                            # 从错误状态恢复
                            self.logger.info(f"检测到打印机从错误状态恢复: {klipper_state}")
                            self.logger.info(f"当前系统状态: {self.state_manager.state.name}")
                            self.state_manager.transition_to(SystemStateEnum.IDLE)
                            self.logger.info("系统状态已从ERROR转换为IDLE")
                    else:
                        self.logger.debug(f"Klipper状态未变化，保持为: {klipper_state}")
                        
                        # ★★★【承重墙·删除/重构前必读·任务#286】★★★
                        # 此PAUSED轮询是【两条续料链路共同的恢复闭环承重墙】(Quentin3审计#282结论):
                        # 1) RUNOUT链路: RUNOUT→PAUSED→此处轮询触发_runout_clear_and_feed
                        #    →FEEDING→传感器恢复→RESUMING→resume。
                        # 2) 低重链路: _auto_change_filament发0x01后【不转FEEDING保持PAUSED】
                        #    (见其步骤5承重墙注释), 恢复闭环借道此处轮询→第二次触发
                        #    _runout_clear_and_feed→它置FEEDING→新料到头→resume。
                        #    删除/改动此轮询会直接断掉低重链路自动恢复(打印永停PAUSED)。
                        # 3) 任务#286 FEEDING超时兜底: 超时回PAUSED后靠此处轮询≤5s自动重走
                        #    编排重发0x01(天然重试循环, F05侧分流⑤粘滞态正好接住, #262闭环)。
                        # 如果已经在PAUSED状态，检查是否需要补料
                        if (klipper_state == 'paused' and self.state_manager.state == SystemStateEnum.PAUSED
                                and not self._copy_pure_runout() and not getattr(self, '_copy_refill_context', None)):
                            # 检查活跃挤出机的断料状态
                            active_extruder = self.klipper_monitor.active_extruder if self.klipper_monitor else 0
                            
                            # 检查传感器状态缓存
                            if hasattr(self, '_last_filament_status') and active_extruder < len(self.klipper_monitor.filament_sensor_objects):
                                sensor_obj_name = self.klipper_monitor.filament_sensor_objects[active_extruder]
                                if sensor_obj_name in self._last_filament_status:
                                    filament_detected = self._last_filament_status[sensor_obj_name]
                                    if not filament_detected:
                                        # 活跃挤出机缺料，检查是否已经在送料中
                                        if self.state_manager.state != SystemStateEnum.FEEDING:
                                            # lancaigang261015 任务#442: 自动续料失败锁拦截——F05回执报错已判
                                            # 本次续料失败, 即使之后插料盘/传感器变化也不再自动重走编排(等人工/新打印)
                                            if self._auto_feed_failed:
                                                if not self._feeding_giveup_logged:
                                                    self.logger.error(f"自动续料已失败锁定(任务#442), 跳过活跃挤出机{active_extruder}自动补料; "
                                                                      f"请人工处理或开始新一轮打印以解除锁定")
                                                    self._feeding_giveup_logged = True
                                            # 任务#286: FEEDING超时重试已达上限时放弃自动重试(防重试风暴),
                                            # 保持PAUSED等人工处理, 只记一次ERROR日志防刷屏
                                            elif self._feeding_timeout_retry_count >= FEEDING_MAX_TIMEOUT_RETRIES:
                                                if not self._feeding_giveup_logged:
                                                    self.logger.error(f"FEEDING超时重试已达上限({FEEDING_MAX_TIMEOUT_RETRIES}次), "
                                                                      f"放弃活跃挤出机{active_extruder}自动补料重试, 保持PAUSED等待人工处理 "
                                                                      f"(F05侧可能真ERROR如无兼容候选, 其侧有弹窗提示)")
                                                    self._feeding_giveup_logged = True
                                            elif self._pending_feed_after_can:
                                                # 任务#459: 已有挂起的0x01(退料编排已执行过), 只补发不重走全量编排,
                                                # 避免CAN长时间不同时每轮重复 30s停稳+三段退料
                                                asyncio.create_task(self._try_flush_pending_feed("PAUSED轮询"))
                                            elif not self._refill_motion_busy() and not self._pause_refill_source_ok('PAUSED轮询'):
                                                pass  # lancaigang261025103309 任务#563: 人工暂停/来源不明, 缺料也不续料, 等人工
                                            elif not self._refill_motion_busy():
                                                # lancaigang261022103000 fixflow：调度前去重；入口仍复检迟到任务。
                                                # hotfix11: 门要与 _runout_clear_and_resume 入口的 _refill_motion_busy()
                                                # 一致, 否则清缓冲/恢复任务在途时每 5 秒派一个必被静默拒绝的空任务。
                                                self.logger.info(f"检测到活跃挤出机 {active_extruder} 在PAUSED状态下缺料，触发补料请求")
                                                # 手动触发补料动作
                                                self._pause_orchestrated = True  # 任务#388: 本暂停会话有编排
                                                asyncio.create_task(self._handle_paused_filament_runout(active_extruder))

            # 解析断料传感器状态
            filament_status_changed = False
            
            # 检查所有断料传感器状态变化
            for sensor_obj_name in self.klipper_monitor.filament_sensor_objects:
                if sensor_obj_name in status and 'filament_detected' in status[sensor_obj_name]:
                    current_status = status[sensor_obj_name]['filament_detected']
                    # 检查状态是否变化
                    if not hasattr(self, '_last_filament_status'):
                        self._last_filament_status = {}
                    
                    last_status = self._last_filament_status.get(sensor_obj_name)
                    if last_status is None or last_status != current_status:
                        self._last_filament_status[sensor_obj_name] = current_status
                        # lancaigang261012 任务#411: 维护"连续有料"起始时刻(G3数据源)
                        if not hasattr(self, '_sensor_fil_since'):
                            self._sensor_fil_since = {}
                        self._sensor_fil_since[sensor_obj_name] = time.monotonic() if current_status else None
                        # lancaigang261025103309 任务#563: 只记真实 有料->无料 翻转(首帧/重连回填 None->无料 不算断料证据)
                        if last_status and not current_status:
                            if not hasattr(self, '_sensor_absent_at'):
                                self._sensor_absent_at = {}
                            self._sensor_absent_at[sensor_obj_name] = time.monotonic()
                        filament_status_changed = True
                        self.logger.info(f"传感器 {sensor_obj_name} 状态变化: {last_status} -> {current_status}")
            
            # 如果断料传感器状态有变化，主动发送通知
            if filament_status_changed:
                asyncio.create_task(self._send_filament_status_notification())
                # lancaigang261025103309 任务#563: 传感器帧晚于暂停边沿到达(Moonraker 分帧)时在此补认断料暂停
                if self.state_manager.state == SystemStateEnum.PAUSED:
                    self._note_pause_edge_vacancy('暂停边沿后分帧')

            # lancaigang261024113000 任务#554-copy108：先收齐本帧传感器，再按参与头集合触发。
            if (klipper_state == 'paused' and self.state_manager.state == SystemStateEnum.PAUSED
                    and self._copy_pure_runout() and not self._auto_feed_failed
                    and not getattr(self, '_copy_refill_context', None) and not self._refill_motion_busy()):
                missing = [i for i in (0, 1) if self._active_sensor_has_filament(i) is False]
                # lancaigang261025103309 任务#563: 复制双头同样过暂停来源门(人工暂停后拔料不续不恢复)
                if missing and self._pause_refill_source_ok('COPY轮询'):
                    asyncio.create_task(self._runout_clear_and_feed(missing[0]))

            # lancaigang261014224500 qoder-agent-f01-leader 任务#438: 每帧累计"退料后连续无料"冗余校验
            self._update_retract_empty_check()
            
            # 使用已经更新的活跃挤出机信息
            current_active_extruder = self.klipper_monitor.active_extruder
                
            # 处理打印中的断料事件
            if self.state_manager.state == SystemStateEnum.PRINTING:
                # 260915修复(开场挤出头切换竞态误暂停): 打印开场数秒内Klipper的
                # toolhead active extruder可能仍是默认extruder0(T1类gcode尚未执行
                # 到切换命令), 此窗口内hub把extruder0当活跃挤出机且其料道无料会
                # 误判断料立即PAUSE(实测260915: 单T1打印开场3秒误暂停)。宽限期内
                # 断料只记录不动作; 宽限期后断料若仍存在, 下一轮轮询(约5秒)照常
                # 触发RUNOUT保护, 不丢保护只延后。
                _in_grace = False
                _ps = getattr(self, '_printing_started_at', None)
                if _ps is not None and (time.monotonic() - _ps) < 20.0:
                    _in_grace = True
                # 首先检查活跃挤出机的断料状态
                runout_detected = False
                active_extruder_runout = False
                # lancaigang 261020 14:50:00 日志风暴治理: 汇总本轮无料的非活跃挤出机, 供下方限频行一次性上报
                _inactive_absent = []
                
                for i, sensor_obj_name in enumerate(self.klipper_monitor.filament_sensor_objects):
                    if sensor_obj_name in status and 'filament_detected' in status[sensor_obj_name]:
                        has_filament = status[sensor_obj_name]['filament_detected']
                        if not has_filament:
                            self.logger.debug(f"[DEBUG] 传感器 {sensor_obj_name} (索引{i}) 检测到断料, 当前活跃挤出机={current_active_extruder}")
                            if i in self._printing_extruders():
                                # lancaigang 260915 201702 治本修复主方案A-2: 查询进料门禁(活跃挤出机是否见过实际进料)
                                _fed_seen = self._extruder_fed_seen.get(current_active_extruder if self._copy_print_mode() else i, False)
                                if _in_grace:
                                    # 260915修复: 宽限期内不判RUNOUT(防开场竞态)
                                    if not getattr(self, '_grace_runout_logged', False):
                                        self.logger.info(f"打印开场宽限期内检测到挤出机 {i} 断料, 暂不暂停(20秒宽限), 宽限期后仍断料将正常触发保护")
                                        self._grace_runout_logged = True
                                    break
                                if not _fed_seen:
                                    # lancaigang 260915 201705 治本修复主方案A-2: 首次进料前不判RUNOUT,
                                    # 与20秒宽限同路径(开场流程宽限), 首次进料后断料照常触发保护, 只推迟不取消
                                    if not getattr(self, '_prefeed_runout_logged', False):
                                        self.logger.info(f"活跃挤出机 {i} 首次进料前, 视为开场流程宽限: 检测到断料暂不暂停, 首次进料后仍断料将正常触发保护")
                                        self._prefeed_runout_logged = True
                                    break
                                # lancaigang261014214500 qoder-agent-f01-leader 任务#437(B): RUNOUT
                                # 边沿去抖——单帧无料不再立即判RUNOUT, 需连续无料>=RUNOUT_DEBOUNCE_SECONDS
                                # (治sensor1受卡料/碎屑污染时单帧假无料误暂停; 真断料为持续无料不受影响)
                                _now_ro = time.monotonic()
                                if not hasattr(self, '_runout_absent_since'):
                                    self._runout_absent_since = {}
                                _abs_since = self._runout_absent_since.get(i)
                                if _abs_since is None:
                                    self._runout_absent_since[i] = _now_ro
                                    _abs_since = _now_ro
                                if _now_ro - _abs_since < RUNOUT_DEBOUNCE_SECONDS:
                                    if _now_ro - getattr(self, '_runout_dbg_log_at', 0.0) >= FEEDING_HOLD_LOG_INTERVAL:
                                        self._runout_dbg_log_at = _now_ro
                                        self.logger.info(f"任务#437: 活跃挤出机 {i} 传感器 {sensor_obj_name} 首帧无料, 边沿去抖中(已{_now_ro - _abs_since:.1f}s<{RUNOUT_DEBOUNCE_SECONDS:.0f}s), 暂不判RUNOUT")
                                    break
                                self.logger.info(f"检测到活跃挤出机 {i} 的传感器 {sensor_obj_name} 断料事件(边沿去抖{RUNOUT_DEBOUNCE_SECONDS:.0f}s已确认)。")
                                # lancaigang261014214500 任务#437(A'): 确认RUNOUT即记本会话真实缺料证据
                                self._session_vacancy_seen = True
                                if not self.state_manager.is_state(SystemStateEnum.RUNOUT):
                                    self.state_manager.transition_to(SystemStateEnum.RUNOUT, extruder=i)
                                    active_extruder_runout = True
                                    break
                            else:
                                # 非活跃挤出机断料，记录但不处理
                                # lancaigang 261020 14:50:00 日志风暴治理: 原每帧INFO(实测12条/分钟,
                                # 占日志19.9%字节), 明细降DEBUG, 由下方限频行汇总上报, 信息不减
                                self.logger.debug(f"检测到非活跃挤出机 {i} 的传感器 {sensor_obj_name} 断料，暂不处理。")
                                _inactive_absent.append(f"{i}:{sensor_obj_name}")
                                runout_detected = True
                        else:
                            # lancaigang261014214500 qoder-agent-f01-leader 任务#437(B): 该传感器有料,
                            # 清除其"连续无料"去抖计时(中断累计, 防抖动帧叠加误判RUNOUT)
                            if getattr(self, '_runout_absent_since', None) and self._runout_absent_since.get(i) is not None:
                                self._runout_absent_since[i] = None
                
                # 如果活跃挤出机没有断料，但有其他挤出机断料，记录警告
                if runout_detected and not active_extruder_runout:
                    # lancaigang 261020 14:50:00 日志风暴治理: 该行原为每帧WARNING(实测2h50m/663条,
                    # 占WARNING信道绝大部分, 淹没[RETRACT-CHECK]等真实告警)。改为INFO+60s限频,
                    # 单条携带全部无料非活跃挤出机, 保留首次可见性, 数量降约92%
                    _now_ir = time.monotonic()
                    if _now_ir - getattr(self, '_inactive_runout_log_at', 0.0) >= 60.0:
                        self._inactive_runout_log_at = _now_ir
                        self.logger.info(f"非活跃挤出机有断料但当前活跃挤出机 {current_active_extruder} 正常, 继续打印"
                                         f"(无料非活跃挤出机={','.join(_inactive_absent)}; 60s限频上报, 逐帧明细见DEBUG)")
                    
            # 处理FEEDING状态下的恢复逻辑
            elif (self.state_manager.state == SystemStateEnum.FEEDING
                  and not getattr(self, '_copy_refill_context', None)
                  and not self._refill_motion_busy()):
                # 检查当前活跃挤出机是否有料(传感器恢复优先, 超时兜底让位于正常恢复)
                _sensor_recovered = False
                for i, sensor_obj_name in enumerate(self.klipper_monitor.filament_sensor_objects):
                    if sensor_obj_name in status and 'filament_detected' in status[sensor_obj_name]:
                        has_filament = status[sensor_obj_name]['filament_detected']
                        # lancaigang261014214500 qoder-agent-f01-leader 任务#437(A'): 本会话活跃
                        # 传感器一旦出现无料, 记为"真实缺料证据"(恢复端交叉核对数据源)
                        if (not has_filament) and i == current_active_extruder:
                            if not getattr(self, '_session_vacancy_seen', False):
                                self._session_vacancy_seen = True
                                self.logger.info(f"任务#437: 活跃挤出机 {i} 传感器 {sensor_obj_name} 检测到无料, 记为本会话真实缺料证据(恢复端交叉核对用)")
                            continue
                        if has_filament and i == current_active_extruder:
                            # lancaigang 260918 20:52:35 任务#296 P0-1改动1: 最短驻留防护窗判定——
                            # 传感器有料【且】驻留窗已满(now >= _feeding_min_hold_until)才转RESUMING。
                            # 治#295环4: runout清管UNLOAD退残料时线尾回带假触发传感器"有料"
                            # (stale帧出现在0x01后0-5s), 旧版首帧stale"有料"4-5秒即RESUME→空管
                            # 打印; 20s窗稳定覆盖stale窗口且不误伤真实恢复(新料到传感器物理
                            # 最短数十秒)。窗内仅记节流日志不转态(判定优先于超时判定的次序不变,
                            # _sensor_recovered不置位, 超时判定照常走且300s>>20s不误触)
                            _now = time.monotonic()
                            _hold_until = getattr(self, '_feeding_min_hold_until', None)
                            if _hold_until is None or _now >= _hold_until:
                                # lancaigang261014214500 qoder-agent-f01-leader 任务#437(A'): 恢复端
                                # 交叉核对——仅当本会话确实出现过无料(真实缺料->续料)才认可"有料"为
                                # 恢复证据; 若本会话从未见过无料, 则"有料"极可能是sensor1被断料卡住
                                # 导致的持续假有料(退料确认同样读到有料), 拒绝转RESUMING, 保持FEEDING
                                # 由超时兜底重试, 杜绝空管误恢复。
                                if not self._recovery_evidence_ok():
                                    if _now - getattr(self, '_vac_block_log_at', 0.0) >= FEEDING_HOLD_LOG_INTERVAL:
                                        self._vac_block_log_at = _now
                                        self.logger.warning(f"任务#438: FEEDING传感器报有料(驻留窗已满)但恢复端证据不足(本会话未见无料 或 退料后未达持续{RETRACT_EMPTY_VERIFY_SECONDS:.0f}s无料), 疑sensor1卡料污染, 拒绝转RESUMING, 保持FEEDING重试")
                                else:
                                    # 窗已满且已核验本会话有真实缺料证据: 真实恢复, 正常转RESUMING
                                    self.logger.info(f"检测到活跃挤出机 {i} 的传感器 {sensor_obj_name} 恢复有料(FEEDING驻留窗已满{_now - (self._feeding_started_at or _now):.0f}s), 已核验真实缺料证据, 准备恢复打印。")
                                    self.state_manager.transition_to(SystemStateEnum.RESUMING, extruder=i)
                                    _sensor_recovered = True
                                    break
                            else:
                                # 窗内stale假有料: 不转RESUMING, 节流记一条INFO日志
                                # (每FEEDING_HOLD_LOG_INTERVAL秒一条防刷屏)
                                if _now - getattr(self, '_feeding_hold_log_at', 0.0) >= FEEDING_HOLD_LOG_INTERVAL:
                                    self._feeding_hold_log_at = _now
                                    self.logger.info(f"FEEDING传感器有料但驻留窗未满(防护stale假有料, 任务#296): 距进窗{_now - (self._feeding_started_at or _now):.0f}s < {FEEDING_MIN_HOLD_SECONDS:.0f}s, 不转RESUMING")
                # 任务#286 V1/V2/V3/V5: FEEDING超时兜底检查——仅当传感器未恢复时介入
                # (防误伤正常长续料: 传感器恢复的同一帧已转RESUMING并置_sensor_recovered,
                # 不会走到超时分支; 超时判定在传感器恢复检查之后, 正常长续料最后一刻恢复不受影响)
                if not _sensor_recovered:
                    self._check_feeding_timeout()
                            
            # ===== lancaigang260922 任务#388: 停滞自愈+状态心跳(断料暂停死锁根治) =====
            # 今晨114实测: 断料暂停→0x01受理→状态机卡滞→传感器有料20min无resume→打印永停。
            # 自愈: 本暂停会话编排过+klippy仍paused+活跃传感器有料持续30s→强制RESUME。
            # 防误伤: 人工暂停(未编排过)永不自愈。
            try:
                _st = self.state_manager.state
                if (
                        klipper_state == 'paused'
                        and not self._auto_feed_failed
                        # lancaigang261013221433 任务#435: 编排暂停 或 hub刚RESUME后短窗反弹
                        # (收窄#433: 不再按"进料会话300s"泛化, 人工暂停紧跟进料也不再被自愈)
                        and (self._pause_orchestrated or self._recent_hub_resume())
                        and not getattr(self, '_copy_refill_context', None)
                        # 任务#433: 编排进行中不抢恢复(此时本应保持暂停, 防态失步/日志刷屏)
                        and not self._refill_motion_busy()
                        and _st in (
                            SystemStateEnum.FEEDING, SystemStateEnum.PAUSED, SystemStateEnum.RUNOUT)):
                    _ae = self.klipper_monitor.active_extruder if self.klipper_monitor else 0
                    _objs = self.klipper_monitor.filament_sensor_objects
                    _has_fil = False
                    if _ae < len(_objs) and hasattr(self, '_last_filament_status'):
                        _has_fil = bool(self._last_filament_status.get(_objs[_ae], False))
                    if _has_fil:
                        if self._stuck_recover_since is None:
                            self._stuck_recover_since = time.monotonic()
                            self.logger.info(f"任务#388自愈观察开始: 暂停可自愈(编排={self._pause_orchestrated},刚RESUME反弹={self._recent_hub_resume()})+传感器有料, hub={_st.name}, {STUCK_RECOVER_SECONDS:.0f}s无恢复将强制RESUME")
                        elif time.monotonic() - self._stuck_recover_since >= STUCK_RECOVER_SECONDS:
                            self.logger.warning(f"任务#388停滞自愈触发: hub={_st.name}卡滞+传感器有料持续{time.monotonic() - self._stuck_recover_since:.0f}s, 强制RESUME打印")
                            # lancaigang261014095210 任务#436: 自愈点改为"仅闸门预检+状态转换", 不再自行发RESUME——
                            # 交由RESUMING动作(统一出口 _resume_print_gated:2087)预挤100mm后发一次RESUME。
                            # 原#434路径: 自愈点先发RESUME#1(set _hub_resume_at)→转RESUMING后动作再发RESUME#2,
                            # 同一次自愈重复下发, 且#2伴随打印已恢复后的额外预挤。真机261013 22:30+自愈未触发→潜伏。
                            # 保持#434语义: 闸门未放行则不转(不误转/等待下轮30s重试), 由动作三态兜底。
                            _allowed, _reason = self._can_resume_now()
                            # lancaigang261014214500 qoder-agent-f01-leader 任务#437(A'): 自愈出口
                            # 同样需本会话有真实缺料证据, 否则持续假有料会促成一次非真实恢复
                            if _allowed and not self._recovery_evidence_ok():
                                _allowed = False
                                _reason = '任务#438: 恢复端证据不足(未见无料/退料后未达持续无料窗, 疑sensor1卡料污染), 自愈拒绝'
                            if _allowed:
                                self.state_manager.transition_to(SystemStateEnum.RESUMING, extruder=_ae)
                            self._stuck_recover_since = None
                    else:
                        if self._stuck_recover_since is not None:
                            self.logger.info("任务#388自愈观察中止: 传感器转为无料(续料未完成, 交还正常编排)")
                        self._stuck_recover_since = None
                else:
                    # lancaigang260923 任务#389: None=Klipper增量帧无state字段
                    # (print_stats.get("state")=None), 打印状态未知不清计时器——
                    # 全帧(paused)/增量帧(None)交替到达时, 旧else无差别清零导致30s自愈窗
                    # 每5s被清一次永不触发(120实测13:56~13:58连续"观察开始"零RESUME, 打印永停)。
                    # 只有明确非paused(printing/complete/error等真状态)才清。
                    if klipper_state is not None:
                        self._stuck_recover_since = None
                # 状态心跳(30s节流)
                _now_hb = time.monotonic()
                if _now_hb - self._last_status_heartbeat >= 30.0:
                    self._last_status_heartbeat = _now_hb
                    _ae_hb = self.klipper_monitor.active_extruder if self.klipper_monitor else 0
                    _sensors = []
                    if hasattr(self, '_last_filament_status') and self.klipper_monitor:
                        for _o in self.klipper_monitor.filament_sensor_objects:
                            _sensors.append('有' if self._last_filament_status.get(_o) else '无')
                    self.logger.info(f"心跳: hub={self.state_manager.state.name} klippy={klipper_state} 活跃挤出机={_ae_hb} 传感器=[{','.join(_sensors)}]")
            except Exception as _e388:
                self.logger.error(f"任务#388自愈块异常(不影响主流程): {_e388}")

            # lancaigang261017 运行态持久化: 周期性刷新快照(内容未变不写盘)
            self._save_runtime_state("状态更新周期")
            # lancaigang261024163000 任务#554-copy108-hotfix6: COPY/MIRROR 双头加热看护
            # (放在持久化之后: 看护失败既不影响快照, 也不阻断状态机)
            try:
                await self._copy_heater_guard()
            except Exception as _ehg:
                self.logger.error(f"复制双头加热看护异常(不影响主流程): {_ehg}")

        except Exception as e:
            self.logger.error(f"处理Klipper状态更新时发生错误: {str(e)}", exc_info=True)
            # 确保即使出错也不会阻止后续的状态更新
            # 不要在这里转换到ERROR状态，以免影响正常的错误恢复流程

    # ===== lancaigang261025103309 任务#563: 暂停来源门(只有断料/hub自有暂停才自动续料+恢复) =====
    # 根因: PAUSED轮询/状态动作/COPY入口只看"活跃头缺料"就续料并自动RESUME, 不分暂停来源;
    # 人工暂停(暂停时有料)后拔料/退料也会被续料并自动恢复, 料未确认到喷嘴即恢复=空打。
    def _pause_edge_vacancy_heads(self, now):
        """暂停边沿窗内由有料转无料、当前仍无料的参与打印头(=断料暂停证据)。"""
        edge = getattr(self, '_pause_edge_at', None)
        if edge is None or now - edge > PAUSE_EDGE_RUNOUT_POST_SECONDS:
            return []
        objs = getattr(self.klipper_monitor, 'filament_sensor_objects', []) or []
        absent_at = getattr(self, '_sensor_absent_at', {}) or {}
        heads = []
        for i in self._printing_extruders():
            if i is None or not 0 <= i < len(objs):
                continue
            t = absent_at.get(objs[i])
            if self._active_sensor_has_filament(i) is not False:
                continue
            in_edge_window = (t is not None
                              and -PAUSE_EDGE_RUNOUT_PRE_SECONDS <= t - edge <= PAUSE_EDGE_RUNOUT_POST_SECONDS)
            # lancaigang261031162723 任务#579: 16:05真实runout在暂停前5.001s才转paused，
            # 原短窗浮点边界漏认。复用PRINTING阶段已有的连续无料去抖起点：暂停前已达到
            # 去抖时长且此刻仍无料，也是断料暂停；人工暂停后才拔料没有该暂停前计时，仍拒绝。
            absent_since = (getattr(self, '_runout_absent_since', {}) or {}).get(i)
            debounced_before_pause = (absent_since is not None and absent_since <= edge
                                      and edge - absent_since >= RUNOUT_DEBOUNCE_SECONDS)
            if in_edge_window or debounced_before_pause:
                heads.append(i)
        return heads

    def _note_pause_edge_vacancy(self, where):
        """暂停边沿窗内认定断料暂停(置 _session_vacancy_seen), 供来源门放行自动续料。"""
        if getattr(self, '_session_vacancy_seen', False):
            return
        fro_context = getattr(self, '_refill_context', None)
        if fro_context and fro_context.get('fro_session') and not fro_context.get('fro_precleared'):
            return  # FRO只接受本session真实翻转并由清料任务完成3秒去抖。
        heads = self._pause_edge_vacancy_heads(time.monotonic())
        if not heads:
            return
        self._session_vacancy_seen = True
        self.logger.info(f"任务#563/#579: {where}: 挤出机{heads}在暂停边沿短窗翻转或打印中持续无料已达去抖, "
                         f"认定断料暂停, 允许自动续料")

    def _pause_refill_source_ok(self, where):
        """本次暂停是否有自动续料来源: 断料证据/低重借道/hub编排。否则视为人工暂停, 不续料不恢复。"""
        if (getattr(self, '_low_weight_refill_pending', False) or getattr(self, '_pause_orchestrated', False)
                or getattr(self, '_session_vacancy_seen', False)):
            return True
        now = time.monotonic()
        if now - getattr(self, '_source_gate_log_at', 0.0) >= PAUSE_SOURCE_GATE_LOG_INTERVAL:
            self._source_gate_log_at = now
            self.logger.warning(f"任务#563: {where}: 本次暂停无断料/低重/编排来源(人工暂停或来源不明), "
                                f"活跃头缺料也不自动续料/恢复, 保持PAUSED等人工")
        return False

    def _refuse_auto_resume(self, source, extruder=None):
        """I2 不满足(冷态/闸门拦满): 判本轮续料失败停在 PAUSED; 失败锁建不起来时也解除武装, 绝不放行 RESUME。"""
        self._fail_current_refill(source)
        if self._auto_feed_failed:
            return
        self.logger.error(f"{source}; 续料失败锁未建立(事务上下文无效), 解除自动续料/恢复武装, 保持PAUSED等人工")
        self._pause_orchestrated = False
        self._session_vacancy_seen = False
        self._low_weight_refill_pending = False
        self._stuck_recover_since = None
        self._hub_resume_at = None
        self.state_manager.transition_to(
            SystemStateEnum.PAUSED,
            extruder=extruder if extruder is not None else getattr(self.klipper_monitor, 'active_extruder', None))

    # ===== lancaigang261012 任务#411: 恢复打印闸门(冗余校验, 治"料未到位就自己恢复") =====
    def _active_sensor_has_filament(self, extruder=None):
        """读取指定(默认活跃)挤出机断料传感器的最新缓存值; 无缓存/异常返回None(降级放行不误伤)"""
        try:
            km = self.klipper_monitor
            if km is None:
                return None
            idx = km.active_extruder if extruder is None else extruder
            objs = getattr(km, 'filament_sensor_objects', []) or []
            if idx is None or idx >= len(objs):
                return None
            return (getattr(self, '_last_filament_status', {}) or {}).get(objs[idx])
        except Exception:
            return None

    def _can_resume_now(self):
        """
        恢复出口闸门: 返回 (是否允许恢复, 原因)

        G1 无换料/清管编排在跑(_refill_orchestration_busy)——正在退料/清管绝不恢复(用户红线)
        G2 挤出机未在退料(覆盖hub未编排但挤出机在退的场景, 如人工UNLOAD_FILAMENT)
        G3 传感器有料持续 >= RESUME_SENSOR_HOLD_SECONDS(滤1~2s毛刺)
        拦满: 连续拦截达 RESUME_GATE_MAX_BLOCK_SECONDS 判本轮续料失败(lancaigang261025103309 任务#563: 原兜底放行会空打)
        """
        try:
            if self._auto_feed_failed:
                return False, '本轮续料已失败，等待打印侧人工恢复'
            now = time.monotonic()
            reason = None
            km = self.klipper_monitor

            copy_context = getattr(self, '_copy_refill_context', None)
            if copy_context:
                if (not self._copy_refill_is_current(copy_context)
                        or getattr(self, '_refill_orchestration_task', None) is not asyncio.current_task()):
                    return False, '复制续料归属已失效或非本轮恢复出口'
                return self._copy_heads_ready()
            if self._copy_print_mode():
                ready, reason = self._copy_heads_ready()
                if not ready:
                    return False, reason

            if self._refill_motion_busy():
                # lancaigang261023120000 任务#540：机械互斥是硬门，不能被300s兜底反向放行。
                return False, 'G1 换料/清管编排进行中(侧=%s)' % getattr(self, '_refill_orchestration_owner', None)

            if reason is None and km is not None:
                vel = float((getattr(km, 'motion_report', {}) or {}).get('live_extruder_velocity', 0.0) or 0.0)
                # hotfix8: 同 _copy_heads_ready——帧到达不等于在动, 用 print_time 推进当证据
                mts = getattr(km, '_motion_alive_ts', None)
                if mts is None:
                    mts = getattr(km, '_last_motion_ts', None)
                if vel <= RESUME_RETRACT_VEL_THRESHOLD and mts is not None and (now - mts) <= RESUME_MOTION_FRESH_SECONDS:
                    reason = 'G2 挤出机正在退料(vel=%.2fmm/s)' % vel

            if reason is None:
                since = None
                if km is not None:
                    objs = getattr(km, 'filament_sensor_objects', []) or []
                    ae = getattr(km, 'active_extruder', 0)
                    if ae is not None and ae < len(objs):
                        since = (getattr(self, '_sensor_fil_since', {}) or {}).get(objs[ae])
                if since is None:
                    reason = 'G3 活跃挤出机传感器当前无料'
                elif now - since < RESUME_SENSOR_HOLD_SECONDS:
                    reason = 'G3 传感器有料仅%.1fs(<%.0fs, 疑毛刺)' % (now - since, RESUME_SENSOR_HOLD_SECONDS)

            if reason is None:
                self._gate_block_since = None
                return True, 'G1无编排/G2非退料/G3有料持续 全部通过'
        except Exception as e:
            if self._copy_print_mode() or getattr(self, '_copy_refill_context', None):
                self.logger.error(f'[COPY-REFILL] 双头恢复证据无效，不放行：{e}')
                return False, '复制双头恢复证据无效'
            # lancaigang261025103309 任务#563: 判定异常=料/温/退料状态均未确认, 原"降级放行"会空打; 改不放行并计入拦截时长
            self.logger.error('任务#563: 恢复闸门判定异常(不放行): %s' % e, exc_info=True)
            now, reason = time.monotonic(), '闸门异常不放行: %s' % e
        return self._gate_blocked(now, reason)

    def _gate_blocked(self, now, reason):
        """lancaigang261025103309 任务#563: 闸门拦截计时; 拦满判本轮续料失败(原兜底放行会在料/温未确认时空打)。"""
        if getattr(self, '_gate_block_since', None) is None:
            self._gate_block_since = now
        blocked = now - self._gate_block_since
        if blocked >= RESUME_GATE_MAX_BLOCK_SECONDS:
            self._refuse_auto_resume('任务#563: 恢复闸门连续拦截%.0fs(>=%.0fs)仍未满足(%s), 判定本轮续料失败, 不兜底放行'
                                     % (blocked, RESUME_GATE_MAX_BLOCK_SECONDS, reason))
            return False, '恢复闸门连续拦截%.0fs, 判定本轮续料失败: %s' % (blocked, reason)
        if now - getattr(self, '_gate_log_at', 0.0) >= RESUME_GATE_LOG_INTERVAL:
            self._gate_log_at = now
            self.logger.info(
                '任务#411: 恢复闸门拦截RESUME(不发): %s | 已拦截%.0fs, 达%.0fs判本轮续料失败(任务#563)'
                % (reason, blocked, RESUME_GATE_MAX_BLOCK_SECONDS))
        return False, reason

    # ===== lancaigang261014224500 qoder-agent-f01-leader 任务#438: 退料后空料冗余校验 =====
    def _arm_retract_empty_check(self):
        """退料完成后武装"退料后稳定无料"校验(抖动判别+确认超时)。

        退料是上位机主动下发的动作, 退料完成后活跃 filament_switch_sensor 物理上必然持续无料
        >=RETRACT_EMPTY_VERIFY_SECONDS。该事实作传感器可信度的确定性锚点: 退料后仍报有料/抖动不达窗,
        即"传感器与机械事实矛盾"(线材断在打印头开关内卡住), 拒绝自动恢复(不空管误恢复)。
        本方法同时初始化抖动判别(翻转计数)与确认超时状态(线材可能拉不出去)。
        """
        if self._auto_feed_failed:
            return
        self._retract_cmd_at = time.monotonic()
        self._retract_empty_since = None
        self._retract_empty_ok = False
        # lancaigang261014233000 任务#439: 抖动判别+确认超时状态
        self._retract_toggle_count = 0          # 窗口内 有料<->无料 真实翻转次数
        self._retract_prev_has = None           # 上一观测(用于边沿计数)
        self._retract_verify_timeout_logged = False
        self._retract_verify_failed = False
        # lancaigang 261020 14:20:34 任务#439观测: 武装时刻打点。低频调用(每次退料一轮一次),
        # 用于把"退料完成时刻/期望稳定窗/确认超时"与步骤4/5及后续恢复闸门对齐成一条时间线
        self.logger.info(
            f"[RETRACT-CHECK] #439武装: 退料后稳定无料校验开始(挤出机={getattr(self.klipper_monitor, 'active_extruder', None)}), "
            f"期望连续无料>={RETRACT_EMPTY_VERIFY_SECONDS:.0f}s, 确认超时={RETRACT_EMPTY_VERIFY_TIMEOUT_SECONDS:.0f}s, "
            f"抖动上限={RETRACT_EMPTY_MAX_TOGGLES}次")

    def _update_retract_empty_check(self):
        """每帧状态更新时累计"退料后连续无料"时长(仅退料后生效), 并做抖动判别与确认超时。

        ① 抖动判别: 统计窗口内 有料<->无料 真实边沿翻转次数; 稳定无料(连续>=RETRACT_EMPTY_VERIFY_SECONDS)
           才置证可信, 翻转中断则作废重计——以此区分"真无料"与"sensor抖动"。
        ② 确认超时: 退料后 RETRACT_EMPTY_VERIFY_TIMEOUT_SECONDS 内仍未取得稳定无料确认, 判退料失败
           (线材拉不出去/断在打印头内卡死), 停止自动恢复保持暂停等人工处理, 不无限等待。
        """
        if getattr(self, '_retract_cmd_at', None) is None:
            return
        # lancaigang261015104408 任务#441: 已判失败的同轮不得再取得迟到成功证据；
        # 新一轮退料由_arm_retract_empty_check复位，保留既有重试及#440成功锁存。
        if getattr(self, '_retract_verify_failed', False):
            return
        try:
            objs = getattr(self.klipper_monitor, 'filament_sensor_objects', []) or []
            ae = getattr(self.klipper_monitor, 'active_extruder', 0)
            has = True
            if objs and ae is not None and ae < len(objs):
                has = bool((getattr(self, '_last_filament_status', {}) or {}).get(objs[ae], True))
            now = time.monotonic()

            # lancaigang261014233000 任务#439(抖动判别): 统计窗口内 有料<->无料 的真实边沿翻转次数
            _prev = getattr(self, '_retract_prev_has', None)
            if _prev is not None and _prev != has:
                self._retract_toggle_count = getattr(self, '_retract_toggle_count', 0) + 1
                self.logger.info(f"任务#439: 退料后传感器抖动观测: {_prev}->{has} (累计翻转{self._retract_toggle_count}次)")
            self._retract_prev_has = has

            if not has:
                if getattr(self, '_retract_empty_since', None) is None:
                    self._retract_empty_since = now
                # 稳定无料窗: 连续无料>=阈值 且 翻转未超限 才判"真实无料"
                if (now - self._retract_empty_since) >= RETRACT_EMPTY_VERIFY_SECONDS and not getattr(self, '_retract_empty_ok', False):
                    self._retract_empty_ok = True
                    self.logger.info(f"任务#439: 退料后活跃挤出机 {ae} 传感器稳定无料>= {RETRACT_EMPTY_VERIFY_SECONDS:.0f}s(抖动翻转={getattr(self, '_retract_toggle_count', 0)}次<={RETRACT_EMPTY_MAX_TOGGLES}), 判真实无料+退料到位, 允许后续自动恢复")
            else:
                # lancaigang261014235500 任务#440: 修复#439回归——一旦已取得'稳定无料>=阈值'确认(_retract_empty_ok=True),
                # 之后传感器转有料='续料把料推到位'的正常成功路径, 必须锁存不撤销; 仅'尚未确认'阶段被有料中断才作废重计时(抖动/卡料污染)。
                if not getattr(self, '_retract_empty_ok', False):
                    if getattr(self, '_retract_empty_since', None) is not None:
                        self.logger.warning(f"任务#439: 退料后无料窗在确认前被有料中断(已持续{now - self._retract_empty_since:.1f}s<{RETRACT_EMPTY_VERIFY_SECONDS:.0f}s), 证据作废重新计时(疑sensor1卡料/抖动污染)")
                    self._retract_empty_since = None

            # lancaigang261014233000 任务#439(超时机制): 期限内未取得"稳定无料"确认 -> 判退料失败
            # (线材拉不出去/断在打印头内卡死/传感器持续抖动), 停止自动恢复, 保持暂停等人工处理, 不无限等待
            if (not getattr(self, '_retract_empty_ok', False)
                    and (now - self._retract_cmd_at) >= RETRACT_EMPTY_VERIFY_TIMEOUT_SECONDS
                    and not getattr(self, '_retract_verify_timeout_logged', False)):
                self._retract_verify_timeout_logged = True
                self._retract_verify_failed = True
                _tg = getattr(self, '_retract_toggle_count', 0)
                if _tg > RETRACT_EMPTY_MAX_TOGGLES:
                    _diag = f"传感器持续抖动(窗口内真实翻转{_tg}次>{RETRACT_EMPTY_MAX_TOGGLES}), 非真实无料"
                elif has:
                    _diag = "退料后传感器仍持续报'有料'(线材可能没被拉出/断在打印头内卡死)"
                else:
                    _diag = "退料后无料窗口反复被中断, 无法形成稳定无料"
                self._fail_current_refill(f"退料{RETRACT_EMPTY_VERIFY_TIMEOUT_SECONDS:.0f}s未达连续无料确认: {_diag}",
                                          FeederCabinetCAN.ERROR_MECHANICAL)
                # lancaigang 261020 14:20:34 任务#439观测: 显式标记失败结论与耗时, 便于与
                # [RECOVERY-GATE]的拒绝原因、以及后续 PAUSED 轮询重新武装形成对照
                self.logger.warning(
                    f"[RETRACT-CHECK] verdict=FAIL_TIMEOUT 耗时{now - self._retract_cmd_at:.1f}s>={RETRACT_EMPTY_VERIFY_TIMEOUT_SECONDS:.0f}s "
                    f"稳定无料={getattr(self, '_retract_empty_ok', False)} 翻转={_tg}次 当前传感器={'有料' if has else '无料'} "
                    f"-> 后续 _recovery_evidence_ok() 将拒绝自动恢复")
        except Exception as e:
            self.logger.error(f"任务#438: 退料空料校验异常(降级放行不阻断): {e}", exc_info=True)

    def _recovery_evidence_ok(self) -> bool:
        """恢复端交叉核对总门(替代单看"有料")。

        - 必须本打印会话出现过无料(_session_vacancy_seen, 由RUNOUT确认/FEEDING观测置位);
        - 若本会话执行过退料(_retract_cmd_at非None), 还须"退料后持续无料>=RETRACT_EMPTY_VERIFY_SECONDS"
          的冗余证据(_retract_empty_ok)。
        缺任一即判证据不足, 拒绝自动恢复(防sensor1卡料持续假有料空管打印)。纯runout(无退料)会话不受第二条件约束。
        """
        if self._auto_feed_failed:
            self._log_recovery_evidence(False, '本轮已失败锁定，迟到有料不恢复')
            return False
        if not getattr(self, '_session_vacancy_seen', False):
            self._log_recovery_evidence(False, '本会话未见无料(RUNOUT确认/FEEDING观测均未置位)')
            return False
        # lancaigang261015104408 任务#441: 失败优先于成功标记，约束正常恢复和#388自愈；
        # 只作用于已武装退料轮次，不给纯runout增加退料条件或新的超时机制。
        if getattr(self, '_retract_cmd_at', None) is not None:
            if getattr(self, '_retract_verify_failed', False):
                self._log_recovery_evidence(False, '任务#439已判退料失败(确认超时内未取得稳定无料)')
                return False
            if not getattr(self, '_retract_empty_ok', False):
                self._log_recovery_evidence(False, f'退料后未达稳定无料{RETRACT_EMPTY_VERIFY_SECONDS:.0f}s')
                return False
        self._log_recovery_evidence(True, '')
        return True

    def _log_recovery_evidence(self, allowed: bool, reason: str):
        """lancaigang 261020 14:20:34 任务#438观测: 恢复端证据结论变化时打一条。

        本函数被 FEEDING 轮询每帧调用(高频), 故只在结论翻转时记录, 不得每次打印;
        旧日志由调用方节流输出笼统的'证据不足', 看不出具体缺哪一条证据。
        """
        _old = getattr(self, '_recovery_evidence_log_state', None)
        if allowed == _old:
            return
        self._recovery_evidence_log_state = allowed
        self.logger.info(
            f"[RECOVERY-GATE] 恢复端证据{'通过' if allowed else '拒绝'}({_old}->{allowed}): "
            f"挤出机={getattr(self.klipper_monitor, 'active_extruder', None)} "
            f"会话无料={getattr(self, '_session_vacancy_seen', False)} "
            f"退料已武装={getattr(self, '_retract_cmd_at', None) is not None} "
            f"稳定无料={getattr(self, '_retract_empty_ok', False)} "
            f"已判失败={getattr(self, '_retract_verify_failed', False)}"
            + (f" | 拒绝原因: {reason}" if reason else ''))

    async def _resume_print_gated(self):
        """所有自动恢复路径的统一出口: 先过闸门, 通过才发RESUME(正常路径与#388自愈均经此)。

        lancaigang261013215024 任务#434: 返回值由 bool 改为三态字符串——
        旧版单一 False 同时表示"闸门主动拦截(未发)"与"RESUME发送失败", 调用方
        (_handle_state_change_actions RESUMING分支)把拦截也当失败→误转ERROR
        (261013 21:47:55实测: 闸门G3拦截"传感器有料仅0.0s<5s"→RESUMING→ERROR, 打印不恢复)。
        三态: 'sent'=闸门通过且已成功发送; 'blocked'=闸门主动拦截(非失败, 调用方应保持/等待
        重试, 严禁据blocked转ERROR); 'failed'=闸门通过但RESUME发送失败(真失败, 可转ERROR)。
        """
        allowed, reason = self._can_resume_now()
        if not allowed:
            # 拦截详情由_can_resume_now按RESUME_GATE_LOG_INTERVAL节流打印, 此处不重复刷屏
            return 'blocked'
        self.logger.info('任务#411: 恢复闸门通过(%s), 发送RESUME' % reason)
        _sent = await self.klipper_monitor.resume_print()
        if _sent:
            # lancaigang261013221433 任务#435: 打点"hub本次已发出RESUME", 供#388短窗反弹自愈判据
            # (所有自动恢复路径统一经此出口, 天然覆盖正常恢复与#388自身强恢复)
            self._hub_resume_at = time.monotonic()
            return 'sent'
        return 'failed'

    # ===== lancaigang261013221433 任务#435: hub刚RESUME后短窗反弹自愈判据(收窄#433) =====
    def _recent_hub_resume(self) -> bool:
        """hub最近是否刚发出过RESUME(HUB_RESUME_BOUNCE_WINDOW_SECONDS内)。
        用途: #388仅在"hub刚让打印机恢复、它却短窗内又弹回paused"(本次恢复没生效, 疑似
        外部干扰/竞态)时才自愈; 距上次RESUME超出短窗的暂停一律尊重(含人工暂停, 永不自愈)。
        lancaigang261014100043 任务#436(发现1·A方案): 注——因本窗(20s)<STUCK_RECOVER_SECONDS(30s),
        该判据单独武装时为死支(详见常量:85处说明), 保持现状不改。"""
        _t = getattr(self, '_hub_resume_at', None)
        return _t is not None and (time.monotonic() - _t) <= HUB_RESUME_BOUNCE_WINDOW_SECONDS

    def _fro_purge_busy(self):
        task = getattr(self, '_fro_purge_task', None)
        return task is not None and not task.done() and task is not self._current_async_task()

    def _refill_motion_busy(self):
        """lancaigang261023120000 任务#540：退料编排或清缓冲未退出前禁止恢复挤出。"""
        clear_task = getattr(self, '_buffer_clear_task', None)
        resume_task = getattr(self, '_resume_task', None)
        fro_task = getattr(self, '_fro_purge_task', None)
        current = self._current_async_task()
        return (getattr(self, '_refill_orchestration_busy', False)
                or (clear_task is not None and not clear_task.done())
                or (resume_task is not None and not resume_task.done() and resume_task is not current)
                or (fro_task is not None and not fro_task.done() and fro_task is not current))

    def _cancel_refill_tasks(self, reason: str, include_orchestration=False):
        """lancaigang261023120000 任务#540：撤销旧任务但不提前释放它们的互斥所有权。"""
        self._feed_verdict_probe_seq = None
        names = ['_buffer_clear_task', '_feed_verdict_task', '_resume_task', '_error_probe_task',
                 '_fro_purge_task']
        if include_orchestration:
            names.append('_refill_orchestration_task')
        current = self._current_async_task()
        pending = []
        seen = set()
        for name in names:
            task = getattr(self, name, None)
            if (task is not None and task is not current and not task.done()
                    and id(task) not in seen):
                seen.add(id(task))
                task.cancel()
                pending.append(task)
                self.logger.info(f"续料任务撤销({reason}): {name}")
        return pending

    def _on_state_changed(self, old_state: SystemStateEnum, new_state: SystemStateEnum, payload: Dict[str, Any]):
        """当状态机状态改变时，执行相应的动作"""
        self.logger.info(f"State Change: {old_state.name} -> {new_state.name} | Payload: {payload}")
        # lancaigang261023120000 任务#540：结束/取消后旧等待任务不能再退料或发补料；
        # 离开FEEDING时撤销旧裁决与清缓冲，保持当前低重/超时重试编排自己的生命周期。
        if (not self._auto_feed_failed
                and new_state in (SystemStateEnum.IDLE, SystemStateEnum.ERROR, SystemStateEnum.PRINTING)):
            self._refill_failure_epoch = getattr(self, '_refill_failure_epoch', 0) + 1
            # lancaigang261025173000 任务#554-hotfix11: epoch 推进即宣告上一轮事务上下文作废, 必须同步清空。
            # 只递增 epoch 不清 context, 会让下一轮 _begin_refill_context 永久返回 None(自动续料静默不动)。
            self._refill_context = None
            self._pending_feed_after_can = None
            self._cancel_refill_tasks(f"进入{new_state.name}", include_orchestration=True)
        elif old_state in (SystemStateEnum.FEEDING, SystemStateEnum.RESUMING):
            self._cancel_refill_tasks(f"离开{old_state.name}进入{new_state.name}")

        # 任务#286 V1/V2/V3/V5: FEEDING进入/离开时刻维护(超时兜底数据源,
        # 统一在此覆盖所有转换路径, 不依赖各业务点单独赋值)
        if new_state == SystemStateEnum.FEEDING and self._feeding_started_at is None:
            self._feeding_started_at = time.monotonic()
            # lancaigang 260918 20:52:35 任务#296 P0-1改动1: 进入FEEDING同时启动最短驻留
            # 防护窗(治#295环4 stale假有料): 与_feeding_started_at同生命周期同源赋值,
            # 统一在此覆盖所有进入FEEDING的转换路径(低重/非低重/超时重试重进统一受益)
            self._feeding_min_hold_until = time.monotonic() + FEEDING_MIN_HOLD_SECONDS
            self._feeding_hold_log_at = time.monotonic()  # 窗内节流日志基准复位(新窗口新节流周期)
        # lancaigang261024104414 任务#554: PAUSED/RESUMING交接不重置首次FEEDING预算。
        # 真正成功(传感器恢复转RESUMING)或进入新打印/空闲时清零超时重试计数与放弃标志
        # (新打印任务新篇章, 不携带上一任务的失败计数)
        if not self._auto_feed_failed and new_state in [SystemStateEnum.PRINTING, SystemStateEnum.IDLE]:
            self._feeding_timeout_retry_count = 0
            self._feeding_giveup_logged = False
            # lancaigang261016 任务#446(用户261016定策): 续料成功恢复(或进入新打印/空闲)即复位本次
            # 自动续料相关状态机(含事件级分流标志 _low_weight_refill_pending)。核心修复: 原成功路径
            # (转RESUMING)不清分流标志, 致本次低重续料成功后的 _low_weight_refill_pending 残留, 使本
            # 会话后续独立runout仍被误判为"低重续料在途"走完整退料序列(旧盘三段0x1E分段退料)。
            self._reset_auto_refill_state(f"续料恢复/进入{new_state.name}", full=True)
        # lancaigang261015 任务#442: 自动续料失败锁解除——仅新一轮打印(进入PRINTING)或回到IDLE时解除;
        # 插料盘/传感器变化/RESUMING均不解除(锁定后正常路径不会走到RESUMING)
        if new_state in (SystemStateEnum.PRINTING, SystemStateEnum.IDLE):
            self._copy_refill_context = None
        # 内部PRINTING/IDLE不是人工事件；失败只由已确认打印状态结束入口解除。
        
        # lancaigang261017 运行态持久化: 每次状态迁移都落盘(字段已在本方法内维护完);
        # 进入IDLE=本单结束(完成/取消/空闲), 快照立即作废, 防带入下一单。
        if new_state == SystemStateEnum.IDLE and not self._auto_feed_failed:
            self._clear_runtime_state(f"进入IDLE({old_state.name}->IDLE)")
        else:
            self._save_runtime_state(f"状态迁移{old_state.name}->{new_state.name}", force=True)

        # 创建异步任务来处理状态变化的动作
        asyncio.create_task(self._handle_state_change_actions(old_state, new_state, payload))
    
    def _check_feeding_timeout(self):
        """
        任务#286: FEEDING态超时兜底检查(审计#282 V1/V2/V3/V5静默死等族修复)
        
        背景: 旧版FEEDING态只轮询打印头传感器恢复, 对F05侧失败(ERROR粘滞/session保留/
        分流⑤可重拉)与0x01静默丢帧(CAN层发送成功但F05没收)全程无感知, hub永卡FEEDING死等。
        
        兜底: FEEDING持续>FEEDING_TIMEOUT_SECONDS(300s>F05 INIT_FEED最长300s不误伤)时:
        - 未达重试上限: WARN日志→回PAUSED→PAUSED轮询≤5s自动重走编排重发0x01(天然重试
          循环, F05侧分流⑤粘滞态正好接住, #262设计闭环补全)
        - 达到上限(FEEDING_MAX_TIMEOUT_RETRIES): ERROR日志→回PAUSED→PAUSED轮询门槛拦截
          不再触发编排, 保持PAUSED等人工(防重试风暴刷屏)
        
        调用时机: _handle_klipper_status_update的FEEDING分支, 传感器恢复检查之后
        (传感器恢复优先, 防误伤正常长续料)。
        """
        if self._auto_feed_failed:
            return
        if self._feeding_started_at is None:
            self._fail_current_refill("FEEDING起点缺失，不能重新授予300秒预算")
            return
        elapsed = time.monotonic() - self._feeding_started_at
        if elapsed < 0 or elapsed >= FEEDING_TIMEOUT_SECONDS:
            self._fail_current_refill(f"FEEDING期限耗尽或时钟异常({elapsed:.1f}s)，不重试整套编排")
    
    # ===== lancaigang261016 qoder-agent-f01-leader 任务#446(用户261016定策): 单次自动续料状态机复位 =====
    def _reset_auto_refill_state(self, reason: str, full: bool = True):
        """复位"本次自动续料相关状态机", 防本次续料中间态串入下一次自动续料。

        用户261016定策: 本次自动续料无论成功或失败, 都要清空本次续料相关状态机, 不要串入
        下次自动续料; 或在下次自动续料开始时清初始状态。两侧挤出机共用同一单状态机
        (hub单状态机架构), 一处复位即覆盖左右两侧。调用点:
          · 端点(full=True): FEEDING→RESUMING(真实恢复)/进入PRINTING/IDLE(_on_state_changed)
            与 0x01 回执 STATUS_ERROR(弹窗失败); 连同 FEEDING 计时与分流标志一并复位。
          · 编排起点(full=False): _auto_change_filament / _runout_clear_and_feed 取得编排互斥后,
            只清"编排进度类"中间态(退料核验/停滞自愈), 给每次续料编排一个干净起点。

        刻意不复位(承重墙/在途/跨编排, 由各自生命周期管理):
          · _refill_orchestration_busy/_refill_orchestration_owner: 编排互斥, 由编排 finally 释放;
          · _low_weight_refill_pending: 低重链路"两趟借道"承重墙, 起点不复位(full=False), 仅端点复位;
          · _pause_orchestrated: 自愈#388 跨编排承重墙, 由 printing/失败端点复位;
          · _feeding_timeout_retry_count/_feeding_giveup_logged: 需跨 _runout_clear_and_feed 重试
            轮次累积(防重试风暴), 起点不复位;
          · _auto_feed_failed: 失败锁, 仅新打印/空闲解除(任务#442)。
        """
        # lancaigang261024104414 任务#554: 普通收尾不能清失败首因、退料证据和期限。
        if self._auto_feed_failed:
            return
        # 编排进度类中间态(每次续料编排都应有干净起点)
        self._retract_cmd_at = None
        self._retract_empty_ok = False
        self._retract_empty_since = None
        self._retract_verify_failed = False
        self._retract_verify_timeout_logged = False
        self._retract_toggle_count = 0
        self._retract_prev_has = None
        self._stuck_recover_since = None
        # lancaigang261025103309 任务#563: 闸门拦截计时属于本次续料, 起点/端点都归零(原从不复位, 跨次累计提前触发拦满)
        self._gate_block_since = None
        if full:
            # 端点复位: 连同 FEEDING 计时与分流标志一并清空
            # 注意: 刻意不清 _feeding_giveup_logged——它与失败锁 _auto_feed_failed 同生命周期:
            #   失败分支(0x01回执STATUS_ERROR)显式置 True 防刷屏; 成功端点由 _on_state_changed 的
            #   [RESUMING/PRINTING/IDLE] 分支显式清 False。若在此清会覆盖失败分支刚置的 True(死赋值),
            #   致失败锁定后再补打一条"已失败锁定"ERROR。
            self._feeding_started_at = None
            self._feeding_min_hold_until = None
            self._feeding_hold_log_at = 0.0
            self._feeding_timeout_retry_count = 0
            self._f05_nack_count = 0
            self._low_weight_refill_pending = False
            self._low_weight_seen_this_print = False
        self.logger.info(f"自动续料状态机复位({reason}): 已清本次续料中间态(退料核验/停滞自愈"
                         f"{'/FEEDING计时/分流标志' if full else ''}), 防串入下次续料(任务#446)")

    def _refill_attempt_is_current(self, failure_epoch: int) -> bool:
        """fixflow：失败锁解除只允许新轮次，旧异步发送不得继续推进状态。"""
        return (not self._auto_feed_failed
                and failure_epoch == self._refill_failure_epoch)

    def _copy_print_mode(self):
        """真实双车模式；不改写Klipper单值active_extruder。"""
        mode = (getattr(self.klipper_monitor, 'dual_carriage_info', {}) or {}).get('carriage_1')
        return mode if mode in ('COPY', 'MIRROR') else None

    def _copy_refill_ctx_live(self):
        """复制续料事务是否仍在途(任务#554-copy108-hotfix16 统一口径)。

        失败收尾刻意保留 _copy_refill_context 供编排 finally 做 RESUMING→PAUSED 身份比对收口,
        所以入口与状态回切判据都必须按 interrupted/complete 判活, 不能只看对象非空。
        旧实现 low_weight 已改判活、runout 仍是非空即拒, 两条链路口径分叉: runout 失败后
        停在 PAUSED 时残留对象会把后续纯 runout 永久挡在门外, 直到进 PRINTING/IDLE 才被
        _on_state_changed 清掉。
        """
        context = getattr(self, '_copy_refill_context', None)
        return (context is not None
                and not context.get('interrupted')
                and not context.get('complete'))

    def _copy_pure_runout(self):
        # 低重第二趟仍由既有完整链处理，不能被纯runout抢走。
        return self._copy_print_mode() and not getattr(self, '_low_weight_refill_pending', False)

    def _printing_extruders(self):
        return (0, 1) if self._copy_print_mode() else (self.klipper_monitor.active_extruder,)

    # ===== lancaigang261024163000 任务#554-copy108-hotfix6: COPY/MIRROR 双头加热看护 =====
    def _head_target_known(self, index: int):
        """本单内该头最近一次有效加热目标; None=本单没有证据(不瞎猜)。"""
        _v = getattr(self, '_head_target_snapshot', {}).get(index)
        return _v if _v is not None and _v >= COPY_HEATER_GUARD_MIN_KNOWN_TARGET else None

    def _heater_repair_target(self, index: int):
        """补发目标: 优先本单快照, 其次对侧当前目标; 都拿不到返回 None。"""
        _known = self._head_target_known(index)
        if _known:
            return _known
        _peer = (self.klipper_monitor.extruder_info, self.klipper_monitor.extruder1_info)[1 - index]
        _peer_target = float(_peer.get('target', 0) or 0)
        return _peer_target if _peer_target >= COPY_HEATER_GUARD_MIN_KNOWN_TARGET else None

    async def _send_head_heater_target(self, index: int, target: float, reason: str) -> bool:
        """补发单侧加热目标(M104 带 T 编号, 不串侧; 不阻塞等温, 不抢加热)。"""
        _now = time.monotonic()
        _guard_at = getattr(self, '_head_guard_at', None)
        if _guard_at is None:
            _guard_at = self._head_guard_at = {}
        if _now - _guard_at.get(index, 0.0) < COPY_HEATER_GUARD_INTERVAL_SECONDS:
            return False
        _guard_at[index] = _now
        _info = (self.klipper_monitor.extruder_info,
                 self.klipper_monitor.extruder1_info)[index]
        _cmd = 'M104 T%d S%.0f' % (index, target)
        _ok = await self.klipper_monitor.execute_gcode(_cmd)
        self.logger.warning(
            '[HEATER-GUARD] %s: 打印头%d 目标被清零(温度%.1f/目标%.1f, 已知目标%.0f), '
            '补发 %s -> %s'
            % (reason, index, float(_info.get('temperature', 0) or 0),
               float(_info.get('target', 0) or 0), target, _cmd, '已发送' if _ok else '发送失败'))
        return _ok

    async def _copy_heater_guard(self):
        """COPY/MIRROR 状态帧加热看护: 单侧目标被外部清零时自动补回。

        只在"对侧仍在加热且本侧已明显冷"时动手: 两侧同时归零(END_PRINT/CANCEL_PRINT/
        idle_timeout)一律不动, 避免与关机指令抢加热; 暂停中同样生效, 但同样要求对侧仍热。
        """
        km = self.klipper_monitor
        if not self._copy_print_mode():
            return
        _job = self._job_key_from_print_stats(km.print_stats)
        if _job != getattr(self, '_head_guard_job', None):
            self._head_guard_job = _job
            self._head_target_snapshot = {}
            self._head_guard_at = {}
        _state = km.print_stats.get('state') or km.printer_state
        if _state not in ('printing', 'paused'):
            return
        _infos = (km.extruder_info, km.extruder1_info)
        _targets = [float(_i.get('target', 0) or 0) for _i in _infos]
        for i in (0, 1):
            if _targets[i] >= COPY_HEATER_GUARD_MIN_KNOWN_TARGET:
                self._head_target_snapshot[i] = _targets[i]
        for i in (0, 1):
            if _targets[i] > 0:
                continue
            if _targets[1 - i] < COPY_HEATER_GUARD_MIN_KNOWN_TARGET:
                continue
            _want = self._heater_repair_target(i)
            if not _want:
                continue
            if float(_infos[i].get('temperature', 0) or 0) >= _want - COPY_HEATER_GUARD_LOW_TEMP_MARGIN:
                continue
            await self._send_head_heater_target(i, _want, '状态帧加热看护')

    async def _copy_heaters_ensure(self, context):
        """续料事务预挤前等双头到温: 补目标 + 等温, 不满足如实失败(不冷挤)。"""
        if not self._copy_print_mode():
            return
        km = self.klipper_monitor
        _deadline = time.monotonic() + COPY_HEATER_WAIT_MAX_SECONDS
        _wait_logged = False
        while True:
            if not self._copy_refill_is_current(context):
                raise RuntimeError('双头加热等待中归属/连接已变化')
            _infos = (km.extruder_info, km.extruder1_info)
            _snap = getattr(self, '_head_target_snapshot', None)
            if _snap is None:
                _snap = self._head_target_snapshot = {}
            _cold = []
            for i, _info in enumerate(_infos):
                _target = float(_info.get('target', 0) or 0)
                _temp = float(_info.get('temperature', 0) or 0)
                if _target >= COPY_HEATER_GUARD_MIN_KNOWN_TARGET:
                    _snap[i] = _target
                    if _temp < _target - COPY_HEATER_GUARD_LOW_TEMP_MARGIN:
                        _cold.append(i)
                    continue
                _want = self._heater_repair_target(i)
                if _want is None:
                    raise RuntimeError('打印头%d无已知加热目标，拒绝冷态预挤' % i)
                _cold.append(i)
                await self._send_head_heater_target(i, _want, '续料事务预挤前补加热目标')
            if not _cold:
                self.logger.info('[COPY-REFILL] 双头加热就绪: T0=%.1f/%.1f T1=%.1f/%.1f'
                                 % (float(_infos[0].get('temperature', 0) or 0),
                                    float(_infos[0].get('target', 0) or 0),
                                    float(_infos[1].get('temperature', 0) or 0),
                                    float(_infos[1].get('target', 0) or 0)))
                return
            if not _wait_logged:
                _wait_logged = True
                self.logger.info('[COPY-REFILL] 预挤前等待双头到温: 冷头=%s T0=%.1f/%.1f T1=%.1f/%.1f'
                                 % (_cold,
                                    float(_infos[0].get('temperature', 0) or 0),
                                    float(_infos[0].get('target', 0) or 0),
                                    float(_infos[1].get('temperature', 0) or 0),
                                    float(_infos[1].get('target', 0) or 0)))
            if time.monotonic() >= _deadline:
                raise RuntimeError('双头加热等待超时(冷头不做预挤)')
            await asyncio.sleep(1.0)

    def _copy_refill_gate(self, context):
        """归属门逐项诊断: 返回不满足项的中文列表(空列表=全部通过)。只读, 不推进任何状态。

        lancaigang261024175000 任务#554-copy108-hotfix13: 旧实现只返回布尔, 现场 17:48:58
        只看到一句"退料中归属/连接已变化", 十几项硬条件里是哪一项不成立完全无从判断——
        真相是它自己刚切的活跃挤出机还没切回来。凡多条件与门都必须能说出失败的那一项。
        """
        if context is None:
            return ['无续料事务上下文']
        km = self.klipper_monitor
        mapping = self.config.get('extruders', {}).get('mapping', {})
        bad = []
        if getattr(self, '_copy_refill_context', None) is not context:
            bad.append('事务上下文已被替换或释放')
        if context.get('interrupted'):
            bad.append('事务已被中断标记')
        if context.get('complete'):
            bad.append('事务已完成')
        if self._auto_feed_failed:
            bad.append('本轮已置失败锁')
        if context.get('epoch') != self._refill_failure_epoch:
            bad.append('失败轮次epoch已变(%s->%s)' % (context.get('epoch'), self._refill_failure_epoch))
        if self.state_manager.state not in (SystemStateEnum.PAUSED, SystemStateEnum.FEEDING,
                                            SystemStateEnum.RESUMING):
            bad.append('系统状态=%s' % self.state_manager.state)
        if not km.ws_connected:
            bad.append('Klipper WebSocket未连接')
        if not self.can_comm.connected:
            bad.append('F05 CAN未连接')
        if not km.pause_resume_is_paused:
            bad.append('pause_resume未暂停')
        if km.printer_state != 'paused':
            bad.append('Klipper打印状态=%s' % km.printer_state)
        if self._last_printer_state != 'paused':
            bad.append('hub上次打印状态=%s' % self._last_printer_state)
        if km.toolhead_info.get('extruder') != 'extruder':
            bad.append('toolhead活跃挤出机=%s' % km.toolhead_info.get('extruder'))
        if km.active_extruder != 0:
            bad.append('active_extruder=%s' % km.active_extruder)
        if self._copy_print_mode() != context.get('mode'):
            bad.append('双车模式=%s(期望=%s)' % (self._copy_print_mode(), context.get('mode')))
        if (mapping.get(0), mapping.get(1)) != context.get('mapping'):
            bad.append('映射=%s(期望=%s)' % ((mapping.get(0), mapping.get(1)), context.get('mapping')))
        if self._job_key_from_print_stats(km.print_stats) != context.get('job'):
            bad.append('任务身份=%s(期望=%s)'
                       % (self._job_key_from_print_stats(km.print_stats), context.get('job')))
        return bad

    def _copy_refill_is_current(self, context):
        return not self._copy_refill_gate(context)

    def _copy_refill_stale_error(self, context, label: str):
        """归属门不通过时构造带原因列表的异常, 现场一眼能看出是哪一项变了。"""
        _why = self._copy_refill_gate(context)
        if not _why:
            return RuntimeError(label)
        return RuntimeError('%s -> %s' % (label, '; '.join(_why)))

    async def _copy_wait_pause_confirmed(self, context):
        """等 pause_resume 帧落地；返回 confirmed/timeout/invalid。

        lancaigang261024114500 任务#554-copy108-hotfix2: 状态帧与 pause_resume 帧不同批到达时,
        立即判 _copy_refill_is_current 必然失败。这里只等"暂停确认"这一项, 其余归属条件一旦
        变化立即 invalid(不推进), 超时 timeout(保持 PAUSED 但不置失败锁)。
        """
        km = self.klipper_monitor
        deadline = time.monotonic() + COPY_PAUSE_CONFIRM_TIMEOUT_SECONDS
        while True:
            if (getattr(self, '_copy_refill_context', None) is not context
                    or context.get('interrupted') or context.get('complete')
                    or not self._refill_attempt_is_current(context.get('epoch'))
                    or self.state_manager.state != SystemStateEnum.PAUSED
                    or not km.ws_connected or not self.can_comm.connected
                    or self._copy_print_mode() != context.get('mode')
                    or self._job_key_from_print_stats(km.print_stats) != context.get('job')):
                return 'invalid'
            if (km.pause_resume_is_paused and km.printer_state == 'paused'
                    and self._last_printer_state == 'paused'):
                self.logger.info('[COPY-REFILL] 已确认同单真实暂停(pause_resume在途帧已落地)')
                return 'confirmed'
            if time.monotonic() >= deadline:
                return 'timeout'
            await asyncio.sleep(0.2)

    def _copy_heads_ready(self):
        """双头共同预挤/恢复硬门，未知或冷却不按主头有料降级放行。"""
        km = self.klipper_monitor
        if (km.toolhead_info.get('extruder') != 'extruder'
                or km.extruder1_info.get('motion_queue') != 'extruder'
                or km.extruder_info.get('motion_queue') not in (None, 'extruder')):
            return False, '复制双头挤出队列不同步'
        for i, info in enumerate((km.extruder_info, km.extruder1_info)):
            since = self._sensor_fil_since.get(km.filament_sensor_objects[i])
            if (self._active_sensor_has_filament(i) is not True or since is None
                    or time.monotonic() - since < RESUME_SENSOR_HOLD_SECONDS):
                return False, f'打印头{i}尚未稳定有料'
            if (info.get('can_extrude') is not True or info.get('temperature', 0) <= 175
                    or info.get('target', 0) <= 175):
                return False, f'打印头{i}温度/加热目标不满足预挤条件'
        _vel = float(km.motion_report.get('live_extruder_velocity', 0) or 0)
        # lancaigang261024160000 任务#554-copy108-hotfix8: 108 现场 15:51:07 到料后仍判"挤出机仍在
        # 退料(vel=-0.339mm/s)"中止、保持 PAUSED 不恢复。该值正是上一次真实退料的速度残值——
        # paused 下 motion_report 不归零。上一版只把阈值从 -0.5 收到 -0.05(治浮点噪声), 治不了
        # 真实残值。加运动队列活跃判定: 只有 print_time 还在推进(真在执行退料动作)才拦。
        _mats = getattr(km, '_motion_alive_ts', None)
        if _mats is None:
            _mats = getattr(km, '_last_motion_ts', None)  # 拿不到 print_time 时退回旧口径, 不放开安全门
        if (_vel <= COPY_RETRACT_VEL_THRESHOLD and _mats is not None
                and (time.monotonic() - _mats) <= RESUME_MOTION_FRESH_SECONDS):
            return False, '挤出机仍在退料(vel=%.3fmm/s, 运动队列活跃)' % _vel
        return True, '复制双头均已稳定有料且可同步挤出'

    def _copy_mode_label(self) -> str:
        """COPY=复制, MIRROR=镜像; 两者共用同一套续料事务, 仅在日志与证据里区分。"""
        return '镜像' if self._copy_print_mode() == 'MIRROR' else '复制'

    def _copy_purge_script(self, distance_mm: float) -> str:
        """lancaigang261024143000 任务#554-copy108-hotfix4: COPY/MIRROR 双头预挤脚本。

        显式重申 extruder1->extruder 挤出队列同步: 该指令幂等, 且 COPY/MIRROR 两个宏
        (ACTIVATE_COPY_MODE/ACTIVATE_MIRROR_MODE)在打印机侧本来就是同一条同步, 所以对
        两种双车模式都成立; 但它把"预挤一定同时驱动两个挤出机"从"假设"变成"本次显式声明"。
        """
        return ('SAVE_GCODE_STATE NAME=HUB_COPY_REFILL\n'
                'SYNC_EXTRUDER_MOTION EXTRUDER="extruder1" MOTION_QUEUE="extruder"\n'
                'M83\n'
                'G1 E%.1f F%.0f\n'
                'M400\n'
                'RESTORE_GCODE_STATE NAME=HUB_COPY_REFILL') % (distance_mm, COPY_PURGE_FEEDRATE)

    async def _copy_gcode_error_since(self, after_ts: float):
        """取回 Klipper 在 after_ts 之后的运行期报错; 取不到(传输异常)返回 None 表示不可取证。

        现场教训: _send_gcode 返回 True 只代表 Moonraker 受理了脚本请求, Klipper 运行期
        拒绝(Extrude only move too long / Move out of range / 低温)不会变成 jsonrpc error,
        旧实现于是把"命令被拒"当成"预挤成功"继续 RESUME。不可取证时不反向锁死打印。
        """
        store = await self.klipper_monitor.fetch_gcode_store(60)
        if store is None:
            return None
        hits = []
        for item in store:
            try:
                _ts = float(item.get('time') or 0.0)
            except (TypeError, ValueError):
                continue
            if _ts <= after_ts:
                continue
            _msg = (item.get('message') or '').strip()
            if (_msg.startswith('!!') or 'Extrude only move too long' in _msg
                    or 'Move out of range' in _msg
                    or 'below minimum temp' in _msg):
                hits.append(_msg[:120])
        return hits

    async def _copy_purge_once(self, context, distance_mm: float, label: str):
        """单段预挤 + 前后硬校验(双车模式/挤出队列同步/Klipper 运行期报错)。返回 (ok, reason)。"""
        km = self.klipper_monitor
        _mode = context.get('mode')
        if self._copy_print_mode() != _mode:
            return False, '%s预挤前双车模式已变(%s->%s)' % (label, _mode, self._copy_print_mode())
        if (km.extruder1_info.get('motion_queue') != 'extruder'
                or km.extruder_info.get('motion_queue') not in (None, 'extruder')):
            return False, '%s预挤前挤出队列未同步(挤出机=%s, 挤出机1=%s)' % (
                label, km.extruder_info.get('motion_queue'), km.extruder1_info.get('motion_queue'))
        _store = await self.klipper_monitor.fetch_gcode_store(20)
        _since = 0.0
        for item in (_store or []):
            try:
                _since = max(_since, float(item.get('time') or 0.0))
            except (TypeError, ValueError):
                continue
        if not await km.execute_gcode(self._copy_purge_script(distance_mm)):
            return False, '%s预挤指令未被受理(%.0fmm)' % (label, distance_mm)
        _errs = await self._copy_gcode_error_since(_since)
        if _errs:
            return False, '%s预挤被Klipper拒绝: %s' % (label, _errs[0])
        if self._copy_print_mode() != _mode:
            return False, '%s预挤期间双车模式被打断(%s->%s)' % (label, _mode, self._copy_print_mode())
        if km.extruder1_info.get('motion_queue') != 'extruder':
            return False, '%s预挤期间挤出队列同步被打断(挤出机1=%s)' % (
                label, km.extruder1_info.get('motion_queue'))
        return True, '%s预挤%.0fmm已确认执行' % (label, distance_mm)

    async def _copy_purge_verified(self, context) -> None:
        """lancaigang261024143000 任务#554-copy108-hotfix4: COPY/MIRROR 分段预挤与出料确认。

        总行程不变(COPY_PURGE_TOTAL_MM), 分 COPY_PURGE_SEGMENTS 段, 段间复核:
        双车模式未变、extruder1 仍同步到 extruder 队列、Klipper 无运行期拒绝。
        最后复核本次续料头的断料开关仍稳定"有料"(预挤窗口内翻转=料没被真实吃掉/送料不稳,
        一律停在 PAUSED 等人工, 不再带病恢复打印)。
        """
        total = float(COPY_PURGE_TOTAL_MM)
        seg = max(1, int(COPY_PURGE_SEGMENTS))
        per = total / seg
        fed = list(context.get('fed_heads') or [])
        _label = self._copy_mode_label()
        self.logger.info('[COPY-REFILL] %s双头分段预挤: 共%.0fmm/%d段, 本次续料头=%s' % (_label, total, seg, fed))
        for idx in range(seg):
            _ok, _reason = await self._copy_purge_once(context, per, '%s第%d/%d段' % (_label, idx + 1, seg))
            if not _ok:
                raise RuntimeError(_reason)
            if not self._copy_refill_is_current(context):
                raise RuntimeError('%s预挤第%d段后归属已变化' % (_label, idx + 1))
            self.logger.info('[COPY-REFILL] %s' % _reason)
        _bad = [i for i in fed if self._active_sensor_has_filament(i) is not True]
        if _bad:
            raise RuntimeError('%s预挤出料确认失败: 续料头%s断料开关已非稳定有料' % (_label, _bad))
        self.logger.info('[COPY-REFILL] %s预挤出料确认通过(续料头%s开关稳定有料, 双车模式=%s)'
                         % (_label, fed, self._copy_print_mode()))

    async def _copy_runout_and_resume(self):
        """lancaigang261024113000 任务#554-copy108：现场头1无料被active=0吞掉；分管串行、共同恢复。"""
        # lancaigang261024200000 任务#554-copy108-hotfix16: 与低重链路同口径, 按事务存活判定;
        # 且拒绝原因必须分项打点(旧实现整句静默 return, 现场无法归因)。
        if (self._auto_feed_failed or self._copy_refill_ctx_live()
                or self._refill_motion_busy() or self.state_manager.state != SystemStateEnum.PAUSED):
            _why = []
            if self._auto_feed_failed:
                _why.append('失败锁')
            if self._copy_refill_ctx_live():
                _ctx = getattr(self, '_copy_refill_context', None) or {}
                _why.append('在途复制事务(mode=%s owner=%s tube=%s)'
                            % (_ctx.get('mode'), _ctx.get('owner'), _ctx.get('tube')))
            if self._refill_motion_busy():
                _why.append('编排/清缓冲/恢复任务在途')
            if self.state_manager.state != SystemStateEnum.PAUSED:
                _why.append('状态不符(%s)' % self.state_manager.state.name)
            self.logger.info('[COPY-REFILL] 纯runout续料未启动: ' + '/'.join(_why))
            return
        missing = [i for i in (0, 1) if self._active_sensor_has_filament(i) is False]
        if not missing:
            return
        # lancaigang261025103309 任务#563: 复制双头同样只续断料/hub自有暂停; 人工暂停后拔料不续不恢复
        if not self._pause_refill_source_ok('COPY续料入口'):
            return
        mapping = self.config.get('extruders', {}).get('mapping', {})
        context = {'mode': self._copy_print_mode(), 'mapping': (mapping.get(0), mapping.get(1)),
                   'job': self._job_key_from_print_stats(self.klipper_monitor.print_stats),
                   'epoch': self._refill_failure_epoch, 'tube': None, 'seq': None, 'complete': False, 'fed_heads': []}
        task = asyncio.current_task()
        self._copy_refill_context = context
        self._refill_orchestration_busy = True
        self._refill_orchestration_task = task
        self._refill_orchestration_owner = missing[0]
        self._pause_orchestrated = True
        self._cancel_refill_tasks('进入复制纯runout')
        self.logger.info(f"[COPY-REFILL] 开始 mode={context['mode']} missing={missing} mapping={context['mapping']}")
        try:
            if (set(context['mapping']) != {0, 1} or not context['job']
                    or self._low_weight_refill_pending):
                raise RuntimeError('映射/任务身份无效或低重事务尚未结束')
            self._reset_auto_refill_state('复制纯runout', full=False)
            # 状态帧先到、pause_resume 帧后到时不能立刻判死(108实测差约1秒)。
            _pause = await self._copy_wait_pause_confirmed(context)
            if _pause != 'confirmed':
                if _pause == 'timeout':
                    context['pause_unconfirmed'] = True
                raise RuntimeError(f'未确认同单真实暂停({_pause})')
            if not self._copy_refill_is_current(context):
                raise RuntimeError('未确认同单真实暂停')
            if not await self.klipper_monitor.execute_gcode('M400'):
                raise RuntimeError('停稳确认失败')
            await asyncio.sleep(10.0)
            for extruder in (0, 1):
                if not self._copy_refill_is_current(context):
                    raise RuntimeError('续料归属/连接已变化')
                has = self._active_sensor_has_filament(extruder)
                if has is None:
                    raise RuntimeError(f'打印头{extruder}传感器状态未知')
                if has is True:
                    continue
                self._refill_orchestration_owner = extruder
                context['tube'] = context['mapping'][extruder]
                context['seq'] = (self.can_comm._feed_seq + 1) & 0xFF
                self._session_vacancy_seen = True
                started = time.monotonic()
                self.state_manager.transition_to(SystemStateEnum.FEEDING, extruder=extruder, copy_owned=True)
                self._save_runtime_state('复制续料发送前', force=True)
                self.logger.info(f"[COPY-REFILL] 请求 head={extruder} tube={context['tube']} seq={context['seq']}")
                if not await self.can_comm.request_feed(tube_id=context['tube']):
                    raise RuntimeError('0x01发送失败')
                while True:
                    if not self._copy_refill_is_current(context):
                        raise self._copy_refill_stale_error(context, '续料等待中归属/连接已变化')
                    now = time.monotonic()
                    sensor = self.klipper_monitor.filament_sensor_objects[extruder]
                    since = self._sensor_fil_since.get(sensor)
                    if (self._active_sensor_has_filament(extruder) is True and since is not None
                            and now - since >= RESUME_SENSOR_HOLD_SECONDS
                            and now - started >= FEEDING_MIN_HOLD_SECONDS):
                        break
                    if now - started >= FEEDING_TIMEOUT_SECONDS:
                        raise RuntimeError(f'打印头{extruder}续料超时，不重发')
                    await asyncio.sleep(0.5)
                self.logger.info(f"[COPY-REFILL] 到料 head={extruder} tube={context['tube']}")
                context.setdefault('fed_heads', []).append(extruder)
                self.logger.info('[COPY-REFILL] 本次续料头登记 head=%d (本轮已续料=%s)'
                                 % (extruder, context.get('fed_heads')))
                context['tube'] = None
                context['seq'] = None
            # lancaigang261024163000 任务#554-copy108-hotfix6: 预挤前先补双头加热目标并等温
            # (否则 _copy_heads_ready 的 target>175 硬门会因外部清零而永久 blocked, 或冷头硬预挤)。
            await self._copy_heaters_ensure(context)
            ready, reason = self._can_resume_now()
            if not ready:
                raise RuntimeError(reason)
            await self._copy_purge_verified(context)
            if not self._copy_refill_is_current(context):
                raise self._copy_refill_stale_error(context, '预挤后归属已变化')
            self.state_manager.transition_to(SystemStateEnum.RESUMING, copy_owned=True)
            context['resuming'] = True
            if await self._resume_print_gated() != 'sent':
                raise RuntimeError('复制双头恢复门未通过或RESUME失败')
            context['complete'] = True
            self.logger.info('[COPY-REFILL] RESUME已发送，等待printing确认')
        except asyncio.CancelledError:
            # printing可先于RESUME回执到达；其余取消均不得补投旧命令。
            if context.get('resuming') and self._last_printer_state == 'printing':
                context['complete'] = True
            raise
        except Exception as e:
            self.logger.error(f'[COPY-REFILL] 停止自动续料：{e}；保持暂停等待人工')
        finally:
            if not context['complete']:
                if context.get('pause_unconfirmed'):
                    # 暂停尚未被 Klipper 确认, 属时序竞态而非续料失败; 不写失败锁, 等人工/后续PAUSED帧。
                    self.logger.warning('[COPY-REFILL] 暂停确认超时：不推进续料、不置失败锁，保持PAUSED等待人工')
                elif self._copy_refill_context is context and not self._auto_feed_failed:
                    self._apply_feed_failure(4, 0, context.get('tube'), 0, '复制续料终止')
                if self._copy_refill_context is context and self.state_manager.state == SystemStateEnum.RESUMING:
                    self.state_manager.transition_to(SystemStateEnum.PAUSED, copy_owned=True)
                if context.get('tube') is not None and self.can_comm.connected:
                    try:
                        await self.can_comm.stop_feed(tube_id=context['tube'])
                    except Exception as e:
                        self.logger.error(f'[COPY-REFILL] 停料发送失败：{e}')
            if self._refill_orchestration_task is task:
                self._refill_orchestration_task = None
                self._refill_orchestration_owner = None
                self._refill_orchestration_busy = False
            self._save_runtime_state('复制续料退出', force=True)

    def _copy_retract_assist_script(self, distance_mm: float, extruder: int) -> str:
        """lancaigang261025100000 任务#554-copy108-hotfix10: 单头协同退料脚本(相对挤出反转)。

        hotfix9 用 extruder1->extruder 的同步声明让两个头一起反转 3mm。实测 108 16:22:47
        脚本确实被受理, 但 3mm 远小于挤出机齿轮到喷嘴的熔融段长度, 线仍被热端锚住,
        F05 一抽还是硬拽。hotfix10:
          (1) 量: 单次 25mm, 足以把线从熔融段拉脱, 欠料由 100mm 预挤补回;
          (2) 单头: 先把 extruder1 解绑到自身队列, 再 ACTIVATE_EXTRUDER 切到本次低重的
              那一个头, 只退这一侧。另一侧料是好的, 白退会把好料也抽出来。
        还原动作放在独立脚本(见 _copy_retract_assist_restore_script), 不依赖本脚本成败。
        """
        _tgt = 'extruder1' if int(extruder) == 1 else 'extruder'
        return ('SAVE_GCODE_STATE NAME=HUB_COPY_RETRACT\n'
                'SYNC_EXTRUDER_MOTION EXTRUDER="extruder1" MOTION_QUEUE="extruder1"\n'
                'ACTIVATE_EXTRUDER EXTRUDER="%s"\n'
                'M83\n'
                'G1 E-%.1f F%.0f\n'
                'M400\n'
                'RESTORE_GCODE_STATE NAME=HUB_COPY_RETRACT') % (_tgt, distance_mm, COPY_RETRACT_ASSIST_FEEDRATE)

    def _copy_retract_assist_restore_script(self) -> str:
        """把 COPY/MIRROR 的挤出队列同步与活跃挤出机还原, 与退料脚本成败无关都要发。

        退料脚本解绑了 extruder1 并切过 ACTIVATE_EXTRUDER。不还原的话 _copy_purge_once 的
        "extruder1 必须同步到 extruder"前置校验会硬拦(不会带病 RESUME), 但本次续料就白做了,
        所以每次反转后都必须显式还原。
        """
        return ('SYNC_EXTRUDER_MOTION EXTRUDER="extruder1" MOTION_QUEUE="extruder"\n'
                'ACTIVATE_EXTRUDER EXTRUDER="extruder"\n'
                'M400')

    async def _copy_wait_owner_back(self, label: str, timeout: float = None) -> bool:
        """等单头反转脚本里的 ACTIVATE_EXTRUDER 还原真正落地。

        lancaigang261024175000 任务#554-copy108-hotfix13: 合并脚本里还原段排在 G1+M400 之后,
        所以"活跃挤出机/挤出队列回到 extruder"等价于"反转运动已完成且队列已重新同步",
        比固定 sleep 可靠(25mm@F300 实测 5.36s)。返回 False 即真的没归位, 由归属门中止。
        """
        km = self.klipper_monitor
        _limit = COPY_RETRACT_OWNER_BACK_TIMEOUT_SECONDS if timeout is None else timeout
        _t0 = time.monotonic()
        while time.monotonic() - _t0 < _limit:
            if (km.toolhead_info.get('extruder') == 'extruder'
                    and km.active_extruder == 0
                    and km.extruder1_info.get('motion_queue') in (None, 'extruder')):
                self.logger.info('[COPY-REFILL] 单头反转落地确认(%s): 活跃挤出机/挤出队列已归位, 耗时%.1fs'
                                 % (label, time.monotonic() - _t0))
                return True
            await asyncio.sleep(0.2)
        return False

    async def _copy_retract_head_assist(self, context, label: str, extruder: int):
        """退料前让本次低重的那个打印头单独反转, 把线从热端送出; 任何异常都不阻断 F05 退料。"""
        km = self.klipper_monitor
        _used = float(context.get('retract_assist_mm') or 0.0)
        if _used >= COPY_RETRACT_ASSIST_MAX_MM:
            return
        _step = min(COPY_RETRACT_ASSIST_MM, COPY_RETRACT_ASSIST_MAX_MM - _used)
        # hotfix10: 只校验本次要退的那个头; 另一侧不参与反转, 其温度不影响本次动作。
        _info = km.extruder1_info if int(extruder) == 1 else km.extruder_info
        _temp = float(_info.get('temperature', 0) or 0)
        if _temp < COPY_RETRACT_ASSIST_MIN_TEMP:
            self.logger.warning('[COPY-REFILL] 打印头协同退料跳过(%s): 打印头%d热端过冷 %.1f\u2103'
                                % (label, extruder, _temp))
            return
        _store = await km.fetch_gcode_store(20)
        _since = 0.0
        for item in (_store or []):
            try:
                _since = max(_since, float(item.get('time') or 0.0))
            except (TypeError, ValueError):
                continue
        # hotfix13: 反转与还原必须同批下发, 且要等"真正落地"再往下走。
        # _send_gcode 只等 Moonraker 受理(不等运动执行): 108 实测 17:48:58 assist 受理后
        # 15ms 就受理了还原, 而还原实际排在 G1 E-25(5.36s)之后才执行; 此刻 toolhead 仍是
        # extruder1, 下一句 _copy_refill_is_current 立即判"归属已变化" -> 整条低重续料中止
        # 并置失败锁(现场现象: 槽10/通道9 低重自动续料"没反应")。
        _script = (self._copy_retract_assist_script(_step, extruder) + '\n'
                   + self._copy_retract_assist_restore_script())
        _ok = await km.execute_gcode(_script)
        if not _ok:
            self.logger.warning('[COPY-REFILL] 打印头协同退料指令未被受理(%s, %.1fmm), 退回纯F05抽线'
                                % (label, _step))
            # 受理超时的脚本仍可能迟到生效, 因此仍需等归位, 免得下游归属门误判
            if not await self._copy_wait_owner_back(label, 4.0):
                self.logger.error('[COPY-REFILL] 协同退料未受理且活跃挤出机未归位(%s): '
                                  'toolhead=%s active=%s 挤出队列=%s'
                                  % (label, km.toolhead_info.get('extruder'), km.active_extruder,
                                     km.extruder1_info.get('motion_queue')))
            return
        _back_at = time.monotonic()
        if not await self._copy_wait_owner_back(label):
            self.logger.error('[COPY-REFILL] 单头反转后活跃挤出机未归位(%s): toolhead=%s active=%s 挤出队列=%s, '
                              '按归属门中止本事务'
                              % (label, km.toolhead_info.get('extruder'), km.active_extruder,
                                 km.extruder1_info.get('motion_queue')))
            return
        _errs = await self._copy_gcode_error_since(_since)
        if _errs:
            self.logger.warning('[COPY-REFILL] 打印头协同退料被Klipper拒绝(%s): %s, 退回纯F05抽线'
                                % (label, _errs[0]))
            return
        context['retract_assist_mm'] = _used + _step
        self.logger.info('[COPY-REFILL] 打印头协同退料已执行: %s 打印头%d %.1fmm(单头反转, 累计%.1fmm, '
                         '落地归位耗时%.1fs), 交F05抽回'
                         % (label, extruder, _step, context['retract_assist_mm'],
                            time.monotonic() - _back_at))

    async def _copy_retract_head_until_empty(self, context, extruder: int, tube: int):
        """lancaigang261024160000 任务#554-copy108-hotfix5: 低重前单侧退料(F05 0x1E)。

        为什么不能用 _runout_clear_and_feed 的 UNLOAD_FILAMENT 步骤: 该宏默认
        RETRACT_LENGTH=300mm, 在 COPY/MIRROR 下会连另一侧好料一起抽空。
        低重退料必须由两段协同完成(hotfix10): 先让本次低重的那个打印头单独反转 25mm
        (见 _copy_retract_head_assist), 把线从热端熔融段拉脱, 再由 F05 侧 0x1E 把管内
        线尾抽回(0x1E 携带 buffer 编号, 物理上只动这一侧)。少了打印头那一段, F05 就是
        在送料柜硬拽线材: 线被拉细/拉扁(0x01 顶不进去), 重则拉断在管内。
        退不干净(3次补退后仍有料)绝不发 0x01: 新料会顶住旧线尾堵管, 保持 PAUSED 等人工。
        """
        seg_plan = ((3, 0, '第一段(20mm, 10mm/s)'),
                    (4, 0, '第二段(50mm, 10mm/s)'),
                    (4, 5, '第三段(50mm, 60mm/s)'))
        self._last_f05_nack_reason = None
        # hotfix9/hotfix10: 先把线从热端送出来, 再由 F05 抽; 顺序反了就是送料柜硬拽线材。
        await self._copy_retract_head_assist(context, '首段前', extruder)
        for seg_idx, (seg_dist, seg_spd, seg_label) in enumerate(seg_plan):
            if not self._copy_refill_is_current(context):
                raise self._copy_refill_stale_error(context, '退料中归属/连接已变化')
            if self._active_sensor_has_filament(extruder) is False:
                self.logger.info('[COPY-REFILL] 低重退料: 打印头%d在本段前已无料, 跳过剩余退料段' % extruder)
                break
            if seg_idx > 0:
                await asyncio.sleep(4.5)
            self.logger.info('[COPY-REFILL] 低重退料 送料柜%s tube=%d head=%d' % (seg_label, tube, extruder))
            if not await self.can_comm.retract_filament(tube, distance=seg_dist, speed=seg_spd):
                raise RuntimeError('%s退料命令发送失败' % seg_label)
            await asyncio.sleep(1.0)  # NACK回帧观察窗: 0x1F 一般在命令发出后1s内到达
            if self._last_f05_nack_reason == 0x01:
                self.logger.warning('[COPY-REFILL] 低重退料 %s 被F05拒绝(原因码0x01 运动锁窗口外): '
                                    '判定F05已自行退料, 中止剩余段, 交传感器确认' % seg_label)
                break
        await self.klipper_monitor.execute_gcode('M400')
        await asyncio.sleep(2.0)  # 传感器稳定窗(与低重/runout链路同款)
        # lancaigang261024154500 任务#554-copy108-hotfix7: 108现场 15:13:19 / 15:29:11 两次
        # 低重续料全部中止在退料验证(120mm三段退完 + 3x10mm补退后传感器仍"有料"), 判失败并锁定;
        # 而心跳铁证: 15:18:51 才第一次出现"传感器=[有,无]"——料头正好停在传感器临界点,
        # 判死(15:14:01)之后 4分50秒才真正脱开, 与"退料无效"的结论相反。
        # 原窗口 = M400 + 2s 稳定 + 3x(10mm退料@10mm/s + 1.5s) ≈ 6.5s, 比物理迟滞小一个数量级。
        # 改: 补退 20mm(档位3)x5, 每轮等 2.5s; 判死后加 15s 只读观察窗(不动电机)。
        # 只加退料量与等待, 不改判据方向、不新增全局开关/闸门。
        for retry in range(5):
            if not self._copy_refill_is_current(context):
                raise self._copy_refill_stale_error(context, '退料确认中归属/连接已变化')
            if self._active_sensor_has_filament(extruder) is False:
                self.logger.info('[COPY-REFILL] 低重退料到位: 打印头%d传感器无料(第%d次确认)' % (extruder, retry + 1))
                return
            self.logger.warning('[COPY-REFILL] 打印头%d仍有料, 第%d/5次补退(20mm@10mm/s)' % (extruder, retry + 1))
            await self._copy_retract_head_assist(context, '补退%d' % (retry + 1), extruder)
            if not await self.can_comm.retract_filament(tube, 3, 0):
                raise RuntimeError('补充退料命令发送失败')
            await asyncio.sleep(2.5)
        # 长观察窗: 料尾可能已被退过传感器, 但开关复位/料在临界点回弹需要时间(108实测滞后~5min,
        # 15s 只覆盖开关复位与轻微回弹; 仍不通过就按原设计保持 PAUSED 等人工, 不盲发 0x01)。
        for observe in range(3):
            await asyncio.sleep(5.0)
            if not self._copy_refill_is_current(context):
                raise self._copy_refill_stale_error(context, '退料观察中归属/连接已变化')
            if self._active_sensor_has_filament(extruder) is False:
                self.logger.info('[COPY-REFILL] 低重退料到位: 打印头%d传感器无料(观察窗第%d次确认)' % (extruder, observe + 1))
                return
        raise RuntimeError('打印头%d退料5次补退+15s观察后仍有料(旧线尾未退出传感器), 不请求续料' % extruder)

    async def _copy_low_weight_refill(self, extruder: int, tube: int, net_weight: int):
        """lancaigang261024160000 任务#554-copy108-hotfix5: COPY/MIRROR 下的低重(0x1D)续料事务。

        与纯 runout 事务(_copy_runout_and_resume)共用同一套件: 归属复核/暂停确认/到料判据/
        双头分段预挤/恢复门; 差异只有两点:
          ① 缺料头由低重事件给定(打印头此刻仍有料, 不能用传感器推导 missing);
          ② 进料前必须先做单侧退料(见 _copy_retract_head_until_empty), 否则新料无处可走。
        """
        # lancaigang261024191500 任务#554-copy108-hotfix14: 在途事务要按“存活”判定。
        # 旧实现只看 _copy_refill_context 非空, 而失败收尾刻意保留该对象给编排 finally 做
        # RESUMING→PAUSED 收口, 于是已终止(interrupted/complete)的事务会一直把后续
        # 低重挡在门外 —— 108 实测 19:09:13 / 19:09:43 两次槽1低重被无端拒掉。
        _ctx = getattr(self, '_copy_refill_context', None)
        _ctx_live = (_ctx is not None
                     and not _ctx.get('interrupted') and not _ctx.get('complete'))
        if (self._auto_feed_failed or _ctx_live
                or self._refill_motion_busy()
                or self.state_manager.state not in (SystemStateEnum.PRINTING, SystemStateEnum.PAUSED)):
            # lancaigang261024191500 任务#554-copy108-hotfix14(日志铁律): 四个拒绝条件必须
            # 分别打点。合并成一句"已有事务/编排/失败锁或状态不符"的旧日志让现场无法归因,
            # 108 实测只能靠时间线反推是 _copy_refill_context 残留。
            _why = []
            if self._auto_feed_failed:
                _why.append('失败锁')
            if _ctx_live:
                _why.append('在途复制事务(mode=%s head=%s tube=%s)'
                            % (_ctx.get('mode'), _ctx.get('owner'), _ctx.get('tube')))
            if self._refill_motion_busy():
                _why.append('编排/清缓冲/恢复任务在途')
            if self.state_manager.state not in (SystemStateEnum.PRINTING, SystemStateEnum.PAUSED):
                _why.append('状态不符(%s)' % self.state_manager.state.name)
            self.logger.info('[COPY-REFILL] 低重续料未启动: ' + '/'.join(_why))
            return
        mapping = self.config.get('extruders', {}).get('mapping', {})
        if (mapping.get(extruder) != tube
                or set((mapping.get(0), mapping.get(1))) != {0, 1}):
            self.logger.error('[COPY-REFILL] 低重续料拒绝: 归属/映射不成立 head=%s tube=%s mapping=%s'
                              % (extruder, tube, mapping))
            return
        context = {'mode': self._copy_print_mode(), 'mapping': (mapping.get(0), mapping.get(1)),
                   'job': self._job_key_from_print_stats(self.klipper_monitor.print_stats),
                   'epoch': self._refill_failure_epoch, 'tube': None, 'seq': None,
                   'complete': False, 'fed_heads': [], 'low_weight': True, 'owner': extruder}
        if not context['job']:
            self.logger.error('[COPY-REFILL] 低重续料拒绝: 无任务身份(print_stats为空)')
            return
        task = asyncio.current_task()
        self._copy_refill_context = context
        self._refill_orchestration_busy = True
        self._refill_orchestration_task = task
        self._refill_orchestration_owner = extruder
        self._pause_orchestrated = True
        self._low_weight_seen_this_print = True
        self._cancel_refill_tasks('进入复制低重续料')
        self.logger.info('[COPY-REFILL] 低重续料开始 mode=%s head=%d tube=%d 净重=%sg'
                         % (context['mode'], extruder, tube, net_weight))
        try:
            self._reset_auto_refill_state('复制低重续料', full=False)
            # 步骤1: 低重必须马上暂停(用户定策), 再等同单真实暂停落地
            if not self.klipper_monitor.pause_resume_is_paused:
                _paused = await self._pause_print_guarded('低重(head=%d,tube=%d)' % (extruder, tube))
                if _paused != 'ok' and not self.klipper_monitor.pause_resume_is_paused:
                    raise RuntimeError('PAUSE未确认生效(%s)' % _paused)
            _pause = await self._copy_wait_pause_confirmed(context)
            if _pause != 'confirmed':
                if _pause == 'timeout':
                    context['pause_unconfirmed'] = True
                raise RuntimeError('未确认同单真实暂停(%s)' % _pause)
            if not await self.klipper_monitor.execute_gcode('M400'):
                raise RuntimeError('停稳确认失败')
            await asyncio.sleep(10.0)
            # 步骤2: 单侧退料(仅 F05 0x1E), 直到该头传感器无料
            await self._copy_retract_head_until_empty(context, extruder, tube)
            if not self._copy_refill_is_current(context):
                raise self._copy_refill_stale_error(context, '退料后归属/连接已变化')
            # 步骤3: 请求续料(0x01), 归属+seq 交给 _handle_feed_ack 的 COPY 分支裁失败
            context['tube'] = tube
            context['seq'] = (self.can_comm._feed_seq + 1) & 0xFF
            self._session_vacancy_seen = True
            started = time.monotonic()
            self.state_manager.transition_to(SystemStateEnum.FEEDING, extruder=extruder, copy_owned=True)
            self._save_runtime_state('复制低重续料发送前', force=True)
            self.logger.info('[COPY-REFILL] 低重请求 head=%d tube=%d seq=%d' % (extruder, tube, context['seq']))
            if not await self.can_comm.request_feed(tube_id=tube):
                raise RuntimeError('0x01发送失败')
            while True:
                if not self._copy_refill_is_current(context):
                    raise self._copy_refill_stale_error(context, '续料等待中归属/连接已变化')
                now = time.monotonic()
                sensor = self.klipper_monitor.filament_sensor_objects[extruder]
                since = self._sensor_fil_since.get(sensor)
                if (self._active_sensor_has_filament(extruder) is True and since is not None
                        and now - since >= RESUME_SENSOR_HOLD_SECONDS
                        and now - started >= FEEDING_MIN_HOLD_SECONDS):
                    break
                if now - started >= FEEDING_TIMEOUT_SECONDS:
                    raise RuntimeError('打印头%d续料超时，不重发' % extruder)
                await asyncio.sleep(0.5)
            self.logger.info('[COPY-REFILL] 低重到料 head=%d tube=%d' % (extruder, tube))
            context.setdefault('fed_heads', []).append(extruder)
            context['tube'] = None
            context['seq'] = None
            # lancaigang261024163000 任务#554-copy108-hotfix6: 预挤前先补双头加热目标并等温
            await self._copy_heaters_ensure(context)
            ready, reason = self._can_resume_now()
            if not ready:
                raise RuntimeError(reason)
            await self._copy_purge_verified(context)
            if not self._copy_refill_is_current(context):
                raise self._copy_refill_stale_error(context, '预挤后归属已变化')
            self.state_manager.transition_to(SystemStateEnum.RESUMING, copy_owned=True)
            context['resuming'] = True
            if await self._resume_print_gated() != 'sent':
                raise RuntimeError('复制双头恢复门未通过或RESUME失败')
            context['complete'] = True
            self.logger.info('[COPY-REFILL] 低重RESUME已发送，等待printing确认')
        except asyncio.CancelledError:
            if context.get('resuming') and self._last_printer_state == 'printing':
                context['complete'] = True
            raise
        except Exception as e:
            context['abort_reason'] = str(e)
            self.logger.error(f'[COPY-REFILL] 低重续料中止：{e}；保持暂停等待人工')
        finally:
            if not context['complete']:
                if context.get('pause_unconfirmed'):
                    # 暂停未被确认属时序竞态而非续料失败: 不置失败锁, 同时放开事务归属,
                    # 让 F05 30s 重发的 0x1D 还能重新受理(否则事务指针永久占位=永不重试)。
                    self.logger.warning('[COPY-REFILL] 低重暂停确认超时：不推进续料、不置失败锁，'
                                        '保持PAUSED等待0x1D重发')
                    if self._copy_refill_context is context:
                        self._copy_refill_context = None
                elif self._copy_refill_context is context and not self._auto_feed_failed:
                    self._apply_feed_failure(4, 0, context.get('tube'), 0,
                                             '复制低重续料终止(%s)'
                                             % (context.get('abort_reason') or '未知原因'))
                if self._copy_refill_context is context and self.state_manager.state == SystemStateEnum.RESUMING:
                    self.state_manager.transition_to(SystemStateEnum.PAUSED, copy_owned=True)
                if context.get('tube') is not None and self.can_comm.connected:
                    try:
                        await self.can_comm.stop_feed(tube_id=context['tube'])
                    except Exception as e:
                        self.logger.error(f'[COPY-REFILL] 停料发送失败：{e}')
            if self._refill_orchestration_task is task:
                self._refill_orchestration_task = None
                self._refill_orchestration_owner = None
                self._refill_orchestration_busy = False
            self._save_runtime_state('复制低重续料退出', force=True)

    async def _runout_clear_and_feed(self, extruder: int):
        """
        RUNOUT暂停后的清管续料编排（任务#278 H2）
            
        ★★★【承重墙·删除/重构前必读·任务#286】★★★
        本编排被【低重链路恢复闭环借用】: 低重_auto_change_filament发0x01后不转FEEDING
        保持PAUSED(其步骤5承重墙注释), 恢复闭环借道PAUSED轮询→第二次触发本编排→本函数
        置FEEDING→新料到头→FEEDING轮询转RESUMING→resume。删除/重构本函数或PAUSED轮询
        会直接断掉低重链路自动恢复(打印永停PAUSED)——Quentin3审计#282结论, 任务#286存档。
        另: 本编排也是任务#286 FEEDING超时兜底的重试载体(超时回PAUSED后由PAUSED轮询
        重走本编排重发0x01, F05侧分流⑤粘滞态正好接住)。
    
        【用户定策·Tina定稿·原话】"F05只上报不暂停，让打印头耗完料管存料，
        直到打印头自己无料才暂停，暂停后按续料开关分叉"

        场景差异（相对低重链路_auto_change_filament）：
        - runout场景线已在打印头处断（runout传感器触发=线尾已过传感器），不需要切刀
          （低重链路的撞切刀由F05侧补料状态机在0x01后完成，本链路同理）
        - 编排入口时打印已处于PAUSED（RUNOUT保护触发过暂停），无需再pause_print
        - 先UNLOAD_FILAMENT宏退出挤出机内线尾（塑形+净后退55mm级别），
          再0x1E让F05收回管内残线，最后0x01请求补料

        编排序列对齐低重链路_auto_change_filament（逐步骤同款宏/函数/顺序/等待）：
        M400→10s停稳(lancaigang261014205850用户拍板30s→10s)→UNLOAD宏(异步启动)→塑形等待2.5s→0x1E(20mm@10mm/s)→5.5s
        →0x1E(50mm@10mm/s)→5.5s→0x1E(50mm@60mm/s)→M400→2s传感器稳定
        →传感器确认循环(补充退料10mm@10mm/s最多3次)→FEEDING防重检查→0x01→FEEDING态

        【lancaigang261015 qoder-agent-f01-leader 用户261015补充定策·细粒度分流(粗粒度撤销后的修正)】
        在"撤销#296场景分流"基础上, 用户261015补充明确: 【剪断(纯runout, 非低重)自动续料
        打印头不需要退料】——打印头里那一小段线材被UNLOAD宏净后退抽回后又被打印头传感器判为
        "有料", 逻辑错乱(#295线尾回带假有料同源)。故按会话类型细粒度分流:
          · 低重链路续料在途(已发0x01待恢复借道, _low_weight_refill_pending=True): 维持完整
            序列, 含打印头UNLOAD宏退线尾(低重旧盘线尾连着, 必须退净)。
          · 其余runout独立事件(_low_weight_refill_pending=False, 如剪断; 含本会话此前受理过低重
            但已恢复的情形): 跳过打印头
            UNLOAD_FILAMENT宏(打印头不退料, 消除回带假有料); lancaigang261016(任务#445, 用户
            261016再定策)进一步: 旧盘FRO本身已无料、管内无线尾可清, 连带【跳过分段退料(三段
            0x1E)与传感器确认】；任务#580在0x01前先实际吐料50mm清打印头残料，再让F01槽匹配新料盘。(注: #296当年跳过0x1E导致管内旧
            线尾未清→0x01续料失败#442, 本次前提是旧盘FRO已无料=管内确无残线, 故可跳。)

        互斥：与_auto_change_filament共用_refill_orchestration_busy标志，
        防止runout清管与低重换料双编排并发（双UNLOAD/双0x1E/双0x01）；
        两处触发入口（状态机PAUSED action + PAUSED态轮询）也由本标志去重。

        已知边界（对齐低重链路既有行为，不新造不一致容错）：
        - 编排期间活跃挤出机切换：UNLOAD宏作用于printer.toolhead.extruder当前活跃侧
          （低重链路同款既有边界）
        - 0x1E为盲发无回执（F05侧无0x1E完成应答帧），靠时序等待+传感器确认兜底
          （低重链路同款口径；runout场景传感器本已无料，确认循环防抖动）
        - 编排期间用户手动恢复打印：Klipper printing但hub状态机维持PAUSED，
          轮询会再次触发编排（低重链路同款既有边界）
        """
        if self._copy_pure_runout() or getattr(self, '_copy_refill_context', None):
            await self._copy_runout_and_resume()
            return
        # ---- 互斥入口（两处触发入口+低重链路共用） ----
        # 任务#286 V10: 维持全局单标志结构(hub单状态机架构不支持真并发编排: UNLOAD宏
        # 作用于toolhead当前活跃侧无侧参数, 分侧标志会让左右两编排并发跑导致双UNLOAD宏
        # 叠加+状态机payload互相覆盖)。仅加owner分侧日志可观测: 被拒时明确哪侧占用/哪侧
        # 被延后; 对侧0x1D不排队但F05侧WAIT_CUT 30s重发0x1D已兜底收敛(宽限后重触发)。
        if self._refill_motion_busy():
            self.logger.info(f"已有换料/清管编排进行中(占用侧: 挤出机{getattr(self, '_refill_orchestration_owner', None)}), "
                             f"跳过本次挤出机{extruder}侧runout清管触发（防双编排并发, 单状态机串行）")
            return
        # lancaigang261022103000 fixflow：create_task时的PAUSED可能已过期，执行入口重新裁决。
        if (self._auto_feed_failed or self.state_manager.state not in (
                SystemStateEnum.PAUSED, SystemStateEnum.RUNOUT)):
            return
        # lancaigang261025103309 任务#563: PAUSED状态动作/_handle_paused_filament_runout 入口同样过来源门(人工暂停后拔料不续不恢复)
        if not self._pause_refill_source_ok('runout清管入口'):
            return
        tube = self.config.get('extruders', {}).get('mapping', {}).get(extruder)
        context = self._begin_refill_context(extruder, tube)
        if context is None:
            self.logger.warning(f"runout清管续料未启动: 挤出机{extruder}料管{tube}的事务上下文无效"
                                f"(失败锁={self._auto_feed_failed}, 状态={self.state_manager.state.name}), "
                                f"保持PAUSED等人工(任务#554-hotfix11)")
            return
        self._refill_orchestration_busy = True
        self._refill_orchestration_owner = extruder
        self._refill_orchestration_task = asyncio.current_task()
        failure_epoch = self._refill_failure_epoch
        try:
            self._f05_nack_count = 0  # 任务#286 V6: 新编排开始, 清零F05 NACK连续计数
            self._last_f05_nack_reason = None  # 任务#549: 同步清零"最近NACK原因", 防上一轮编排的原因码串到本轮
            # lancaigang261016 任务#446(用户261016定策): 新一次续料编排起点清初始中间态(full=False,
            # 保留承重墙分流标志 _low_weight_refill_pending 供低重链路第二趟借道判定; 也保留 FEEDING
            # 超时重试计数/放弃标志的跨轮次累积, 防重试风暴)。
            self._reset_auto_refill_state("runout清管续料编排开始", full=False)
            # lancaigang261015 qoder-agent-f01-leader 用户261015补充定策(细粒度分流):
            # 剪断(纯runout, 本会话未受理过0x1D)场景【打印头不需要退料】——UNLOAD宏净后退会把
            # 打印头里那一小段线材抽回又被传感器判为"有料", 逻辑错乱。故按会话类型分流:
            #   低重会话 → 完整序列(含打印头UNLOAD宏退线尾; 低重旧盘线尾连着必须退净);
            #   非低重/纯runout → 跳过打印头UNLOAD宏; lancaigang261016(任务#445, 用户261016再
            #     定策)进一步连带跳过分段退料(三段0x1E清管)+传感器确认(旧盘FRO本身已无料、
            #     管内无线尾可清), 直接0x01让F01槽匹配新料盘。
            # (注: 与#296不同——#296当年跳过0x1E致管内旧线尾未清→0x01续料失败#442;
            #  本次能跳的前提是旧盘FRO已无料=管内确无残线, 故安全不重现#442。)
            # lancaigang261016 任务#446(用户261016定策): 分流判据由会话级 _low_weight_seen_this_print
            # 改为事件级 _low_weight_refill_pending——仅低重链路续料在途(已发0x01待恢复借道)时
            # 才算"低重会话"走完整序列; 纯runout(即使本会话此前受理过低重但已恢复)一律跳过退料。
            is_non_low_weight = not self._low_weight_refill_pending
            if is_non_low_weight:
                if context.get('fro_session') and context.get('fro_precleared'):
                    self.logger.info("自动续料清管编排: FRO人工清料已确认，复用停稳/归属/续料门，不再额外固定吐料50mm")
                else:
                    self.logger.info("自动续料清管编排: 非低重/纯runout会话(剪断), 跳过分段退料与传感器确认；停稳后先实际吐料50mm清残料，再请求续料")
            else:
                self.logger.info("自动续料清管编排: 低重会话, 走完整序列(打印头UNLOAD宏+三段0x1E+传感器确认)(用户261015定策)")

            # 挤出机→料管映射（维持_handle_paused_filament_runout原有映射逻辑；
            # config.yaml实配mapping={0:0,1:1}，tube_id与buffer_id同值，
            # 低重链路request_feed/retract也是同值直传）
            extruder_mapping = self.config.get('extruders', {}).get('mapping', {})
            tube_id = extruder_mapping.get(extruder, extruder)
            buffer_id = tube_id  # 0x1E的data[2]为缓冲区编号，与tube同值（对齐低重链路传法）

            self.logger.info(f"=== RUNOUT清管续料编排开始: 挤出机={extruder}, 料管/缓冲区={tube_id} ===")

            # lancaigang 261020 14:20:34 任务#438观测: 非低重分支会跳过步骤4/5, 先置默认值避免步骤6引用未定义
            _confirm_started_at = time.monotonic()
            _confirm_verdict = 'SKIPPED_NON_LOW_WEIGHT' if is_non_low_weight else 'PENDING'

            # 步骤1: M400等待暂停后残余移动完成（对齐低重链路步骤1的M400）
            self.logger.info("步骤1: M400等待移动完成（打印已由RUNOUT保护暂停）")
            await self.klipper_monitor.execute_gcode("M400")
            self.logger.info("移动已完成")

            # 步骤2: 停稳等待——统一30s(lancaigang261016 qoder-agent-f01-leader 任务#445 用户拍板
            # 10s→30s: 10s内打印头可能尚未移动到暂停位置, F01槽已同步开始退料; 与_auto_change_filament同步)
            self.logger.info("步骤2: 等待30秒确保打印机完全停稳(10s→30s, 用户261016定策)...")
            await asyncio.sleep(30.0)
            if not self._refill_attempt_is_current(failure_epoch):
                return
            self.logger.info("等待完成，打印机已完全停稳")

            # lancaigang261015 qoder-agent-f01-leader 用户261015补充定策(细粒度分流):
            # 步骤3(打印头UNLOAD退料)仅低重会话执行; 非低重/纯runout(剪断)跳过——
            # 打印头无料场景线已在打印头处断, 打印头里那一小段线材不需退, UNLOAD净后退反而把它
            # 抽回被传感器判"有料"(#295线尾回带假有料同源)。步骤4(0x1E清管)/步骤5(传感器确认)
            # 不变, 对所有会话无差别保留(与#296"整体精简"的关键区别, 防管内旧线尾未清#442)。
            if is_non_low_weight:
                self.logger.info("步骤3: 非低重/纯runout会话(剪断), 跳过打印头UNLOAD_FILAMENT宏(打印头不需要退料; 用户261015定策)")
            else:
                # 步骤3: 检查并启动UNLOAD_FILAMENT宏（对齐低重链路步骤2，挤出机退出线尾）
                self.logger.info("步骤3: 检查并启动UNLOAD_FILAMENT宏（挤出机退出线尾，塑形+净后退55mm）")
                if not await self.klipper_monitor.check_gcode_macro_exists("UNLOAD_FILAMENT"):
                    self.logger.error("未找到UNLOAD_FILAMENT宏，中止清管编排（打印保持暂停，等待人工处理）")
                    return

                # 启动UNLOAD_FILAMENT宏（异步执行，不等待完成，与低重链路同款）
                self.logger.info("启动UNLOAD_FILAMENT宏（塑形+后退，挤出机退出线尾）")
                if not await self.klipper_monitor.execute_gcode("UNLOAD_FILAMENT"):
                    self.logger.error("执行UNLOAD_FILAMENT宏失败，中止清管编排（打印保持暂停）")
                    return

                self.logger.info("UNLOAD_FILAMENT宏已启动")

            # lancaigang261016 qoder-agent-f01-leader 任务#445 用户261016定策:
            # runout非低重(剪断)场景——旧盘FRO本身已是无料, 管内无残线线尾可清, 分段退料
            # (三段0x1E清管)无意义。故跳过分段退料, 让F01槽直接匹配下一个新料盘进料
            # (直接0x01 → F01侧CHECK_LOSS → switch_to_next_filament)。本分支不执行任何
            # 0x1E退料, 故也不武装"退料后无料"冗余校验(纯runout会话本就不受该约束)。
            if is_non_low_weight:
                self.logger.info("步骤4/5: 非低重/纯runout会话跳过分段退料与传感器确认；"
                                 "FRO已清料轮次直接交接，其余轮次执行50mm实际吐料")
            else:
                # 步骤4: 等待塑形阶段，然后送料柜同步退料（对齐低重链路步骤3的三段式0x1E）
                self.logger.info("步骤4: 等待挤出机塑形阶段完成（约2.5秒），然后送料柜退料清管")
                await asyncio.sleep(2.5)

                # 任务#549(用户261023现场 槽1/槽6 实证): 旧实现三段0x1E是盲发——retract_filament 只代表
                # "命令已发出", F05 的拒绝(NACK 0x1F)走异步 _handle_f05_nack, 完全不改变返回值。于是
                # 当 F05 已自行退料(运动锁窗口外, 原因码0x01)时三段全被拒, 仍打出"送料柜已收回管内残线"
                # 假成功日志。现在按段读 _last_f05_nack_reason: 命中 0x01 即认为后段必然同样被拒, 中止剩余段。
                self._last_f05_nack_reason = None
                seg_plan = (
                    (3, 0, "第一段(20mm, 10mm/s)"),   # 塑形阶段配合，档位3
                    (4, 0, "第二段(50mm, 10mm/s)"),   # 档位4，速度档0
                    (4, 5, "第三段(50mm, 60mm/s)"),   # 档位4，速度档5
                )
                seg_sent = 0
                nack_locked = False
                for seg_idx, (seg_dist, seg_spd, seg_label) in enumerate(seg_plan):
                    if seg_idx > 0:
                        # 段间等待合计仍为 5.5s(4.5s sleep + 段内 1.0s NACK观察窗), 与原时序对齐
                        await asyncio.sleep(4.5)
                        self.logger.info(f"送料柜{seg_label}退料")
                    if not await self.can_comm.retract_filament(buffer_id, distance=seg_dist, speed=seg_spd):
                        self.logger.error(f"送料柜{seg_label}退料命令发送失败，中止清管编排（打印保持暂停）")
                        return
                    seg_sent += 1
                    await asyncio.sleep(1.0)  # NACK回帧观察窗: 0x1F 一般在命令发出后1s内到达
                    if self._last_f05_nack_reason == 0x01:
                        self.logger.warning(
                            f"送料柜{seg_label}被F05拒绝(原因码0x01 运动锁窗口外): 判定F05已自行退料/运动锁占用, "
                            f"中止剩余{len(seg_plan) - seg_sent}段(不再盲发0x1E), 后续以传感器确认兜底")
                        nack_locked = True
                        break

                # 等待挤出机的所有移动完成（对齐低重链路的收尾M400）
                await self.klipper_monitor.execute_gcode("M400")
                if nack_locked:
                    # 任务#549: 没真退成就不许打"完成", 也不武装"退料后持续无料"校验(会误判)
                    self.logger.warning(
                        f"清管退料未完成: 已发{seg_sent}/3段后F05持续拒绝(运动锁窗口外), "
                        f"F05侧自行退料已接管, 继续走传感器确认兜底")
                else:
                    self.logger.info("清管退料完成：挤出机已退线尾，送料柜已收回管内残线")
                    # lancaigang261014224500 qoder-agent-f01-leader 任务#438: 武装"退料后持续无料"冗余校验
                    self._arm_retract_empty_check()

                # 步骤5: 等待并确认传感器无料（对齐低重链路步骤4的传感器确认循环）
                # runout场景触发编排时传感器本已无料，此循环主要防传感器抖动/残留误报
                sensor_name = f"Filament_Sensor{extruder}"
                sensor_obj_name = f"filament_switch_sensor {sensor_name}"
                max_retries = 3
                retract_distance = 2  # 档位2 = 10mm
                retract_speed = 0     # 档位0 = 10mm/s

                # 等待传感器状态稳定（对齐低重链路的2s稳定窗）
                # lancaigang 261020 14:20:34 任务#438观测: RUNOUT链路同款确认门禁打点, 两侧结论可直接对比
                _confirm_started_at = time.monotonic()
                self.logger.info(
                    f"[RETRACT-CHECK] 清管退料确认门禁启动: 缓冲区={buffer_id} 挤出机={extruder} "
                    f"传感器={sensor_name} 最大补退={max_retries}次(每次10mm@10mm/s) 会话类型=低重")
                self.logger.info("步骤5: 等待传感器状态稳定（2秒）...")
                await asyncio.sleep(2.0)

                for retry in range(max_retries + 1):
                    # 若系统已转FEEDING（其他路径已触发补料），退出编排防重（对齐低重链路）
                    if self.state_manager.state == SystemStateEnum.FEEDING:
                        self.logger.info("✓ 检测到系统已转换到FEEDING状态，补料已由其他路径触发，退出清管编排")
                        return

                    # 获取传感器状态（使用本地缓存_last_filament_status，与低重链路同款）
                    has_filament = self._last_filament_status.get(sensor_obj_name, True)

                    self.logger.info(f"传感器 {sensor_name} 状态检查 (第{retry + 1}次): {'有料' if has_filament else '无料'}")

                    if not has_filament:
                        _confirm_verdict = 'CONFIRMED_EMPTY'
                        self.logger.info(f"✓ 传感器确认无料，清管到位 [RETRACT-CHECK] verdict=CONFIRMED_EMPTY "
                                         f"第{retry + 1}次检查通过, 耗时{time.monotonic() - _confirm_started_at:.1f}s")
                        break

                    if retry == 0:
                        self.logger.warning("⚠ 传感器仍检测到有料，补充退料清管")

                    # 仍有料，尝试补充退料（最多3次，对齐低重链路）
                    if retry < max_retries:
                        self.logger.warning(f"执行第 {retry + 1}/{max_retries} 次补充退料 (10mm, 10mm/s)")
                        if await self.can_comm.retract_filament(buffer_id, retract_distance, retract_speed):
                            self.logger.info("补充退料命令已发送，等待完成（1.5秒）")
                            await asyncio.sleep(1.5)
                            # lancaigang 261020 14:20:34 任务#438观测: 补退完成打点(与低重链路同格式)
                            self.logger.info(f"[RETRACT-CHECK] 第{retry + 1}/{max_retries}次补充退料已完成(10mm@10mm/s), "
                                             f"进入第{retry + 2}次复检")
                        else:
                            self.logger.error("发送补充退料命令失败")
                            break
                else:
                    # 对齐低重链路260912定策: 重试耗尽仍有料——仅警告，不中止，强行继续续料
                    # lancaigang 261020 14:20:34 任务#438观测: 与低重链路同格式的强行放行标记
                    _confirm_verdict = 'FORCED_CONTINUE'
                    self.logger.warning(
                        f"⚠ 重试{max_retries}次后传感器仍检测到有料，但强行继续续料流程 "
                        f"[RETRACT-CHECK] verdict=FORCED_CONTINUE 缓冲区={buffer_id} 挤出机={extruder} "
                        f"检查耗时={time.monotonic() - _confirm_started_at:.1f}s "
                        f"#439校验={'已判失败' if getattr(self, '_retract_verify_failed', False) else ('已取得稳定无料' if getattr(self, '_retract_empty_ok', False) else '计时中')}")

            if is_non_low_weight:
                if context.get('fro_session') and context.get('fro_precleared'):
                    self.logger.info("步骤6A: 本轮FRO人工取料已验证真实无料边沿和3秒去抖，跳过额外固定50mm残料挤出")
                else:
                    self.logger.info("步骤6A: 纯runout暂停后强制吐料50mm，清打印头残料")
                    if not await self._purge_runout_residue(extruder):
                        self._fail_current_refill("runout暂停后50mm残料吐料未确认",
                                                  FeederCabinetCAN.ERROR_KLIPPER)
                        return
                    if not self._refill_attempt_is_current(failure_epoch):
                        return

            # 步骤6: 发送续料命令（对齐低重链路步骤5）
            self.logger.info("步骤6: 发送续料命令")

            # 防重检查：若已FEEDING则无需再发（对齐低重链路）
            if self.state_manager.state == SystemStateEnum.FEEDING:
                self.logger.info("✓ 系统已在FEEDING状态，补料流程已启动，无需重复发送")
                self.logger.info("=== RUNOUT清管续料编排完成（补料已由其他路径触发） ===")
                return

            if (not self._refill_attempt_is_current(failure_epoch)
                    or not self._refill_context_is_current(context)):
                return
            self.logger.info(f"发送续料命令到送料柜 (料管ID: {tube_id})... "
                             f"(req_origin=runout_clear_and_feed 挤出机={extruder} 退料确认={_confirm_verdict} "
                             f"当前状态={self.state_manager.state.name})")
            _feed_sent = await self.can_comm.request_feed(tube_id=tube_id)
            if (not self._refill_attempt_is_current(failure_epoch)
                    or not self._refill_context_is_current(context)):
                return
            if not _feed_sent and not self.can_comm.connected:
                # lancaigang261019 任务#459: CAN断连不是"续料失败"。退料编排已执行完, 这里只
                # 挂起0x01, 保持PAUSED等CAN恢复后补发(旧逻辑此处直接进ERROR且无自动出口,
                # 261019 09:21:25实测锁死直到人工干预)。
                self._pending_feed_after_can = {
                    'tube_id': tube_id,
                    'extruder': extruder,
                    'at': time.monotonic(),
                    'last_try': time.monotonic(),
                    'wall': time.time(),
                }
                self.logger.error(f"ACTION PENDING: CAN未连接, 为挤出机 {extruder} (料管ID: {tube_id}) 的续料请求无法发出; "
                                  f"已挂起(最长{CAN_PENDING_FEED_TIMEOUT_SECONDS:.0f}s), 保持{self.state_manager.state.name}"
                                  f"等CAN恢复后补发(不转ERROR, 任务#459)")
                return
            if not _feed_sent:
                self.logger.error(f"ACTION FAILED: 为挤出机 {extruder} (料管ID: {tube_id}) 请求补料失败！进入错误状态。")
                self.state_manager.transition_to(SystemStateEnum.ERROR, reason=f"Failed to request feed for extruder {extruder}")
            else:
                # 任务#459: 已发出则清掉可能残留的挂起记录, 防CAN重连回调重复补发
                if self._pending_feed_after_can:
                    self.logger.info("续料请求已发出, 清除挂起的补发记录(任务#459)")
                    self._pending_feed_after_can = None
                self.logger.info("补料请求已发送，转换为FEEDING状态，等待F05送料完成后自动恢复打印")
                self.state_manager.transition_to(SystemStateEnum.FEEDING, extruder=extruder)
                self.logger.info("=== RUNOUT清管续料编排完成（等待续料） ===")

        except Exception as e:
            self.logger.error(f"处理RUNOUT清管续料编排时出错: {e}", exc_info=True)
            if context.get('fro_session') and self._refill_context_is_current(context):
                self._fail_current_refill(f"FRO续料交接异常: {e}", FeederCabinetCAN.ERROR_KLIPPER)
            else:
                self.state_manager.transition_to(SystemStateEnum.ERROR, reason=str(e))
        finally:
            # 任务#278 H2: 无论成功/失败/提前return，都释放编排互斥标志
            self._refill_orchestration_task = None
            self._refill_orchestration_owner = None
            self._refill_orchestration_busy = False
    
    async def _handle_paused_filament_runout(self, extruder: int):
        """处理PAUSED状态下检测到的断料情况（任务#278 H2: 走清管续料编排，不再直发0x01）"""
        # 旧版此处直接request_feed(0x01)——线尾停在toolhead传感器处，不清管的话
        # F05新料推送会顶住管内旧线尾堵管。现改为清管编排，物理步骤对齐低重链路
        # （UNLOAD_FILAMENT宏退出线尾→0x1E收回管内残线→0x01补料），
        # 详见_runout_clear_and_feed。
        await self._runout_clear_and_feed(extruder)

    # ===== lancaigang261019 任务#459: CAN断连导致的续料失败不再锁死 =====
    async def _try_flush_pending_feed(self, source: str) -> bool:
        """CAN 恢复后补发挂起的 0x01(任务#459)。

        只补发续料请求, 不重走退料编排——退料/清管在挂起前已执行完, 重走会重复三段0x1E。
        调用点: CAN重连回调 + PAUSED轮询(每轮检测)。
        """
        if self._auto_feed_failed:
            self._pending_feed_after_can = None
            return False
        pending = self._pending_feed_after_can
        if not pending or self._refill_motion_busy():
            return False
        now = time.monotonic()
        age = now - pending.get('at', now)
        if age > CAN_PENDING_FEED_TIMEOUT_SECONDS:
            self.logger.error(f"[{source}] 挂起的续料请求已超时({age:.0f}s > {CAN_PENDING_FEED_TIMEOUT_SECONDS:.0f}s), "
                              f"丢弃(挤出机{pending.get('extruder')} 料管{pending.get('tube_id')}), 需人工确认(任务#459)")
            self._pending_feed_after_can = None
            return False
        # 只在这三个态补发: 打印已结束时状态会转IDLE, 此时补发=对新任务误续料
        if self.state_manager.state not in (SystemStateEnum.PAUSED, SystemStateEnum.RUNOUT, SystemStateEnum.ERROR):
            self.logger.info(f"[{source}] 当前状态={self.state_manager.state.name}, 已不需要续料, "
                             f"丢弃挂起请求(已挂起{age:.0f}s, 任务#459)")
            self._pending_feed_after_can = None
            return False
        if not self.can_comm.connected:
            self.logger.debug(f"[{source}] CAN仍未连接, 继续挂起(已{age:.0f}s)")
            return False
        if now - pending.get('last_try', 0.0) < CAN_PENDING_FEED_RETRY_INTERVAL:
            return False
        pending['last_try'] = now
        failure_epoch = self._refill_failure_epoch
        self._refill_orchestration_busy = True
        self._refill_orchestration_owner = pending['extruder']
        # lancaigang261022103000 fixflow：只取消发送子任务，不中断CAN重连调用者的生命周期。
        self._refill_orchestration_task = asyncio.create_task(
            self.can_comm.request_feed(tube_id=pending['tube_id']))
        try:
            sent = await self._refill_orchestration_task
            if not self._refill_attempt_is_current(failure_epoch):
                return False
            if sent:
                self.logger.info(f"[{source}] CAN已恢复, 补发续料请求成功: 挤出机{pending['extruder']} "
                                 f"料管{pending['tube_id']} (挂起{age:.1f}s) -> FEEDING(任务#459)")
                self._pending_feed_after_can = None
                self.state_manager.transition_to(SystemStateEnum.FEEDING, extruder=pending['extruder'])
                return True
            self.logger.warning(f"[{source}] 补发续料请求仍失败(已挂起{age:.0f}s), 下轮重试(任务#459)")
            return False
        except asyncio.CancelledError:
            if not self._refill_attempt_is_current(failure_epoch):
                return False
            raise
        finally:
            self._refill_orchestration_task = None
            self._refill_orchestration_owner = None
            self._refill_orchestration_busy = False

    async def _maybe_selfheal_can_error(self):
        """ERROR 自愈看门狗(任务#459)。

        仅处理"CAN不可用导致续料请求失败"这一类 ERROR(旧逻辑无自动出口)。
        其余 ERROR(Klipper报错/预挤出失败/恢复打印失败)一律不动, 避免掩盖真实故障。
        自愈条件: CAN已连通 + 最后已知打印状态确未在运行(打印中/暂停中都不自愈)。
        """
        if self._auto_feed_failed or self.state_manager.state != SystemStateEnum.ERROR:
            self._error_can_since = None
            return
        _payload = self.state_manager.get_payload() or {}
        reason = str(_payload.get('reason', ''))
        if 'Failed to request feed' not in reason:
            # 非CAN类ERROR: 不碰, 只清看门狗计时
            self._error_can_since = None
            return
        if not self.can_comm.connected:
            self._error_can_since = None
            return
        if self._last_printer_state not in ('cancelled', 'complete', 'standby', 'ready'):
            # 打印/暂停/未知态下断料事实仍在, 不能清错
            self._error_can_since = None
            return
        now = time.monotonic()
        if self._error_can_since is None:
            self._error_can_since = now
            self.logger.warning(f"CAN已恢复但系统仍处ERROR(CAN类原因: {reason}), 进入"
                                f"{ERROR_CAN_SELFHEAL_OBSERVE_SECONDS:.0f}s观察窗(打印机={self._last_printer_state}, 任务#459)")
            return
        held = now - self._error_can_since
        if held < ERROR_CAN_SELFHEAL_OBSERVE_SECONDS:
            return
        self._error_can_since = None
        self.logger.warning(f"ERROR自愈: CAN已连通{held:.0f}s且打印确未在运行({self._last_printer_state}), "
                            f"ERROR->IDLE解除锁死(原因: {reason}, 任务#459)")
        self.state_manager.transition_to(SystemStateEnum.IDLE)

    def _pause_abort_state_path(self) -> str:
        """任务#447c: 中止标记文件路径(日志目录下, 与 feeder_cabinet.log 同处, 权限天然可用)。"""
        log_dir = getattr(self.log_manager, 'log_dir', None) or DEFAULT_LOG_DIR
        return os.path.join(log_dir, PAUSE_ABORT_STATE_FILENAME)

    def _pause_abort_age(self):
        """任务#447c: 中止标记已存在多久(秒); None=无标记。

        优先用墙钟: monotonic 不跨进程, 重启后必须靠墙钟重建“已过去多久”。
        """
        if getattr(self, '_pause_abort_wall', None):
            return max(0.0, time.time() - self._pause_abort_wall)
        if getattr(self, '_pause_abort_at', None) is not None:
            return max(0.0, time.monotonic() - self._pause_abort_at)
        return None

    def _save_pause_abort_marker(self, extruder_id: int, buffer_id: int):
        """任务#447c: 落盘“PAUSE 回执超时中止”标记, 使 hub 重启后仍能接续换料。"""
        self._pause_abort_at = time.monotonic()
        self._pause_abort_wall = time.time()
        self._pause_abort_extruder = extruder_id
        path = self._pause_abort_state_path()
        payload = {
            'wall': self._pause_abort_wall,
            'extruder_id': extruder_id,
            'buffer_id': buffer_id,
            'pid': os.getpid(),
        }
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
            self.logger.warning(f"已落盘PAUSE中止标记: {path} (挤出机{extruder_id}, 缓冲区{buffer_id}); "
                                f"{PAUSE_ABORT_RECOVER_WINDOW:.0f}s内即使进程重启也可接续换料(任务#447c)")
        except Exception as e:
            self.logger.error(f"落盘PAUSE中止标记失败(仅影响重启后自救): {e}")

    def _clear_pause_abort_marker(self, reason: str = ''):
        """任务#447c: 清除中止标记(内存 + 落盘)。"""
        self._pause_abort_at = None
        self._pause_abort_wall = None
        self._pause_abort_extruder = None
        path = self._pause_abort_state_path()
        try:
            if os.path.exists(path):
                os.remove(path)
                self.logger.info(f"已清除PAUSE中止标记({reason}): {path}")
        except Exception as e:
            self.logger.warning(f"清除PAUSE中止标记失败: {e}")

    def _load_pause_abort_marker(self):
        """任务#447c: 启动时恢复上次进程遗留的中止标记(超自救窗则直接丢弃)。"""
        path = self._pause_abort_state_path()
        try:
            if not os.path.exists(path):
                return
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            wall = float(data.get('wall') or 0)
            age = time.time() - wall
            if age < 0 or age > PAUSE_ABORT_RECOVER_WINDOW:
                self.logger.info(f"发现PAUSE中止标记但已过期({age:.0f}s 不在0~{PAUSE_ABORT_RECOVER_WINDOW:.0f}s内), 删除")
                os.remove(path)
                return
            self._pause_abort_wall = wall
            self._pause_abort_at = time.monotonic() - age
            self._pause_abort_extruder = data.get('extruder_id')
            self.logger.warning(f"恢复重启前遗留的PAUSE中止标记(已过{age:.0f}s, 挤出机{self._pause_abort_extruder}, "
                                f"缓冲区{data.get('buffer_id')}): 若打印机仍为paused, 下一次低重0x1D将直接接续换料编排(任务#447c)")
        except Exception as e:
            self.logger.warning(f"读取PAUSE中止标记失败(忽略): {e}")

    # ===== lancaigang261017 运行态持久化: 部署/重启后续态(用户261017定策) =====
    # 设计要点:
    #   1) 时间一律存墙钟(monotonic 不跨进程), 载入时换算回 monotonic;
    #   2) 快照带"打印任务身份"(文件名+start_time), 只有重启后仍是同一单才恢复,
    #      换单/取消/空闲一律丢弃, 避免把上一单的失败锁/借道标志带进新单;
    #   3) 状态机不做无条件恢复: 受 Klipper 现状约束(见 _apply_runtime_state 映射表),
    #      FEEDING 可恢复(否则退回 PAUSED 会被承重墙轮询重走编排→双退料),
    #      RESUMING 不恢复(重启后只能发一次恢复, 被闸门拦截无重试路径→卡死),
    #      RUNOUT 不恢复(其动作只在状态迁移时触发一次, 载入不会补发)→退 PAUSED 由轮询接手。
    def _runtime_state_path(self) -> str:
        """运行态快照文件路径(与日志/中止标记同目录)。"""
        log_dir = getattr(self.log_manager, 'log_dir', None) or DEFAULT_LOG_DIR
        return os.path.join(log_dir, RUNTIME_STATE_FILENAME)

    @staticmethod
    def _job_key_from_print_stats(print_stats: Dict[str, Any]) -> str:
        """打印任务身份: 文件名 + 起始时刻(epoch)。用于判断快照是否属于当前这一单。"""
        if not print_stats:
            return ''
        fn = print_stats.get('filename') or ''
        if not fn:
            return ''
        try:
            st = int(float(print_stats.get('start_time') or 0))
        except Exception:
            st = 0
        return f"{fn}|{st}"

    def _collect_runtime_state(self) -> Dict[str, Any]:
        """采集需要跨重启保留的运行态(只收"同一单内会变、且丢了会改行为"的字段)。"""
        now_wall = time.time()
        now_mono = time.monotonic()

        def _wall(mono_t):
            if mono_t is None:
                return None
            try:
                # 秒级取整: 浮点微秒漂移会让"内容未变不写"失效(实测每帧重写, 磨损 eMMC),
                # 且会让外部对快照的人工注入被周期写回覆盖。秒级精度对续态完全够用。
                return int(now_wall - (now_mono - float(mono_t)))
            except Exception:
                return None

        ps = {}
        if self.klipper_monitor is not None:
            ps = getattr(self.klipper_monitor, 'print_stats', {}) or {}
        return {
            'version': RUNTIME_STATE_VERSION,
            'ts': now_wall,
            'pid': os.getpid(),
            'job_key': self._job_key_from_print_stats(ps),
            'state': self.state_manager.state.name,
            'data': {
                # 开场门禁: 重启后不应把打印中当成开场流程(否则低重通知被无限缓发)
                'printing_started_wall': _wall(getattr(self, '_printing_started_at', None)),
                'extruder_fed_seen': {str(k): bool(v) for k, v in (self._extruder_fed_seen or {}).items()},
                # 低重会话/借道标志: 丢了会让后续真 RUNOUT 误走免退料捷径(堵管风险)
                'low_weight_seen_this_print': bool(getattr(self, '_low_weight_seen_this_print', False)),
                'low_weight_refill_pending': bool(getattr(self, '_low_weight_refill_pending', False)),
                # 失败锁: 丢了重启后会自动重试→重复损耗(用户已遇过的坑)
                'auto_feed_failed': bool(getattr(self, '_auto_feed_failed', False)),
                # lancaigang261024114500 任务#554-copy108-hotfix2: 只有已发0x01且尚未确认到料(tube/seq仍在)
                # 才需要在重启后锁住防重投; 暂停确认竞态阶段没发任何机械命令, 锁死只会让同单不再续料。
                'copy_refill_interrupted': bool(
                    getattr(self, '_copy_refill_context', None)
                    and (self._copy_refill_context.get('tube') is not None
                         or self._copy_refill_context.get('seq') is not None)),
                # FRO尚未交接FEEDING时重启不能续挤；同单恢复必须fail-closed等人工。
                'fro_purge_interrupted': bool(
                    getattr(self, '_refill_context', None)
                    and self._refill_context.get('fro_session')
                    and self.state_manager.state != SystemStateEnum.FEEDING),
                # FEEDING 超时兜底连续性与防重试风暴
                'feeding_started_wall': _wall(getattr(self, '_feeding_started_at', None)),
                'feeding_min_hold_wall': _wall(getattr(self, '_feeding_min_hold_until', None)),
                'feeding_timeout_retry_count': int(getattr(self, '_feeding_timeout_retry_count', 0) or 0),
                'feeding_giveup_logged': bool(getattr(self, '_feeding_giveup_logged', False)),
                # 在途PAUSE守卫: 丢了重启后会再发一条PAUSE(迟到生效风险)
                'pause_pending_wall': _wall(getattr(self, '_pause_pending_since', None)),
                'pause_orchestrated': bool(getattr(self, '_pause_orchestrated', False)),
                'f05_nack_count': int(getattr(self, '_f05_nack_count', 0) or 0),
                # 恢复端交叉核对证据(#437/#438): 丢了 FEEDING 永远无法转 RESUMING
                'session_vacancy_seen': bool(getattr(self, '_session_vacancy_seen', False)),
                'retract_cmd_wall': _wall(getattr(self, '_retract_cmd_at', None)),
                'retract_empty_ok': bool(getattr(self, '_retract_empty_ok', False)),
                'retract_empty_since_wall': _wall(getattr(self, '_retract_empty_since', None)),
                'retract_verify_failed': bool(getattr(self, '_retract_verify_failed', False)),
            },
        }

    def _save_runtime_state(self, reason: str = '', force: bool = False):
        """落盘运行态快照: 内容无变化不写(护 eMMC), 原子替换。"""
        try:
            if self.state_manager.state in (SystemStateEnum.STARTING, SystemStateEnum.IDLE,
                                            SystemStateEnum.DISCONNECTED):
                # 无在进行的打印会话, 快照无意义(进入IDLE时已由 _clear_runtime_state 清)
                return
            snap = self._collect_runtime_state()
            if not snap.get('job_key'):
                # 尚未拿到任务身份(刚连上/换单途中), 不覆盖已有快照
                return
            sig = snap['state'] + '|' + json.dumps(snap['data'], sort_keys=True)
            if not force and sig == self._runtime_save_sig:
                return
            path = self._runtime_state_path()
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, path)
            self._runtime_save_sig = sig
            self._runtime_saved_at = time.monotonic()
            self.logger.info(f"已保存运行态快照({reason}): state={snap['state']}, job={snap['job_key']}")
        except Exception as e:
            self.logger.warning(f"保存运行态快照失败(仅影响重启后续态): {e}")

    def _clear_runtime_state(self, reason: str = ''):
        """作废并删除运行态快照(新单/取消/空闲)。"""
        self._runtime_save_sig = None
        self._runtime_snapshot = None
        path = self._runtime_state_path()
        try:
            if os.path.exists(path):
                os.remove(path)
                self.logger.info(f"已清除运行态快照({reason}): {path}")
        except Exception as e:
            self.logger.warning(f"清除运行态快照失败: {e}")

    def _load_runtime_state(self):
        """启动时读入上次进程的调用快照(只载入不应用; Klipper 连上校验同单后才恢复)。"""
        path = self._runtime_state_path()
        self._runtime_snapshot = None
        try:
            if not os.path.exists(path):
                return
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if int(data.get('version') or 0) != RUNTIME_STATE_VERSION:
                self.logger.info(f"运行态快照版本不匹配({data.get('version')} != {RUNTIME_STATE_VERSION}), 丢弃")
                os.remove(path)
                return
            if not data.get('job_key'):
                os.remove(path)
                return
            self._runtime_snapshot = data
            age = max(0.0, time.time() - float(data.get('ts') or 0))
            self.logger.warning(f"发现重启前运行态快照(state={data.get('state')}, job={data.get('job_key')}, "
                                f"写入于{age:.0f}s前, pid={data.get('pid')}), 待与Klipper现状校验后恢复")
        except Exception as e:
            self.logger.warning(f"读取运行态快照失败(忽略并删除): {e}")
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    def _apply_runtime_state(self, printer_status: Dict[str, Any]) -> bool:
        """Klipper已连接后恢复运行态; 非同一打印任务/无在进行的单则丢弃。返回是否已接管状态机。"""
        snap = getattr(self, '_runtime_snapshot', None)
        if not snap:
            return False
        try:
            ks = printer_status.get('printer_state')
            cur_key = self._job_key_from_print_stats(printer_status.get('print_stats') or {})
            snap_key = snap.get('job_key') or ''
            if (not cur_key):
                # 启动早期 moonraker 刚起来时 print_stats 可能为空: 不能当成"换单"丢快照,
                # 保留快照, 由首帧状态就绪后的 _handle_klipper_status_update 补应用。
                self.logger.info("当前打印任务身份未就绪(print_stats无filename), 保留运行态快照待首帧后重试")
                return False
            if cur_key != snap_key:
                self.logger.info(f"运行态快照不属于当前打印任务(快照={snap_key}, 当前={cur_key}), 丢弃不恢复")
                self._clear_runtime_state("任务不一致")
                return False
            if ks not in ('printing', 'paused'):
                self.logger.info(f"Klipper当前状态({ks})无在进行的打印, 运行态快照作废")
                self._clear_runtime_state(f"Klipper={ks}")
                return False

            data = snap.get('data') or {}

            # ---- 目标状态(受Klipper现状约束) ----
            snap_state = snap.get('state')
            if ks == 'printing':
                target = SystemStateEnum.PRINTING
            else:  # paused
                # FEEDING 必须恢复: 退回 PAUSED 会被 PAUSED 承重墙轮询重走编排→双退料
                # RESUMING 不恢复: 重启后只有一次恢复机会, 被闸门拦截无重试路径;
                #             退 PAUSED 后由 PAUSED轮询+#388自愈接手
                if snap_state == 'FEEDING' and data.get('feeding_started_wall') is not None:
                    target = SystemStateEnum.FEEDING
                else:
                    target = SystemStateEnum.PAUSED

            if ks == 'paused' and (data.get('copy_refill_interrupted')
                                   or data.get('fro_purge_interrupted')):
                target = SystemStateEnum.PAUSED
            # lancaigang261025103309 任务#563: 墙钟早于快照写入时刻=时钟回退, 快照年龄(FEEDING计时/在途守卫)全不可信,
            # 无法判断这段暂停里人工做过什么; 不续 FEEDING, 暂停来源一律作废, 交人工。
            clock_back = ks == 'paused' and time.time() < float(snap.get('ts') or 0)
            if clock_back:
                target = SystemStateEnum.PAUSED

            # 迁移前先恢复: 让 _handle_state_change_actions 的拦截判据(失败锁/在途/证据)看到真实值
            self._restore_runtime_data(data, target)

            self.logger.warning(f"按运行态快照恢复(任务={cur_key}, 快照状态={snap_state} -> 目标={target.name}, "
                                f"Klipper={ks}): 失败锁={self._auto_feed_failed}, "
                                f"低重借道={self._low_weight_refill_pending}, 进料门禁={self._extruder_fed_seen}, "
                                f"FEEDING重试={self._feeding_timeout_retry_count}, 在途守卫={self._pause_pending_since is not None}")
            ae = self.klipper_monitor.active_extruder if self.klipper_monitor else 0
            self.state_manager.transition_to(target, extruder=ae, restored_from_snapshot=True)
            # 迁移回调会按"新打印/恢复成功"语义复位部分会话标志(PRINTING/RESUMING 分支), 这里再恢复一次,
            # 否则重启续态等于没续(失败锁/借道标志/进料门禁会被状态迁移的复位清掉)。
            self._restore_runtime_data(data, target)
            # lancaigang261025103309 任务#563: 暂停来源只属于"那一次暂停"。Klipper 已 printing=暂停已结束(重启期间人工恢复),
            # 与 paused->printing 边沿同口径作废; 否则留给下一次人工暂停, 拔料即被当断料续料+恢复。
            if clock_back or target == SystemStateEnum.PRINTING:
                if clock_back:
                    self.logger.warning("任务#563: 墙钟早于快照写入时刻(时钟回退), 快照年龄不可信, 暂停来源不明, 不自动续料/恢复")
                self._session_vacancy_seen = False
                self._low_weight_refill_pending = False
                self._pause_orchestrated = False
            # 应用完释放快照, 之后由正常保存链路刷新
            self._runtime_snapshot = None
            self._runtime_save_sig = None
            self._save_runtime_state("快照恢复后刷新", force=True)
            return True
        except Exception as e:
            self.logger.error(f"应用运行态快照失败(回落Klipper现状对齐): {e}", exc_info=True)
            self._runtime_snapshot = None
            return False

    def _restore_runtime_data(self, data: Dict[str, Any], target: SystemStateEnum):
        """按快照回填运行数据。时间字段由墙钟换算回 monotonic。"""
        now_mono = time.monotonic()
        now_wall = time.time()

        def _mono(wall):
            if wall is None:
                return None
            try:
                return now_mono - max(0.0, now_wall - float(wall))
            except Exception:
                return None

        _fed = data.get('extruder_fed_seen') or {}
        self._extruder_fed_seen = {0: bool(_fed.get('0', False)), 1: bool(_fed.get('1', False))}
        self._low_weight_seen_this_print = bool(data.get('low_weight_seen_this_print', False))
        self._low_weight_refill_pending = bool(data.get('low_weight_refill_pending', False))
        # lancaigang261024104414 任务#554(计划「hub：重启与解锁」)：快照失败锁按归属恢复——
        # 同一打印任务仍 paused 时必须保留(服务重启不等于人工解锁)；目标 PRINTING 或换单时清。
        # 旧实现无条件清 False，与「明确失败立即停止等待人工」冲突；#463 的永久粘死风险
        # 由 _apply_runtime_state 已做的 job_key 同单校验 + 仍 paused 双条件封住。
        self._auto_feed_failed = bool(data.get('auto_feed_failed')) and target != SystemStateEnum.PRINTING
        # 重启本身就是人工介入窗口, 且解锁依赖"klipper 变 printing"的边沿, 进程重启会把边沿抹掉,
        # 一旦锁被恢复就永久粘死(261019 11:28 实测: 快照恢复 失败锁=True, hub 恒 PAUSED 不再发 0x01)。
        # 重启后允许本次自动续料重新尝试一轮(重试计数一并归零)。
        if target != SystemStateEnum.PRINTING and data.get('copy_refill_interrupted'):
            self._copy_refill_context = {'interrupted': True}
            self._auto_feed_failed = True
        if target != SystemStateEnum.PRINTING and data.get('fro_purge_interrupted'):
            self._fro_purge_task = None
            self._auto_feed_failed = True
            self.logger.error("同单快照显示FRO人工清料尚未交接FEEDING：保持PAUSED并恢复失败锁，禁止重启后自动续料")
        self._feeding_timeout_retry_count = int(data.get('feeding_timeout_retry_count', 0) or 0)
        self._feeding_giveup_logged = bool(data.get('feeding_giveup_logged', False))
        self._pause_orchestrated = bool(data.get('pause_orchestrated', False))
        self._f05_nack_count = int(data.get('f05_nack_count', 0) or 0)
        self._session_vacancy_seen = bool(data.get('session_vacancy_seen', False))
        self._retract_empty_ok = bool(data.get('retract_empty_ok', False))
        self._retract_verify_failed = bool(data.get('retract_verify_failed', False))
        self._retract_cmd_at = _mono(data.get('retract_cmd_wall'))
        self._retract_empty_since = _mono(data.get('retract_empty_since_wall'))
        self._printing_started_at = _mono(data.get('printing_started_wall'))
        _pp = _mono(data.get('pause_pending_wall'))
        # 在途守卫只在自身时限内恢复, 超时等同自然过期
        self._pause_pending_since = _pp if (_pp is not None and (now_mono - _pp) < PAUSE_PENDING_MAX_WAIT) else None

        if target == SystemStateEnum.FEEDING:
            self._feeding_started_at = _mono(data.get('feeding_started_wall'))
            _hold = _mono(data.get('feeding_min_hold_wall'))
            self._feeding_min_hold_until = _hold if _hold is not None else (now_mono + FEEDING_MIN_HOLD_SECONDS)
        else:
            # 非FEEDING不保留FEEDING计时, 防重启即误触发超时兜底
            self._feeding_started_at = None
            self._feeding_min_hold_until = None

    async def _wait_klipper_state_ready(self, timeout: float = 8.0) -> Dict[str, Any]:
        """lancaigang261017 部署/重启同步: 等 Klipper/Moonraker 首帧状态落地后再用于对齐。

        connect() 里的 printer.objects.query 是异步发帧, 返回时未必已收到回帧;
        旧版 start() 固定 sleep(1) 就读, moonraker 刚重启时可能读到 unknown/空 print_stats,
        导致误判 IDLE、运行态快照被误丢弃、开场门禁无从恢复。
        策略: 轮询等待 >0.5s 就绪; 超时则主动 refresh 再等 1s, 仍不成就按现状降级(后续首帧纠正)。
        """
        km = self.klipper_monitor
        if km is None:
            return {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = getattr(km, 'printer_state', None)
            ps = getattr(km, 'print_stats', {}) or {}
            if km.ws_connected and st and st != 'unknown' and ps.get('state'):
                self.logger.info(f"Klipper状态已就绪(等了{timeout - (deadline - time.monotonic()):.1f}s): {st}")
                return km.get_printer_status()
            if not km.ws_connected:
                break
            await asyncio.sleep(0.5)
        # 兜底: 主动拉一次全量, 再给 1s 收帧
        try:
            await km.refresh_status()
        except Exception as e:
            self.logger.warning(f"启动主动刷新Klipper状态失败: {e}")
        await asyncio.sleep(1)
        _snap = km.get_printer_status()
        self.logger.warning(f"Klipper状态就绪等待超时({timeout:.0f}s), 按当前快照降级对齐: "
                            f"state={_snap.get('printer_state')}, print_stats={bool(_snap.get('print_stats'))}")
        return _snap

    async def _pause_print_guarded(self, source: str) -> str:
        """lancaigang261017 任务#447: 带“队列探测 + 生效确认”的守卫式暂停(全部暂停入口共用)。

        为何不能直接 pause_print: _send_gcode 的回执只代表 Klipper 受理了请求。升温(M191/M109)、
        调平(QUAD_GANTRY_LEVEL/网床)、换头/长宏执行期间 gcode 队列被阻塞, PAUSE 会排在队列尾部,
        十几分钟后才迟到生效, 打印在任意时刻被莫名打断(261017 09:55 事故)。

        Returns:
            'ok'      : 已确认暂停(pause_resume.is_paused=True)
            'blocked' : 队列阻塞, 本次未下发 PAUSE(调用方应稍后重试/等 0x1D 重发)
            'failed'  : PAUSE 未被受理或确认窗内未生效(命令可能迟到生效, 已在途守卫)。
                        调用方必须落盘自救标记(_save_pause_abort_marker), 使暂停迟到生效后可接续
        """
        if not await self.klipper_monitor.probe_gcode_queue(5.0):
            self.logger.warning(f"{source}: gcode队列阻塞(升温/调平/长命令执行中), 暂不下发PAUSE")
            return 'blocked'
        if not await self.klipper_monitor.pause_print():
            # 任务#447b: 回执超时/被拒 != 没暂停。PAUSE 宏含 M400+停车+抬Z, is_paused 在宏中段就翻 True,
            # 应答却要等整个宏跑完(实测>10s)。此处补一次生效确认, 确认为真则按 ok 继续编排,
            # 避免"打印机已暂停、hub 却中止换料"导致槽 0g 卡死(261017 10:55 事故)。
            if await self.klipper_monitor.wait_paused(PAUSE_ACK_RECHECK_SECONDS):
                self._pause_pending_since = None
                self.logger.warning(f"{source}: PAUSE回执超时, 但{PAUSE_ACK_RECHECK_SECONDS:.0f}s内确认"
                                    f"pause_resume.is_paused=True(迟到确认), 判定已暂停, 继续编排(任务#447b)")
                return 'ok'
            self._pause_pending_since = time.monotonic()
            self.logger.error(f"{source}: PAUSE未被Klipper受理(回执超时/被拒), 命令可能迟到生效, 已在途守卫")
            return 'failed'
        if not await self.klipper_monitor.wait_paused(5.0):
            # 任务#447b: 回执已回但状态未翻, 再补一次确认窗(宏可能刚跑完/订阅帧刚到), 再判失败。
            if await self.klipper_monitor.wait_paused(PAUSE_ACK_RECHECK_SECONDS):
                self._pause_pending_since = None
                self.logger.warning(f"{source}: PAUSE已受理, {PAUSE_ACK_RECHECK_SECONDS:.0f}s补确认窗内"
                                    f"pause_resume.is_paused 翻为True, 判定已暂停, 继续编排(任务#447b)")
                return 'ok'
            self._pause_pending_since = time.monotonic()
            self.logger.error(f"{source}: PAUSE已受理但未确认生效(pause_resume.is_paused仍为False), 已在途守卫")
            return 'failed'
        self._pause_pending_since = None
        self.logger.info(f"{source}: 打印已暂停(pause_resume.is_paused=True)")
        return 'ok'

    async def _pause_for_runout_with_retry(self, extruder: int):
        """lancaigang261017 任务#447: RUNOUT 暂停的阻塞期后台重试。

        队列被升温/调平/长宏阻塞时每 10s 重试一次(每次先 M400 探测), 队列一畅通立即暂停,
        避免“硬发 PAUSE 导致迟到生效 + 回执超时直接置 ERROR”的双重问题。
        退出条件: 已暂停 / 打印机已不在 printing / 状态机已离开 RUNOUT / 超时置 ERROR。
        """
        deadline = time.monotonic() + RUNOUT_PAUSE_RETRY_TIMEOUT
        attempt = 0
        while True:
            attempt += 1
            _res = await self._pause_print_guarded(f"RUNOUT(挤出机{extruder})暂停第{attempt}次")
            if _res == 'ok':
                return
            if self.klipper_monitor.pause_resume_is_paused:
                self.logger.info(f"RUNOUT暂停重试结束: pause_resume.is_paused=True(第{attempt}次前已生效)")
                return
            if self.klipper_monitor.printer_state != 'printing':
                self.logger.info(f"RUNOUT暂停重试结束: 打印机已不在printing({self.klipper_monitor.printer_state})")
                return
            if self.state_manager.state != SystemStateEnum.RUNOUT:
                self.logger.info(f"RUNOUT暂停重试结束: 状态机已离开RUNOUT({self.state_manager.state.name})")
                return
            if time.monotonic() >= deadline:
                self.logger.error(f"RUNOUT暂停重试已满{RUNOUT_PAUSE_RETRY_TIMEOUT:.0f}s仍未成功"
                                  f"(Klipper持续阻塞), 进入错误状态等待人工处理")
                self.state_manager.transition_to(SystemStateEnum.ERROR,
                                                 reason="Failed to pause print for runout (queue blocked too long)")
                return
            if attempt % 6 == 0:
                self.logger.warning(f"RUNOUT暂停重试中(第{attempt}次): Klipper队列仍阻塞, "
                                    f"继续每{RUNOUT_PAUSE_RETRY_INTERVAL:.0f}s重试")
            await asyncio.sleep(RUNOUT_PAUSE_RETRY_INTERVAL)

    async def _handle_state_change_actions(self, old_state: SystemStateEnum, new_state: SystemStateEnum, payload: Dict[str, Any]):
        """异步处理状态变化的具体动作"""
        try:
            # lancaigang261023120000 任务#540：取消/换轮后尚未调度的旧状态动作不再生效。
            if self.state_manager.state != new_state or self.state_manager.get_payload() is not payload:
                self.logger.info(f"状态动作已过期: {old_state.name}->{new_state.name}，不执行")
                return
            if new_state == SystemStateEnum.RUNOUT:
                extruder = payload.get('extruder')
                fro_context = getattr(self, '_refill_context', None)
                fro_task = getattr(self, '_fro_purge_task', None)
                if (fro_context and fro_context.get('fro_session') and fro_task is not None
                        and not fro_task.done()):
                    self.logger.info("FRO任务已接管守卫式暂停，跳过通用RUNOUT重复暂停")
                    return
                self.logger.info(f"ACTION: 为挤出机 {extruder} 断料事件暂停打印。")
                # lancaigang261017 任务#447: 不再“发完即置ERROR”。旧实现 pause_print 回执超时就进
                # ERROR(261017 10:44:48 实测), 而那条 PAUSE 最终仍迟到生效, 人卡死且时机错乱。
                # 改为后台重试: 队列阻塞期不发 PAUSE, 队列一畅通立即暂停; 超过30min才置ERROR。
                if self.klipper_monitor.pause_resume_is_paused:
                    # lancaigang261017 任务#448: 不再"只记日志就收场"。Klipper 已暂停时,
                    # RUNOUT->PAUSED 的边沿永远不会自己到来(暂停事件早就发生过), 状态机会一直
                    # 停在 RUNOUT: 261017 14:44:30 实测卡 7分10秒, 无编排/无 request_feed,
                    # 14:51:40 随打印取消直接 RUNOUT->IDLE, 断料未续上。此处主动补上边沿,
                    # 交回 PAUSED 状态动作/PAUSED轮询(承重墙 :1814)按缺料事实触发续料编排。
                    # 定策依据: "人工暂停时活跃挤出机传感器无料"属断料例外, 应续料并自动恢复。
                    self.logger.warning(f"打印机已是暂停态(pause_resume.is_paused=True), "
                                        f"主动补齐 RUNOUT->PAUSED 边沿以触发续料编排(任务#448)")
                    self.state_manager.transition_to(SystemStateEnum.PAUSED, extruder=extruder)
                else:
                    asyncio.create_task(self._pause_for_runout_with_retry(extruder))
            
            elif new_state == SystemStateEnum.PAUSED:
                if self._copy_pure_runout() or getattr(self, '_copy_refill_context', None):
                    await self._copy_runout_and_resume()
                    return
                if old_state in [SystemStateEnum.PRINTING, SystemStateEnum.RESUMING, SystemStateEnum.RUNOUT]:
                    # 优先使用当前活跃挤出机
                    extruder = self.klipper_monitor.active_extruder if self.klipper_monitor else 0
                    self.logger.debug(f"[DEBUG] PAUSED状态处理: old_state={old_state}, active_extruder={extruder}")
                    
                    # 检查当前活跃挤出机的断料传感器状态
                    filament_detected = True  # 默认假设有料
                    sensor_status_available = False
                    
                    if self.klipper_monitor and hasattr(self, '_last_filament_status'):
                        # 直接使用本地缓存的传感器状态，这个在_handle_klipper_status_update中实时更新
                        # 检查活跃挤出机对应的传感器状态
                        if extruder < len(self.klipper_monitor.filament_sensor_objects):
                            sensor_obj_name = self.klipper_monitor.filament_sensor_objects[extruder]
                            self.logger.debug(f"[DEBUG] 检查传感器: extruder={extruder}, sensor_obj_name={sensor_obj_name}")
                            self.logger.debug(f"[DEBUG] 缓存的传感器状态: {self._last_filament_status}")
                            if sensor_obj_name in self._last_filament_status:
                                filament_detected = self._last_filament_status[sensor_obj_name]
                                sensor_status_available = True
                                self.logger.info(f"检查活跃挤出机 {extruder} 的传感器 {sensor_obj_name} 状态: {'有料' if filament_detected else '缺料'}")
                            else:
                                self.logger.warning(f"无法获取活跃挤出机 {extruder} 的传感器状态，传感器对象: {sensor_obj_name}")
                        else:
                            self.logger.warning(f"活跃挤出机 {extruder} 没有对应的传感器配置")
                    
                    # 只有在检测到缺料时才走清管续料编排（任务#278 H2: 不再直发0x01，
                    # 线尾停在toolhead传感器处，不清管的话F05新料会顶住管内旧线尾堵管；
                    # 编排入口有互斥标志，与轮询触发路径及低重链路去重）
                    if not filament_detected:
                        # lancaigang261015 任务#442: 自动续料失败锁拦截(状态迁移入口)——F05回执报错已判
                        # 本次续料失败, 即使之后插料盘/传感器变化也不再自动重走编排(等人工/新打印)
                        if self._auto_feed_failed:
                            self.logger.error(f"ACTION: 活跃挤出机 {extruder} 缺料但自动续料已失败锁定(任务#442), "
                                              f"跳过清管续料编排; 请人工处理或开始新一轮打印以解除锁定")
                        else:
                            self.logger.info(f"ACTION: 检测到活跃挤出机 {extruder} 缺料，走清管续料编排（UNLOAD退线尾+0x1E清管+0x01补料）")
                            await self._runout_clear_and_feed(extruder)
                    else:
                        # 当前挤出机有料，不需要补料
                        status_reason = "有料" if sensor_status_available else "无法检测传感器状态（假设有料）"
                        self.logger.info(f"ACTION: 活跃挤出机 {extruder} {status_reason}，暂停时无需补料，保持暂停状态。")
                        # 保持在暂停状态，不转换到其他状态

            elif new_state == SystemStateEnum.RESUMING:
                if payload.get('copy_owned'):
                    return
                # lancaigang261023120000 任务#540：状态动作异步调度，旧恢复不得在取消/新轮次后挤出。
                resume_payload = self.state_manager.get_payload()
                failure_epoch = self._refill_failure_epoch

                def current_resume():
                    return (self._refill_attempt_is_current(failure_epoch)
                            and self.state_manager.state == SystemStateEnum.RESUMING
                            and self.state_manager.get_payload() is resume_payload)

                if not current_resume() or payload is not resume_payload:
                    self.logger.info("恢复动作已过期，不预挤、不RESUME")
                    return
                if self._refill_motion_busy():
                    self.logger.info("退料任务尚未退出，不启动恢复预挤，回FEEDING等待")
                    self.state_manager.transition_to(SystemStateEnum.FEEDING, **payload)
                    return
                self._resume_task = asyncio.current_task()
                # lancaigang261016 任务#446: 低重链路已成功恢复(转RESUMING), 撤销"待恢复借道"标志,
                # 使此后独立runout不再被误判为低重会话(会话级污染修复)。
                self._low_weight_refill_pending = False
                extruder = payload.get('extruder')
                self.logger.info(f"ACTION: 挤出机 {extruder} 恢复有料，检查温度并准备恢复打印。")
                
                # 检查挤出机温度
                temperature = await self._get_extruder_temperature(extruder)
                if not current_resume():
                    return
                if temperature is not None and temperature > 175.0:
                    self.logger.info(f"挤出机 {extruder} 温度为 {temperature:.1f}°C，大于175°C，先挤出100mm料清理喷嘴。")
                    # 先挤出100mm料
                    extruded = await self._extrude_filament(extruder, 100)
                    if not current_resume():
                        return
                    if extruded:
                        self.logger.info(f"挤出机 {extruder} 预挤出100mm完成，现在恢复打印。")
                        # lancaigang261012 任务#411: 预挤后复检(冗余第4道)——料若真到位,
                        # 挤100mm后传感器仍应有料; 转无料=管内无线(旧盘退料未完成/料未续上),
                        # 此时恢复必空管打印, 取消恢复转FEEDING交回超时兜底(FEEDING无业务分支, 不会重复发补料)
                        # lancaigang261025103309 任务#563: 读不到传感器(None)同样不能确认料到位, 原"is False"把未知当有料放行
                        if self._active_sensor_has_filament(extruder) is not True:
                            self.logger.warning("任务#411/#563: 预挤100mm后传感器无料或读不到(不能确认料到位), 取消恢复打印, 转FEEDING等待兜底")
                            self.state_manager.transition_to(SystemStateEnum.FEEDING, extruder=extruder)
                            return
                    else:
                        self.logger.error(f"挤出机 {extruder} 预挤出失败！")
                        self.state_manager.transition_to(SystemStateEnum.ERROR, reason="Failed to pre-extrude filament")
                        return
                else:
                    # lancaigang261025103309 任务#563: 冷态/读不到温度不能预挤确认料到喷嘴, 原"直接恢复打印"会空打;
                    # 判本轮续料失败停在 PAUSED 等人工(喷嘴加热后人工恢复)。
                    _t = f"{temperature:.1f}°C" if temperature is not None else "未知"
                    self._refuse_auto_resume(f"任务#563: 挤出机{extruder}喷嘴温度{_t}(需>175°C), "
                                             f"无法预挤确认料到喷嘴, 拒绝自动恢复", extruder)
                    return
                
                # 恢复打印
                # lancaigang261013215024 任务#434: 分三态处理, 闸门拦截不再误判为失败转ERROR
                _res = await self._resume_print_gated()
                if not current_resume():
                    return
                if _res == 'sent':
                    self.logger.info("恢复打印成功，等待状态同步。")
                    # 恢复打印后，等待Klipper状态变为printing，由状态更新逻辑处理转换
                elif _res == 'blocked':
                    # 闸门主动拦截(如传感器有料未稳/退料中): 非失败, 回退FEEDING等待重试
                    # (转入FEEDING会重装20s驻留窗, 重试时传感器已稳定>5s闸门必过, 有界无紧循环;
                    #  且FEEDING态#388自愈经_pause_orchestrated仍武装(本恢复由编排触发), 30s兜底强制RESUME)
                    self.logger.warning("ACTION: 恢复被#411闸门拦截(非失败), 回退FEEDING等待重试, 不转ERROR")
                    self.state_manager.transition_to(SystemStateEnum.FEEDING, extruder=extruder)
                else:
                    self.logger.error("ACTION FAILED: 恢复打印失败(闸门已放行但发送失败)！进入错误状态。")
                    self.state_manager.transition_to(SystemStateEnum.ERROR, reason="Failed to resume print after feeding")
                    
            elif new_state == SystemStateEnum.ERROR:
                reason = payload.get('reason', 'Unknown error')
                self.logger.error(f"ACTION: 系统进入错误状态，原因: {reason}")
                # lancaigang261019 任务#460: 不得把 hub 自身编排失败当成“打印机错误”告知 F01。
                # F01 收到 0x09 后 canserial_is_printing()=0, Weight_CheckLowWeight 首门直接
                # return -> 低重/断料保护整体静默。261019 槽4 事故: hub 10:03:00 因 RESUME
                # 回执超时进 ERROR 并发了 0x09, 而 klipper 实际一直 printing, F01 st=9
                # 持续 7分26秒, 低重 0x1D 一封未发。
                _real = self._resolve_klipper_real_state()
                if _real and _real != 'error':
                    self.logger.warning(f"任务#460: hub={new_state.name} 但Klipper真实状态={_real}, "
                                        f"按真实状态通知F01(不发0x09, 保低重链路)")
                    await self._send_printer_status_notification(_real)
                else:
                    self.logger.warning("任务#460: 无可用Klipper真实状态, 退回发送0x09打印机错误")
                    await self.can_comm.send_printer_error(error_code=99)

        except Exception as e:
            self.logger.error(f"ACTION FAILED: 状态处理时发生严重错误: {e}", exc_info=True)
            self.state_manager.transition_to(SystemStateEnum.ERROR, reason=f"Exception in state handler: {e}")
        finally:
            if getattr(self, '_resume_task', None) is asyncio.current_task():
                self._resume_task = None

    
    def init(self) -> bool:
        """
        初始化应用程序组件
        
        Returns:
            bool: 初始化是否成功
        """
        try:
            # 记录日志配置信息
            self.logger.info(f"日志文件目录: {self.log_manager.log_dir}")
            self.logger.info(f"日志文件: {self.log_manager.log_file}")
            self.logger.info(f"日志级别: {logging.getLevelName(self.log_manager.log_level)}")
            self.logger.info(f"日志轮转: 文件大小限制={self.log_manager.max_file_size/1024/1024}MB, 保留数量={self.log_manager.backup_count}")
            self.logger.info(f"日志自动清理: {self.log_manager.max_age_days}天")
            
            # 初始化CAN通信
            can_config = self.config['can']
            self.logger.info(f"初始化CAN通信，接口: {can_config['interface']}, 波特率: {can_config['bitrate']}")
            self.can_comm = FeederCabinetCAN(
                interface=can_config['interface'],
                bitrate=can_config['bitrate']
            )
            self.can_comm.set_query_callback(self._handle_filament_status_query)
            self.can_comm.set_mapping_set_callback(self._handle_feeder_mapping_set)
            # lancaigang261017: 消费0x10查询应答, 用于hub启动/CAN重连后与F01对齐映射
            self.can_comm.set_mapping_response_callback(self._handle_feeder_mapping_response)
            self.can_comm.set_reconnect_callback(self._handle_can_reconnect)
            self.can_comm.set_rfid_callback(self._handle_rfid_message)  # 设置RFID回调
            self.can_comm.set_filament_out_callback(self._handle_filament_runout)  # 设置断料回调
            self.can_comm.set_low_weight_callback(self._handle_low_weight_notify)  # 设置低重量通知回调
            self.can_comm.set_weight_fault_callback(self._handle_weight_sensor_fault)  # 任务#454: 称重故障纯通知回调(0x22)
            self.can_comm.set_slot_status_callback(self._handle_slot_status)  # 任务#575: 槽位展示快照(0x23)
            self.can_comm.set_slot_active_callback(self._handle_slot_active)  # 真实活跃槽展示(0x24)
            self.can_comm.set_nack_callback(self._handle_f05_nack)  # 任务#286 V6: 设置F05 NACK回调(0x1E被拒可观测)
            # lancaigang 260918 20:52:35 任务#296 P1-1改动3: 设置0x01回执回调
            # (F05对request_feed的0x03响应帧, seq匹配时回调, 第一版纯观测不改状态机)
            self.can_comm.set_feed_ack_callback(self._handle_feed_ack)
            
            # 为CAN通信模块设置logger
            self.can_comm.logger = self.log_manager.get_child_logger(self.logger, "can")
            
            # 初始化RFID解析器
            if self.config.get('rfid', {}).get('enabled', True):
                self.logger.info("初始化RFID解析器")
                self.rfid_parser = RFIDDataParser()
                self.rfid_parser.logger = self.log_manager.get_child_logger(self.logger, "rfid")
                self.logger.info("RFID功能已启用")
            else:
                self.rfid_parser = None
                self.logger.info("RFID功能已禁用")
                
            # 初始化Spoolman客户端
            spoolman_config = self.config.get('spoolman', {})
            if spoolman_config.get('enabled', False) and spoolman_config.get('url'):
                self.spoolman_client = SpoolmanClient(
                    spoolman_config['url'],
                    moonraker_url=self.config['klipper']['moonraker_url'],
                )
                self.logger.info(f"Spoolman集成已启用: {spoolman_config['url']}")
            else:
                self.spoolman_client = None
                self.logger.info("Spoolman集成已禁用")
            
            # 初始化Klipper监控器
            klipper_config = self.config['klipper']
            self.logger.info(f"初始化Klipper监控器，Moonraker URL: {klipper_config['moonraker_url']}")
            self.klipper_monitor = KlipperMonitor(
                can_comm=self.can_comm,
                moonraker_url=klipper_config['moonraker_url'],
                extruder_config=self.config.get('extruders', None)
            )
            self.klipper_monitor.register_status_callback(self._handle_klipper_status_update)
            self.klipper_monitor.register_disconnect_callback(self._handle_klipper_disconnect)
            
            # 为Klipper监控模块设置logger
            self.klipper_monitor.logger = self.log_manager.get_child_logger(self.logger, "klipper")
            
            # 设置默认活跃挤出机
            extruder_config = self.config.get('extruders', {})
            active_extruder = extruder_config.get('active', 0)
            if active_extruder in [0, 1]:
                self.klipper_monitor.active_extruder = active_extruder
                if active_extruder == 0:
                    self.klipper_monitor.toolhead_info['extruder'] = 'extruder'
                else:
                    self.klipper_monitor.toolhead_info['extruder'] = 'extruder1'
                self.logger.info(f"设置默认活跃挤出机: {active_extruder}")
            
            # 配置断料检测
            filament_config = self.config['filament_runout']
            if filament_config.get('enabled', True):
                # 获取传感器配置
                sensor_names = []
                for sensor in filament_config.get('sensors', []):
                    name = sensor.get('name')
                    if name:
                        sensor_names.append(name)
                
                # 设置传感器名称
                if sensor_names:
                    self.klipper_monitor.filament_sensor_names = sensor_names
                    self.logger.info(f"配置断料传感器: {sensor_names}")
                
                # 启用断料检测
                self.klipper_monitor.enable_filament_runout_detection()
                
                # 配置自动重连
                self.klipper_monitor.auto_reconnect = True
                self.klipper_monitor.reconnect_interval = 5
            
            return True
        except Exception as e:
            self.logger.error(f"初始化应用程序时发生错误: {str(e)}", exc_info=True)
            self.state_manager.transition_to(SystemStateEnum.ERROR, error=str(e))
            return False
    
    async def _state_sync_check_task(self):
        """定期检查状态同步的任务"""
        self.logger.info("启动状态同步检查任务")
        check_interval = 60  # 每60秒检查一次
        
        while self.state_manager.state != SystemStateEnum.DISCONNECTED:
            try:
                await asyncio.sleep(check_interval)
                
                if self.klipper_monitor and self.klipper_monitor.ws_connected:
                    # 获取Klipper内部缓存的状态
                    printer_status = self.klipper_monitor.get_printer_status()
                    klipper_state = printer_status.get('printer_state') if printer_status else None
                    
                    # 如果两个缓存不一致，说明可能存在同步问题
                    if klipper_state and klipper_state != self._last_printer_state:
                        self.logger.warning(f"检测到状态不同步 - 本地: {self._last_printer_state}, Klipper: {klipper_state}")
                        
                        # 重新订阅并查询状态
                        await self.klipper_monitor.resubscribe_objects()
                        await asyncio.sleep(1.0)
                        
                        # 重新获取状态并发送
                        printer_status = self.klipper_monitor.get_printer_status()
                        if printer_status and 'printer_state' in printer_status:
                            current_state = printer_status['printer_state']
                            self.logger.info(f"状态同步后发送最新状态: {current_state}")
                            await self._send_printer_status_notification(current_state)
                            self._last_printer_state = current_state

                    # lancaigang261015 qoder-agent-f01-leader 任务#443: hub状态机对齐纠偏
                    # 场景: 打印paused期间重启hub, 之后用户在Klipper侧外部恢复打印(paused->printing),
                    # 因回PRINTING入口只认{IDLE,RESUMING}(L1422)不含PAUSED -> hub永久卡PAUSED,
                    # RUNOUT检测(L1546, state==PRINTING门禁)与PAUSED轮询(L1481)双双不覆盖
                    # -> 断料保护静默失效。按Klipper实际printing周期纠偏回PRINTING。
                    await self._reconcile_hub_state_with_klipper(klipper_state)
                            
            except asyncio.CancelledError:
                self.logger.info("状态同步检查任务被取消")
                break
            except Exception as e:
                self.logger.error(f"状态同步检查任务异常: {str(e)}", exc_info=True)
                
        self.logger.info("状态同步检查任务结束")
    
    async def _reconcile_hub_state_with_klipper(self, klipper_state):
        """任务#443: hub状态机与Klipper实际状态对齐纠偏。

        仅纠偏一种脱节: Klipper实际 printing 而 hub 卡在 PAUSED(典型成因=打印paused期间
        重启hub, 之后Klipper侧外部恢复打印)。用"连续>=2个同步周期(>=120s)持续printing"去抖,
        避开陈旧帧/瞬时抖动误判; 其它状态组合各有既有入口, 不在此纠偏。
        """
        if klipper_state != 'printing':
            self._sync_printing_streak = 0          # 去抖中断
            return
        if self.state_manager.state != SystemStateEnum.PAUSED:
            self._sync_printing_streak = 0          # 仅PAUSED需纠偏
            return
        self._sync_printing_streak = getattr(self, '_sync_printing_streak', 0) + 1
        if self._sync_printing_streak < 2:
            self.logger.warning(f"任务#443: Klipper已printing但hub处于PAUSED, 去抖中"
                                f"({self._sync_printing_streak}/2周期), 暂不纠偏")
            return
        self.logger.warning(f"任务#443: Klipper持续printing而hub卡PAUSED(连续{self._sync_printing_streak}周期), "
                            f"纠偏对齐回PRINTING(恢复RUNOUT保护)")
        self._sync_printing_streak = 0
        # 该打印会话仍在进行: 刻意不复位会话级标志(_extruder_fed_seen等),
        # 只做状态对齐; 进PRINTING经_on_state_changed自动清超时重试/失败锁(幂等)
        # lancaigang261025103309 任务#563: 但暂停来源(断料证据/低重借道/编排)只属于那一次暂停, 与 paused->printing
        # 边沿(_can_restore_printing 分支)同口径作废; 漏看边沿(重连回填)时不清, 会留给下一次人工暂停, 拔料即被续料+自动恢复。
        if not self._refill_motion_busy() and not self._copy_refill_ctx_live():
            self._session_vacancy_seen = False
            self._low_weight_refill_pending = False
            self._pause_orchestrated = False
            self._gate_block_since = None
        self.state_manager.transition_to(SystemStateEnum.PRINTING)

    async def _can_reconnect_task(self):
        """CAN自动重连任务"""
        self.logger.info("启动CAN自动重连任务")
        reconnect_interval = 5  # 重连间隔5秒
        
        while self.state_manager.state != SystemStateEnum.DISCONNECTED:
            try:
                await asyncio.sleep(reconnect_interval)
                
                if not self.can_comm.connected:
                    self.logger.info(f"CAN重连任务：检测到断开，当前系统状态={self.state_manager.state.name}")
                    self.logger.info("尝试重新连接CAN总线...")
                    if await self.can_comm.connect():
                        self.logger.info("CAN总线重连成功")
                        
                        # 发送当前打印状态和余料状态
                        if self.klipper_monitor:
                            # 获取最新的打印机状态
                            printer_status = self.klipper_monitor.get_printer_status()
                            if printer_status and 'printer_state' in printer_status:
                                current_state = printer_status['printer_state']
                                self.logger.info(f"CAN重连后发送最新打印机状态: {current_state}")
                                await self._send_printer_status_notification(current_state)
                                
                                # 更新缓存的打印机状态，确保后续状态变化能够被正确检测
                                self._last_printer_state = current_state
                                self.logger.info(f"更新打印机状态缓存: {current_state}")
                            else:
                                self.logger.warning("CAN重连后无法获取打印机状态")
                        await self._send_filament_status_notification()
                        
                        break
                    else:
                        self.logger.warning(f"CAN总线重连失败，{reconnect_interval}秒后重试")
                else:
                    # 已经连接，退出重连任务
                    break
                    
            except asyncio.CancelledError:
                self.logger.info("CAN重连任务被取消")
                break
            except Exception as e:
                self.logger.error(f"CAN重连任务异常: {str(e)}", exc_info=True)
                await asyncio.sleep(reconnect_interval)
        
        self.logger.info("CAN自动重连任务结束")
            
    async def start(self) -> bool:
        """
        启动应用程序
        
        Returns:
            bool: 启动是否成功
        """
        if self.state_manager.state != SystemStateEnum.STARTING:
            self.logger.info(f"应用程序已经在运行中或处于非启动状态: {self.state_manager.state.name}")
            return True
            
        try:
            # 连接CAN总线
            if not await self.can_comm.connect():
                self.logger.error("初次连接CAN总线失败，将启动自动重连任务")
                # 启动CAN自动重连任务
                asyncio.create_task(self._can_reconnect_task())
            else:
                # CAN连接成功，发送初始状态
                self.logger.info("CAN连接成功，发送初始状态给送料柜")
                await self.can_comm.send_message(self.can_comm.CMD_PRINTER_IDLE)
                
            # 连接Klipper，增加重试机制
            klipper_connected = False
            retry_interval = 5  # 重试间隔5秒
            self.logger.info("正在尝试连接到Klipper...")
            while not klipper_connected and self.state_manager.state != SystemStateEnum.DISCONNECTED:
                if await self.klipper_monitor.connect():
                    klipper_connected = True
                    self.logger.info("成功连接到Klipper。")
                else:
                    self.logger.warning(f"连接Klipper失败，将在 {retry_interval} 秒后重试...")
                    try:
                        await asyncio.sleep(retry_interval)
                    except asyncio.CancelledError:
                        self.logger.info("Klipper连接任务被取消。")
                        break
            
            if klipper_connected:
                # 如果CAN已连接，发送当前打印状态和余料状态
                if self.can_comm and self.can_comm.connected:
                    # lancaigang261017 部署/重启同步: 先等 Klipper/Moonraker 状态就绪(主动 query + 等首帧),
                    # 再用于快照校验与状态机对齐; 避免 moonraker 刚起来时 print_stats 为空→误判IDLE。
                    printer_status = await self._wait_klipper_state_ready(8.0)
                    self.logger.info(f"启动状态对齐: klipper={printer_status.get('printer_state')}, "
                                     f"job={self._job_key_from_print_stats(printer_status.get('print_stats') or {})}, "
                                     f"is_paused={printer_status.get('is_paused')}, "
                                     f"活跃挤出机={printer_status.get('active_extruder')}")
                    if printer_status and 'printer_state' in printer_status:
                        klipper_state = printer_status['printer_state']
                        if klipper_state == 'paused' and not printer_status.get('is_paused'):
                            # 交叉核对: print_stats.state 为 paused 但 pause_resume.is_paused 未翻,
                            # 属于订阅帧/宏执行中的短暂不一致。状态机以 print_stats.state 为准,
                            # 但记录一条告警便于排查(编排会用 is_paused 决定是否跳步骤1)。
                            self.logger.warning("启动对齐: print_stats.state=paused 但 pause_resume.is_paused=False(帧未同步), "
                                                "暂以 print_stats 为准, 待后续订阅帧刷新")
                        self.logger.info(f"初始化时发送打印机状态: {klipper_state}")
                        await self._send_printer_status_notification(klipper_state)
                        # 更新缓存
                        self._last_printer_state = klipper_state
                        
                        # 同步系统状态
                        # lancaigang261017 运行态持久化: 同一打印任务的快照优先接管状态机
                        # (恢复失败锁/低重借道/FEEDING计时/在途守卫等), 快照不适用时才按现状粗对齐。
                        if self._apply_runtime_state(printer_status):
                            self.logger.info(f"启动时已按运行态快照恢复系统状态: {self.state_manager.state.name}")
                        elif klipper_state == 'printing':
                            self.logger.info("初始化时检测到打印中状态，同步系统状态为PRINTING")
                            self.state_manager.transition_to(SystemStateEnum.PRINTING)
                            # lancaigang261017 运行态持久化缺陷修复: 重启时为printing也必须记下打印开始时刻,
                            # 否则开场缓发门禁(_printing_started_at 为None)失效。未恢复快照时按当前时刻重生。
                            self._printing_started_at = time.monotonic()
                        elif klipper_state == 'paused':
                            self.logger.info("初始化时检测到暂停状态，同步系统状态为PAUSED")
                            # 获取当前活跃挤出机信息
                            active_extruder = self.klipper_monitor.active_extruder if self.klipper_monitor else 0
                            self.state_manager.transition_to(SystemStateEnum.PAUSED, extruder=active_extruder)
                    
                    # 发送余料状态
                    await self._send_filament_status_notification()
            else:
                self.logger.error("在指定时间内未能连接到Klipper，程序将继续运行，但无法与打印机交互，请检查Klipper/Moonraker服务状态。")

            # 启动Klipper监控
            update_interval = self.config['klipper']['update_interval']
            self.klipper_monitor.start_monitoring(interval=update_interval)
            
            # 启动状态同步检查任务
            asyncio.create_task(self._state_sync_check_task())
            # lancaigang261019 任务#460: F01打印状态周期心跳(15s, 防hub卡死后F01状态漂移)
            asyncio.create_task(self._f01_status_heartbeat_task())
            
            # 启动RFID会话清理任务
            asyncio.create_task(self._cleanup_rfid_sessions())
            self.logger.info("启动RFID会话清理任务")
            
            # 如果系统还在STARTING状态，才转换到IDLE（避免覆盖已经设置的PRINTING状态）
            if self.state_manager.state == SystemStateEnum.STARTING:
                self.state_manager.transition_to(SystemStateEnum.IDLE)
                self.logger.info("应用程序已启动，进入空闲状态")
            else:
                self.logger.info(f"应用程序已启动，当前状态: {self.state_manager.state.name}")
            
            return True
        except Exception as e:
            self.logger.error(f"启动应用程序时发生错误: {str(e)}")
            self.state_manager.transition_to(SystemStateEnum.ERROR, error=str(e))
            await self.stop()
            return False
    
    async def stop(self):
        """停止应用程序"""
        self.logger.info("正在停止应用程序...")
        self.state_manager.transition_to(SystemStateEnum.DISCONNECTED)
        
        try:
            if self.klipper_monitor:
                self.logger.info("正在断开Klipper监控器...")
                await self.klipper_monitor.disconnect()
                
            if self.can_comm:
                self.logger.info("正在断开CAN通信...")
                await self.can_comm.disconnect()

            self.logger.info("应用程序已成功停止。")
        except Exception as e:
            self.logger.error(f"停止应用程序时发生错误: {str(e)}", exc_info=True)
    
    async def run(self):
        """运行应用程序"""
        if not self.init():
            self.logger.error("初始化失败，退出程序")
            return
            
        if not await self.start():
            self.logger.error("启动失败，退出程序")
            return
            
        # 保持程序运行
        try:
            while self.state_manager.state != SystemStateEnum.DISCONNECTED:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.logger.info("接收到终止信号，正在停止...")
        finally:
            await self.stop()

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="自动续料系统")
    parser.add_argument(
        "-c", "--config",
        help=f"配置文件路径 (默认: {DEFAULT_CONFIG_PATH})",
        default=DEFAULT_CONFIG_PATH
    )
    parser.add_argument(
        "-v", "--verbose",
        help="增加输出详细程度",
        action="store_true"
    )
    parser.add_argument(
        "--check-config",
        help="只检查配置文件是否有效",
        action="store_true"
    )
    parser.add_argument(
        "--dry-run",
        help="初始化但不启动系统",
        action="store_true"
    )
    parser.add_argument(
        "--log-stats",
        help="显示日志统计信息并退出",
        action="store_true"
    )
    parser.add_argument(
        "--archive-logs",
        help="归档旧日志文件",
        action="store_true"
    )
    
    return parser.parse_args()

def main():
    """主函数"""
    try:
        # 最早的错误捕获 - 先创建一个简单的日志记录器
        import traceback
        
        # 创建临时日志记录器以便调试启动问题
        temp_logger = logging.getLogger("feeder_cabinet.startup")
        temp_logger.setLevel(logging.DEBUG)
        
        # 添加控制台和文件处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)
        temp_logger.addHandler(console_handler)
        
        # 尝试创建日志目录并添加文件处理器
        try:
            os.makedirs(DEFAULT_LOG_DIR, exist_ok=True)
            file_handler = logging.FileHandler(os.path.join(DEFAULT_LOG_DIR, 'feeder_cabinet_startup.log'))
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(console_formatter)
            temp_logger.addHandler(file_handler)
        except Exception as e:
            temp_logger.error(f"无法创建日志文件: {e}")
        
        temp_logger.info("启动 feeder_cabinet 主程序...")
        
        args = parse_args()
        temp_logger.info(f"命令行参数: {args}")
        
        # 创建应用程序实例
        temp_logger.info(f"加载配置文件: {args.config}")
        app = FeederCabinetApp(config_path=args.config)
        temp_logger.info("应用程序实例创建成功")
        
    except Exception as e:
        # 如果无法创建应用，至少输出错误到标准输出
        error_msg = f"创建应用程序实例失败: {str(e)}\n{traceback.format_exc()}"
        if 'temp_logger' in locals():
            temp_logger.error(error_msg)
        else:
            print(error_msg, file=sys.stderr)
        sys.exit(1)
    
    # 如果指定了详细输出，设置日志级别为DEBUG
    if args.verbose:
        app.log_manager.update_log_level(app.logger, "DEBUG")
    
    # 如果显示日志统计
    if args.log_stats:
        stats = app.log_manager.get_log_stats()
        app.logger.info("日志统计信息：")
        app.logger.info(f"  日志目录: {stats['log_dir']}")
        app.logger.info(f"  日志文件数: {len(stats['files'])}")
        app.logger.info(f"  总大小: {stats['total_size']/1024/1024:.2f} MB")
        if stats['oldest_file']:
            app.logger.info(f"  最旧文件: {os.path.basename(stats['oldest_file'])}")
        if stats['newest_file']:
            app.logger.info(f"  最新文件: {os.path.basename(stats['newest_file'])}")
        for file_info in stats['files']:
            app.logger.info(f"    - {os.path.basename(file_info['path'])}: {file_info['size']/1024:.2f} KB")
        return
    
    # 如果归档日志
    if args.archive_logs:
        if app.log_manager.archive_logs():
            app.logger.info("日志归档成功")
        else:
            app.logger.error("日志归档失败")
        return
    
    # 如果只检查配置
    if args.check_config:
        app.logger.info("配置检查完成")
        return
    
    # 如果是试运行模式
    if args.dry_run:
        if app.init():
            app.logger.info("初始化成功，试运行模式，不启动系统")
        else:
            app.logger.error("初始化失败")
        return
    
    # 正常运行
    try:
        asyncio.run(app.run())
    except Exception as e:
        app.logger.error(f"运行时发生错误: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main() 
