import pandas as pd
from neo4j import GraphDatabase
import os
import sys

# ============================================================================
# ⚠️ 注意：本脚本会 **清空整个 Neo4j 数据库**（MATCH (n) DETACH DELETE n），
#    然后只导入原始的 clean_triplets.csv（约 2558 条 / 743 节点）。
#
#    如果要导入包含扩展知识图谱的完整数据（1320 节点 / 5693 关系），
#    请改用：python scripts/08_import_extended_kg.py
#
#    为防误清库，检测到库中已有扩展图谱数据时会要求输入 yes 确认；
#    非交互环境请显式加参数 --yes 跳过确认。
# ============================================================================

# 扩展图谱独有的标签，用来判断库里是否已有扩展数据
EXTENDED_LABELS = ["生育期", "品种", "养分元素", "季节", "作物类别", "杂草", "防治措施"]

# ========== 配置区 ==========
NEO4J_CONFIG = {
    "uri": "bolt://localhost:7687",
    "auth": ("neo4j", "123456")
}
CSV_FILE_PATH = "clean_triplets.csv"
# 关系-标签映射（核心修复：按关系自动匹配实体类型）
RELATION_LABEL_MAPPING = {
    "主产区": ("作物", "地域"),          # 作物→主产区→地域
    "适宜土壤": ("作物", "土壤"),          # 作物→适宜土壤→土壤
    "适宜气候": ("作物", "气候"),          # 作物→适宜气候→气候
    "适配肥料": ("作物", "农药肥料"),      # 作物→适配肥料→农药肥料
    "易感病害": ("作物", "病虫害"),        # 作物→易感病害→病虫害
    "易感虫害": ("作物", "病虫害"),        # 作物→易感虫害→病虫害
    "防治方法": ("病虫害", "农药肥料"),    # 病虫害→防治方法→农药肥料
    "栽培要点": ("作物", "栽培要点")       # 作物→栽培要点→栽培要点
}
ENTITY_TYPE_MAPPING = {
    "作物": "作物",
    "病虫害": "病虫害",
    "地域": "地域",
    "农药肥料": "农药肥料",
    "栽培要点": "栽培要点",
    "土壤": "土壤",
    "气候": "气候"
}

def import_triples():
    # 1. 检查CSV文件
    if not os.path.exists(CSV_FILE_PATH):
        print(f"错误：找不到 CSV 文件！路径：{CSV_FILE_PATH}")
        return

    # 2. 读取CSV（适配中文编码）
    try:
        df = pd.read_csv(CSV_FILE_PATH, encoding='utf-8')
    except UnicodeDecodeError:
        df = pd.read_csv(CSV_FILE_PATH, encoding='gbk')

    # 检查必要列
    required_cols = ["实体1", "关系", "实体2"]
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        print(f"错误：CSV缺少列：{missing_cols}")
        return

    # 过滤无效行（关系不在映射中则跳过）
    df = df[df["关系"].isin(RELATION_LABEL_MAPPING.keys())]
    print(f"成功读取 {len(df)} 条有效三元组")

    # 3. 连接Neo4j并导入
    try:
        driver = GraphDatabase.driver(**NEO4J_CONFIG)
        driver.verify_connectivity()
        print("成功连接Neo4j")
    except Exception as e:
        print(f"Neo4j连接失败：{e}")
        return

    with driver.session() as session:
        # 清空前的安全确认：库里已有扩展图谱数据时，避免一键清零
        cnt = session.run(
            "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $ls) RETURN count(n) AS c",
            ls=EXTENDED_LABELS).single()["c"]
        if cnt and "--yes" not in sys.argv:
            print("=" * 66)
            print("[警告] 检测到库中已有 %d 个扩展图谱节点（生育期/品种/杂草等）。" % cnt)
            print("    本脚本会清空整个数据库，只导入 clean_triplets.csv 的原始三元组。")
            print("    如需保留扩展图谱，请改用：python scripts/08_import_extended_kg.py")
            print("=" * 66)
            try:
                ans = input("确认要继续清空并重导吗？输入 yes 继续：").strip().lower()
            except EOFError:
                ans = ""
            if ans != "yes":
                print("已取消，未对数据库做任何修改。")
                driver.close()
                return

        # 清空旧数据
        session.run("MATCH (n) DETACH DELETE n")
        print("已清空旧数据")

        # 创建索引（加速查询）
        for label in ENTITY_TYPE_MAPPING.values():
            session.run(f"CREATE INDEX IF NOT EXISTS FOR (n:`{label}`) ON (n.name)")
        print("已创建索引")

        # 批量导入（按关系映射自动分配标签）
        success_count = 0
        fail_count = 0
        for idx, row in df.iterrows():
            if pd.isna(row["实体1"]) or pd.isna(row["关系"]) or pd.isna(row["实体2"]):
                fail_count += 1
                continue

            source = str(row["实体1"]).strip()
            relation = str(row["关系"]).strip()
            target = str(row["实体2"]).strip()

            # 按关系获取标签（核心修复）
            source_label, target_label = RELATION_LABEL_MAPPING[relation]

            # 执行导入
            try:
                session.run("""
                    MERGE (s:`{source_label}` {{name: $source}})
                    MERGE (t:`{target_label}` {{name: $target}})
                    MERGE (s)-[r:REL {{type: $relation}}]->(t)
                """.format(source_label=source_label, target_label=target_label),
                source=source, target=target, relation=relation)
                success_count += 1
            except Exception as e:
                fail_count += 1
                if idx % 500 == 0:
                    print(f"第 {idx} 行导入失败：{e}")

            if idx % 500 == 0:
                print(f"进度：{idx}/{len(df)} | 成功：{success_count} | 失败：{fail_count}")

    driver.close()
    print(f"\n导入完成！成功：{success_count} | 失败：{fail_count}")

if __name__ == "__main__":
    import_triples()