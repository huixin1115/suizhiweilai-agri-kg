#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第六步：生成最终data.txt（微调版）
功能：对齐实体表，合理补充数据，避免无意义重复
"""
import pandas as pd
import os
import sys
import io

# 强制UTF-8输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def generate_final_data():
    # ===================== 路径校验 =====================
    input_path = "../output/clean_triplets.csv"
    output_path = "../output/data.txt"
    target_count = 1000  # 目标条数

    if not os.path.exists(input_path):
        print("[错误] 未找到输入文件 {}".format(input_path))
        print("   请先执行第五步脚本05_triplet_clean.py（优化版）")
        sys.exit(1)
    os.makedirs("../output", exist_ok=True)

    # ===================== 生成最终文件（优化版：合理补充） =====================
    try:
        # 1. 读取清洗后的三元组
        df = pd.read_csv(input_path, encoding='utf-8')
        clean_count = len(df)
        print("[信息] 读取到清洗后三元组 {} 条（对齐实体定义表）".format(clean_count))

        # 2. 格式标准化
        df = df[['实体1', '关系', '实体2']]  # 固定列序
        for col in df.columns:
            df[col] = df[col].str.strip()

        # 3. 合理补充数据（仅当有效条数<500时补充，按关系均匀重复）
        if clean_count < target_count and clean_count < 500:
            print("[信息] 有效三元组 {} 条，按关系均匀补充至 {} 条...".format(clean_count, target_count))
            # 按关系分组，分别重复，保证各关系占比均匀
            df_extended = []
            for rel, group in df.groupby('关系'):
                group_repeat = group.sample(n=int(target_count / len(df['关系'].unique())), replace=True,
                                            random_state=42)
                df_extended.append(group_repeat)
            df = pd.concat(df_extended, ignore_index=True).head(target_count)
        # 若有效条数≥500，直接保留原数据（无需无意义重复）
        elif clean_count < target_count:
            print("[信息] 有效三元组 {} 条，直接保留原数据（无需重复）".format(clean_count))

        # 4. 最终去重（避免补充时的重复）
        df = df.drop_duplicates(subset=['实体1', '关系', '实体2'], keep='first').head(target_count)

        # 5. 保存为最终的data.txt
        df.to_csv(output_path, index=False, encoding='utf-8', sep=',')
        final_count = len(df)

        # 输出结果
        print("[成功] 第六步：最终data.txt生成完成（对齐实体表）")
        print("   - 最终三元组条数：{} 条".format(final_count))
        print("   - 输出文件：{}".format(output_path))
        print("\n[信息] 最终数据各关系分布：")
        rel_count = df['关系'].value_counts()
        for rel, cnt in rel_count.items():
            print("   - {}：{} 条（{:.2f}%）".format(rel, cnt, cnt / final_count * 100))
        # 打印前10条示例
        print("\n[信息] 前10条示例数据：")
        print(df.head(10).to_string(index=False))

    except Exception as e:
        print("[错误] 生成最终文件出错：{}".format(str(e)))
        sys.exit(1)


if __name__ == "__main__":
    generate_final_data()