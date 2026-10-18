"""智慧树新版播放页的批量 AI 随堂练习；保存回执不等同判题正确。"""
from __future__ import annotations

import asyncio

from .answer.ai import match_option
from .answer.base import AnswerResult, Option, Question
from .logger import Logger

logger = Logger()
ROW_SEL = ".ai-class-exercise-dialog .ques-list .item"
SAVE_API = "https://studyservice-api.zhihuishu.com/gateway/t/v1/popupAnswer/batchSaveLessonPopupExamSaveAnswer"


async def extract_questions(page) -> list[Question]:
    questions = []
    rows = page.locator(ROW_SEL)
    for index in range(await rows.count()):
        row = rows.nth(index)
        stem = " ".join((await row.locator(".question-info").inner_text()).split())
        type_text = await row.locator(".type").inner_text()
        qtype = ("multiple" if "多选" in type_text else "judge" if "判断" in type_text
                 else "single" if "单选" in type_text else "unknown")
        options, selected = [], []
        nodes = row.locator(".option")
        for i in range(await nodes.count()):
            node = nodes.nth(i)
            key = (await node.locator(".class-question-select").inner_text()).strip()
            options.append(Option(key, " ".join((await node.locator('.answer').inner_text()).split())))
            if await node.locator(".class-question-select.isSelect").count():
                selected.append(key)
        questions.append(Question(stem, options, qtype=qtype, index=index, selected_keys=selected))
    return questions


async def process_popup(page, adapter, config, provider, cache) -> None:
    questions = await extract_questions(page)
    if not questions:
        return
    for question in questions:
        if question.selected_keys:
            continue
        if not question.is_choice:
            logger.warn("新版随堂练习题型未识别，保留题目供人工处理。")
            continue
        result = cache.get(question) if config.answer_cache else None
        if result is None and provider is not None:
            result = await provider.solve(question)
        if not isinstance(result, AnswerResult) or result.empty:
            logger.warn("新版随堂练习没有参考答案，保留题目，不猜测或强行关闭。")
            continue
        keys = set(match_option(result, question))
        valid = {option.key for option in question.options}
        if not keys or not keys <= valid or (question.qtype != "multiple" and len(keys) != 1):
            continue
        row = page.locator(ROW_SEL).nth(question.index)
        if " ".join((await row.locator('.question-info').inner_text()).split()) != question.stem:
            return
        for index, option in enumerate(question.options):
            if option.key in keys:
                await row.locator(".option").nth(index).click(timeout=3000)
    current = await extract_questions(page)
    if ([q.fingerprint for q in current] != [q.fingerprint for q in questions]
            or not all(q.selected_keys for q in current)):
        logger.warn("新版随堂练习仍有未确认选中的题目，未提交，保留页面。")
        return
    if not (config.auto_submit or config.retry_until_correct):
        logger.info("新版随堂练习已填写，按设置保留供人工提交。")
        return
    button = page.locator(".ai-class-exercise-dialog .el-dialog__footer") \
        .get_by_role("button", name="提交作答", exact=True)
    if not await button.count() or not await button.is_enabled():
        await adapter.close_question(page)
        return
    try:
        async with page.expect_response(
            lambda r: r.url.split('?', 1)[0] == SAVE_API and r.request.method == 'POST',
            timeout=15000,
        ) as pending:
            await button.click(timeout=3000)
        data = await (await pending.value).json()
        if str(data.get('code')) != '0':
            logger.warn("新版随堂练习未取得有效保存回执，不记为答对。")
            return
        logger.info(f"新版随堂练习 {len(current)} 题已提交并取得保存回执；不将保存或关闭当成判对。")
        await asyncio.sleep(0.3)
        await adapter.close_question(page)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn(f"新版随堂练习提交未确认，保留现场：{Logger.summarize(exc)}")
