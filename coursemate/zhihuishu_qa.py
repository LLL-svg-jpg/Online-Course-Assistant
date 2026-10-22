"""课程问答：原生发布、回执核对、待审核及无效记录。"""
from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import re
from urllib.parse import urlparse
from playwright.async_api import Error as PlaywrightError

from .answer.base import Question, normalize
from .logger import Logger

logger = Logger()
SAVE_PATH = "/creditqa/gateway/t/v1/web/qa/saveQuestion"
MINE_PATH = "/creditqa/gateway/t/v1/web/qa/myQuestionList"


def qa_response(response, path):
    parsed = urlparse(response.url)
    return (parsed.scheme == "https" and (parsed.hostname or "").endswith(".zhihuishu.com")
            and parsed.path == path and response.request.method == "POST")


def business_ok(data):
    """网站同时返回数字和字符串成功码，仍须核对业务数据及发布身份。"""
    return isinstance(data, dict) and str(data.get("status")) == "200"


def business_status(data):
    def code(key):
        value = str(data.get(key)) if isinstance(data, dict) else "未知"
        return value if re.fullmatch(r"[A-Za-z0-9_-]{1,16}", value) else "未知"
    return f"status={code('status')}, code={code('code')}"


def shown(value):
    return str(value) == "1"


def suitable_question(text: str, topic: str, previous: list[str]) -> bool:
    """仅接受具体小节主题相关的完整问题；审核结果仍由网站决定。"""
    text = text.strip()
    if not 15 <= len(text) <= 500 or not text.endswith(("?", "？")):
        return False
    if re.search(r"https?://|www\.|\b1[3-9]\d{9}\b|[\w.+-]+@[\w.-]+|```", text):
        return False
    chunks = re.findall(r"[\u4e00-\u9fffA-Za-z]{4,}", topic)
    terms = {chunk[i:i + 4] for chunk in chunks for i in range(len(chunk) - 3)}
    if not terms or not any(term in text for term in terms):
        return False
    current = normalize(text)
    return not any(SequenceMatcher(None, current, normalize(old)).ratio() >= .8
                   for old in previous)


async def own_posts(page, answers=False):
    """通过自己的问答入口触发正常查询，不把别人的帖子算进自己的数量。"""
    path = MINE_PATH.replace("myQuestionList", "myAnswerList") if answers else MINE_PATH
    async with page.expect_response(lambda r: qa_response(r, path), timeout=15000) as receipt:
        await qa_click(page, page.get_by_text("我的回答" if answers else "我的提问", exact=True))
    response = await receipt.value
    data = await response.json()
    label = "我的回答" if answers else "我的提问"
    if not business_ok(data):
        raise RuntimeError(f"{label}列表未返回成功业务状态（HTTP {response.status}, {business_status(data)}）")
    result = data.get("rt")
    if result is None:
        rows, more = [], False
    else:
        key = "myAnswers" if answers else "questionInfoList"
        rows = result.get(key)
        if key in result and rows is None:
            rows = []
        if not isinstance(rows, list):
            raise RuntimeError(f"{label}列表缺少可识别的数据")
        index = result.get("pageIndex")
        more = index is not None and int(index) > 0
    await page.go_back(wait_until="domcontentloaded", timeout=15000)
    await page.locator(".ask-btn").wait_for(state="visible", timeout=15000)
    return rows, more


async def dismiss_welcome(page):
    notice = page.locator(".dialog:visible").filter(has_text="请查看问答注意事项及相关规则")
    button = notice.get_by_text("下次再说", exact=True)
    if await notice.count() == 1 and await button.count() == 1:
        await button.click(timeout=3000)
        await notice.wait_for(state="hidden", timeout=3000)
        return True
    return False


async def qa_click(page, target):
    await dismiss_welcome(page)
    try:
        await target.click(timeout=5000)
    except PlaywrightError:
        if not await dismiss_welcome(page):
            raise
        await target.click(timeout=5000)


