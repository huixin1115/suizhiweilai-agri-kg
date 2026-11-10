#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第四步：农业文本三元组抽取（修复版）
功能：放宽匹配规则，抽取所有类型三元组（主产区/病虫害/防治方法/栽培要点等）
"""
import re
import csv
import os
import sys
from entity_list import (
    CROPS, DISEASES_INSECTS, RESOURCES, ENVIRONMENTS,
    CULTIVATIONS, REGIONS, RELATION_PATTERNS
)


def extract_triplets():
    # ===================== 路径配置 =====================
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    input_path = os.path.join(project_root, "output", "clean_agri_data.txt")
    output_path = os.path.join(project_root, "output", "temp_triplets.csv")

    # 检查输入文件
    if not os.path.exists(input_path):
        print(f"[错误] 未找到输入文件 {input_path}")
        print("   请先执行第二步脚本 02_data_preprocess.py！")
        sys.exit(1)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # ===================== 优化后的实体匹配函数 =====================
    def find_entities(text, entity_list):
        """匹配所有实体，按长度降序避免短实体覆盖长实体"""
        matched = []
        for entity in sorted(entity_list, key=len, reverse=True):
            if entity in text and entity not in matched:
                matched.append(entity)
        return matched

    # ===================== 扩展的关系匹配规则（核心修复） =====================
    # 新增覆盖文本常见表述的匹配模式
    EXTENDED_RELATION_PATTERNS = {
        "主产区": [r"主产区[为是：]([^，,；;]+)", r"主要分布于([^，,；;]+)", r"主产([^，,；;]+)"],
        "易感病害": [r"易[发生感染患]([^，,；;]+病)", r"常发([^，,；;]+病)", r"易受([^，,；;]+病)危害"],
        "易感虫害": [r"易[发生感染遭]([^，,；;]+虫|螨|蚜|象)", r"常受([^，,；;]+虫)危害", r"易遭([^，,；;]+虫)侵害"],
        "防治方法": [r"([^，,；;]+病|虫)[:：]([^，,；;]+剂|肥)", r"喷施([^，,；;]+剂)防治([^，,；;]+)",
                     r"([^，,；;]+)防治([^，,；;]+病|虫)", r"([^，,；;]+病|虫)用([^，,；;]+)防治"],
        "危害部位": [r"危害([^，,；;]+部位|叶|茎|根|果|穗|花)", r"([^，,；;]+病|虫)危害([^，,；;]+)"],
        "适配肥料": [r"施用([^，,；;]+肥)", r"追施([^，,；;]+肥)", r"适配([^，,；;]+肥)"],
        "适宜气候": [r"适宜([^，,；;]+气候)", r"适合([^，,；;]+气候)", r"喜([^，,；;]+气候)"],
        "适宜土壤": [r"适宜([^，,；;]+土)", r"适合([^，,；;]+土壤)", r"喜([^，,；;]+土)"],
        "栽培要点": [r"需([^，,；;]+)", r"栽培要点[:：]([^，,；;]+)", r"([^，,；;]+)栽培", r"苗期([^，,；;]+)",
                     r"花期([^，,；;]+)"]
    }

    # ===================== 核心抽取逻辑（修复版） =====================
    with open(input_path, 'r', encoding='utf-8') as f:
        texts = [line.strip() for line in f if line.strip()]

    triplets = []
    for idx, text in enumerate(texts):
        # 匹配当前文本中的所有实体
        crops = find_entities(text, CROPS)
        diseases_insects = find_entities(text, DISEASES_INSECTS)
        resources = find_entities(text, RESOURCES)
        environments = find_entities(text, ENVIRONMENTS)
        cultivations = find_entities(text, CULTIVATIONS)
        regions = find_entities(text, REGIONS)

        # 1. 抽取主产区：作物 + 主产区 + 地域
        if crops and regions:
            for crop in crops:
                for region in regions:
                    if crop in text and region in text:
                        # 只要文本中同时有作物和地域，就判定为主产区关系（覆盖文本表述）
                        triplets.append([crop, "主产区", region])

        # 2. 抽取易感病虫害：作物 + 易感病害/虫害 + 病虫害
        if crops and diseases_insects:
            for crop in crops:
                for di in diseases_insects:
                    if crop in text and di in text:
                        # 判断是病害还是虫害
                        if "病" in di:
                            triplets.append([crop, "易感病害", di])
                        elif any(word in di for word in ["虫", "螨", "蚜", "象", "蓟马"]):
                            triplets.append([crop, "易感虫害", di])

        # 3. 抽取防治方法：病虫害 + 防治方法 + 农药/肥料
        if diseases_insects and resources:
            for di in diseases_insects:
                for res in resources:
                    if di in text and res in text and any(word in text for word in ["防治", "喷施", "施用", "用"]):
                        triplets.append([di, "防治方法", res])

        # 4. 抽取危害部位：病虫害 + 危害部位 + 部位
        if diseases_insects:
            for di in diseases_insects:
                if di in text:
                    # 匹配常见危害部位
                    part_matches = re.findall(r"危害(叶|茎|根|果|穗|花|果实|叶片|茎秆|根部|籽粒|蕾|铃)", text)
                    for part in part_matches:
                        if part:
                            triplets.append([di, "危害部位", part])

        # 5. 抽取适配肥料：作物 + 适配肥料 + 肥料
        if crops and resources:
            for crop in crops:
                for res in resources:
                    if crop in text and res in text and "肥" in res:
                        triplets.append([crop, "适配肥料", res])

        # 6. 抽取适宜环境：作物 + 适宜气候/土壤 + 环境
        if crops and environments:
            for crop in crops:
                for env in environments:
                    if crop in text and env in text:
                        if "气候" in env:
                            triplets.append([crop, "适宜气候", env])
                        elif any(word in env for word in ["土", "pH", "壤"]):
                            triplets.append([crop, "适宜土壤", env])
                        elif any(word in env for word in ["℃", "温度"]):
                            triplets.append([crop, "适宜温度", env])
                        elif "湿度" in env:
                            triplets.append([crop, "适宜湿度", env])

        # 7. 抽取栽培要点：作物 + 栽培要点 + 栽培措施
        if crops and cultivations:
            for crop in crops:
                for cul in cultivations:
                    if crop in text and cul in text:
                        triplets.append([crop, "栽培要点", cul])

    # ===================== 去重+保存 =====================
    # 去重（避免重复三元组）
    unique_triplets = []
    seen = set()
    for triplet in triplets:
        # 过滤空值和过短的实体
        if all(len(str(x).strip()) > 0 for x in triplet) and len(triplet) == 3:
            key = tuple(triplet)
            if key not in seen:
                seen.add(key)
                unique_triplets.append(triplet)

    # 写入CSV
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["实体1", "关系", "实体2"])
        writer.writerows(unique_triplets)

    # 打印结果（按关系类型统计，方便验证）
    print(f"[成功] 第四步：三元组抽取完成")
    print(f"   - 输入文本条数：{len(texts)}")
    print(f"   - 抽取三元组条数（去重后）：{len(unique_triplets)}")
    print(f"   - 输出文件：{output_path}")

    # 按关系类型统计
    relation_count = {}
    for t in unique_triplets:
        rel = t[1]
        relation_count[rel] = relation_count.get(rel, 0) + 1
    print(f"\n   各关系类型数量：")
    for rel, count in relation_count.items():
        print(f"   - {rel}：{count} 条")


if __name__ == "__main__":
    extract_triplets()