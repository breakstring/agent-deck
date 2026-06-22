"""Agent Deck 硬件输入归一路由包。

本包存放从 fake hardware、StreamDock SDK event 等底层输入到业务 intent/event 的纯映射逻辑。
模块不访问真实硬件、不启动线程、不写文件；真实监听与执行动作由调用方负责。
"""
