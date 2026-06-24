"""Agent Deck 外部动作执行器包。

本包中的模块负责把已经归一化、经过 runtime 安全检查的 action intent 转换成系统副作用。
导入本包不执行任何动作；真实副作用只发生在调用具体 executor 函数时。
"""
