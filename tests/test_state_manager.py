import pytest
import asyncio
from unittest.mock import Mock, MagicMock
from feeder_cabinet.state_manager import StateManager, SystemStateEnum

@pytest.fixture
def mock_logger():
    """创建一个模拟的logger对象"""
    return MagicMock()

@pytest.mark.asyncio
async def test_initial_state(mock_logger):
    """测试状态管理器的初始状态"""
    sm = StateManager(mock_logger)
    assert sm.state == SystemStateEnum.STARTING

@pytest.mark.asyncio
async def test_state_transition(mock_logger):
    """测试基本的状态转换"""
    sm = StateManager(mock_logger)
    sm.transition_to(SystemStateEnum.IDLE)
    assert sm.state == SystemStateEnum.IDLE
    mock_logger.info.assert_called_with("系统状态转换: STARTING -> IDLE")

@pytest.mark.asyncio
async def test_state_transition_with_payload(mock_logger):
    """测试带附加信息的状态转换"""
    sm = StateManager(mock_logger)
    payload = {"extruder": 1, "reason": "runout"}
    sm.transition_to(SystemStateEnum.ERROR, **payload)
    assert sm.state == SystemStateEnum.ERROR
    assert sm.get_payload() == payload

@pytest.mark.asyncio
async def test_no_transition_to_same_state(mock_logger):
    """测试转换到相同状态时不会触发日志记录"""
    sm = StateManager(mock_logger)
    sm.transition_to(SystemStateEnum.IDLE)
    mock_logger.info.reset_mock() # 重置模拟对象
    
    sm.transition_to(SystemStateEnum.IDLE)
    mock_logger.info.assert_not_called()

@pytest.mark.asyncio
async def test_state_change_callback(mock_logger):
    """测试状态转换回调函数是否被正确调用"""
    sm = StateManager(mock_logger)
    
    callback_mock = Mock()
    
    def state_change_callback(old_state, new_state, payload):
        callback_mock(old_state, new_state, payload)

    sm.set_state_change_callback(state_change_callback)
    
    payload = {"test": "data"}
    sm.transition_to(SystemStateEnum.PRINTING, **payload)
    
    callback_mock.assert_called_once_with(SystemStateEnum.STARTING, SystemStateEnum.PRINTING, payload)

@pytest.mark.asyncio
async def test_state_change_callback_exception_handling(mock_logger):
    """测试当回调函数抛出异常时，错误能被正确记录"""
    sm = StateManager(mock_logger)
    
    error_message = "Callback Error"
    
    def faulty_callback(old_state, new_state, payload):
        raise ValueError(error_message)
        
    sm.set_state_change_callback(faulty_callback)
    
    sm.transition_to(SystemStateEnum.IDLE)
    
    # 验证错误日志是否被调用
    mock_logger.error.assert_called_once()
    # 验证错误信息是否包含我们预期的内容
    args, kwargs = mock_logger.error.call_args
    assert "执行状态转换回调时出错" in args[0]
    assert "exc_info=True" in str(kwargs)

@pytest.mark.asyncio
async def test_is_state_method(mock_logger):
    """测试 is_state 方法"""
    sm = StateManager(mock_logger)
    assert sm.is_state(SystemStateEnum.STARTING)
    assert not sm.is_state(SystemStateEnum.IDLE)
    
    sm.transition_to(SystemStateEnum.IDLE)
    assert sm.is_state(SystemStateEnum.IDLE)
    assert not sm.is_state(SystemStateEnum.STARTING) 