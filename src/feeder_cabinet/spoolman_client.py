"""
Spoolman API客户端模块 - 用于与Spoolman进行耗材管理

此模块提供与Spoolman API的集成，包括：
- 创建/更新供应商信息
- 创建/更新耗材类型
- 创建/更新耗材卷轴
- 从RFID数据同步到Spoolman
"""

import logging
import asyncio
import aiohttp
from typing import Dict, Any, Optional, List
from datetime import datetime
from urllib.parse import urljoin

from .rfid_parser import OpenTagFilamentData

logger = logging.getLogger(__name__)


class SpoolmanClient:
    """Spoolman API客户端"""
    
    def __init__(self, base_url: str):
        """
        初始化Spoolman客户端
        
        Args:
            base_url: Spoolman API基础URL (例如: http://localhost:7912)
        """
        self.base_url = base_url.rstrip('/')
        self.api_url = f"{self.base_url}/api/v1"
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def __aenter__(self):
        """进入上下文管理器"""
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出上下文管理器"""
        if self.session:
            await self.session.close()
            
    async def _request(self, method: str, endpoint: str, json: Optional[Dict] = None) -> Dict:
        """
        发送HTTP请求
        
        Args:
            method: HTTP方法
            endpoint: API端点
            json: 请求体数据
            
        Returns:
            响应数据
        """
        if not self.session:
            self.session = aiohttp.ClientSession()
            
        url = urljoin(self.api_url + '/', endpoint.lstrip('/'))
        
        try:
            async with self.session.request(method, url, json=json) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"Spoolman API请求失败: {method} {url}, 错误: {e}")
            raise
            
    async def get_vendors(self) -> List[Dict]:
        """获取所有供应商"""
        return await self._request('GET', 'vendor')
        
    async def get_vendor_by_name(self, name: str) -> Optional[Dict]:
        """根据名称获取供应商"""
        vendors = await self.get_vendors()
        for vendor in vendors:
            if vendor.get('name') == name:
                return vendor
        return None
        
    async def create_vendor(self, name: str) -> Dict:
        """
        创建供应商
        
        Args:
            name: 供应商名称
            
        Returns:
            创建的供应商信息
        """
        data = {
            'name': name,
            'comment': f'自动从RFID创建于 {datetime.now().isoformat()}'
        }
        return await self._request('POST', 'vendor', json=data)
        
    async def get_or_create_vendor(self, name: str) -> Dict:
        """获取或创建供应商"""
        vendor = await self.get_vendor_by_name(name)
        if vendor:
            logger.info(f"找到现有供应商: {name} (ID: {vendor['id']})")
            return vendor
        else:
            logger.info(f"创建新供应商: {name}")
            return await self.create_vendor(name)
            
    async def get_filaments(self, vendor_id: Optional[int] = None) -> List[Dict]:
        """获取耗材列表"""
        params = {}
        if vendor_id:
            params['vendor_id'] = vendor_id
        # 简化实现，实际应该添加查询参数
        return await self._request('GET', 'filament')
        
    async def create_filament(self, rfid_data: OpenTagFilamentData, vendor_id: int) -> Dict:
        """
        从RFID数据创建耗材类型
        
        Args:
            rfid_data: RFID解析的耗材数据
            vendor_id: 供应商ID
            
        Returns:
            创建的耗材信息
        """
        data = {
            'name': f"{rfid_data.material_name} - {rfid_data.color_name}",
            'vendor_id': vendor_id,
            'material': rfid_data.material_name,
            'color_hex': f"{rfid_data.color_hex:06X}" if rfid_data.color_hex else None,
            'diameter': rfid_data.diameter_target / 1000.0,  # 转换为mm
            'density': rfid_data.density / 1000.0,  # 转换为g/cm³
            'weight': rfid_data.weight_nominal,
            'settings_extruder_temp': rfid_data.print_temp,
            'settings_bed_temp': rfid_data.bed_temp,
            'comment': f"RFID导入: {rfid_data.serial_number or '无序列号'}"
        }
        
        # 移除None值
        data = {k: v for k, v in data.items() if v is not None}
        
        return await self._request('POST', 'filament', json=data)
        
    async def create_spool(self, rfid_data: OpenTagFilamentData, filament_id: int) -> Dict:
        """
        从RFID数据创建耗材卷轴
        
        Args:
            rfid_data: RFID解析的耗材数据
            filament_id: 耗材类型ID
            
        Returns:
            创建的卷轴信息
        """
        # 计算剩余重量
        remaining_weight = None
        if rfid_data.filament_weight_measured and rfid_data.filament_weight_measured != 0xFFFF:
            remaining_weight = rfid_data.filament_weight_measured
        elif rfid_data.weight_nominal:
            remaining_weight = rfid_data.weight_nominal
            
        data = {
            'filament_id': filament_id,
            'remaining_weight': remaining_weight,
            'location': rfid_data.serial_number,
            'lot_nr': rfid_data.serial_number,
            'comment': f"RFID自动导入于 {datetime.now().isoformat()}"
        }
        
        # 如果有制造日期
        if rfid_data.manufacture_date:
            data['registered'] = rfid_data.manufacture_date.isoformat()
            
        return await self._request('POST', 'spool', json=data)
        
    async def sync_rfid_to_spoolman(self, rfid_data: OpenTagFilamentData) -> Dict:
        """
        将RFID数据同步到Spoolman
        
        Args:
            rfid_data: RFID解析的耗材数据
            
        Returns:
            包含vendor_id, filament_id, spool_id的字典
        """
        try:
            # 1. 获取或创建供应商
            vendor = await self.get_or_create_vendor(rfid_data.manufacturer)
            vendor_id = vendor['id']
            
            # 2. 查找或创建耗材类型
            filaments = await self.get_filaments(vendor_id)
            filament = None
            
            # 查找匹配的耗材
            for f in filaments:
                if (f.get('material') == rfid_data.material_name and 
                    f.get('name', '').find(rfid_data.color_name) != -1):
                    filament = f
                    logger.info(f"找到现有耗材: {f['name']} (ID: {f['id']})")
                    break
                    
            if not filament:
                logger.info(f"创建新耗材: {rfid_data.material_name} - {rfid_data.color_name}")
                filament = await self.create_filament(rfid_data, vendor_id)
                
            filament_id = filament['id']
            
            # 3. 创建卷轴
            logger.info(f"创建新卷轴，序列号: {rfid_data.serial_number}")
            spool = await self.create_spool(rfid_data, filament_id)
            spool_id = spool['id']
            
            logger.info(f"成功同步到Spoolman: 供应商ID={vendor_id}, 耗材ID={filament_id}, 卷轴ID={spool_id}")
            
            return {
                'vendor_id': vendor_id,
                'filament_id': filament_id,
                'spool_id': spool_id,
                'vendor_name': vendor['name'],
                'filament_name': filament['name'],
                'serial_number': rfid_data.serial_number
            }
            
        except Exception as e:
            logger.error(f"同步到Spoolman失败: {e}", exc_info=True)
            raise
            
    async def get_spool(self, spool_id: int) -> Dict:
        """获取卷轴信息"""
        return await self._request('GET', f'spool/{spool_id}')
        
    async def use_filament(self, spool_id: int, length: Optional[float] = None, 
                          weight: Optional[float] = None) -> Dict:
        """
        使用耗材（减少卷轴剩余量）
        
        Args:
            spool_id: 卷轴ID
            length: 使用长度(mm)
            weight: 使用重量(g)
            
        Returns:
            更新后的卷轴信息
        """
        data = {}
        if length is not None:
            data['length'] = length
        elif weight is not None:
            data['weight'] = weight
        else:
            raise ValueError("必须指定length或weight之一")
            
        return await self._request('PUT', f'spool/{spool_id}/use', json=data)


# 使用示例
if __name__ == "__main__":
    import asyncio
    
    async def test_spoolman():
        """测试Spoolman客户端"""
        # 创建测试数据
        test_data = OpenTagFilamentData()
        test_data.manufacturer = "MINGDA 3D"
        test_data.material_name = "PLA-HS"
        test_data.color_name = "White"
        test_data.diameter_target = 1750
        test_data.weight_nominal = 1000
        test_data.density = 1240
        test_data.print_temp = 210
        test_data.bed_temp = 60
        test_data.serial_number = "TEST-001"
        
        # 测试同步
        async with SpoolmanClient("http://localhost:7912") as client:
            try:
                result = await client.sync_rfid_to_spoolman(test_data)
                print(f"同步成功: {result}")
            except Exception as e:
                print(f"同步失败: {e}")
                
    # 运行测试
    asyncio.run(test_spoolman())