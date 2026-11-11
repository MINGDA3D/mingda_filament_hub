#!/bin/bash
# lancaigang261012 任务#411 配套: 本地(WSL) -> 机器(192.168.26.161) 代码同步
#
# 工作制度(用户定策 261012): filament_hub 的代码【先在本地改, 再同步到机器】,
# 禁止直接在机器上改。本脚本是唯一的"上传"通道。
#
# 用法:
#   ./scripts/sync_to_device.sh            # 预演(dry-run, 只看会传哪些文件, 不改任何东西)
#   ./scripts/sync_to_device.sh --apply    # 实际同步: 远端先打包备份 -> rsync -> 远端语法校验
#
# 注意:
#   - 只同步 src/ 与 config/(与"机器->本地"拉取范围一致), 不动机器上的 .git、日志、bak
#   - 同步后代码不会立即生效, 需自行择时: sudo systemctl restart mingda_filament_hub
#     (重启会中断当前编排, 打印中不要随便重启)
#   - 排除: __pycache__ *.pyc *.bak* *.egg-info/

set -euo pipefail

# lancaigang261013174907: 修正两处与现状不符(261013实测): 本地目录名补 -filament_hub(原少后缀不存在); 机器IP .102->.109(.102不可达, 实测通)
# lancaigang261015141200 qoder-agent-f01-leader: 部署目标 109->113(用户261015纠正当前目标机为 192.168.26.113, 109 为历史地址)
# lancaigang261015162600 qoder-agent-f01-leader: 部署目标 113->161(用户261015: 机器重烧系统, 新目标机 192.168.26.161; 113 转为历史目标)
# lancaigang261015214500 qoder-agent-f01-leader: 部署目标 161->140(用户261015: 新机器 192.168.26.140 部署新版hub; 161 转为历史目标)
LBASE=/home/lancaigang-xm/lancaigang-mingda-RD/mingda-600D-机器拉取-2609261508-120-filament_hub/mingda_filament_hub
HOST=mingda@192.168.26.140
RBASE=/home/mingda/mingda_filament_hub
SSHOPT=(-o StrictHostKeyChecking=no -o ConnectTimeout=25)
EXCL=(--exclude '__pycache__/' --exclude '*.pyc' --exclude '*.bak*' --exclude '*.egg-info/')

MODE=${1:-dry}
TS=$(date +%y%m%d%H%M%S)

# lancaigang261022103000 fixflow：显式目标的单文件流水线，不夹带CAN改动或覆盖运行配置。
# 用法：--check-main/--apply-main mingda@192.168.26.121 <远端原main.py的完整SHA256>
# --check-main只读预检；--apply-main要求本地main已提交，空闲才备份并原子替换，不自动重启。
if [[ "$MODE" == --check-main || "$MODE" == --apply-main ]]; then
    HOST=${2:?必须明确指定目标主机}
    EXPECTED=${3:?必须提供远端原main.py的SHA256}
    [[ "$HOST" =~ ^mingda@192\.168\.26\.[0-9]{1,3}$ ]] || { echo "目标格式错误" >&2; exit 2; }
    [[ "$EXPECTED" =~ ^[0-9a-f]{64}$ ]] || { echo "原文件SHA256格式错误" >&2; exit 2; }
    FILE=src/feeder_cabinet/main.py
    SSH=(sshpass -p mingda ssh "${SSHOPT[@]}" "$HOST")
    check_main_target() {
        "${SSH[@]}" python3 - "$RBASE/$FILE" "$EXPECTED" <<'PY'
import hashlib
import json
import math
from pathlib import Path
import sys
import urllib.request

source = Path(sys.argv[1])
if hashlib.sha256(source.read_bytes()).hexdigest() != sys.argv[2]:
    sys.exit('远端main.py已变化，停止覆盖；请重新核对差异')
url = 'http://127.0.0.1:7125/printer/objects/query?print_stats&pause_resume&motion_report'
with urllib.request.urlopen(url, timeout=10) as response:
    status = json.load(response)['result']['status']
state = status['print_stats']['state']
paused = status['pause_resume']['is_paused']
velocities = [status['motion_report'][key]
              for key in ('live_velocity', 'live_extruder_velocity')]
if (state not in ('standby', 'ready', 'complete', 'cancelled') or paused
        or any(not math.isfinite(v) or abs(v) > 0.001 for v in velocities)):
    sys.exit('设备未安全空闲，拒绝部署：state=%s paused=%s velocity=%s'
             % (state, paused, velocities))
print('目标版本与空闲门禁通过：%s' % state)
PY
    }
    echo "单文件流水线：$MODE $HOST:$RBASE/$FILE"
    check_main_target
    [[ "$MODE" == --check-main ]] && exit 0
    git -C "$LBASE" diff --quiet HEAD -- "$FILE" || {
        echo "本地main.py尚有未提交改动，拒绝部署" >&2; exit 2;
    }
    python3 -m py_compile "$LBASE/$FILE"
    LOCAL_SHA=$(sha256sum "$LBASE/$FILE" | cut -d' ' -f1)
    STAGED="$RBASE/src/feeder_cabinet/.main.$TS.py"
    BACKUP="/tmp/hub_main_backup_$TS.py"
    rsync -avz -e "sshpass -p mingda ssh ${SSHOPT[*]}" "$LBASE/$FILE" "$HOST:$STAGED"
    "${SSH[@]}" python3 - "$STAGED" "$LOCAL_SHA" <<'PY'
