#!/usr/bin/env python3
"""F01 0x01 续料回执语义契约 —— 任务#463 B(机制收敛)的唯一真值源。

## 为什么单独成模块
261019 一天内同一处逻辑改了 4 次(A/B/R2/R3), 每次都是"改一个冒一个":
  · 改前(status 恒 0x00): hub 把"中间态默认值"当"没受理" -> 退掉已进好的料;
  · 改后(status 中间态 -> 0x02): F01 受理 373ms 后就回落 IDLE -> hub 只认一帧, 死锁自愈永不触发。
根因不是某一行写错, 而是【两侧从没签过契约】: status 一个字节同时被当成"受理/在途/失败"用,
判定逻辑散在 main.py 各分支里, 加一个机制就改掉其它机制的触发条件。

故把契约抽成【无依赖纯函数 + 一张表】:
  · 语义只在这里定义, main.py 只负责按键分发动作(不再各处分叉 if);
  · 纯函数可被 tests/test_feed_ack_contract.py 用真机 4 个场景直接回放, 改判定必须先过单测;
  · 契约变更 = 改本文件 + 加 fixture, 一眼可见影响面。

## 契约表(F01 固件 >= 任务#463)
  0x00 IDLE      : 当下【不在续料事务里】。注意 0x00 也可能是"刚置位后立刻回落", 故只当"待裁决";
  0x02 FEEDING   : 请求已被接住(WAIT_FOR_FEED/CHECK_LOSS/ADJUST_POS/RETRACT_*) —— 但不保证持续(真机 373ms 回落);
  0x03 COMPLETE  : 续料已完成 -> 不动;
  0x04 ERROR 或 error_code!=0 : 失败 -> 失败锁(任务#442), 回 PAUSED。
  结论: 0x00 与 0x02 都【不能单独作为动作依据】, 必须 10s 后再问一次(0x03 查询)才裁决。

## 收敛后的唯一决策链(main.py 对应位置)
  [1] 0x01 回执        -> classify_feed_ack()
  [2] ERROR            -> _apply_feed_failure()            失败锁 #442
  [3] 非 ERROR         -> _arm_feed_verdict()              统一挂 10s 裁决(FEED_VERDICT_DELAY_SECONDS)
  [4] 10s 到           -> _feed_verdict() 发 0x03 查询      (can_communication.query_feed_state)
  [5] 探测回执         -> decide_verdict()
        HEAL     : 曾受理(0x02) 且此刻 0x00 -> _clear_buffer_and_refeed()  <== 唯一自愈路径
        PROGRESS : 0x02/0x03                   -> 不动
        FAIL     : 0x04/error!=0               -> 失败锁 #442
        OBSERVE  : 未曾受理(旧固件/未分类)      -> 只记日志, 交 300s 超时(旧固件兼容安全网)
  [6] 探测无回执(15s)  -> 交回 _check_feeding_timeout()  300s 兜底 -> 回 PAUSED 重走编排
  [7] 300s x3          -> 放弃自动重试, 保持 PAUSED 等人工
  => 60s 观察窗(BUFFER_CLEAR_CONFIRM_SECONDS)已退休: 它的职责被 [3][4][5] 完全覆盖, 且更快(10s vs 60s)。

## 适用范围(重要)
本契约以【F01 固件含任务#463(中间态映射 FEEDING)】为前提。若把固件回滚到 #463 之前:
  旧固件对成功续料也回 0x00, 此时"探测回 0x00 -> 清缓冲"会退掉已经送达的料。
  安全网: 旧固件下 saw_accepted 恒为 False(永远收不到 0x02) -> 裁决只走 OBSERVE, 不会退料,
  即自动退化为 #462 之前的"300s 超时"行为。回滚固件无需同时回滚 hub。
"""

# F01 侧状态码(与 can_communication.FeederCabinetCAN.STATUS_* 保持一致;
# 此处重复定义是为了让本模块零依赖、可被单测直接导入)
STATUS_IDLE = 0x00
STATUS_FEEDING = 0x02
STATUS_COMPLETE = 0x03
STATUS_ERROR = 0x04
ERROR_NONE = 0x00

# ---- 0x01 回执分类结果 ----
ACK_FAILED = "failed"                  # 明确失败 -> 失败锁
ACK_ACCEPTED_PENDING = "accepted_pending"  # 已接住, 但需 10s 后复核
ACK_NOT_ACCEPTED = "not_accepted"      # 当下不在续料事务里, 需 10s 后复核
ACK_OBSERVE = "observe"                # 未分类状态, 仅可观测

# ---- 10s 后裁决结论 ----
VERDICT_HEAL = "heal"          # 曾受理但此刻没在续料 -> 唯一自愈路径
VERDICT_PROGRESS = "progress"  # 确在续料/已完成 -> 不动
VERDICT_OBSERVE = "observe"    # 信息不足 -> 只记日志, 交 300s 超时
VERDICT_FAIL = "fail"          # 探测回失败 -> 失败锁


def classify_feed_ack(status: int, error_code: int = ERROR_NONE) -> str:
    """把 0x01 回执分类成唯一动作类别(见模块 docstring 契约表)。"""
    if status == STATUS_ERROR or error_code != ERROR_NONE:
        return ACK_FAILED
    if status == STATUS_FEEDING:
        return ACK_ACCEPTED_PENDING
    if status == STATUS_IDLE:
        return ACK_NOT_ACCEPTED
    return ACK_OBSERVE


def decide_verdict(probe_status: int, error_code: int = ERROR_NONE,
                   saw_accepted: bool = False) -> str:
    """把 10s 后 0x03 探测的真实状态裁决成动作。

    Args:
        probe_status: 探测回执里的 status(反映 F01 当下真实状态)
        error_code: 探测回执里的 error_code
        saw_accepted: 本次续料事务此前是否收到过 0x02(判断"曾受理")

    Returns:
        VERDICT_HEAL / VERDICT_PROGRESS / VERDICT_OBSERVE / VERDICT_FAIL
    """
    if probe_status == STATUS_ERROR or error_code != ERROR_NONE:
        return VERDICT_FAIL
    if probe_status in (STATUS_FEEDING, STATUS_COMPLETE):
        return VERDICT_PROGRESS
    if probe_status == STATUS_IDLE:
        # 只有"先被接住、10s 后却不在事务里"才是死锁(缓冲有料/料盘 FRO 无料)。
        # 从未受理(旧固件恒回 0x00)不在此列 —— 那属于"请求根本没被接住", 交 300s 超时重发。
        return VERDICT_HEAL if saw_accepted else VERDICT_OBSERVE
    return VERDICT_OBSERVE