def reconcile_posts(record, rows, complete=True, answers=False):
    posts = record.setdefault("answers" if answers else "questions", [])
    id_key, text_key, status_key = (("aid", "answerContent", "answerContentShowStatus")
                                  if answers else ("questionId", "content", "contentShowStatus"))
    for row in rows:
        question_id, content = str(row.get(id_key, "")), str(row.get(text_key, "")).strip()
        if not question_id or status_key not in row:
            raise RuntimeError("自己的提问缺少身份或显示状态，未推断审核结果")
        post = next((p for p in posts if p.get("id") == question_id
                     or (content and normalize(p["text"]) == normalize(content))), None)
        if post is None:
            post = {"id": question_id, "text": content}
            posts.append(post)
        post.update(id=question_id, status="published" if shown(row[status_key]) else "blocked")
        if answers:
            post["qid"] = str(row["qid"])
    if complete:
        present = {str(row[id_key]) for row in rows}
        for post in posts:
            if post.get("status") == "published" and post.get("id") not in present:
                post["status"] = "removed"
    return posts


async def generate_question(provider, course_title, topic, previous, should_stop):
    for attempt in range(3):
        prompt = ("请为课程问答区拟写一个真实、有明确学习价值的中文问题，把问题正文写进 text 字段。"
                  "不回答问题，不写前言、序号、个人经历或得分要求，不写泛泛套话。"
                  "只提出一个有讨论价值的核心疑问，建议60到180字，以问号结尾。"
                  "不得问简单名词解释、是非判断、时间地点人物或可直接查得固定答案的问题，"
                  "不得在问题里附上答案。结合本小节提出具体情境、条件权衡或开放讨论。"
                  "不要声称教材、老师明确说过未提供的原文，不编造他人的观点。\n"
                  f"课程：{course_title}\n本题小节：{topic}\n"
                  "避免与这些已提问题重复或仅换词：\n" + "\n".join(previous[-50:]))
        result = await provider.solve(Question(prompt, qtype="fill", context="course_question"))
        if should_stop():
            return ""
        candidate = result.text.strip()
        if suitable_question(candidate, topic, previous):
            review = await provider.solve(Question(
                f"请核对拟发布的学习问题。课程：{course_title}；实际小节：{topic}。\n"
                f"拟发布问题：{candidate}\n"
                "仅当问题直接围绕该小节概念、依据、概念区别或应用，表述清楚且有学习价值，"
                "没有捏造个人经历、求分、广告、灌水、偏题或空泛套话时，text字段仅填‘通过’；"
                "简单名词解释、是非判断、可直接查得固定答案、把答案附在问题里，或声称引用了未提供的教材原话，均不通过。"
                "否则text仅填‘不通过’，confidence如实反映把握。不要回答这个问题。",
                qtype="fill", context="course_question_review"))
            if should_stop():
                return ""
            if review.text.strip() == "通过" and review.confidence >= .8:
                return candidate
        logger.warn(f"AI 提问 [{course_title} / {topic}]：第 {attempt + 1} 次生成未通过切题/格式/去重检查，未发布。")
    return ""


