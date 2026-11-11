"""
CAN通信模块 - 管理与送料柜之间的CAN通信 (Asyncio Version)

此模块提供与送料柜的CAN总线通信功能，包括：
- 初始化CAN连接和握手
- 发送命令和状态查询
- 接收和解析状态消息
- 错误处理和重连机制
"""

import can
import logging
import asyncio
import shutil
import subprocess
import time
from collections import deque
from datetime import date
from typing import Optional, Callable, Dict, List, Any, Union, TYPE_CHECKING, Coroutine

if TYPE_CHECKING:
    import can

# ============ 任务#286 V6: F05 NACK回帧功能码(模块级常量便于对齐修改) ============
# F05侧任务#284对hub命令(0x1E等)被拒时回NACK帧。hub→F05发送方向0x1F已被
# CMD_EXTRUDE_FILAMENT占用(挤出耗材), 但两方向CAN ID不同(hub发0x10A/F05发0x10B)
# 不冲突。若F05侧最终定稿功能码不同, 只需改此一处常量(leader在#284/#286收口时对齐)。
NACK_FUNC_CODE = 0x1F
# F05 NACK带帧头格式的帧头标识(帧: 0xA5/长度/功能码/原功能码/原因码/CRC, 以F05侧实现为准)
NACK_FRAME_HEAD = 0xA5

