"""Agent Deck 随包分发的静态视觉资产。

本包只放运行时需要读取的只读 PNG 等资源，例如 N4 Pro 默认 splash 和品牌 logo。
资源由渲染层通过 `importlib.resources` 读取；本包自身不执行文件 I/O 或硬件动作。
"""
