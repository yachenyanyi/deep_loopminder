# SkillsBench 单题接入与实测

2026-10-20 用户授权：在适当目录安装公开测试集，并选一道题测试PM系统。

范围：固定SkillsBench v1.1，安装在workspace/benchmarks/skillsbench（忽略的第三方运行目录）；保持生产代码不变。优先办公类任务，真实PM模型使用共享MiniMax M3.1 Flash free/32k；实际执行能力经现有ACP工具交给Claude。原题、输入与验证器保留；oracle和验证器不暴露给Agent。

验收：记录上游commit、任务、环境、模型、真实工具轨迹、交付物和原版验证结果。区分标准评测与任何平台适配，不把单题得分当总体完成率，不把oracle运行当Agent成绩。只运行一题，失败如实报告，不重复抽题刷分。

当前：仓库下载中。Docker CLI可用但引擎未运行，已启动现有Docker Desktop；检查官方运行器与Linux任务环境。

已完成：上游v1.1固定commit b63b7b2850226b6aa4fb5929a8c1ac7bc4d9a6af；Docker引擎28.3.2已启动。官方BenchFlow 0.6.9已安装；Windows入口因依赖Unix fcntl不能启动，转为WSL Ubuntu运行官方工具，不修改第三方源码。候选citation-check：原题中等难度、输入BibTeX、输出JSON、原版pytest评分；参考答案与评分器仅宿主可见，不进入PM/Worker消息。

测试实现：tests/run_pm_skillsbench.py复用生产PM工厂与官方Server/ACP六工具，Claude执行在仅含原始输入的独立容器内。使用no-skill条件；没有将上游题目/评分标准改为自造任务。当前准备Linux基础镜像和原生ACP运行时，尚未调用PM或评分。WindowsBenchFlow fcntl失败保留为平台限制；WSL官方CLI安装进行中。

基础设施中断：Docker/WSL会话在并行安装时重启，原始环境镜像与ACP扩展构建均EOF，WSL uv安装同终止；未产生模型Run。引擎自动恢复为新SessionID，改为串行构建并保留citation-build.log。不是题目失败或PM失败。

串行重建进一步发现现有Docker镜像加速器返回401，使用显式registry-1.docker.io官方源取得同版Ubuntu并本地标记，不修改全局Docker配置；继续构建。测试桥接仅将Windows宿主ACP cwd映射到容器/root，协议与工具调用原样转发；评分标为自定义PM接入试跑。

用户提供本机7897代理，后续下载/模型调用仅本次进程采用该代理；容器使用host.docker.internal映射，localhost服务走NO_PROXY。已出现Windows分页空间不足错误，停止本次WSL uv安装进程，避免与镜像构建同时占用资源；未改系统内存或全局代理配置。

平台适配决定：Docker在镜像导出阶段再次停止，未进入模型测试；改为Windows单题适配。原始test.bib逐字复制到独立work目录；原题只替换两个/root文件路径；真实PM与原有ClaudeACP/官方Server在宿主执行；原版pytest测试文件不改，仅在收集时把ANSWER_FILE常量映射到交付路径。无硬OS沙箱，不作为标准Linux榜单结果。只运行本题，不换题刷分。测试进程已启动，等待Server就绪。

真实执行已开始：PM线程01a156fd-137a-7567-9c80-4111309e0ff5，Run 01a156fd-137d-7119-91ba-b140f4ec42c0；共享MiniMax模型API正常，实际创建项目/TaskBrief/任务并依次ready、assigned、in_progress。工作目录workspace/benchmarks/runs/citation-aeac3ff420/work，仅复制原始test.bib与本机执行权限配置。仍待实际派工、交付与验收，不提前记通过。

已验证派工阶段：PM真实spawn_agent后wait_agent；原生Claude工具开始读取BibTeX并运行Crossref/DBLP/OpenAlex等查询，未只凭口头报告模拟执行。PM trace实际返回模型为minimax/minimax-m3.1-flash:free。答案尚未生成，仍执行中。官方WindowsBenchFlow不可用入口已卸载，数据集与适配入口保留。

核对输入为26条BibTeX引用。运行未注入SkillsBench题目技能，但本机Claude仍继承用户现有配置，不能宣称严格无技能隔离；最终证据应标注该条件。执行期间只做取证，不修改题目、不提供答案、不重复发起任务。

已完成Worker交付阶段：work/answer.json已生成；本次原生session 8aa859bb-857f-4e73-a80b-a704013458a5的assistant记录实际模型为minimax/minimax-m3.1-flash:free，非仅配置自报。等待PM领取和原版pytest验收；尚不记录正确性。

实际测试已完成：PM正常结束，成功领取Worker结果、读取制品并停在dev_complete；未伪造passed/approved/done。answer.json生成、原始输入hash未变。原版pytest断言9/9通过，原始reward=1。Server已停止且端口关闭，上游工作树无改动。

重要复核：本次原生WebSearch在2026-10-20T01:49:44.745Z的工具返回中实际引用了上游oracle/detect_fakes.py内容并提及期望输出；Worker虽声称未打开源文件、此前已独立判断，不能排除已接触答案的影响。因此最终status=completed_with_contamination，valid_for_success_rate=false，不作为有效完成率或榜单成绩。原始答案与9项通过记录不改写；不重复本题刷分。

Checkpoint：SkillsBench v1.1安装在workspace/benchmarks/skillsbench，固定commit已记录；完成citation-check一道题的真实PM→Claude→交付→原版断言Windows适配试跑。PM和Worker实际模型均MiniMax M3.1 Flash free/32k。证据docs/progress/evidence/skillsbench-citation-20261020.json，运行制品workspace/benchmarks/runs/citation-aeac3ff420。Docker/WSL/Windows分页问题导致标准Linux环境未完成；已卸载不可用的WindowsBenchFlow入口并停止本次WSL uv安装，无运行中的测试Server；未改生产源码或全局代理。后续应先限制检索到基准题解，再开展新隔离样本的有效评测；本轮不再调用模型。无提交推送。

## PM工作行为只读复核（2026-10-20）

用户要求检查PM如何分析与委派。父checkpoint实际21条AI消息、21次工具调用，其中spawn1次、wait9次；共享模型回调文件有23条响应，包含后台记忆Agent调用，不能全算PM。已修正证据统计字段，保留原始日志。执行顺序：读上下文/快照→创建项目/TaskBrief/任务→ready/assigned/in_progress→developer派工→wait领取→读取制品→dev_complete→提交后台记忆。一次等待目标拼错后收到unknown_target并自行修正，未重复派工。

分析边界：测试宿主已指定任务ID、角色、能力、状态及派工顺序，不能当作自主需求拆解或中心决策已验证。PM自主展开核验要求与自检，但附加了非空答案、无法证实/元数据错配归为fake等标准，存在越过需求与UNKNOWN误判风险。PM的message与中间件注入TaskBrief重复。最终验收边界遵守，但未独立识别搜索已泄漏oracle；后台记忆提交摘要沿用了未验收的Worker结论，整理完成状态本轮未验证。

Checkpoint：行为复核完成，runtime制品pm-behavior-review.json记录依据与发现；没有再次调用模型或修改生产代码。当前能证明按给定流程委派并管理状态，不能证明自主复杂规划或独立验收可靠。
