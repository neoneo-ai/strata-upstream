"""neo/tests - M1 的测试。

test_supervisor 用 serve/server.py --engine mock 起真实进程，跑完整条"启动→就绪→被杀→
自动重启→熔断"的链路；不需要 GPU，也不需要装好的模型。
"""
