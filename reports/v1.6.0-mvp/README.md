# DADC 1.6.0.dev0 最小可扩展闭环验收

2026-08-21 在 Linux/Python 3.12 环境完成离线垂直切片和完整回归。垂直切片从可复现 PyAEDT 文档夹具开始，经过语义章节、可重建检索、类型化调用、预算化搜索、故意失败调用保留、最优点独立复核和优化证据包，最后形成可验证、可追溯的 DADC warehouse。

完整回归为 63/63 通过。初始基线中的两项失败来自 Git 在 Linux 上把原始 HFSS Touchstone 的 CRLF 转为 LF；现在通过 `.gitattributes` 将 Touchstone 固定为原始二进制字节，恢复测试原本钉住的 SHA-256。

本次没有在 Linux 主机上运行 AEDT/HFSS。真实 `pyaedt_patch` 后端、固定参数白名单、命令构造、全带 S1P 复算和入库路径已经实现；数值物理验收必须转到 Windows + AEDT/PyAEDT 主机继续执行，不能用解析夹具结果替代。
