"""
协作智能体员工注册表

定义所有协作智能体的 Employee 配置，用于 AgentCommunicationMiddleware。
"""

from src.middlewares.agent_communication import Employee


COLLABORATIVE_EMPLOYEES: list[Employee] = [
    Employee(
        name="chat_agent",
        role="前台接待",
        expertise=["闲聊", "意图识别", "简单任务", "文本生成"],
        description="系统的第一接触点，负责识别用户意图并路由到正确的专家。擅长处理日常对话、简单问答和情感陪伴。"
    ),
    Employee(
        name="coordinator_agent",
        role="项目经理",
        expertise=["任务拆解", "进度追踪", "结果合成", "冲突仲裁"],
        description="协调多个专家代理完成复杂任务。负责将大目标分解为可执行的原子任务，并整合各专家的输出。"
    ),
    Employee(
        name="coder_agent",
        role="高级工程师",
        expertise=["代码编写", "调试", "构建执行", "依赖管理"],
        description="执行所有代码相关的任务。可以编写、修改代码，运行构建和测试命令。"
    ),
    Employee(
        name="researcher_agent",
        role="情报分析师",
        expertise=["网络搜索", "文档查阅", "数据整理", "信息验证"],
        description="收集和验证信息。擅长网络搜索、文档查阅和数据分析。"
    ),
    Employee(
        name="assistant_agent",
        role="私人秘书",
        expertise=["日程管理", "记忆存储", "提醒设置", "个人信息管理"],
        description="管理用户的个人信息和偏好。维护用户画像、待办事项和重要日期。"
    ),
]


def get_employee_by_name(name: str) -> Employee | None:
    """根据名称获取员工配置"""
    for employee in COLLABORATIVE_EMPLOYEES:
        if employee["name"] == name:
            return employee
    return None


def get_all_employee_names() -> list[str]:
    """获取所有员工名称列表"""
    return [e["name"] for e in COLLABORATIVE_EMPLOYEES]