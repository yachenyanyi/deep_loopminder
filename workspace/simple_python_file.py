#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一个简单的Python文件，包含：
1. Hello World打印语句
2. 计算两个数字相加的函数
3. 判断数字是否为偶数的函数
4. 函数调用和结果打印
"""

# 1. Hello World打印语句
print("Hello World!")

# 2. 计算两个数字相加的函数
def add_numbers(a, b):
    """
    计算两个数字的和
    
    参数:
    a (int/float): 第一个数字
    b (int/float): 第二个数字
    
    返回:
    int/float: 两个数字的和
    """
    return a + b

# 3. 判断数字是否为偶数的函数
def is_even(number):
    """
    判断一个数字是否为偶数
    
    参数:
    number (int): 要检查的数字
    
    返回:
    bool: 如果是偶数返回True，否则返回False
    """
    if number % 2 == 0:
        return True
    else:
        return False

# 4. 在文件末尾调用这些函数并打印结果
if __name__ == "__main__":
    print("\n=== 函数调用结果 ===")
    
    # 测试加法函数
    num1 = 10
    num2 = 25
    sum_result = add_numbers(num1, num2)
    print(f"{num1} + {num2} = {sum_result}")
    
    # 测试判断偶数函数
    test_numbers = [3, 8, 15, 22, 31]
    print("\n判断数字是否为偶数:")
    for num in test_numbers:
        if is_even(num):
            print(f"  {num} 是偶数")
        else:
            print(f"  {num} 是奇数")