"""企业采集/RPA 客户端：通过 Chromium 与合成实验室门户、档案台页面交互。

职责边界见 docs/collector-design.md：本包只读取合成来源、搬运授权范围内的
正式签发依据，不调用来源演练控制接口，不替实验室签发或批准任何结论。
"""

__all__ = ["browser", "ledger", "engine", "errors"]
