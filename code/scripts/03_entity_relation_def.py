#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第三步：实体与关系定义（修复权限问题）
功能：生成标准化的实体/关系定义表（Excel格式），用于论文附录
"""
import pandas as pd
import os
import sys
import io
import stat

# 强制设置标准输出为UTF-8（适配Windows终端）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def generate_entity_relation_def():
    # ===================== 路径校验+权限准备 =====================
    output_path = "../output/entity_relation_def.xlsx"
    os.makedirs("../output", exist_ok=True)

    # 核心修复：如果文件已存在，先删除（避免占用导致权限拒绝）
    if os.path.exists(output_path):
        try:
            # 移除文件只读属性
            os.chmod(output_path, stat.S_IWRITE)
            os.remove(output_path)
        except:
            pass

    # ===================== 定义实体和关系 =====================
    try:
        # 1. 实体类型定义（完整覆盖42种作物）
        entity_def = pd.DataFrame({
            "实体类型": [
                "作物类", "病虫害类", "资源类", "环境类", "栽培类", "地域类"
            ],
            "具体示例": [
                "小麦、玉米、大豆、水稻、棉花、油菜、花生、高粱、大麦、燕麦、荞麦、谷子、糜子、马铃薯、甘薯、蚕豆、豌豆、绿豆、红豆、菜豆、豇豆、番茄、黄瓜、茄子、辣椒、白菜、萝卜、芹菜、韭菜、大葱、大蒜、洋葱、西兰花、菜花、菠菜、生菜、油麦菜、香菜、茼蒿、冬瓜、南瓜、丝瓜、苦瓜",
                "锈病、白粉病、霜霉病、灰霉病、炭疽病、枯萎病、根腐病、叶斑病、稻瘟病、纹枯病、蚜虫、红蜘蛛、蓟马、螟虫、飞虱、叶蝉、菜青虫、小菜蛾、地老虎、蛴螬、蝼蛄、金针虫、根结线虫",
                "尿素、磷酸二铵、氯化钾、硫酸钾、过磷酸钙、复合肥、有机肥、生物肥、三唑酮乳油、戊唑醇悬浮剂、多菌灵可湿性粉剂、百菌清、代森锰锌、吡虫啉、阿维菌素、辛硫磷颗粒剂、敌百虫",
                "温带季风气候、亚热带季风气候、温带大陆性气候、砂壤土、黏壤土、壤土、冲积土、红壤、20-25℃、60%-70%湿度、pH值6.0-7.5",
                "灌浆期补水、疏花30%、晒田控旺、整枝打杈、打顶摘心、引蔓压蔓、培土追肥、温汤浸种、种子包衣、轮作倒茬、中耕除草、控水控肥",
                "华北平原、东北平原、长江中下游平原、黄淮海平原、四川盆地、黄土高原、山东寿光、河南中牟、新疆南疆、陕西洛川、河北赵县、山西运城、浙江黄岩、广东茂名、云南元谋"
            ],
            "备注": [
                "共42种常见农作物",
                "包含真菌性病害、细菌性病害、病毒性病害、虫害",
                "包含化肥、有机肥、农药（乳油/悬浮剂/粉剂）",
                "包含气候类型、土壤类型、温湿度、pH值",
                "包含田间管理、育苗、定植、收获等措施",
                "包含大区域和特色农产品产区"
            ]
        })

        # 2. 关系类型定义（完整9类关系）
        relation_def = pd.DataFrame({
            "关系类型": [
                "易感病害", "易感虫害", "防治方法", "危害部位",
                "适配肥料", "适宜气候", "适宜土壤", "栽培要点", "主产区"
            ],
            "抽取规则": [
                "匹配文本中的'易感XX病'/'易患XX病'/'易发生XX病'",
                "匹配文本中的'易感XX虫'/'易受XX蚜侵害'/'XX螨危害'",
                "匹配文本中的'喷施XX药剂防治XX病'/'撒施XX颗粒剂防XX虫'",
                "匹配文本中的'XX病危害叶片'/'XX虫侵害茎秆'/'XX螨危害果实'",
                "匹配文本中的'适配XX肥'/'适宜施用XX肥'/'推荐使用XX肥'",
                "匹配文本中的'适宜XX气候'/'XX气候下生长良好'",
                "匹配文本中的'适宜砂壤土'/'黏壤土种植最佳'/'壤土适配'",
                "匹配文本中的'需XX管理'/'及时XX'/'控制XX条件'",
                "匹配文本中的'主产区为XX'/'XX地区为核心产区'"
            ],
            "示例三元组": [
                "小麦-易感病害-锈病",
                "番茄-易感虫害-蚜虫",
                "小麦锈病-防治方法-喷施三唑酮乳油",
                "番茄蚜虫-危害部位-叶片",
                "玉米-适配肥料-磷酸二铵",
                "水稻-适宜气候-亚热带季风气候",
                "黄瓜-适宜土壤-砂壤土",
                "苹果-栽培要点-疏花30%",
                "小麦-主产区-华北平原"
            ]
        })

        # 3. 保存为Excel（多sheet），设置写入权限
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            entity_def.to_excel(writer, sheet_name="实体类型定义", index=False)
            relation_def.to_excel(writer, sheet_name="关系类型定义", index=False)

        # 设置文件可写权限
        os.chmod(output_path, stat.S_IWRITE)

        print("[成功] 第三步：实体关系定义表生成完成")
        print("   - 输出文件：{}".format(output_path))
        print("   - 包含实体类型：{}类".format(len(entity_def)))
        print("   - 包含关系类型：{}类".format(len(relation_def)))

    except ImportError:
        print("[错误] 缺少openpyxl库，请执行 pip install openpyxl")
        sys.exit(1)
    except PermissionError:
        print("[错误] 无权限写入Excel文件，请关闭该文件后重新运行！")
        sys.exit(1)
    except Exception as e:
        print("[错误] 第三步执行出错：{}".format(str(e)))
        sys.exit(1)


if __name__ == "__main__":
    generate_entity_relation_def()