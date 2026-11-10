#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第五步：三元组清洗去重（优化版）
功能：放宽过滤规则，避免误删有效三元组，仅做必要清洗
"""
import pandas as pd
import os
import sys
import io

# 强制UTF-8输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
# 修正导入：从entity_list导入（去掉开头的00）
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from entity_list import *

# 合并所有有效实体，用于过滤
ALL_VALID_ENTITIES = CROPS + DISEASES_INSECTS + RESOURCES + ENVIRONMENTS + CULTIVATIONS + REGIONS
# 所有有效关系（对齐用户定义）
ALL_VALID_RELATIONS = ["易感病害", "易感虫害", "防治方法", "危害部位", "适配肥料", "适宜气候", "适宜土壤", "栽培要点",
                       "主产区"]


def clean_triplets():
    # ===================== 路径校验 =====================
    input_path = "../output/temp_triplets.csv"
    output_path = "../output/clean_triplets.csv"

    if not os.path.exists(input_path):
        print("[错误] 未找到输入文件 {}".format(input_path))
        print("   请先执行第四步脚本04_triplet_extract.py（优化版）")
        sys.exit(1)
    os.makedirs("../output", exist_ok=True)

    # ===================== 读取并清洗三元组（优化版：仅做必要清洗） =====================
    try:
        # 1. 读取临时三元组
        df = pd.read_csv(input_path, encoding='utf-8')
        original_count = len(df)
        print("[信息] 读取到临时三元组 {} 条".format(original_count))

        # 2. 基础清洗（仅做核心步骤，避免误删）
        df = df.dropna(subset=['实体1', '关系', '实体2'])  # 删除空值
        df = df.drop_duplicates(subset=['实体1', '关系', '实体2'], keep='first')  # 去重
        df['实体1'] = df['实体1'].str.strip()  # 去除空格
        df['实体2'] = df['实体2'].str.strip()
        df['关系'] = df['关系'].str.strip()

        # 3. 过滤无效数据（仅过滤不在实体字典/关系列表中的数据）
        df = df[df['实体1'].isin(ALL_VALID_ENTITIES)]  # 实体1必须在有效实体中
        df = df[df['实体2'].isin(ALL_VALID_ENTITIES + ["叶片", "茎秆", "果实", "根部", "豆荚", "穗部"])]  # 部位补充
        df = df[df['关系'].isin(ALL_VALID_RELATIONS)]  # 关系必须有效

        # 4. 重置索引
        df = df.reset_index(drop=True)
        clean_count = len(df)

        # 5. 保存清洗后的三元组
        df.to_csv(output_path, index=False, encoding='utf-8', sep=',')

        # 输出结果
        print("[成功] 第五步：三元组清洗完成（优化版）")
        print("   - 清洗前：{} 条".format(original_count))
        print("   - 清洗后：{} 条（有效率 {:.2f}%）".format(clean_count, clean_count / original_count * 100))
        print("   - 输出文件：{}".format(output_path))
        # 打印清洗后各关系数量
        rel_count = df['关系'].value_counts()
        for rel, cnt in rel_count.items():
            print("     - {}：{} 条".format(rel, cnt))

    except Exception as e:
        print("[错误] 清洗三元组出错：{}".format(str(e)))
        sys.exit(1)


if __name__ == "__main__":
    clean_triplets()