class FeederCabinetCAN:
    """送料柜CAN通信类"""
    
    # 状态码定义
    STATUS_IDLE = 0x00      # 空闲
    STATUS_READY = 0x01     # 就绪/准备送料
    STATUS_FEEDING = 0x02   # 送料中
    STATUS_COMPLETE = 0x03  # 完成
    STATUS_ERROR = 0x04     # 错误
    
    # 错误码定义
    ERROR_NONE = 0x00          # 无错误
    ERROR_MECHANICAL = 0x01    # 机械错误
    ERROR_MATERIAL_MISSING = 0x02  # 材料缺失
    ERROR_OTHER = 0x03         # 其他错误
    ERROR_KLIPPER = 0x04       # Klipper错误
    ERROR_MOONRAKER = 0x05     # Moonraker错误
    ERROR_COMMUNICATION = 0x06 # 通信错误
    
    # 命令类型定义
    CMD_REQUEST_FEED           = 0x01       # 请求送料
    CMD_STOP_FEED              = 0x02       # 停止送料
    CMD_QUERY_STATUS           = 0x03       # 查询状态
    CMD_PRINTING               = 0x04       # 打印中
    CMD_PRINT_COMPLETE         = 0x05       # 打印完成
    CMD_PRINT_PAUSE            = 0x06       # 打印暂停
    CMD_PRINT_CANCEL           = 0x07       # 打印取消 
    CMD_PRINTER_IDLE           = 0x08       # 打印机空闲
    CMD_PRINTER_ERROR          = 0x09       # 打印机错误
    CMD_HEARTBEAT              = 0x0A       # 心跳包
    CMD_LOAD_FILAMENT          = 0x0B       # 进料
    CMD_UNLOAD_FILAMENT        = 0x0C       # 退料
    CMD_QUERY_PRINTER_FILAMENT_STATUS  = 0x0D       # 送料柜查询左右料管对应打印机挤出机余料状态
    CMD_PRINTER_FILAMENT_STATUS_RESPONSE = 0x0E    # 送料柜左右料管对应打印机挤出机余料状态响应
    CMD_SET_FEEDER_MAPPING             = 0x0F       # 设置料管与挤出机对应关系
    CMD_QUERY_FEEDER_MAPPING           = 0x10       # 查询料管与挤出机对应关系
    CMD_FEEDER_MAPPING_RESPONSE        = 0x11       # 料管与挤出机对应关系响应
    
    # RFID相关命令
    CMD_RFID_RAW_DATA_NOTIFY   = 0x14       # 主动通知RFID原始数据（起始包）
    CMD_RFID_RAW_DATA_REQUEST  = 0x15       # 请求RFID原始数据
    CMD_RFID_RAW_DATA_RESPONSE = 0x16       # RFID原始数据响应（起始包）
    CMD_RFID_DATA_PACKET       = 0x17       # RFID数据包
    CMD_RFID_DATA_END          = 0x18       # RFID数据传输结束
    CMD_RFID_READ_ERROR        = 0x19       # RFID读取错误
    CMD_FILAMENT_OUT_NOTIFY    = 0x1A       # 断料通知
    CMD_LOW_WEIGHT_NOTIFY      = 0x1D       # 低重量通知
    # lancaigang261017 任务#454: F01称重故障主动通知(F01->hub, 纯通知). 0x22经grep确认
    # 全仓未占用(存量最大0x21温湿度). 语义="该槽raw持续>=60s不可信(未就绪/越健康窗/未去皮)",
    # 判据只读raw/tare, 与净重无关(去皮后0g、取下料盘0g均不会上报), hub侧只记录不动作。
    CMD_WEIGHT_SENSOR_FAULT    = 0x22       # 称重传感器故障通知
    # lancaigang261030233801 任务#575: 周期槽态只进入展示链，复用称重/RFID回调会误触业务动作
    CMD_SLOT_STATUS            = 0x23       # 动态槽位展示快照
    CMD_SLOT_ACTIVE            = 0x24       # 左右buffer真实活跃槽展示
    # lancaigang261031184500 任务#582: 独立F01语义版本，旧固件/未确认版本保持旧展示行为。
    CMD_F01_FW_VERSION_NOTIFY  = 0x25
    # lancaigang261031195545 任务#583: 型号仅作身份元数据，不参与版本能力门。
    CMD_F01_MACHINE_MODEL_NOTIFY = 0x26
    F01_SLOT_SNAPSHOT_MIN_VERSION = (1, 0, 4)
    CMD_RETRACT_FILAMENT       = 0x1E       # 回退耗材
    CMD_EXTRUDE_FILAMENT       = 0x1F       # 挤出耗材

    # ===== lancaigang261021104900 qoder-f05-leader: CAN 接口 bus-off/掉载自愈参数 =====
    # 现场事故(261021 09:40 实测): 261020 与 F01 联调后拔线, hub 仍在发帧但因无 ACK,
    # TX 错误计数累计到 127 → 内核把 can1 置为 BUS-OFF; 该接口 restart-ms=0(不自愈),
    # 次日接 F05 后接口永久停在 <NO-CARRIER,NOARP,UP> state DOWN + BUS-OFF,
    # 每次握手 0x3F0 都 5s 超时 → 无限重连; F05 侧收不到心跳, 屏幕恒显
    # "CAN主机=未连接"(RTT 日志 conn_ok=0 铁证)。
    # 关键: 纯软件重连无效——socketcan 在 bus-off 状态下仍能成功建 bus,
    #       只是帧发不出去, 必须 down/up 重置接口才能恢复。
    # 安全边界: 只在 connect() 建 bus 前检查; 正常 ERROR-ACTIVE 一律不动作(误判率0);
    #           60s 限频防重置风暴; ip/sudo 不可用时 fail-open 仅告警, 不阻塞连接。
    CAN_IFACE_RESTART_MS = 100           # bus-off 后内核自动重启延时(ms), 0=不自愈
    CAN_IFACE_RESET_MIN_INTERVAL = 60.0  # 两次接口重置的最小间隔(秒)

    def __init__(self, interface: str = 'can0', bitrate: int = 1000000):
        """
        初始化CAN通信类
        
        Args:
            interface: CAN接口名称
            bitrate: CAN总线波特率
        """
        self.interface = interface
        self.bitrate = bitrate
        self.bus: Optional[can.BusABC] = None
        self.logger = logging.getLogger("feeder_cabinet.can")
        
        # CAN消息ID定义
        self.SEND_ID = 0x10A       # 打印机 -> 送料柜
        self.RECEIVE_ID = 0x10B    # 送料柜 -> 打印机
        self.HANDSHAKE_SEND_ID = 0x3F0  # 握手发送ID
        self.HANDSHAKE_RECEIVE_ID = 0x3F1  # 握手接收ID
        
        # 握手消息定义
        self.HANDSHAKE_DATA = [0x01, 0xF0, 0x10, 0x00, 0x00, 0x06, 0x01, 0x05]
        self.HANDSHAKE_RESPONSE = [0x05]  # 送料柜返回0x05表示握手成功
        
        # 状态和回调
        self.connected = False
        self.status_callback: Optional[Callable[[Dict], Coroutine]] = None
        self.query_callback: Optional[Callable[[], Coroutine]] = None
        self.mapping_query_callback: Optional[Callable[[], Coroutine]] = None
        self.mapping_response_callback: Optional[Callable[[Dict], Coroutine]] = None
        self.mapping_set_callback: Optional[Callable[[Dict], Coroutine]] = None
        self.reconnect_callback: Optional[Callable[[], Coroutine]] = None  # 重连成功回调
        self.rfid_callback: Optional[Callable[[Dict], Coroutine]] = None  # RFID数据回调
        self.filament_out_callback: Optional[Callable[[Dict], Coroutine]] = None  # 断料通知回调
        self.low_weight_callback: Optional[Callable[[Dict], Coroutine]] = None  # 低重量通知回调
        self.weight_fault_callback: Optional[Callable[[Dict], Coroutine]] = None  # 任务#454: 称重故障通知回调(0x22)
        self.slot_status_callback: Optional[Callable[[Dict], Coroutine]] = None  # 任务#575: 槽位展示回调(0x23)
        self.slot_active_callback: Optional[Callable[[Dict], Coroutine]] = None  # 活跃槽展示回调(0x24)
        self.nack_callback: Optional[Callable[[Dict], Coroutine]] = None  # 任务#286 V6: F05 NACK回帧回调
        # lancaigang 260918 20:52:35 任务#296 P1-1改动3: 0x01补料请求回执接收状态——
        # _feed_seq: 0x01帧data[1]的递增序列号(0-255绕回, 每次request_feed发送时+1);
        # _pending_feed_ack_seq: 最近一次已发送0x01的seq暂存, F05回0x03回执帧seq回显
        # 与之匹配才回调feed_ack_callback(标准ACK一次性语义, 回调后清除防seq绕回误匹配);
        # feed_ack_callback: 回执回调(main.py注册_handle_feed_ack, 第一版纯观测不改状态机)
        self._feed_seq = 0
        self._pending_feed_ack_seq = None
        # lancaigang261024102700 任务#554: seq必须连同物理管和本地事务归属登记，停止回执独立消费。
        self._feed_requests = {}
        self._retired_stop_acks = deque(maxlen=16)
        self._connection_generation = 0
        self._tube_generation = [0, 0]
        self._handshake_frames = deque(maxlen=128)
        self._motion_requests = {}
        self.f05_protocol_version = None
        self.f05_version = None
        self.f01_firmware_version = None
        self.f01_machine_model = None
        self._invalid_022_logged_at = None
        self._invalid_025_logged_at = None
        self._invalid_026_logged_at = None
        self.feed_ack_callback: Optional[Callable[[Dict], Coroutine]] = None
        
        # 异步任务和锁
        self.rx_task: Optional[asyncio.Task] = None
        self.auto_reconnect = True  # 启用自动重连
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()
        self.reconnect_lock = asyncio.Lock()
        
        # 自动重连
        self.auto_reconnect = True
        self.reconnect_interval = 5  # seconds
        
        # 心跳响应监控
        self.heartbeat_sent_time = 0
        self.heartbeat_response_received = False
        self.heartbeat_timeout = 2  # 心跳响应超时2秒
        self.heartbeat_interval = 3  # 心跳发送间隔3秒

        # lancaigang261021104900 qoder-f05-leader: 上次重置 CAN 接口的 monotonic 时间,
        # 用于 CAN_IFACE_RESET_MIN_INTERVAL 限频; 0.0=本进程内从未重置过。
        self._last_iface_reset = 0.0

    # ===== lancaigang261021104900 qoder-f05-leader: CAN 接口状态探针 =====
    def _can_iface_probe(self) -> str:
        """读取 CAN 接口的内核状态原文, 失败返回空串。

        判据只认内核真实状态(ip -details link show 输出), 不做推理:
          正常态: <NOARP,UP,LOWER_UP,ECHO> state UP + can state ERROR-ACTIVE
          异常态: BUS-OFF / NO-CARRIER / state DOWN
        """
        ip_bin = shutil.which('ip') or '/sbin/ip'
        try:
            r = subprocess.run([ip_bin, '-details', 'link', 'show', self.interface],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=3, text=True)
            return r.stdout or ''
        except Exception as e:
            self.logger.warning(f"读取 {self.interface} 接口状态失败: {e}")
            return ''

    # ===== lancaigang261021104900 qoder-f05-leader: CAN 接口重置 =====
    def _can_iface_reset(self) -> bool:
        """down → 重设 bitrate/restart-ms → up, 把 BUS-OFF 接口拉回可用态。

        socketcan 下 python-can 的 bitrate 参数不生效(接口必须由系统预先配置好),
        所以这里用 ip link 显式下发; restart-ms 让内核在下次 bus-off 时自动重启,
        避免重演"拔一次线就永久卡死"。sudo 免密(NOPASSWD: ALL)实测可用。
        """
        ip_bin = shutil.which('ip') or '/sbin/ip'
        cmds = [
            ['link', 'set', self.interface, 'down'],
            ['link', 'set', self.interface, 'type', 'can',
             'bitrate', str(int(self.bitrate)),
             'restart-ms', str(int(self.CAN_IFACE_RESTART_MS))],
            ['link', 'set', self.interface, 'up'],
        ]
        for args in cmds:
            try:
                r = subprocess.run(['sudo', '-n', ip_bin] + args,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=5, text=True)
            except Exception as e:
                self.logger.error(f"CAN接口重置失败(执行异常) {' '.join(args)}: {e}")
                return False
            if r.returncode != 0:
                self.logger.error(f"CAN接口重置失败(rc={r.returncode}) {' '.join(args)}: "
                                  f"{(r.stdout or '').strip()}")
                return False
        return True

    # ===== lancaigang261021104900 qoder-f05-leader: 连接前接口自愈 =====
    async def _ensure_interface_ready(self) -> None:
        """建 bus 之前把 CAN 接口从 BUS-OFF/NO-CARRIER/down 拉回可用态。

        为何必须放在建 bus 之前: bus-off 状态下 socketcan 依然能成功创建 bus,
        失败要到 5s 握手超时才暴露, 若只在失败后重试, 接口永远不会自愈。
        为何 fail-open: ip/sudo 不可用时不能挡住正常连接(那会引入新故障),
        只告警并交由人工处置。
        """
        state = await asyncio.to_thread(self._can_iface_probe)
        if not state:
            return
        bad = ('BUS-OFF' in state) or ('NO-CARRIER' in state) or ('state DOWN' in state)
        if not bad:
            return
        now = time.monotonic()
        if self._last_iface_reset:
            wait = self.CAN_IFACE_RESET_MIN_INTERVAL - (now - self._last_iface_reset)
            if wait > 0:
                self.logger.warning(
                    f"CAN接口 {self.interface} 仍异常, 但距上次重置不足"
                    f"{int(self.CAN_IFACE_RESET_MIN_INTERVAL)}s(还剩{wait:.0f}s), 本轮跳过")
                return
        self._last_iface_reset = now
        self.logger.warning(f"检测到 CAN 接口异常, 开始重置 {self.interface}:\n{state.strip()}")
        ok = await asyncio.to_thread(self._can_iface_reset)
        after = await asyncio.to_thread(self._can_iface_probe)
        if ok:
            self.logger.info(f"CAN接口 {self.interface} 重置完成, 当前状态:\n{after.strip()}")
        else:
            self.logger.error(
                f"CAN接口 {self.interface} 自动重置失败, 需人工执行: "
                f"sudo ip link set {self.interface} down; "
                f"sudo ip link set {self.interface} type can bitrate {int(self.bitrate)} "
                f"restart-ms {int(self.CAN_IFACE_RESTART_MS)}; "
                f"sudo ip link set {self.interface} up")

    async def connect(self) -> bool:
        """
        连接到CAN总线并执行握手过程
        
        Returns:
            bool: 连接和握手是否成功
        """
        if self.connected:
            self.logger.info("已经连接到CAN总线")
            return True
            
        try:
            self.logger.info(f"正在连接到CAN总线 {self.interface}...")
            self._connection_generation += 1
            self.invalidate_feed_requests()
            self.f05_protocol_version = None
            self.f05_version = None
            self.f01_firmware_version = None
            self.f01_machine_model = None
            self._handshake_frames.clear()
            # lancaigang261021104900 qoder-f05-leader: 建 bus 前先自愈接口状态,
            # 否则 bus-off 下握手必然 5s 超时且永不恢复(261021 现场实况)。
            await self._ensure_interface_ready()
            self.bus = can.interface.Bus(
                channel=self.interface,
                bustype='socketcan',
                bitrate=self.bitrate,
                receive_own_messages=False
            )
            self.logger.info(f"成功连接到CAN总线 {self.interface}")
            
            # 执行握手过程
            if not await self._perform_handshake():
                self.logger.error("握手过程失败")
                # 清理连接但保持auto_reconnect
                await self._cleanup_connection()
                return False
            
            self.connected = True

            # 启动后台任务
            self.rx_task = asyncio.create_task(self._receive_loop())
            self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            self.logger.info("CAN连接和握手完成")

            # lancaigang261017: 连接成功后主动查询F01侧当前映射(首次连接/hub重启同样生效),
            # 避免hub停机期间F01点按"左右管交换"造成两边映射永久分叉。
            try:
                await asyncio.sleep(0.3)
                if await self.query_feeder_mapping():
                    self.logger.info("连接成功后已发送料管映射查询命令(启动对齐)")
                else:
                    self.logger.warning("连接成功后发送料管映射查询命令失败")
            except Exception as e:
                self.logger.warning(f"连接成功后查询料管映射时发生异常: {e}")

            return True
        except Exception as e:
            self.logger.error(f"连接CAN总线失败: {str(e)}", exc_info=True)
            if self.bus:
                try:
                    self.bus.shutdown()
                except Exception:
                    pass
                self.bus = None
            return False
    
    async def _cleanup_connection(self):
        """清理连接但不修改auto_reconnect标志"""
        self.connected = False
        self.invalidate_feed_requests()
        self.f05_protocol_version = None
        self.f05_version = None
        self.f01_firmware_version = None
        self.f01_machine_model = None
        
        tasks_to_cancel = []
        if self.rx_task:
            self.rx_task.cancel()
            tasks_to_cancel.append(self.rx_task)
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            tasks_to_cancel.append(self.heartbeat_task)

        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        if self.bus:
            try:
                self.bus.shutdown()
            except Exception as e:
                self.logger.error(f"关闭CAN总线时发生错误: {str(e)}")
            self.bus = None
            
        self.logger.info("已清理CAN总线连接")
    
    async def disconnect(self):
        """断开CAN总线连接"""
        self.connected = False
        self.auto_reconnect = False # 在手动断开时禁用自动重连
        self.invalidate_feed_requests()
        self.f05_protocol_version = None
        self.f05_version = None
        self.f01_firmware_version = None
        self.f01_machine_model = None
        
        tasks_to_cancel = []
        if self.rx_task:
            self.rx_task.cancel()
            tasks_to_cancel.append(self.rx_task)
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            tasks_to_cancel.append(self.heartbeat_task)

        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        if self.bus:
            try:
                self.bus.shutdown()
            except Exception as e:
                self.logger.error(f"关闭CAN总线时发生错误: {str(e)}")
            self.bus = None
            
        self.logger.info("已断开CAN总线连接")
    
    async def reconnect(self):
        """断开并重新连接CAN总线"""
        if not self.auto_reconnect:
            self.logger.warning("自动重连已禁用，跳过重连过程。")
            return

        async with self.reconnect_lock:
            if self.connected:
                return

            self.logger.info("开始CAN总线重连过程...")
            
            # 停止现有任务
            if self.rx_task: self.rx_task.cancel()
            if self.heartbeat_task: self.heartbeat_task.cancel()
            
            # 循环尝试直到重连成功
            while self.auto_reconnect and not self.connected:
                if await self.connect():
                    self.logger.info("CAN总线重连成功！")
                    
                    # 调用重连成功回调
                    if self.reconnect_callback:
                        try:
                            await self.reconnect_callback()
                        except Exception as e:
                            self.logger.error(f"执行重连回调时发生错误: {e}", exc_info=True)
                    
                    # lancaigang261017: 映射查询已上移到 connect() 成功后统一执行(首次连接/重连同一路径),
                    # 此处不再重复发送, 避免一次重连连发两帧 0x10。
                    return
                else:
                    self.logger.warning(f"重连失败，将在 {self.reconnect_interval} 秒后重试。")
                    await asyncio.sleep(self.reconnect_interval)

    async def _perform_handshake(self) -> bool:
        """
        执行握手过程
        
        Returns:
            bool: 握手是否成功
        """
        try:
            # 发送握手消息
            handshake_msg = can.Message(
                arbitration_id=self.HANDSHAKE_SEND_ID,
                data=self.HANDSHAKE_DATA,
                is_extended_id=False
            )
            self.bus.send(handshake_msg)
            self.logger.info(f"已发送握手消息: ID=0x{self.HANDSHAKE_SEND_ID:03X}, 数据={[hex(x) for x in self.HANDSHAKE_DATA]}")
            
            # lancaigang261024102700 任务#554: 单一socket接收者，临时Notifier会吞掉紧随握手的0x22。
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                msg = self.bus.recv(timeout=0)
                if msg is None:
                    await asyncio.sleep(0.01)
                    continue
                if msg.arbitration_id == self.HANDSHAKE_RECEIVE_ID:
                    if list(msg.data) == self.HANDSHAKE_RESPONSE:
                        self.logger.info("收到正确的握手响应")
                        return True
                    self.logger.warning(f"忽略错误握手响应: {list(msg.data)}")
                elif msg.arbitration_id == self.RECEIVE_ID:
                    self._handshake_frames.append(msg)
                await asyncio.sleep(0)
            self.logger.error("握手超时 - 5秒内未收到响应")
            return False
            
        except can.CanError as e:
            self.logger.error(f"握手过程中发生CAN错误: {e}")
            return False
        except Exception as e:
            self.logger.error(f"握手过程发生错误: {str(e)}")
            return False
    
    async def _receive_loop(self):
        """接收消息循环，在独立异步任务中运行"""
        self.logger.info("异步接收任务已启动")
        
        while self.auto_reconnect and self.connected:
            try:
                # 使用非阻塞接收，超时时间短以保持响应性
                msg = (self._handshake_frames.popleft() if self._handshake_frames
                       else self.bus.recv(timeout=0))
                
                if msg is None:
                    # 没有消息时短暂异步睡眠，让出控制权
                    await asyncio.sleep(0.01)
                    continue
                    
                if msg.arbitration_id == self.RECEIVE_ID:
                        self.logger.debug(f"收到消息: ID=0x{msg.arbitration_id:03X}, 数据={[hex(x) for x in msg.data]}")
                        
                        if not msg.data:
                            self.logger.warning("收到空数据帧")
                            continue

                        command = msg.data[0]

                        if command == self.CMD_QUERY_PRINTER_FILAMENT_STATUS and self.query_callback:
                            asyncio.create_task(self.query_callback())
                        elif command == self.CMD_SET_FEEDER_MAPPING and self.mapping_set_callback:
                             if len(msg.data) >= 4 and msg.data[3] == 0x00 and msg.data[1] < 2 and msg.data[2] < 2 and msg.data[1] != msg.data[2]:
                                mapping_data = {
                                    'left_tube': msg.data[1],
                                    'right_tube': msg.data[2],
                                    'status': msg.data[3]
                                }
                                asyncio.create_task(self.mapping_set_callback(mapping_data))
                             else:
                                # lancaigang261017: 非法映射不再静默丢弃(F01侧已落Flash, hub若静默则两边分叉且现场无感知)
                                self.logger.warning(f"收到非法料管映射设置命令, 已丢弃: 数据={[hex(x) for x in msg.data]}")
                        elif command == self.CMD_QUERY_FEEDER_MAPPING and self.mapping_response_callback:
                            # lancaigang261017: F01对0x10查询的应答命令字仍为0x10(F01 canserial.c process_query_feeder_mapping),
                            # 旧代码只定义了0x11且无消费分支 => 查询是死回路。此处按F01真实应答解析。
                            if len(msg.data) >= 4:
                                mapping_response = {
                                    'left_tube': msg.data[1],
                                    'right_tube': msg.data[2],
                                    'status': msg.data[3]
                                }
                                asyncio.create_task(self.mapping_response_callback(mapping_response))
                            else:
                                self.logger.warning(f"料管映射应答长度不足: {len(msg.data)} < 4")
                        elif command in [self.CMD_RFID_RAW_DATA_NOTIFY, self.CMD_RFID_RAW_DATA_RESPONSE, 
                                       self.CMD_RFID_DATA_PACKET, self.CMD_RFID_DATA_END, self.CMD_RFID_READ_ERROR]:
                            # RFID相关消息
                            self.logger.debug(f"收到RFID消息: 命令=0x{command:02X}, 数据={[hex(x) for x in msg.data]}")
                            if self.rfid_callback:
                                rfid_data = {
                                    'command': command,
                                    'data': list(msg.data)
                                }
                                asyncio.create_task(self.rfid_callback(rfid_data))
                            else:
                                self.logger.warning("收到RFID消息但没有设置回调函数")
                        elif command == self.CMD_FILAMENT_OUT_NOTIFY:
                            # 断料通知消息
                            if len(msg.data) >= 6:
                                is_valid = msg.data[1]
                                filament_id = msg.data[2]
                                # 任务#286 V11字段语义校正: F05侧0x1A的msg[3]实为buffer_id(缓冲区编号,
                                # 与0x1D低重通知的msg[3]同语义), 旧代码误解析为extruder_id(门禁
                                # extruder_id<2恰因buffer_id也<2而未拦错误帧, 仅日志口径错位)。
                                # 现按真义buffer_id解析, 帧格式不改(0x1A原样透传), 上层
                                # main._handle_filament_runout按buffer_id接收并反查挤出机用于日志。
                                buffer_id = msg.data[3]
                                status = msg.data[4]
                                material_type = msg.data[5] if len(msg.data) >= 6 else 0x00
                                
                                # 解析耗材类型
                                material_types = {
                                    0x00: "未知",      # Unknown
                                    0x01: "PLA",       # PLA
                                    0x02: "PETG",      # PETG  
                                    0x03: "ABS",       # ABS
                                    0x04: "ASA",       # ASA
                                    0xFF: "其他"       # Other
                                }
                                material_name = material_types.get(material_type, "未定义")
                                
                                self.logger.info(f"收到断料通知: 有效={is_valid}, 耗材通道={filament_id}, 缓冲区={'左' if buffer_id == 0 else '右'}({buffer_id}), 状态={status}, 耗材类型={material_name}(0x{material_type:02X})")
                                
                                if is_valid == 0x01 and status == 0x01 and filament_id < 10 and buffer_id < 2 and self.filament_out_callback:  # 260915门禁改10槽+任务#286 V11: msg[3]按buffer_id门禁(<2物理实体检查不变)
                                    filament_out_data = {
                                        'is_valid': is_valid,
                                        'filament_id': filament_id,
                                        'buffer_id': buffer_id,  # 任务#286 V11: 字段名随语义改为buffer_id(原extruder_id键废弃, 上层已同步改为按buffer_id接收)
                                        'status': status,
                                        'material_type': material_type,
                                        'material_name': material_name
                                    }
                                    asyncio.create_task(self.filament_out_callback(filament_out_data))
                                else:
                                    self.logger.debug("断料通知无效或没有设置回调函数")
                            else:
                                self.logger.warning(f"断料通知数据长度不足: {len(msg.data)} < 6")
                        elif command == self.CMD_LOW_WEIGHT_NOTIFY:
                            # 低重量通知消息
                            if len(msg.data) >= 7:
                                is_valid = msg.data[1]
                                filament_id = msg.data[2]
                                buffer_id = msg.data[3]
                                net_weight = (msg.data[4] << 8) | msg.data[5]  # 高字节在前
                                material_type = msg.data[6]

                                # 解析耗材类型
                                material_types = {
                                    0x00: "未知",      # Unknown
                                    0x01: "PLA",       # PLA
                                    0x02: "PETG",      # PETG
                                    0x03: "ABS",       # ABS
                                    0x04: "ASA",       # ASA
                                    0xFF: "其他"       # Other
                                }
                                material_name = material_types.get(material_type, "未定义")

                                self.logger.info(f"收到低重量通知: 有效={is_valid}, 耗材通道={filament_id}, "
                                               f"缓冲区={'左' if buffer_id == 0 else '右'}({buffer_id}), 净重量={net_weight}g, "
                                               f"耗材类型={material_name}(0x{material_type:02X})")

                                if is_valid == 0x01 and filament_id < 10 and buffer_id < 2 and self.low_weight_callback:  # 260915 qoder-expert-leader: 6槽残留门禁改10槽,槽10低重0x1D曾在此被拦致续料0x01永发不出
                                    low_weight_data = {
                                        'is_valid': is_valid,
                                        'filament_id': filament_id,
                                        'buffer_id': buffer_id,
                                        'net_weight': net_weight,
                                        'material_type': material_type,
                                        'material_name': material_name
                                    }
                                    asyncio.create_task(self.low_weight_callback(low_weight_data))
                                else:
                                    self.logger.debug("低重量通知无效或没有设置回调函数")
                            else:
                                self.logger.warning(f"低重量通知数据长度不足: {len(msg.data)} < 7")
                        elif command == self.CMD_WEIGHT_SENSOR_FAULT:
                            self._handle_version_or_weight_fault(msg.data)
                        elif command == self.CMD_F01_FW_VERSION_NOTIFY:
                            self._handle_f01_version(msg.data)
                        elif command == self.CMD_F01_MACHINE_MODEL_NOTIFY:
                            self._handle_f01_machine_model(msg.data)
                        elif command == self.CMD_SLOT_STATUS:
                            # 旧F01/未确认版本静默保持旧行为，不得把正常旧帧周期刷成非法告警。
                            if not self.f01_supports_slot_snapshot():
                                continue
                            # lancaigang261030233801 任务#575: 固定8字节并先验完整契约，坏帧不得污染展示缓存
                            if (len(msg.data) == 8 and 1 <= msg.data[2] <= 64
                                    and msg.data[1] < msg.data[2]
                                    and msg.data[5] in (0, 1) and msg.data[7] in (0, 1, 2)):
                                if self.slot_status_callback:
                                    slot_status = {
                                        'slot': msg.data[1],
                                        'slot_count': msg.data[2],
                                        'net_weight': (msg.data[3] << 8) | msg.data[4],
                                        'weight_valid': msg.data[5],
                                        'fault_code': msg.data[6],
                                        'info_state': msg.data[7],
                                    }
                                    asyncio.create_task(self.slot_status_callback(slot_status))
                            else:
                                self.logger.warning(f"收到非法槽位状态帧: {list(msg.data)}")
                        elif command == self.CMD_SLOT_ACTIVE:
                            if not self.f01_supports_slot_snapshot():
                                continue
                            # 活跃槽只进入独立展示回调；保留字节必须为零，0xFF表示本侧无活跃槽。
                            if (len(msg.data) == 8 and msg.data[1] in (0, 1)
                                    and 1 <= msg.data[2] <= 64
                                    and (msg.data[3] == 0xFF or msg.data[3] < msg.data[2])
                                    and not any(msg.data[4:8])):
                                if self.slot_active_callback:
                                    slot_active = {
                                        'buffer': msg.data[1],
                                        'slot_count': msg.data[2],
                                        'active_slot': msg.data[3],
                                    }
                                    asyncio.create_task(self.slot_active_callback(slot_active))
                            else:
                                self.logger.warning(f"收到非法活跃槽帧: {list(msg.data)}")
                        elif command == NACK_FUNC_CODE:
                            # 任务#288对拍定稿: F05侧(6c3da82已烧录)NACK裸格式字节契约——
                            # CAN ID 0x10B DLC=8, msg.data=[0x1F, 被拒cmd(0x1E), 原因码(1=运动锁
                            # 窗口外/2=电机忙), 侧别buffer_id(0=左/1=右), 保留x4]。此前msg.data[3]
                            # 侧别被丢弃(裸分支未解析), 现提取透传供上层定位哪侧被拒(重发决策需侧别)。
                            if len(msg.data) >= 3 and self.nack_callback:
                                nack_data = {
                                    'rejected_cmd': msg.data[1],
                                    'reason_code': msg.data[2],
                                    'buffer_id': msg.data[3] if len(msg.data) > 3 else None,  # 侧别0=左/1=右(F05定稿DLC=8恒存在, 防御异常短帧取None)
                                    'format': 'bare'
                                }
                                self._dispatch_nack(nack_data)
                            else:
                                self.logger.warning(f"收到NACK帧但数据长度不足({len(msg.data)}<3)或未设置回调: {[hex(x) for x in msg.data]}")
                        elif command == NACK_FRAME_HEAD and len(msg.data) >= 5 and msg.data[2] == NACK_FUNC_CODE:
                            # 任务#286 V6: F05 NACK回帧——带帧头格式 [0xA5, 长度, 0x1F, 原功能码, 原因码, CRC...]
                            # (任务给定参考帧格式, 以F05侧最终实现为准, hub两种格式都兼容接收)
                            if self.nack_callback:
                                nack_data = {
                                    'rejected_cmd': msg.data[3],
                                    'reason_code': msg.data[4],
                                    'format': 'framed'
                                }
                                self._dispatch_nack(nack_data)
                            else:
                                self.logger.warning(f"收到带帧头NACK帧但未设置回调: {[hex(x) for x in msg.data]}")
                        else:
                            # lancaigang 260918 20:52:35 任务#296 P1-1改动3: 0x03精确匹配优先——
                            # F05对hub 0x01补料请求的回执帧 [0x03, seq回显, buffer_id, status,
                            # error, 0, 0, 8字节](复用既有CMD_QUERY_STATUS通道, CAN ID=
                            # RECEIVE_ID=F05 assigned_id+1)。0x03同时命中旧口径
                            # STATUS_COMPLETE集合, 故此分支必须在其之前精确拦截(互斥整理:
                            # 新分支按command==0x03精确匹配优先, 旧STATUS_*分支口径原样保留,
                            # 0x03帧不再落入旧分支)。seq匹配_pending_feed_ack_seq才回调
                            # feed_ack_callback并清除pending(标准ACK一次性语义); 不匹配/
                            # 无pending记丢弃日志(旧0x01迟到回执/主动查询响应等)
                            if command == self.CMD_QUERY_STATUS:
                                self._handle_feed_response(msg.data)
                            # 检查是否为心跳响应 (根据你的candump，响应格式为: 05 00 FA E2 7E)
                            elif len(msg.data) >= 1 and msg.data[0] == 0x05:
                                self.logger.debug("收到心跳响应")
                                self.heartbeat_response_received = True
                            # 只有特定的状态命令才调用状态回调
                            elif command in [self.STATUS_IDLE, self.STATUS_READY, self.STATUS_FEEDING, 
                                           self.STATUS_COMPLETE, self.STATUS_ERROR]:
                                if self.status_callback:
                                    status_data = {
                                        'status': msg.data[0],
                                        'progress': msg.data[1] if len(msg.data) > 1 else 0,
                                        'error_code': msg.data[2] if len(msg.data) > 2 else 0,
                                        'raw_data': list(msg.data)
                                    }
                                    asyncio.create_task(self.status_callback(status_data))
                            else:
                                # 未知命令，记录但不处理
                                self.logger.debug(f"收到未知命令: 0x{command:02X}, 数据={[hex(x) for x in msg.data]}")
            except can.CanError as e:
                self.logger.error(f"接收消息时发生CAN错误: {e}")
                self.connected = False
                
                # 记录更多诊断信息
                error_str = str(e).lower()
                if "no such device" in error_str:
                    self.logger.error("CAN设备已消失！可能是硬件断开或驱动问题")
                elif "network is down" in error_str:
                    self.logger.error("CAN网络已关闭！可能是接口被禁用")
                
                asyncio.create_task(self.reconnect())
                return
            except asyncio.CancelledError:
                self.logger.info("接收任务被取消")
                break
            except Exception as e:
                if self.connected:
                    self.logger.error(f"接收消息时发生未知错误: {str(e)}", exc_info=True)
                    self.connected = False
                    asyncio.create_task(self.reconnect())
                    return
        
        self.logger.info("接收任务已结束")
    
    # lancaigang261031184500 任务#582: 固定8字节F01版本；非法帧不改变已确认版本。
    def _handle_f01_version(self, data):
        if len(data) == 8 and data[1] != 0 and not any(data[4:8]):
            version = (data[1], data[2], data[3])
            if version != self.f01_firmware_version:
                self.logger.info(f"F01版本确认: {version}")
            self.f01_firmware_version = version
            return
        now = time.monotonic()
        if self._invalid_025_logged_at is None or now - self._invalid_025_logged_at >= 30:
            self._invalid_025_logged_at = now
            self.logger.warning(f"非法F01版本帧: {list(data)}")

    # lancaigang261031195545 任务#583: 旧F01无此帧保持None；非法帧不覆盖已确认型号。
    def _handle_f01_machine_model(self, data):
        if len(data) == 8 and bytes(data[1:7]) == b"MD-F01" and data[7] == 0:
            if self.f01_machine_model != "MD-F01":
                self.logger.info("F01机器型号确认: MD-F01")
            self.f01_machine_model = "MD-F01"
            return
        now = time.monotonic()
        if self._invalid_026_logged_at is None or now - self._invalid_026_logged_at >= 30:
            self._invalid_026_logged_at = now
            self.logger.warning(f"非法F01机器型号帧: {list(data)}")

    def f01_supports_slot_snapshot(self):
        """只有确认支持周期快照的新F01才接收0x23/0x24展示帧。"""
        return (self.f01_firmware_version is not None
                and self.f01_firmware_version >= self.F01_SLOT_SNAPSHOT_MIN_VERSION)

    # lancaigang261024102700 任务#554: 0x22复用两套旧协议，先验日历版本，再验真实故障契约。
    def _handle_version_or_weight_fault(self, data):
        if len(data) == 8 and data[7] != 0:
            try:
                build_date = date(2000 + data[4], data[5], data[6])
            except ValueError:
                pass
            else:
                version = (data[1], data[2], data[3], build_date.isoformat(), data[7])
                if version != self.f05_version:
                    self.logger.info(f"F05版本确认: {version[:3]}, 日期={version[3]}, 协议V{version[4]}")
                self.f05_version = version
                self.f05_protocol_version = data[7]
                return
        if (len(data) in (6, 8) and data[1] == 1 and data[2] < 10 and data[3] < 2
                and data[4] in (1, 2, 3) and not any(data[6:])):
            fault_names = {1: "raw未就绪/读失败", 2: "raw越健康窗", 3: "未去皮"}
            material_names = {0: "未知", 1: "PLA", 2: "PETG", 3: "ABS", 4: "ASA", 255: "其他"}
            fault = {'is_valid': data[1], 'filament_id': data[2], 'buffer_id': data[3],
                     'fault_code': data[4], 'fault_name': fault_names[data[4]],
                     'material_type': data[5], 'material_name': material_names.get(data[5], "未定义")}
            self.logger.warning(f"收到称重故障通知: 槽={data[2]}, 管={data[3]}, 原因={fault['fault_name']}")
            if self.weight_fault_callback:
                asyncio.create_task(self.weight_fault_callback(fault))
            return
        now = time.monotonic()
        if self._invalid_022_logged_at is None or now - self._invalid_022_logged_at >= 30:
            self._invalid_022_logged_at = now
            self.logger.warning(f"0x22帧不符合版本或称重故障契约: {list(data)}")

    def _request_is_valid(self, request):
        valid = request['valid']
        return (self.connected and request['generation'] == self._connection_generation
                and request['tube_generation'] == self._tube_generation[request['tube']]
                and (valid is None or valid()))

    # lancaigang261024103548 任务#554: NACK在接收瞬间绑定真正发出的段，不绑定排队中的新段。
    def _dispatch_nack(self, data):
        key = (data.get('buffer_id'), data.get('rejected_cmd'))
        request = self._motion_requests.pop(key, None)
        data['context'] = (request['context'] if request is not None
                           and self._request_is_valid(request) else None)
        asyncio.create_task(self._deliver_nack(data, request))

    async def _deliver_nack(self, data, request):
        if request is not None and not self._request_is_valid(request):
            data['context'] = None
        if self.nack_callback:
            await self.nack_callback(data)

    async def _send_motion(self, msg, tube, context, valid):
        key = (tube, msg.data[0])
        request = {'context': context, 'valid': valid, 'tube': tube,
                   'generation': self._connection_generation,
                   'tube_generation': self._tube_generation[tube]}
        sent = False
        try:
            sent = await self._send_with_retry(
                msg, valid=lambda: self._request_is_valid(request),
                on_send=lambda: self._motion_requests.__setitem__(key, request))
            return sent
        finally:
            if not sent and self._motion_requests.get(key) is request:
                self._motion_requests.pop(key, None)

    async def _deliver_feed_ack(self, data, request):
        # RX调度到回调之间也可能已失败、取消或换单。
        if self.feed_ack_callback and self._request_is_valid(request):
            await self.feed_ack_callback(data)

    def _handle_feed_response(self, data):
        if len(data) < 5 or data[2] not in (0, 1):
            self.logger.warning(f"非法续料回执: {list(data)}")
            return
        key = (data[1], data[2])
        request = self._feed_requests.pop(key, None)
        ack = {'seq': data[1], 'buffer_id': data[2], 'status': data[3],
               'error_code': data[4], 'raw_data': list(data)}
        if request is not None:
            if self._pending_feed_ack_seq == data[1]:
                self._pending_feed_ack_seq = None
            ack.update(context=request['context'], request_cmd=request['command'])
            valid = self._request_is_valid(request)
            waiter = request['waiter']
            if waiter is not None:
                if not waiter.done():
                    waiter.set_result(ack if valid else None)
            elif valid:
                asyncio.create_task(self._deliver_feed_ack(ack, request))
            self.logger.info(f"续料回执: seq={data[1]}, 管={data[2]}, status={data[3]}, error={data[4]}, 归属有效={valid}")
        elif key in self._retired_stop_acks:
            self.logger.debug(f"重复停止回执仅记录: seq={data[1]}, 管={data[2]}")
        elif data[3] == self.STATUS_ERROR:
            ack['unsolicited'] = True
            # 未匹配帧不具有事务权威；main须对当前owner的本管作新鲜查询。
            request = {'generation': self._connection_generation, 'valid': None,
                       'tube': data[2], 'tube_generation': self._tube_generation[data[2]]}
            asyncio.create_task(self._deliver_feed_ack(ack, request))
        else:
            self.logger.debug(f"丢弃未匹配续料回执: seq={data[1]}, 管={data[2]}")

    def invalidate_feed_requests(self, tube_id=None):
        """撤销本管排队发送/已调度回调；已发物理动作仍须独立停止协议收口。"""
        for tube in ((0, 1) if tube_id is None else (tube_id,)):
            self._tube_generation[tube] += 1
        for key, request in list(self._feed_requests.items()):
            if tube_id is None or key[1] == tube_id:
                self._feed_requests.pop(key, None)
                if self._pending_feed_ack_seq == key[0]:
                    self._pending_feed_ack_seq = None
                waiter = request['waiter']
                if waiter is not None and not waiter.done():
                    waiter.set_result(None)
        for key in list(self._motion_requests):
            if tube_id is None or key[0] == tube_id:
                self._motion_requests.pop(key, None)

    async def _heartbeat_loop(self):
        """心跳消息循环，在独立异步任务中运行"""
        self.logger.info("异步心跳任务已启动")
        heartbeat_fail_count = 0
        max_heartbeat_failures = 2  # 连续失败2次后认为断开
        
        while True:
            try:
                if self.connected:
                    # 重置响应标志
                    self.heartbeat_response_received = False
                    import time
                    self.heartbeat_sent_time = time.time()
                    
                    # 发送心跳
                    success = await self.send_message(self.CMD_HEARTBEAT)
                    if not success:
                        heartbeat_fail_count += 1
                        self.logger.warning(f"心跳发送失败 ({heartbeat_fail_count}/{max_heartbeat_failures})")
                        # 发送失败，立即重试
                        await asyncio.sleep(0.1)  # 短暂延迟后重试
                    else:
                        # 发送成功，等待响应
                        await asyncio.sleep(self.heartbeat_timeout)
                        
                        if not self.heartbeat_response_received:
                            heartbeat_fail_count += 1
                            self.logger.warning(f"心跳未收到响应 ({heartbeat_fail_count}/{max_heartbeat_failures})")
                            # 未收到响应，立即发送下一个心跳
                        else:
                            # 收到响应，重置计数器，等待正常间隔
                            if heartbeat_fail_count > 0:
                                self.logger.info("心跳响应恢复正常")
                            heartbeat_fail_count = 0
                            # 正常情况下等待剩余时间（总共3秒间隔）
                            remaining_time = self.heartbeat_interval - self.heartbeat_timeout
                            if remaining_time > 0:
                                await asyncio.sleep(remaining_time)
                    
                    # 检查是否需要判定断开
                    if heartbeat_fail_count >= max_heartbeat_failures:
                        self.logger.error("连续心跳失败或无响应，判定CAN连接已断开")
                        self.connected = False
                        heartbeat_fail_count = 0
                        if not self.reconnect_lock.locked():
                            asyncio.create_task(self.reconnect())
                else:
                    # 未连接时等待
                    await asyncio.sleep(1)
                            
            except asyncio.CancelledError:
                self.logger.info("心跳任务被取消")
                break
            except Exception as e:
                self.logger.error(f"心跳任务异常: {str(e)}")
                if self.connected:
                    self.connected = False
                    if not self.reconnect_lock.locked():
                        asyncio.create_task(self.reconnect())
                await asyncio.sleep(1)  # 异常后短暂等待
        
        self.logger.info("心跳任务已结束")
    
    
    def set_status_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置状态回调函数
        
        Args:
            callback: 状态回调函数，接收一个状态数据字典
        """
        self.status_callback = callback
    
    def set_query_callback(self, callback: Callable[[], Coroutine]):
        """
        设置查询回调函数
        
        Args:
            callback: 查询回调函数，无参数
        """
        self.query_callback = callback
    
    def set_mapping_query_callback(self, callback: Callable[[], Coroutine]):
        """
        设置料管映射查询回调函数
        
        Args:
            callback: 料管映射查询回调函数，无参数
        """
        self.mapping_query_callback = callback
    
    def set_mapping_response_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置料管映射响应回调函数
        
        Args:
            callback: 料管映射响应回调函数，接收一个映射数据字典
        """
        self.mapping_response_callback = callback
    
    def set_mapping_set_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置料管映射设置回调函数（接收送料柜的设置命令）
        
        Args:
            callback: 料管映射设置回调函数，接收一个映射数据字典
        """
        self.mapping_set_callback = callback
    
    def set_reconnect_callback(self, callback: Callable[[], Coroutine]):
        """
        设置重连成功回调函数
        
        Args:
            callback: 重连成功回调函数
        """
        self.reconnect_callback = callback
    
    def set_rfid_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置RFID数据回调函数
        
        Args:
            callback: RFID数据回调函数，接收包含命令和数据的字典
        """
        self.rfid_callback = callback
    
    def set_filament_out_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置断料通知回调函数

        Args:
            callback: 断料通知回调函数，接收包含断料信息的字典
        """
        self.filament_out_callback = callback

    def set_low_weight_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置低重量通知回调函数

        Args:
            callback: 低重量通知回调函数，接收包含低重量信息的字典
        """
        self.low_weight_callback = callback
    
    def set_weight_fault_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        任务#454: 设置称重故障通知回调函数(0x22)

        Args:
            callback: 称重故障回调函数，接收包含槽位/缓冲区/故障码的字典
        """
        self.weight_fault_callback = callback

    def set_slot_status_callback(self, callback: Callable[[Dict], Coroutine]):
        """lancaigang261030233801 任务#575: 注册独立槽位展示回调，不进入称重业务链。"""
        self.slot_status_callback = callback

    def set_slot_active_callback(self, callback: Callable[[Dict], Coroutine]):
        """注册独立活跃槽展示回调，不进入active spool或机械业务链。"""
        self.slot_active_callback = callback

    def set_nack_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        设置F05 NACK回帧回调函数（任务#286 V6）

        Args:
            callback: NACK回调函数，接收包含被拒命令与原因码的字典
                  (rejected_cmd/reason_code/format, format区分bare裸格式与framed带帧头格式)
        """
        self.nack_callback = callback
    
    def set_feed_ack_callback(self, callback: Callable[[Dict], Coroutine]):
        """
        lancaigang 260918 20:52:35 任务#296 P1-1改动3: 设置0x01补料请求回执回调函数

        Args:
            callback: 回执回调函数，接收包含seq/status/buffer_id/error_code的字典。
                  回执来自F05对0x01(request_feed)的0x03状态响应帧(复用既有
                  CMD_QUERY_STATUS通道, CAN ID=RECEIVE_ID), 仅当帧内seq回显与
                  _pending_feed_ack_seq匹配时回调(不匹配丢弃记日志)
        """
        self.feed_ack_callback = callback
    
    async def _send_with_retry(self, msg: 'can.Message', retries: int = 3, retry_delay: float = 0.05,
                               *, valid=None, on_send=None) -> bool:
        """
        带重试机制的发送方法

        Args:
            msg: can.Message 对象
            retries: 重试次数
            retry_delay: 重试间隔 (秒)

        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("CAN总线未连接，无法发送消息")
            if not self.reconnect_lock.locked():
                asyncio.create_task(self.reconnect())
            return False

        bus = self.bus
        generation = self._connection_generation
        async with self.send_lock:
            last_exception = None
            for attempt in range(retries):
                # lancaigang261024102700 任务#554: 排队和重试await之后重新核本轮资格，旧连接也不得补投。
                if (not self.connected or self.bus is not bus or generation != self._connection_generation
                        or (valid is not None and not valid())):
                    return False
                try:
                    if on_send is not None:
                        on_send()
                    self.bus.send(msg, timeout=0.2)  # lancaigang260926 任务#432: 治总线堵死全局锁28s(限时重试保锁活性)
                    return True
                except can.CanError as e:
                    last_exception = e
                    error_str = str(e).lower()
                    if "no such device" in error_str or "network is down" in error_str:
                        self.logger.error(f"发送失败，CAN设备或网络不可用: {e}")
                        self.connected = False
                        asyncio.create_task(self.reconnect())
                        return False
                    
                    self.logger.warning(f"发送消息时发生CAN错误 (尝试 {attempt + 1}/{retries}): {e}")
                    if attempt < retries - 1:
                        await asyncio.sleep(retry_delay)
                except Exception as e:
                    self.logger.error(f"发送消息时发生未知错误: {e}")
                    self.connected = False
                    asyncio.create_task(self.reconnect())
                    return False
        
        self.logger.error(f"发送消息失败，已达到最大重试次数。最后一次错误: {last_exception}")
        self.connected = False
        if not self.reconnect_lock.locked():
            asyncio.create_task(self.reconnect())
        return False

    async def send_message(self, cmd_type: int, extruder: int = 0) -> bool:
        """
        发送通用消息
        
        Args:
            cmd_type: 命令类型
            extruder: 挤出机编号
            
        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            data = [cmd_type, extruder, 0, 0, 0, 0, 0, 0]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )
            
            return await self._send_with_retry(msg)
            
        except Exception as e:
            self.logger.error(f"构建或发送消息时发生未知错误: {str(e)}")
            return False
    
    # lancaigang261024102700 任务#554: 请求、定向查询与终止共用登记，先登记再发送消除快回执竞态。
    async def _send_feed_command(self, command, tube_id, *, context=None, valid=None,
                                 waiter=None, error_code=0):
        if tube_id not in (0, 1) or not self.connected or not self.bus:
            return 0
        if valid is not None and not valid():
            return 0
        if waiter is None:
            for key, old in list(self._feed_requests.items()):
                if key[1] == tube_id and old['command'] == command and old['waiter'] is None:
                    self._feed_requests.pop(key, None)
        for _ in range(255):
            self._feed_seq = self._feed_seq % 255 + 1
            if not any(key[0] == self._feed_seq for key in self._feed_requests):
                break
        else:
            self.logger.error("续料请求序号已耗尽，拒绝覆盖未决请求")
            return 0
        seq = self._feed_seq
        key = (seq, tube_id)
        while key in self._retired_stop_acks:
            self._retired_stop_acks.remove(key)
        request = {'command': command, 'context': context, 'valid': valid, 'waiter': waiter,
                   'generation': self._connection_generation, 'tube': tube_id,
                   'tube_generation': self._tube_generation[tube_id]}
        self._feed_requests[key] = request
        if waiter is None:
            self._pending_feed_ack_seq = seq
        terminate = command == self.CMD_STOP_FEED
        if terminate:
            self._retired_stop_acks.append(key)
        data = [command, seq, tube_id, int(terminate), error_code if terminate else 0, 0, 0, 0]
        sent = False
        try:
            msg = can.Message(arbitration_id=self.SEND_ID, data=data, is_extended_id=False)
            sent = await self._send_with_retry(
                msg, retries=1 if terminate else 3,
                valid=lambda: self._request_is_valid(request))
            if sent:
                self.logger.info(f"已发送续料命令0x{command:02X}: 料管ID={tube_id}, seq={seq}, 数据={data}")
                return seq
            return 0
        finally:
            if not sent and self._feed_requests.get(key) is request:
                self._feed_requests.pop(key, None)
                if self._pending_feed_ack_seq == seq:
                    self._pending_feed_ack_seq = None

    async def _wait_feed_response(self, command, tube_id, *, context=None, valid=None,
                                  error_code=0, timeout=2.0):
        waiter = asyncio.get_running_loop().create_future()
        try:
            seq = await self._send_feed_command(command, tube_id, context=context, valid=valid,
                                                waiter=waiter, error_code=error_code)
            if not seq:
                return None
            response = await asyncio.wait_for(waiter, timeout)
            return response if valid is None or valid() else None
        except asyncio.TimeoutError:
            return None
        finally:
            # 取消可能发生在send_lock内，尚无seq返回；按自己的Future释放，不清别人的请求。
            for key, request in list(self._feed_requests.items()):
                if request['waiter'] is waiter:
                    self._feed_requests.pop(key, None)
            if not waiter.done():
                waiter.cancel()

    async def query_feed_state_response(self, buffer_id=0, *, context=None, valid=None, timeout=2.0):
        return await self._wait_feed_response(self.CMD_QUERY_STATUS, buffer_id, context=context,
                                              valid=valid, timeout=timeout)

    async def terminate_feed(self, tube_id=0, error_code=3, *, context=None, valid=None, timeout=2.0):
        """V3本侧终止只发一次；返回回执而非把send成功当作停止确认。"""
        if self.f05_protocol_version is None or self.f05_protocol_version < 3:
            self.logger.warning(f"管{tube_id}停止未确认：未确认F05协议V3，不发送扩展终止")
            return None
        error_code = error_code if error_code in (1, 2, 3) else self.ERROR_OTHER
        return await self._wait_feed_response(self.CMD_STOP_FEED, tube_id, error_code=error_code,
                                              context=context, valid=valid, timeout=timeout)

    async def request_feed(self, tube_id: int = 0, *, context=None, valid=None) -> bool:
        """
        请求送料
        
        Args:
            tube_id: 料管编号
            
        Returns:
            bool: 请求是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            return bool(await self._send_feed_command(self.CMD_REQUEST_FEED, tube_id,
                                                      context=context, valid=valid))
        except Exception as e:
            self.logger.error(f"构建或发送补料请求时失败: {str(e)}")
            return False
    
    async def stop_feed(self, tube_id: int = 0) -> bool:
        """
        停止送料
        
        Args:
            tube_id: 料管编号
            
        Returns:
            bool: 停止请求是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            # 格式: [CMD_ID, IS_VALID, TUBE_ID, ...]
            data = [self.CMD_STOP_FEED, 0x00, tube_id, 0x00, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送停止送料请求: 料管ID={tube_id}, 数据={[hex(x) for x in data]}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送停止送料请求时失败: {str(e)}")
            return False
    
    async def query_status(self) -> bool:
        """
        查询送料柜状态
        
        Returns:
            bool: 查询是否成功
        """
        return await self.send_message(self.CMD_QUERY_STATUS)
    
    async def query_feed_state(self, buffer_id: int = 0, *, context=None, valid=None) -> int:
        """lancaigang261019114500 任务#463 R2: 补发一次状态查询(0x03), 用于确认 F01 是否真的在续料。

        为什么需要(261019 11:14 真机直证): F01 对 0x01 的回执反映的是"刚置位那一瞬"的 WAIT_FOR_FEED,
        但实际上它可能在一个 tick(约 373ms)内就因"缓冲有料/本侧料盘 FRO 无料"直接回落 IDLE。
        即 0x02 只能证明"请求被接住了", 不能证明"真的在进料"。

        帧格式与 request_feed 同构: [CMD_QUERY_STATUS(0x03), seq 回显, buffer_id, 0...],
        F01 侧 process_query_status 用 data[1] 回显 seq、data[2] 取 buffer_id; 回帧命中
        既有 0x03+seq 匹配通道 -> feed_ack_callback(与 0x01 回执同一出口, 不新增回调)。

        Returns:
            int: 本次查询使用的 seq(1~255); 0 表示发送失败。
                 调用方用这个 seq 精确识别"探测回执"与"0x01 回执", 不再靠 buffer_id 猜。
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线, 无法发送状态查询")
            return 0

        try:
            return await self._send_feed_command(self.CMD_QUERY_STATUS, buffer_id,
                                                  context=context, valid=valid)

        except Exception as e:
            self.logger.error(f"发送状态查询时失败: {str(e)}")
            return False

    async def send_printer_error(self, error_code: int, extruder: int = 0) -> bool:
        """
        发送打印机错误状态
        
        Args:
            error_code: 错误码
            extruder: 挤出机编号
            
        Returns:
            bool: 发送是否成功
        """
        if error_code == self.ERROR_NONE:
            self.logger.warning("尝试发送无错误的错误状态，改为发送空闲状态")
            return await self.send_message(self.CMD_PRINTER_IDLE, extruder)
            
        return await self.send_message(self.CMD_PRINTER_ERROR, extruder)
    
    async def send_filament_status_response(self, is_valid: bool, status_bitmap: int) -> bool:
        """
        发送挤出机余料状态响应
        
        Args:
            is_valid (bool): 数据是否有效
            status_bitmap (int): 挤出机状态位图
            
        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            validity_byte = 0x00 if is_valid else 0x01
            data = [self.CMD_PRINTER_FILAMENT_STATUS_RESPONSE, validity_byte, status_bitmap, 0, 0, 0, 0, 0]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送挤出机余料状态响应: ID=0x{self.SEND_ID:03X}, 数据={[hex(x) for x in data]}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送挤出机余料状态响应时失败: {str(e)}")
            return False
    
    async def set_feeder_mapping(self, left_tube_extruder: int, right_tube_extruder: int) -> bool:
        """
        设置料管与挤出机对应关系
        
        Args:
            left_tube_extruder: 左料管对应的挤出机号
            right_tube_extruder: 右料管对应的挤出机号
            
        Returns:
            bool: 设置是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            data = [self.CMD_SET_FEEDER_MAPPING, left_tube_extruder, right_tube_extruder, 0x00, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送料管映射设置: 左料管->挤出机{left_tube_extruder}, 右料管->挤出机{right_tube_extruder}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送料管映射设置时失败: {str(e)}")
            return False
    
    async def query_feeder_mapping(self) -> bool:
        """
        查询料管与挤出机对应关系
        
        Returns:
            bool: 查询是否成功
        """
        return await self.send_message(self.CMD_QUERY_FEEDER_MAPPING)
    
    async def send_feeder_mapping_response(self, left_extruder: int, right_extruder: int, status: int = 0) -> bool:
        """
        发送料管映射响应
        
        Args:
            left_extruder: 左料管对应的挤出机编号
            right_extruder: 右料管对应的挤出机编号
            status: 状态码 (0=成功, 其他=错误)
            
        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            data = [self.CMD_FEEDER_MAPPING_RESPONSE, left_extruder, right_extruder, status, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送料管映射响应: 左料管->挤出机{left_extruder}, 右料管->挤出机{right_extruder}, 状态={status}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送料管映射响应时失败: {str(e)}")
            return False
    
    async def request_rfid_data(self, extruder_id: int) -> bool:
        """
        请求指定挤出机的RFID数据
        
        Args:
            extruder_id: 挤出机编号
            
        Returns:
            bool: 请求是否成功发送
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False
            
        try:
            data = [self.CMD_RFID_RAW_DATA_REQUEST, 0x00, extruder_id, 0x00, 0x00, 0x00, 0x00, 0x00]
            
            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_with_retry(msg):
                self.logger.info(f"已发送RFID数据请求: 挤出机{extruder_id}")
                return True
            else:
                return False
            
        except Exception as e:
            self.logger.error(f"构建或发送RFID数据请求时失败: {str(e)}")
            return False

    async def retract_filament(self, buffer_id: int, distance: int, speed: int, *,
                               context=None, valid=None) -> bool:
        """
        回退耗材命令

        Args:
            buffer_id: 缓冲区编号 (0=LEFT_BUFFER, 1=RIGHT_BUFFER)
            distance: 回退距离档位 (0-7)
                     0=1mm, 1=5mm, 2=10mm, 3=20mm, 4=50mm, 5=100mm, 6=200mm, 7=1000mm
            speed: 回退速度档位 (0-7)
                  0=10mm/s, 1=20mm/s, 2=30mm/s, 3=40mm/s, 4=50mm/s, 5=60mm/s, 6=70mm/s, 7=80mm/s

        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False

        # 参数验证
        if buffer_id not in [0, 1]:
            self.logger.error(f"无效的缓冲区编号: {buffer_id}，必须为0或1")
            return False

        if distance < 0 or distance > 7:
            self.logger.error(f"无效的距离档位: {distance}，必须为0-7")
            return False

        if speed < 0 or speed > 7:
            self.logger.error(f"无效的速度档位: {speed}，必须为0-7")
            return False

        try:
            # 距离映射表（用于日志显示）
            distance_map = {0: 1, 1: 5, 2: 10, 3: 20, 4: 50, 5: 100, 6: 200, 7: 1000}
            # 速度映射表（用于日志显示）
            speed_map = {0: 10, 1: 20, 2: 30, 3: 40, 4: 50, 5: 60, 6: 70, 7: 80}

            data = [self.CMD_RETRACT_FILAMENT, 0x01, buffer_id, distance, speed, 0x00, 0x00, 0x00]

            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_motion(msg, buffer_id, context, valid):
                self.logger.info(f"已发送耗材回退命令: 缓冲区={'左' if buffer_id == 0 else '右'}, "
                               f"距离={distance_map[distance]}mm, 速度={speed_map[speed]}mm/s, "
                               f"数据={[hex(x) for x in data]}")
                return True
            else:
                return False

        except Exception as e:
            self.logger.error(f"构建或发送耗材回退命令时失败: {str(e)}")
            return False

    async def extrude_filament(self, buffer_id: int, distance: int, speed: int, *,
                               context=None, valid=None) -> bool:
        """
        挤出耗材命令

        Args:
            buffer_id: 缓冲区编号 (0=LEFT_BUFFER, 1=RIGHT_BUFFER)
            distance: 挤出距离档位 (0-7)
                     0=1mm, 1=5mm, 2=10mm, 3=20mm, 4=50mm, 5=100mm, 6=200mm, 7=1000mm
            speed: 挤出速度档位 (0-7)
                  0=10mm/s, 1=20mm/s, 2=30mm/s, 3=40mm/s, 4=50mm/s, 5=60mm/s, 6=70mm/s, 7=80mm/s

        Returns:
            bool: 发送是否成功
        """
        if not self.connected or not self.bus:
            self.logger.error("未连接到CAN总线，无法发送消息")
            return False

        # 参数验证
        if buffer_id not in [0, 1]:
            self.logger.error(f"无效的缓冲区编号: {buffer_id}，必须为0或1")
            return False

        if distance < 0 or distance > 7:
            self.logger.error(f"无效的距离档位: {distance}，必须为0-7")
            return False

        if speed < 0 or speed > 7:
            self.logger.error(f"无效的速度档位: {speed}，必须为0-7")
            return False

        try:
            # 距离映射表（用于日志显示）
            distance_map = {0: 1, 1: 5, 2: 10, 3: 20, 4: 50, 5: 100, 6: 200, 7: 1000}
            # 速度映射表（用于日志显示）
            speed_map = {0: 10, 1: 20, 2: 30, 3: 40, 4: 50, 5: 60, 6: 70, 7: 80}

            data = [self.CMD_EXTRUDE_FILAMENT, 0x01, buffer_id, distance, speed, 0x00, 0x00, 0x00]

            msg = can.Message(
                arbitration_id=self.SEND_ID,
                data=data,
                is_extended_id=False
            )

            if await self._send_motion(msg, buffer_id, context, valid):
                self.logger.info(f"已发送耗材挤出命令: 缓冲区={'左' if buffer_id == 0 else '右'}, "
                               f"距离={distance_map[distance]}mm, 速度={speed_map[speed]}mm/s, "
                               f"数据={[hex(x) for x in data]}")
                return True
            else:
                return False

        except Exception as e:
            self.logger.error(f"构建或发送耗材挤出命令时失败: {str(e)}")
            return False