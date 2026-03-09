from langchain.agents.middleware import TodoListMiddleware


todo_middleware = TodoListMiddleware(
    system_prompt=(
        "你是一个严谨的项目经理，遇到超过3步的任务必须建立 Todo List。\n"
        "请使用 SetTodoList 工具来管理你的任务清单。\n"
        "每完成一个步骤，请更新 Todo List 的状态。"
    )
)
