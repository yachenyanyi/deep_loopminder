# Deep Agent 配置类型说明

本文档描述了 `src/deep_agents/deep_agent.py` 中可用的不同代理配置，每种配置针对特定的使用场景进行了优化。

## 代理类型概览

### 1. Basic_Filesystem_Agent - 基础文件系统代理
**用途**: 安全的本地文件操作
- **存储后端**: `FilesystemBackend` 与沙盒模式
- **特点**: 
  - 使用绝对路径 (`os.path.join(os.getcwd(), "workspace")`)
  - 启用 `virtual_mode=True` 限制文件访问范围
  - 适合文档管理、代码编辑、配置文件维护
- **使用场景**: 需要直接操作本地文件系统的任务

### 2. State_Only_Agent - 临时状态代理  
**用途**: 会话级别的临时存储
- **存储后端**: `StateBackend` (内存存储)
- **特点**:
  - 所有文件存储在内存中
  - 会话结束后文件丢失
  - 最佳性能，无磁盘I/O开销
- **使用场景**: 临时数据分析、草稿编写、快速原型开发

### 3. Persistent_Memory_Agent - 持久化存储代理
**用途**: 跨会话的长期记忆
- **存储后端**: `StoreBackend` + `InMemoryStore`
- **特点**:
  - 跨会话持久化存储
  - 使用 LangGraph 的 BaseStore 接口
  - 适合构建知识库和长期记录
- **使用场景**: 个人知识管理、项目跟踪、长期学习记录

### 4. Hybrid_Storage_Agent - 混合存储代理
**用途**: 智能路由不同存储后端
- **存储后端**: `CompositeBackend` 多路径路由
- **特点**:
  - `/tmp/` → StateBackend (临时文件)
  - `/memories/` → StoreBackend (永久保存)  
  - `/workspace/` → FilesystemBackend (本地文件)
  - 智能路径路由，混合存储策略
- **使用场景**: 需要同时处理临时和持久数据的复杂任务

### 5. Analytics_Agent - 高性能分析代理
**用途**: 针对大数据处理优化
- **存储后端**: `StateBackend` (纯内存)
- **特点**:
  - 最大化性能，无磁盘I/O
  - 适合大数据处理和复杂分析
  - 快速数据转换和统计计算
- **使用场景**: CSV处理、JSON分析、日志分析、性能报告

### 6. Enterprise_Agent - 企业级代理
**用途**: 生产环境配置
- **存储后端**: `CompositeBackend` 企业级路由
- **特点**:
  - `/documents/` → 本地文件系统 (企业文档)
  - `/audit/` → StoreBackend (审计日志)
  - `/config/` → StoreBackend (配置管理)
  - 安全、可靠、可审计
- **使用场景**: 企业文档管理、合规性要求、多用户协作

### 7. Intelligent_Deep_Assistant - 智能深度助手
**用途**: 原有的综合代理（保持兼容性）
- **存储后端**: `FilesystemBackend` 基础配置
- **特点**: 向后兼容原有配置

## 使用指南

### 选择合适的代理

```python
from src.deep_agents.deep_agent import get_agent_by_use_case

# 根据使用场景获取代理
agent = get_agent_by_use_case("basic_filesystem")  # 基础文件操作
agent = get_agent_by_use_case("analytics")         # 数据分析
agent = get_agent_by_use_case("persistent_memory")  # 长期记忆
```

### 查看所有可用代理

```python
from src.deep_agents.deep_agent import list_all_agents

agents = list_all_agents()
for name, description in agents.items():
    print(f"{name}: {description}")
```

## 技术细节

### 存储后端对比

| 后端类型 | 持久性 | 性能 | 使用场景 |
|---------|--------|------|----------|
| StateBackend | 会话级别 | 最高 | 临时数据处理 |
| FilesystemBackend | 本地磁盘 | 中等 | 文件系统操作 |
| StoreBackend | 跨会话 | 较高 | 长期记忆存储 |
| CompositeBackend | 混合 | 可变 | 复杂路由需求 |

### 路径路由规则

在混合存储配置中，路径匹配采用最长前缀匹配：
- `/memories/user/preferences.txt` → StoreBackend
- `/tmp/workfile.csv` → StateBackend  
- `/workspace/project/code.py` → FilesystemBackend
- `/other/file.txt` → 默认后端 (StateBackend)

### 安全考虑

1. **FilesystemBackend**: 始终使用 `virtual_mode=True` 进行沙盒化
2. **路径验证**: 所有后端都包含路径安全检查
3. **权限控制**: 企业级代理支持审计日志记录

## 最佳实践

1. **临时任务**: 使用 `State_Only_Agent` 获得最佳性能
2. **文件操作**: 使用 `Basic_Filesystem_Agent` 进行安全的本地文件管理
3. **知识管理**: 使用 `Persistent_Memory_Agent` 构建长期记忆系统
4. **复杂项目**: 使用 `Hybrid_Storage_Agent` 处理混合数据需求
5. **企业应用**: 使用 `Enterprise_Agent` 满足合规和安全要求

## 扩展开发

要添加新的代理配置：

1. 导入必要的后端类型
2. 定义代理配置参数
3. 添加到 `get_agent_by_use_case` 函数
4. 更新 `list_all_agents` 返回信息
5. 在本文档中添加说明

示例：
```python
# 新的代理配置
My_Custom_Agent = create_deep_agent(
    model=default_model,
    tools=[],
    system_prompt="你的系统提示...",
    backend=YourBackendConfiguration(),
    subagents=[...]
)

# 更新选择函数
def get_agent_by_use_case(use_case: str):
    agents = {
        # ... 现有配置
        "my_custom": My_Custom_Agent,  # 添加新配置
    }
    return agents.get(use_case, Intelligent_Deep_Assistant)
```