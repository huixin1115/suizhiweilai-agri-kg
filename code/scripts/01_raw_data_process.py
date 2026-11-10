#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第一步：原始农业数据预处理（修复路径+编码，确保生成processed文件）
功能：读取raw_agri_data.txt，做基础清洗，输出processed文件
"""
import os
import re


def raw_data_process():
    # ===================== 路径配置（绝对路径+容错） =====================
    # 原始数据路径（项目根目录/raw_data/raw_agri_data.txt）
    current_dir = os.path.dirname(os.path.abspath(__file__))  # scripts文件夹路径
    project_root = os.path.dirname(current_dir)  # 项目根目录
    raw_data_path = os.path.join(project_root, "raw_data", "raw_agri_data.txt")
    output_path = os.path.join(project_root, "output", "raw_agri_data_processed.txt")

    # 1. 检查原始文件是否存在（详细提示）
    if not os.path.exists(raw_data_path):
        print(f"[错误] 原始数据文件不存在！")
        print(f"   请确认文件路径：{raw_data_path}")
        print(f"   要求：在项目根目录的raw_data文件夹中放入raw_agri_data.txt（UTF-8编码）")
        return False

    # 2. 确保output文件夹存在
    os.makedirs(os.path.join(project_root, "output"), exist_ok=True)

    # 3. 读取原始数据（强制UTF-8编码，忽略错误）
    try:
        with open(raw_data_path, 'r', encoding='utf-8', errors='ignore') as f:
            raw_data = f.read()
    except Exception as e:
        print(f"[错误] 读取原始文件失败：{str(e)}")
        print("   请确保文件编码为UTF-8（可右键文件→记事本→另存为→编码选UTF-8）")
        return False

    # 4. 基础清洗（保留中文、数字、常用标点，去除无关字符）
    # 清洗规则：只保留中文、数字、%、℃、+、-、（）、，。；：
    cleaned_data = re.sub(r'[^\u4e00-\u9fa50-9%℃\+\-\(\)，。；：]', '', raw_data)
    # 按行分割，去除空行和重复行
    lines = [line.strip() for line in cleaned_data.split('\n') if line.strip()]
    unique_lines = list(dict.fromkeys(lines))  # 去重（保留顺序）

    # 5. 保存预处理后的数据
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(unique_lines))
        print(f"[成功] 第一步：原始数据预处理完成")
        print(f"   - 输入文件：{raw_data_path}")
        print(f"   - 输出文件：{output_path}")
        print(f"   - 有效文本行数：{len(unique_lines)}")
        return True
    except Exception as e:
        print(f"[错误] 保存预处理文件失败：{str(e)}")
        print("   请检查output文件夹是否有写入权限（以管理员身份运行）")
        return False


if __name__ == "__main__":
    raw_data_process()