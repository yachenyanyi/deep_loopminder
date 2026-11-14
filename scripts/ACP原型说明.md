# ACP 会话和 Windows Job 验证

PM 的开发 Worker 由 `src/middlewares/acp/job.py` 监管：先挂起启动并分配 Job，再恢复适配器。Worker 正常关闭后，Job 内活动进程数归零时写入对应执行身份的 `stopped_verified`。PM 只根据匹配记录收敛项目隔离；状态缺失或未知时保留阻断。

在项目根目录运行 `python scripts/validate_acp_job_prototype.py -v` 可验证标准输入输出、后代进程清理、启动失败、启动器异常退出后的核对，以及无法核验时不伪造停止确认。需要 Windows 和项目的 pywin32 依赖。

运行 `python -m scripts.acp_session_probe --provider claude` 或 `--provider codex` 会读取本机 `settings.json` 的 Worker 配置，使用临时工作目录请求真实模型并产生用量。增加 `--job` 可通过 Job 启动器测试协议通信。输出只包含能力和验证结果，不输出模型回答或凭据。

启动器仅允许目标继承标准输入输出句柄。现有 ACP SDK 对受监管 Worker 的关闭等待延长为 15 秒，以留出 Job 清理时间；超时、状态缺失或身份不匹配均不是已确认停止。

每个启动器启动目标前会建立并确认一个持有相同 Job 句柄的独立核验进程。启动器异常退出时，核验进程在确认执行身份和目标已分配后，终止 Job 并等待活动进程数为零，才写入停止证明；项目状态查询和 PM 写操作可依据该证明收敛隔离。进程创建到分配 Job 的窗口仍可能留下挂起进程；核验进程本身也可能异常退出。遇到这些无法确认的情况必须保留隔离，不自动声称恢复成功。状态文件与项目 Store 不是一个原子事务；原生 Session 可以恢复也不代表中断的官方 Run 自动继续。
