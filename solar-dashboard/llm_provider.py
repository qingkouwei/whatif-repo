# -*- coding: utf-8 -*-
"""
LLM provider 抽象：火山方舟 / 阿里 DashScope / NVIDIA NIM 三家统一入口。

三家的 chat 接口都是 OpenAI 兼容格式，真正的差异只有三个：
    base URL、鉴权 key、模型 ID。
所以这一层不写任何"适配逻辑"，只做解析：按环境变量决定走哪家，
把 (base, key, model) 三元组交给上层。上层（app.py 的
generate_topic / behavior_judge.judge / room_review.review）拿到的
永远是同一形状的参数，切换供应商不碰业务代码。

环境变量（优先级从上到下）：
    LLM_PROVIDER=ark|dashscope|nvidia   显式指定
    ARK_API_KEY        → 火山方舟  https://ark.cn-beijing.volces.com/api/v3
    DASHSCOPE_API_KEY  → 阿里      https://dashscope.aliyuncs.com/compatible-mode/v1
    NVIDIA_API_KEY     → NIM       https://integrate.api.nvidia.com/v1
    LLM_MODEL          模型 ID / 方舟接入点。留空则启动时拉 /models 自动选

注意两个不编造的点：
1. 各家默认模型 ID 不硬编码——豆包/千问的具体型号命名迭代很快，
   写死一个"我以为存在的"名字就是上线 404。留空时拉模型列表现场选，
   选规则见 auto_pick。
2. 方舟的"模型"实际是你创建的推理接入点 ID（形如 ep-xxx）或
   官方自动接入点（doubao-seed-1-6-250615 这类带日期的全名）。
   控制台"开通管理"页能直接复制，本层不猜。
"""

import os

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"


def resolve():
    """返回 (name, base, key, model)。model 可能为空串——由调用方
    在启动时调 list_models/auto_pick 补全，或保持空走本地兜底。

    LLM_BASE 环境变量可覆盖端点：阿里云百炼的专属 MAAS 实例
    （llm-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1）与公共
    DashScope 域名不同，key 也和域名绑定——只改 key 不改 base 必 401。
    """
    p = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not p:
        if os.environ.get("ARK_API_KEY"):
            p = "ark"
        elif os.environ.get("DASHSCOPE_API_KEY"):
            p = "dashscope"
        elif os.environ.get("NVIDIA_API_KEY"):
            p = "nvidia"
        else:
            p = "none"
    table = {
        "ark":       ("ark",       ARK_BASE,       os.environ.get("ARK_API_KEY", "")),
        "dashscope": ("dashscope", DASHSCOPE_BASE, os.environ.get("DASHSCOPE_API_KEY", "")),
        "nvidia":    ("nvidia",    NVIDIA_BASE,    os.environ.get("NVIDIA_API_KEY", "")),
    }
    if p not in table:
        return "none", "", "", ""
    name, base, key = table[p]
    base = os.environ.get("LLM_BASE", "").strip() or base
    return name, base, key, os.environ.get("LLM_MODEL", "").strip()


def extra_body(provider):
    """按供应商补请求体参数。

    实测教训：qwen3.8-flash 是思考型模型，默认先输出近 9000 字的
    reasoning_content 再给正文，一次三档文案生成要 77 秒——远超所有
    调用超时，表现为"偶尔退回 fallback"。加 enable_thinking:false
    后同样请求 2.5 秒返回，JSON 格式反而更干净。
    该参数是 DashScope 私有的，NIM/ARK 传了会报未知字段，所以只给
    dashscope 附加。
    """
    if provider == "dashscope":
        return {"enable_thinking": False}
    return {}


async def list_models(base, key):
    """三家都有 GET {base}/models，返回 id 列表。失败返回 []。"""
    import httpx
    if not key:
        return []
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(base + "/models",
                            headers={"Authorization": "Bearer " + key},
                            timeout=15)
            r.raise_for_status()
            return [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except Exception as e:
        print("[llm] 模型列表获取失败：%s" % e)
        return []


def auto_pick(models):
    """从可用列表里选一个对话模型。关键词优先级按中文对话能力排，
    只选带明确版本号或 pro/plus 字样的，避开 tiny/embed/guard 等变体。"""
    prefer = ("doubao-seed", "doubao-1-5-pro", "doubao-pro",
              "qwen-plus", "qwen-max", "qwen3", "qwen-",
              "nemotron", "llama", "deepseek")
    bad = ("embedding", "embed", "guard", "retriever", "vl", "audio",
           "image", "tiny", "lite", "auto")
    for key in prefer:
        for mid in models:
            low = mid.lower()
            if key in low and not any(b in low for b in bad):
                return mid
    return ""
