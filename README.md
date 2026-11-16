# AgentOS Next

面向大量 Agent 并发运行的执行系统研究。后续代码、实验与文档在此仓库维护。

当前整理的是 [Agent trace 录制工具](motivation/trace/README.md)：使用 Claude Code + DeepSeek 录制模型交互，真实执行工具，并记录资源使用；之后可以离线重放模型响应，重新执行真实工具。

```text
motivation/trace/
  common/        录制、回放、校验、资源观测
  workloads/     SWE-rebench、BigCodeBench、HotpotQA 适配器与工具
  tools/         本机环境检查
  requirements/  负载环境的依赖快照
  tests/         不调用模型 API 的回归测试
  trace.py       统一命令入口
```

从仓库根目录执行：

```bash
python motivation/trace/trace.py --help
python motivation/trace/trace.py check
python motivation/trace/run_tests.py
```

本次只迁入录制相关代码与说明。历史调度实现、实验结果、原始 trace、数据集和虚拟环境不随代码上传；原工作区中的文件保留。