import hashlib
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_bytes()
if hashlib.sha256(source).hexdigest() != sys.argv[2]:
    sys.exit('上传SHA256校验失败，未替换运行文件')
compile(source, sys.argv[1], 'exec')
PY
    # 暂存期间设备可能重新开始打印，替换前再次检查；失败只留下暂存文件。
    check_main_target
    "${SSH[@]}" "cp -p '$RBASE/$FILE' '$BACKUP' && chmod --reference='$RBASE/$FILE' '$STAGED' && mv '$STAGED' '$RBASE/$FILE'"
    REMOTE_SHA=$("${SSH[@]}" sha256sum "$RBASE/$FILE" | cut -d' ' -f1)
    [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]] || { echo "最终SHA256不一致" >&2; exit 1; }
    echo "单文件同步完成，SHA256=$REMOTE_SHA；备份=$BACKUP；尚未重启服务。"
    exit 0
fi

# lancaigang261024140500 任务#554: 单文件模式只能覆盖 main.py，而 F05 协议 V3 需要主收、CAN 收发
# 同批落地（拆分部署会出现"发了终止请求但没人发版本帧"的半套契约）。扩展为显式清单模式：
#   ./scripts/sync_to_device.sh --check-files mingda@192.168.26.108 <清单文件>   # 只读预检
#   ./scripts/sync_to_device.sh --apply-files mingda@192.168.26.108 <清单文件>   # 备份+原子替换
# 清单每行 "<相对仓库根的路径> <该文件预期 SHA256>"，只允许 src/feeder_cabinet/*.py；
# 不碰运行 config.yaml、宏与远端既有删除项。保留：空闲门禁、远端备份、暂存文件语法校验、
# 原子替换、覆盖前哈希复核（远端在本次运行开始时的内容不得变化）、替换后逐文件 SHA 比对；
# 不自动重启服务。目标必须显式指定，不使用脚本旧默认地址。
if [[ "$MODE" == --check-files || "$MODE" == --apply-files ]]; then
    HOST=${2:?必须明确指定目标主机}
    MANIFEST=${3:?必须提供文件清单(每行: 相对路径 sha256)}
    [[ "$HOST" =~ ^mingda@192\.168\.26\.[0-9]{1,3}$ ]] || { echo "目标格式错误" >&2; exit 2; }
    [[ -r "$MANIFEST" ]] || { echo "清单不可读: $MANIFEST" >&2; exit 2; }
    FILES=(); EXPECTS=()
    while read -r p s; do
        [ -z "${p:-}" ] && continue
        [[ "$p" =~ ^src/feeder_cabinet/[A-Za-z0-9_]+\.py$ ]] || { echo "清单路径不在允许范围: $p" >&2; exit 2; }
        [[ "$s" =~ ^[0-9a-f]{64}$ ]] || { echo "清单SHA256格式错误: $p" >&2; exit 2; }
        FILES+=("$p"); EXPECTS+=("$s")
    done < "$MANIFEST"
    [ "${#FILES[@]}" -gt 0 ] || { echo "清单为空" >&2; exit 2; }
    SSH=(sshpass -p mingda ssh "${SSHOPT[@]}" "$HOST")

    # 1. 本地身份：内容等于清单 SHA、已提交且无未提交改动、语法可编译
    for i in "${!FILES[@]}"; do
        f=${FILES[$i]}
        [ -f "$LBASE/$f" ] || { echo "本地缺文件: $f" >&2; exit 2; }
        ACT=$(sha256sum "$LBASE/$f" | cut -d' ' -f1)
        [[ "$ACT" == "${EXPECTS[$i]}" ]] || { echo "本地内容与清单不符: $f 实际=$ACT" >&2; exit 2; }
        git -C "$LBASE" ls-files --error-unmatch "$f" > /dev/null 2>&1 || { echo "文件未纳入版本库: $f" >&2; exit 2; }
        git -C "$LBASE" diff --quiet HEAD -- "$f" || { echo "本地尚有未提交改动: $f" >&2; exit 2; }
        python3 -m py_compile "$LBASE/$f"
    done
    echo "本地清单核验通过: ${#FILES[@]} 个文件"

    # 2. 空闲门禁：打印中/暂停/有速度一律拒绝（不强行取消打印）
    check_files_target() {
        "${SSH[@]}" python3 - <<'PY'
import json
import math
import sys
import urllib.request

url = 'http://127.0.0.1:7125/printer/objects/query?print_stats&pause_resume&motion_report'
with urllib.request.urlopen(url, timeout=10) as response:
    status = json.load(response)['result']['status']
state = status['print_stats']['state']
paused = status['pause_resume']['is_paused']
velocities = [status['motion_report'][key]
              for key in ('live_velocity', 'live_extruder_velocity')]
if (state not in ('standby', 'ready', 'complete', 'cancelled') or paused
        or any(not math.isfinite(v) or abs(v) > 0.001 for v in velocities)):
    sys.exit('设备未安全空闲，拒绝部署：state=%s paused=%s velocity=%s'
             % (state, paused, velocities))
print('空闲门禁通过：%s' % state)
PY
    }
    echo "清单流水线：$MODE $HOST (${#FILES[@]} 个文件)"
    check_files_target
    [[ "$MODE" == --check-files ]] && exit 0

    # 3. 远端当前内容快照，覆盖前复核（期间被他人改动即停）
    BEFORE="/tmp/hub_files_before_$TS.txt"
    : > "$BEFORE"
    for f in "${FILES[@]}"; do
        RS=$("${SSH[@]}" "sha256sum '$RBASE/$f' 2>/dev/null | cut -d' ' -f1")
        [ -n "$RS" ] || { echo "远端缺文件: $f" >&2; exit 1; }
        printf '%s %s\n' "$f" "$RS" >> "$BEFORE"
    done

    # 4. 远端备份 + 逐个文件暂存→校验→原子替换
    BACKUP="/tmp/hub_files_backup_$TS.tar.gz"
    "${SSH[@]}" "cd '$RBASE' && tar czf '$BACKUP' ${FILES[*]} && ls -l '$BACKUP'"
    for i in "${!FILES[@]}"; do
        f=${FILES[$i]}; exp=${EXPECTS[$i]}
        base=$(basename "$f")
        staged="$RBASE/$(dirname "$f")/.${base}.$TS.staged"
        PREV=$(grep "^$f " "$BEFORE" | cut -d' ' -f2)
        CUR=$("${SSH[@]}" "sha256sum '$RBASE/$f' | cut -d' ' -f1")
        [[ "$CUR" == "$PREV" ]] || { echo "远端在部署期间被改动，停止: $f" >&2; exit 1; }
        rsync -avz -e "sshpass -p mingda ssh ${SSHOPT[*]}" "$LBASE/$f" "$HOST:$staged"
        "${SSH[@]}" python3 - "$staged" "$exp" <<'PY'