async def publish_questions(page, provider, record, target, course_title, topics,
                            save, today, should_stop) -> bool:
    if provider is None:
        logger.warn(f"AI 提问 [{course_title}]：需要启用 AI 并配置可用 Key，本轮未发布。")
        return False
    if not topics:
        raise RuntimeError("未读到本课程的小节主题，不能生成课程问题")
    rows, more = await own_posts(page)
    examples = await page.locator(".qa-list-container .question-content").evaluate_all(
        "nodes => nodes.map(e => e.getAttribute('title') || e.textContent.trim()).filter(Boolean).slice(0,50)")
    posts = reconcile_posts(record, rows, complete=not more)
    save()
    uncertain = [p for p in posts if p.get("status") in ("posting", "uncertain", "receipt")]
    if uncertain:
        logger.warn(f"AI 提问 [{course_title}]：有 {len(uncertain)} 条发布结果尚未核实，保留页面，未重复发送。")
        return False
    visible = sum(shown(row.get("contentShowStatus")) for row in rows)
    credited = record.get("valid_questions", 0)
    if not more:
        # 成绩次日才更新，不能用旧有效数抵消当前已屏蔽/已移除的问题。
        credited = min(credited, visible)
    if credited >= target:
        logger.info(f"AI 提问 [{course_title}]：网站已统计 {credited} 条有效提问，目标 {target} 条。")
        return True
    if more and visible < target:
        logger.warn(f"AI 提问 [{course_title}]：已有问题超过当前页，数量尚未完整核实，未补发。")
        return False
    # 查询可能晚于发布回执更新，已拿到 questionId 的本机记录也占用名额。
    published = sum(p.get("status") in ("published", "receipt") for p in posts)
    needed = max(0, target - max(visible, published, credited))
    if needed == 0:
        logger.info(f"AI 提问 [{course_title}]：已发布至目标 {target} 条，待网站审核/次日统计；无效不算有效。")
        return True
    if record.get("question_batch_day") == today:
        logger.warn(f"AI 提问 [{course_title}]：今日已处理一批，审核未通过的项目留待后续核对，不循环补发。")
        return False
    posted = []
    for number in range(needed):
        if should_stop():
            return False
        previous = [p["text"] for p in posts if p.get("text")] + examples
        topic = topics[len(posts) % len(topics)]
        text = await generate_question(provider, course_title, topic, previous, should_stop)
        if not text:
            return False
        await qa_click(page, page.locator(".ask-btn"))
        dialog = page.locator(".dialog-question:visible")
        await dialog.wait_for(state="visible", timeout=5000)
        await dialog.locator("textarea").fill(text, timeout=5000)
        if await dialog.locator("textarea").input_value(timeout=5000) != text:
            raise RuntimeError("提问正文未完整写入表单，未发布")
        if should_stop():
            return False
        post = {"text": text, "topic": topic, "day": today, "status": "posting"}
        posts.append(post)
        record["question_batch_day"] = today
        save()  # 点击前落盘；中途退出也不能盲目重复发送。
        try:
            async with page.expect_response(lambda r: qa_response(r, SAVE_PATH), timeout=15000) as receipt:
                await dialog.locator(".up-btn").click(timeout=5000)
            response = await receipt.value
            data = await response.json()
            question_id = (data.get("rt") or {}).get("questionId")
            if not business_ok(data) or not question_id:
                post["status"] = "rejected"
                save()
                logger.warn(f"AI 提问 [{course_title} / {topic}]：网站未接受发布（{business_status(data)}），"
                            "页面保留，未记为有效提问。")
                return False
            post.update(id=str(question_id), status="receipt")
            posted.append(post)
            save()
            await dialog.wait_for(state="hidden", timeout=5000)
            logger.info(f"AI 提问 [{course_title} / {topic}]：第 {number + 1}/{needed} 条已获发布回执，审核有效性待确认。")
            if number + 1 < needed:
                # 网站发布控件本身有3秒重复提交保护。
                for _ in range(30):
                    if should_stop():
                        return False
                    await asyncio.sleep(.1)
        except BaseException:
            if post["status"] == "posting":
                post["status"] = "uncertain"
                save()
            raise
    for attempt in range(3):
        rows, more = await own_posts(page)
        reconcile_posts(record, rows, complete=not more)
        save()
        if all(p["status"] != "receipt" for p in posted):
            break
        if should_stop():
            return False
        await asyncio.sleep(1)
    if any(p["status"] != "published" for p in posted):
        logger.warn(f"AI 提问 [{course_title}]：部分问题未在自己的列表确认或已被屏蔽，未记为有效，保留页面。")
        return False
    logger.info(f"AI 提问 [{course_title}]：本批 {len(posted)} 条在自己的列表确认，最终有效数以网站后续审核统计为准。")
    return True
