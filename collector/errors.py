"""采集客户端错误类型。

临时故障（网络断开、超时、下载失败）与正式状态（撤签、预览）严格分开：
前者允许稍后重试，绝不能被解释成报告被撤签。
"""


class CollectorError(Exception):
    """采集客户端基础错误。"""


class TransientBrowserError(CollectorError):
    """页面暂时不可达、超时或下载中断；属于可重试故障，不代表签发状态变化。"""


class StructuralBrowserError(CollectorError):
    """页面结构与契约不符、关键元素缺失或内容无法解析，需人工介入。"""


class LedgerError(CollectorError):
    """本地档案台账状态不合法，例如未初始化就移交、原件路径缺失。"""
