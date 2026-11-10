from modelscope.hub.snapshot_download import snapshot_download

# 正确的轻量Qwen模型ID（魔搭社区存在）
model_id = "qwen/Qwen2-0.5B-Instruct"
# 下载到指定路径（和代码里的LLM_MODEL_PATH一致）
target_dir = "./models/Qwen3.5-0.5B-Chat"

# 开始下载（仅1GB，几分钟完成）
snapshot_download(
    model_id=model_id,
    cache_dir=target_dir,
    revision="master"
)

print(f"✅ Qwen3.5-0.5B-Chat 已下载到：{target_dir}")