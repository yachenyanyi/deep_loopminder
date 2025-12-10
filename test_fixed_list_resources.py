#!/usr/bin/env python3
"""
测试修复后的 list_resources 函数
"""

import sys
import os

# 将项目根目录添加到 Python 路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# 导入要测试的函数
from src.tools.api_tools import list_resources

def test_list_resources():
    """测试 list_resources 函数"""
    print("🧪 测试 list_resources 函数")
    print("=" * 50)
    
    # 测试 1: 基本调用
    print("测试 1: 基本调用")
    try:
        result = list_resources()
        if "error" in result:
            print(f"❌ 错误: {result['error']}")
        else:
            print(f"✅ 成功获取 {result['total']} 个工具")
            if result['results']:
                print("前几个工具:")
                for tool in result['results'][:3]:
                    print(f"  - {tool['name']}: {tool['description'][:60]}...")
    except Exception as e:
        print(f"❌ 异常: {e}")
    
    # 测试 2: 带查询参数
    print("\n测试 2: 带查询参数")
    try:
        result = list_resources(query="search")
        if "error" in result:
            print(f"❌ 错误: {result['error']}")
        else:
            print(f"✅ 搜索 'search' 找到 {result['total']} 个工具")
    except Exception as e:
        print(f"❌ 异常: {e}")
    
    # 测试 3: 分页
    print("\n测试 3: 分页测试")
    try:
        result = list_resources(page=1, page_size=2)
        if "error" in result:
            print(f"❌ 错误: {result['error']}")
        else:
            print(f"✅ 第1页，每页2个: 显示 {len(result['results'])} 个工具")
            print(f"   总页数: {result['total_pages']}, 总工具数: {result['total']}")
    except Exception as e:
        print(f"❌ 异常: {e}")

if __name__ == "__main__":
    test_list_resources()