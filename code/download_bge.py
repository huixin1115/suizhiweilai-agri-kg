from modelscope.hub.snapshot_download import snapshot_download
# -*- coding: utf-8 -*-
# bge-small-zh-v1.5 在魔搭社区的正确模型ID
model_id = "BAAI/bge-small-zh-v1.5"
# 下载到指定路径（和rag_search.py里的EMBED_MODEL_PATH一致）
target_dir = "./models/bge-small-zh-v1.5"

# 开始下载（仅120MB，几秒就完成）
snapshot_download(
    model_id=model_id,
    cache_dir=target_dir,
    revision="master"
)

print(f"✅ bge-small-zh-v1.5 已下载到：{target_dir}")