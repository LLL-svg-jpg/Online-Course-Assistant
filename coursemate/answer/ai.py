"""AI 作答。

两个 provider：
- AnthropicProvider    走官方 anthropic SDK，用结构化输出强制 JSON，不做字符串解析
- OpenAICompatProvider 走 httpx，覆盖 DeepSeek 等 OpenAI 兼容端点

为什么必须用结构化输出：让模型自由输出再用正则抠答案，是这类脚本最常见的失败点——
一句"根据题意，答案应该是 B"就能让解析逻辑全线崩溃。
"""
from __future__ import annotations

import json

from ..logger import Logger
from .base import AnswerProvider, AnswerResult, Question, normalize

logger = Logger()

# 结构化输出契约。option_texts 是防错位的第二保险：
# 平台打乱选项顺序时，按字母填答案会填错，此时用文本回退匹配。
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "option_keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "选中的选项字母。非选择题填空数组。",
        },
        "option_texts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "选中选项的完整原文，必须与题目给出的选项文本逐字一致。",
        },
        "text": {
            "type": "string",
            "description": "填空题或简答题的答案正文。选择题填空字符串。",
        },
        "confidence": {
            "type": "number",
            "description": "0 到 1 之间的置信度。不确定时如实给低值，不要虚高。",
        },
        "reasoning": {"type": "string", "description": "一句话作答依据，40 字以内。"},
    },
    "required": ["option_keys", "option_texts", "text", "confidence", "reasoning"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你是一名协助完成网络课程随堂练习的助教。用户会给你一道课程内的题目，请给出你判断的正确答案。

要求：
1. 选择题必须从给定选项中选择，option_keys 填字母，option_texts 填对应选项的完整原文。
2. 判断题的选项通常是"正确/错误"或"对/错"，同样按选项作答。
3. 多选题要选全，不要因为不确定就少选。
4. 填空题、简答题把答案写进 text 字段，简明扼要。
5. confidence 要诚实反映把握程度。题目信息不足时给低值，不要为了显得可靠而虚报。
"""


def _render(question: Question) -> str:
    lines = [f"题型：{question.qtype}", f"题目：{question.stem.strip()}"]
    if question.options:
        lines.append("选项：")
        lines.extend(f"  {opt.key}. {opt.text}" for opt in question.options)
    return "\n".join(lines)


def _image_parts(question: Question) -> tuple[str, str] | None:
    """拆出 data URL，供不同服务商各自组装图片消息。"""
    if not question.image_data_url or "," not in question.image_data_url:
        return None
    header, data = question.image_data_url.split(",", 1)
    media_type = header.split(";", 1)[0].removeprefix("data:") or "image/png"
    return media_type, data


def _to_result(payload: dict, source: str) -> AnswerResult:
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    keys = [str(k).strip().upper() for k in payload.get("option_keys") or [] if str(k).strip()]
    texts = [str(t).strip() for t in payload.get("option_texts") or [] if str(t).strip()]
    return AnswerResult(
        option_keys=keys,
        option_texts=texts,
        text=str(payload.get("text", "") or "").strip(),
        confidence=min(max(confidence, 0.0), 1.0),
        reasoning=str(payload.get("reasoning", "") or "").strip(),
        source=source,
    )


class AnthropicProvider(AnswerProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float,
                 effort: str = "high", proxy: str = ""):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺少 anthropic 依赖，请先执行 pip install -r requirements.txt"
            ) from exc
        self._anthropic = anthropic
        if proxy:
            # anthropic SDK 底层是 httpx，认这两个环境变量。
            # 直接改环境变量而不是塞 http_client，是为了避开
            # SDK 在不同版本间对自定义客户端的差异。
            import os

            os.environ["HTTPS_PROXY"] = proxy
            os.environ["HTTP_PROXY"] = proxy
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)
        self.model = model
        self.effort = effort

    async def solve(self, question: Question) -> AnswerResult:
        a = self._anthropic
        prompt = _render(question)
        content: list[dict] = [{"type": "text", "text": prompt}]
        image = _image_parts(question)
        if image:
            media_type, data = image
            content.insert(0, {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            })
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": ANSWER_SCHEMA},
                },
            )
        except a.BadRequestError as exc:
            logger.error(f"AI 请求被拒绝（可能是模型名或参数不对）：{Logger.summarize(exc)}")
            return AnswerResult()
        except a.AuthenticationError:
            logger.error("AI API Key 无效，请检查 config.toml 的 answer.api_key。")
            return AnswerResult()
        except a.RateLimitError as exc:
            logger.warn(f"AI 触发限流，本题跳过：{Logger.summarize(exc)}")
            return AnswerResult()
        except a.APIStatusError as exc:
            logger.error(f"AI 服务返回错误 {exc.status_code}：{Logger.summarize(exc)}")
            return AnswerResult()
        except a.APIConnectionError as exc:
            logger.error(f"AI 网络连接失败：{Logger.summarize(exc)}")
            return AnswerResult()

        # 安全分类器可能拒答，此时 content 里没有可用 JSON
        if response.stop_reason == "refusal":
            logger.warn("AI 拒绝作答本题，已跳过。")
            return AnswerResult()

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            return AnswerResult()
        try:
            return _to_result(json.loads(text), "ai")
        except json.JSONDecodeError as exc:
            logger.warn(f"AI 返回内容不是合法 JSON：{Logger.summarize(exc)}")
            return AnswerResult()

    async def aclose(self) -> None:
        await self._client.close()


class OpenAICompatProvider(AnswerProvider):
    """DeepSeek 等 OpenAI 兼容端点。走原始 HTTP，不引入第二个 SDK。"""

    name = "openai_compatible"

    def __init__(self, api_key: str, model: str, base_url: str,
                 timeout: float, proxy: str = ""):
        import httpx

        kwargs = {
            "base_url": base_url.rstrip("/"),
            "timeout": timeout,
            "headers": {"Authorization": f"Bearer {api_key}"},
        }
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)
        self.model = model

    async def solve(self, question: Question) -> AnswerResult:
        import httpx

        hint = "\n\n请只输出 JSON，字段为 option_keys, option_texts, text, confidence, reasoning。"
        prompt = _render(question) + hint
        user_content: str | list[dict] = prompt
        if question.image_data_url:
            user_content = [
                {"type": "text", "text": (
                    "请以图片里实际渲染的题目和选项为准；页面文字可能被防复制字体打乱。\n\n"
                    + prompt
                )},
                {"type": "image_url", "image_url": {"url": question.image_data_url}},
            ]
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        try:
            resp = await self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return _to_result(json.loads(content), "ai")
        except httpx.HTTPStatusError as exc:
            logger.error(f"AI 服务返回 {exc.response.status_code}：{exc.response.text[:120]}")
        except httpx.RequestError as exc:
            logger.error(f"AI 网络请求失败：{Logger.summarize(exc)}")
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            logger.warn(f"AI 返回结构异常：{Logger.summarize(exc)}")
        return AnswerResult()

    async def aclose(self) -> None:
        await self._client.aclose()


def build_provider(config) -> AnswerProvider | None:
    """按配置构造 provider。

    返回 None 不是失败——开启试错答题后 AI 只是"让第一次尝试更可能命中"的加速器，
    没有它照样能靠逐个尝试把题答对，只是要多点几次。
    所以这里任何一种缺失都只警告、不抛异常。
    """
    if not config.answer_enabled:
        return None
    if not config.api_key:
        logger.warn("未配置 AI API Key，无法调用 AI。独立测试只使用题库参考或保留已有答案；"
                    "视频弹题是否试错由答错重试设置控制。")
        return None

    # anthropic 之外的服务商（DeepSeek / 通义 / 智谱 / Kimi 等）都是 OpenAI 兼容协议
    try:
        proxy = getattr(config, "proxy", "")
        if config.answer_provider == "anthropic":
            return AnthropicProvider(config.api_key, config.model,
                                     config.answer_timeout, proxy=proxy)
        if not config.base_url:
            logger.warn(
                f"服务商 {config.answer_provider} 未填接口地址，无法调用 AI，"
                "将改为逐个尝试作答。"
            )
            return None
        return OpenAICompatProvider(
            config.api_key, config.model, config.base_url,
            config.answer_timeout, proxy=proxy
        )
    except (ImportError, RuntimeError) as exc:
        logger.warn(f"AI 组件不可用（{Logger.summarize(exc)}），将改为逐个尝试作答。")
        return None


def match_option(result: AnswerResult, question: Question) -> list[str]:
    """把 AI 的答案落到页面实际选项上，返回最终要点选的选项字母。

    先信字母；字母对不上（超出选项范围）时用文本匹配兜底。
    """
    valid = {opt.key.upper(): opt for opt in question.options}
    # 不能假设 option_keys 已经是大写：缓存回读和手工构造都可能带小写
    keys = [k.strip().upper() for k in result.option_keys]
    keys = [k for k in keys if k in valid]
    if keys:
        return keys

    matched: list[str] = []
    for wanted in result.option_texts:
        target = normalize(wanted)
        if not target:
            continue
        for opt in question.options:
            body = normalize(opt.text)
            if body and (body == target or target in body or body in target):
                if opt.key.upper() not in matched:
                    matched.append(opt.key.upper())
                break
    if matched:
        logger.debug(f"选项字母未命中，按文本回退匹配到 {matched}")
    return matched