import hashlib
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_bytes()
if hashlib.sha256(source).hexdigest() != sys.argv[2]:
    sys.exit('暂存文件SHA256校验失败，未替换运行文件')
compile(source, sys.argv[1], 'exec')
PY
        # 暂存期间设备可能重新开始打印，替换前再查一次
        check_files_target
        "${SSH[@]}" "chmod --reference='$RBASE/$f' '$staged' && mv '$staged' '$RBASE/$f'"
        FINAL=$("${SSH[@]}" "sha256sum '$RBASE/$f' | cut -d' ' -f1")
        [[ "$FINAL" == "$exp" ]] || { echo "替换后SHA256不一致: $f" >&2; exit 1; }
        echo "  $f SAME $FINAL"
    done
    echo "清单同步完成，${#FILES[@]} 个文件 SHA256 全部一致；备份=$BACKUP；尚未重启服务。"
    exit 0
fi

echo "=== 本地 -> 机器 同步 (mode=$MODE) ==="
echo "本地: $LBASE"
echo "远端: $HOST:$RBASE"
echo

# 0. 本地语法自检(改坏了不许传)
echo "=== 0. 本地语法自检 ==="
python3 -m py_compile "$LBASE"/src/feeder_cabinet/*.py && echo "本地 py_compile OK"

# 1. 预演/执行
RSYNC=(rsync -avz "${EXCL[@]}" -e "sshpass -p mingda ssh ${SSHOPT[*]}")
case "$MODE" in
  --apply)
    echo
    echo "=== 1. 远端备份(src+config -> /tmp/hub_backup_$TS.tar.gz) ==="
    sshpass -p mingda ssh "${SSHOPT[@]}" "$HOST" \
      "cd $RBASE && tar czf /tmp/hub_backup_$TS.tar.gz src config && ls -la /tmp/hub_backup_$TS.tar.gz"
    echo
    echo "=== 2. 同步 src/ ==="
    "${RSYNC[@]}" "$LBASE"/src/ "$HOST:$RBASE"/src/
    echo
    echo "=== 3. 同步 config/ ==="
    "${RSYNC[@]}" "$LBASE"/config/ "$HOST:$RBASE"/config/
    echo
    echo "=== 4. 远端语法校验 ==="
    sshpass -p mingda ssh "${SSHOPT[@]}" "$HOST" \
      "cd $RBASE && python3 -m py_compile src/feeder_cabinet/*.py && echo 远端_py_compile_OK"
    echo
    echo "=== 5. 同步后 md5 核对 ==="
    for f in src/feeder_cabinet/main.py src/feeder_cabinet/klipper_monitor.py src/feeder_cabinet/can_communication.py config/config.yaml; do
      LM=$(md5sum "$LBASE/$f" | cut -c1-10)
      RM=$(sshpass -p mingda ssh "${SSHOPT[@]}" "$HOST" "md5sum $RBASE/$f | cut -c1-10")
      [ "$LM" = "$RM" ] && echo "  $f SAME" || echo "  $f DIFF local=$LM remote=$RM"
    done
    echo
    echo "同步完成。代码尚未生效, 需择时重启: sudo systemctl restart mingda_filament_hub"
    ;;
  *)
    echo
    echo "=== 预演 src/ ==="
    "${RSYNC[@]}" --dry-run "$LBASE"/src/ "$HOST:$RBASE"/src/
    echo
    echo "=== 预演 config/ ==="
    "${RSYNC[@]}" --dry-run "$LBASE"/config/ "$HOST:$RBASE"/config/
    echo
    echo "以上为预演, 未改动任何文件。确认无误后加 --apply 执行。"
    ;;
esac
