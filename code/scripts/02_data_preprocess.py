#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第二步：农业文本深度预处理（修复路径，确保生成clean文件）
功能：对第一步的processed文件做分词/去噪，输出clean_agri_data.txt
"""
import os
import re


def data_preprocess():
    # ===================== 路径配置（绝对路径） =====================
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    input_path = os.path.join(project_root, "output", "raw_agri_data_processed.txt")
    output_path = os.path.join(project_root, "output", "clean_agri_data.txt")

    # 1. 检查第一步输出文件是否存在
    if not os.path.exists(input_path):
        print(f"[错误] 未找到第一步预处理文件：{input_path}")
        print("   请先执行第一步脚本 01_raw_data_process.py！")
        return False

    # 2. 读取第一步预处理后的数据
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()]

    # 3. 深度清洗规则（针对农业文本优化）
    cleaned_lines = []
    for line in lines:
        # 去除多余空格、重复标点
        line = re.sub(r'\s+', '', line)  # 去除所有空格
        line = re.sub(r'，+', '，', line)  # 多个逗号→一个
        line = re.sub(r'。+', '。', line)  # 多个句号→一个
        # 去除过短的行（少于10个字符，无有效信息）
        if len(line) < 10:
            continue
        # 去除纯数字/纯标点行
        if re.match(r'^[0-9，。；：]+$', line):
            continue
        cleaned_lines.append(line)

    # 4. 保存深度清洗后的数据
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(cleaned_lines))
        print(f"[成功] 第二步：文本深度预处理完成")
        print(f"   - 输入行数：{len(lines)}")
        print(f"   - 输出行数：{len(cleaned_lines)}")
        print(f"   - 输出文件：{output_path}")
        return True
    except Exception as e:
        print(f"[错误] 保存清洗文件失败：{str(e)}")
        return False


if __name__ == "__main__":
    data_preprocess()