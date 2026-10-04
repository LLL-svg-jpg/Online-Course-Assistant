"""智慧树共享课的平时测试/考试页（stuExamWeb）。

平时测试可按章节或整个列表作答并提交；考试仍使用独立控制。
"""
from __future__ import annotations

import asyncio
import base64
import re
from urllib.parse import urlsplit

from playwright.async_api import Page

from .answer.ai import match_option
from .answer.base import AnswerProvider, AnswerResult, Option, Question
from .answer.cache import AnswerCache
from .logger import Logger

logger = Logger()
ROW_SEL = ".examPaper_subject:visible"
OPTION_SEL = ".subject_node .nodeLab"
NEXT_SEL = "div.examPaper_box > div.switch-btn-box > button:nth-child(2)"
HOMEWORK_LIST_API = "https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/student/getStudentHomework"
HOMEWORK_SUBMIT_API = "https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/answer/submit"
HOMEWORK_SAVE_API = "https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/answer/saveStudentAnswer"


def is_work_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    fragment = parsed.fragment.lower()
    return (host == "zhihuishu.com" or host.endswith(".zhihuishu.com")) and (
        "stuexamweb.html" in parsed.path.lower()
        and ("dohomework" in fragment or "doexamination" in fragment)
    )


def is_work_list_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (host == "zhihuishu.com" or host.endswith(".zhihuishu.com")) and \
        parsed.path.lower().endswith("/stuexamweb.html") and \
        parsed.fragment.split("?", 1)[0].rstrip("/").lower() == "/webexamlist"


def is_homework_url(url: str) -> bool:
    return is_work_url(url) and bool(re.fullmatch(
        r"/webexamlist/dohomework(?:/[^?#]*)?", urlsplit(url).fragment.split("?", 1)[0], re.I
    ))


async def _load_homeworks(page: Page, list_url: str, flag: int = 1):
    async with page.expect_response(
        lambda response: response.url.split("?", 1)[0] == HOMEWORK_LIST_API
        and response.request.method == "POST",
        timeout=45000,
    ) as pending:
        if flag == 1:
            if page.url == list_url:
                await page.reload(wait_until="domcontentloaded", timeout=45000)
            else:
                await page.goto(list_url, wait_until="domcontentloaded", timeout=45000)
        else:
            await page.get_by_text("已提交", exact=True).first.click(timeout=5000)
    data = await (await pending.value).json()
    if str(data.get("status")) != "200" or str(data.get("code")) in ("-1", "-2", "-3"):
        raise ValueError("平时测试列表没有有效业务回执")
    rt = data.get("rt", {})
    rows = rt.get("studentHomeworkList", [] if str(rt.get("next")) == "-1" else None)
    if not isinstance(rows, list):
        raise ValueError("平时测试列表结构未识别")
    root = await page.wait_for_function("""([flag, ids]) => {
        return [...document.querySelectorAll('*')].find(el => {
            const d = el.__vue__?.$data;
            const rows = d?.workLists?.StudentHomework;
            return Number(d?.flag) === flag && Array.isArray(rows)
                && JSON.stringify(rows.map(row => String(row.id))) === JSON.stringify(ids);
        });
    }""", arg=[flag, [str(row["id"]) for row in rows]], timeout=10000)
    handles = await root.as_element().query_selector_all(":scope > ul > li")
    if len(handles) != len(rows):
        raise ValueError("平时测试列表数据与页面条目不一致")
    return rows, handles


async def study_tests(page: Page, provider: AnswerProvider | None, cache: AnswerCache,
                      use_cache: bool, should_stop, target_rank: int | None = None) -> bool:
    """只处理平时测试；列表入口处理全部，课程入口按章号匹配。"""
    list_url = page.url
    visited: set[str] = set()
    failed = False
    try:
        if is_homework_url(page.url):
            return await study_work(page, provider, cache, use_cache, True, should_stop)
        if not is_work_list_url(page.url):
            logger.warn("平时测试入口不是测试列表或作答页，未操作考试。")
            return False
        for _ in range(300):
            if should_stop():
                return False
            rows, handles = await _load_homeworks(page, list_url)
            candidates = [(row, handle) for row, handle in zip(rows, handles)
                          if (target_rank is None or row.get("chapterRank") == target_rank)
                          and str(row["id"]) not in visited]
            if not candidates:
                if failed or any(str(row["id"]) in visited for row in rows):
                    logger.warn("其余平时测试已处理，仍有未确认完成的测试；失败题页保留供人工检查。")
                    return False
                if target_rank is None:
                    logger.info("平时测试未提交列表已空，考试入口已跳过。", shift=True)
                    return True
                submitted, _ = await _load_homeworks(page, list_url, 2)
                return any(row.get("chapterRank") == target_rank for row in submitted)
            row, handle = candidates[0]
            visited.add(str(row["id"]))
            if row.get("reviewType", 0) != 0:
                logger.warn("平时测试需要互评，留给人工处理。")
                if target_rank is not None:
                    return False
                failed = True
                continue
            title = await handle.query_selector(".course_ewname")
            if not title or " ".join((await title.inner_text()).split()) != \
                    " ".join(str(row.get("examName", "")).split()):
                logger.warn("平时测试标题与页面条目不一致，未点击。")
                return False
            if should_stop():
                return False
            logger.info(f"正在处理平时测试：{row['examName']}", shift=True)
            before = set(page.context.pages)
            await title.click(timeout=5000)
            work_page = page
            for _ in range(60):
                if should_stop():
                    return False
                opened = [tab for tab in page.context.pages if tab not in before]
                if opened:
                    work_page = opened[-1]
                    break
                if page.url != list_url:
                    break
                await asyncio.sleep(0.25)
            try:
                await work_page.wait_for_url(lambda url: is_homework_url(str(url)), timeout=15000)
                processed = await study_work(work_page, provider, cache, use_cache, True, should_stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warn(f"本份平时测试未完成，保留题页并继续后续测试：{Logger.summarize(exc)}")
                processed = False
            if should_stop():
                return False
            if not processed:
                if target_rank is not None:
                    return False
                failed = True
                if work_page is page:
                    page = await page.context.new_page()
                continue
            if work_page is not page:
                await work_page.close()
        logger.warn("平时测试列表处理达到本轮上限，未确认全部完成。")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn(f"平时测试处理未完成，保留页面供人工检查：{Logger.summarize(exc)}")
    return False


async def extract_questions(page: Page, capture_image: bool = True) -> list[Question]:
    questions: list[Question] = []
    rows = page.locator(ROW_SEL)
    for index in range(await rows.count()):
        row = rows.nth(index)
        title = row.locator(".subject_describe > div, .smallStem_describe > div:nth-child(2)").first
        if not await title.count():
            continue
        stem = " ".join((await title.inner_text()).split())
        if not stem:
            continue
        type_text = " ".join((await row.locator(".subject_type").first.inner_text()).split()) \
            if await row.locator(".subject_type").count() else ""
        qtype = ("multiple" if "多选" in type_text else
                 "judge" if "判断" in type_text else
                 "single" if "单选" in type_text else "unknown")
        options: list[Option] = []
        selected: list[str] = []
        labels = row.locator(OPTION_SEL)
        for option_index in range(await labels.count()):
            label = labels.nth(option_index)
            raw = " ".join((await label.inner_text()).split())
            match = re.match(r"^([A-Z])[.、．\s]+(.+)$", raw, re.I)
            key = match.group(1).upper() if match else chr(ord("A") + option_index)
            options.append(Option(key, match.group(2) if match else raw))
            checked = label.locator('input[type="radio"], input[type="checkbox"]').first
            if (await checked.count() and await checked.is_checked()) or \
                    "is-checked" in ((await label.get_attribute("class")) or "").split():
                selected.append(key)
        image_data_url = ""
        if capture_image:
            try:
                image_data_url = "data:image/png;base64," + base64.b64encode(
                    await row.screenshot(type="png")
                ).decode("ascii")
            except Exception:
                pass
        questions.append(Question(stem=stem, options=options, qtype=qtype,
                                  context="exam", index=index,
                                  selected_keys=selected, image_data_url=image_data_url))
    return questions


async def fill_answer(page: Page, question: Question, keys: list[str]) -> bool:
    wanted = {key.upper() for key in keys}
    if not wanted or not wanted <= {option.key for option in question.options}:
        return False
    if question.qtype != "multiple" and len(wanted) != 1:
        return False
    row = page.locator(ROW_SEL).nth(question.index)
    labels = row.locator(OPTION_SEL)
    # 页面既有答案不能覆盖，也不能靠最后的高亮假装写入成功。
    for index in range(await labels.count()):
        checked = labels.nth(index).locator('input[type="radio"], input[type="checkbox"]').first
        if await checked.count() and await checked.is_checked():
            return False
    for index, option in enumerate(question.options):
        if option.key in wanted:
            await labels.nth(index).click(timeout=3000)
    await asyncio.sleep(0.3)
    selected: set[str] = set()
    for index, option in enumerate(question.options):
        checked = labels.nth(index).locator('input[type="radio"], input[type="checkbox"]').first
        if await checked.count() and await checked.is_checked():
            selected.add(option.key)
    return selected == wanted


async def _save_answer_cards(page: Page, count: int) -> bool:
    """共享课逐题页通过逐项打开答题卡并点“下一题”写入答案。"""
    cards = page.locator(".answerCard_list ul li")
    if await cards.count() != count:
        return False
    for index in range(count):
        await cards.nth(index).click(timeout=3000)
        await asyncio.sleep(0.2)
        next_button = page.locator(NEXT_SEL).first
        if not await next_button.count() or not await next_button.is_enabled():
            return False
        await next_button.click(timeout=3000)
        await asyncio.sleep(0.2)
    return True


async def _submit_work(page: Page, label: str, should_stop) -> bool | None:
    if should_stop():
        return False
    before_url = page.url
    button = page.get_by_role("button", name=re.compile(r"^(提交作业|提交|交卷|提交试卷)$")).first
    if not await button.count() or not await button.is_visible() or not await button.is_enabled():
        logger.warn("未找到明确提交按钮，保留页面供人工提交。")
        return False
    receipt = None
    if is_homework_url(page.url):
        receipt = asyncio.create_task(page.wait_for_event(
            "response", predicate=lambda response: response.url.split("?", 1)[0] == HOMEWORK_SUBMIT_API
            and response.request.method == "POST", timeout=15000,
        ))
        await asyncio.sleep(0)
    try:
        await button.click(timeout=3000)
        dialog = page.locator(".el-message-box__wrapper:visible, .el-dialog__wrapper:visible") \
            .filter(has_text=re.compile(r"提交|交卷")).last
        for _ in range(20):
            if should_stop():
                return False
            if await dialog.count() or (receipt is not None and receipt.done()):
                break
            await asyncio.sleep(0.25)
        if await dialog.count():
            if receipt is not None and re.search(r"还有\s*[1-9]\d*\s*道题未作答", await dialog.inner_text()):
                cancel = dialog.get_by_role("button", name="取消", exact=True).first
                if not await cancel.count() or not await cancel.is_visible() or should_stop():
                    return False
                await cancel.click(timeout=3000)
                await dialog.wait_for(state="hidden", timeout=3000)
                logger.warn("网站提示有漏答题，返回题目补漏，不确认提交。")
                return None
            confirm = dialog.get_by_role("button", name=re.compile(r"^(确定|确认|确认提交|提交)$")).first
            if await confirm.count() and await confirm.is_visible() and not should_stop():
                await confirm.click(timeout=3000)
        if receipt is not None:
            data = await (await receipt).json()
            accepted = str(data.get("status")) == "200" and str(data.get("rt", {}).get("statu")) in ("0", "1", "-1")
            if accepted:
                logger.info("智慧树平时测试已取得服务器提交回执。", shift=True)
            else:
                logger.warn("智慧树平时测试提交未被服务器确认，保留页面供人工检查。")
            return accepted and not should_stop()
        await asyncio.sleep(0.8)
    finally:
        if receipt is not None:
            if not receipt.done():
                receipt.cancel()
            await asyncio.gather(receipt, return_exceptions=True)
    text = (await page.locator("body").inner_text())[:1000]
    if page.url != before_url or any(word in text for word in ("提交成功", "已交卷", "已提交")):
        logger.info(f"智慧树{label}页面显示已提交；请在成绩/结果页最终核对。", shift=True)
        return True
    else:
        logger.warn(f"已点击智慧树{label}提交，但未看到成功回执；页面留供人工核对。")
    return False


async def study_work(page: Page, provider: AnswerProvider | None, cache: AnswerCache,
                     use_cache: bool, auto_submit: bool, should_stop) -> bool:
    """从已经打开的作业/考试题页作答，不代点“开始考试”。"""
    homework_url = page.url if is_homework_url(page.url) else None

    def stopped():
        return should_stop() or (homework_url is not None and page.url != homework_url)

    for attempt in range(3):
        if stopped():
            return False
        result = await _study_work_round(page, provider, cache, use_cache, auto_submit, stopped)
        if result is not None:
            return result
        if stopped():
            return False
        if attempt == 2:
            save = page.get_by_role("button", name=re.compile(r"^(保存|保存答案)$")).first
            if await save.count() and await save.is_visible() and await save.is_enabled():
                async with page.expect_response(
                    lambda response: response.url.split("?", 1)[0] == HOMEWORK_SAVE_API
                    and response.request.method == "POST", timeout=15000,
                ) as pending:
                    await save.click(timeout=3000)
                data = await (await pending.value).json()
                if str(data.get("status")) == "200" and str(data.get("rt", {}).get("statu")) == "1":
                    logger.info("连续三轮仍有漏题，已取得保存回执；本测试不提交，保留题页并继续后续任务。", shift=True)
                else:
                    logger.warn("连续三轮仍有漏题，保存未被确认；不提交，保留题页供人工检查。")
            else:
                logger.warn("连续三轮仍有漏题，没有明确保存按钮；不提交，保留题页供人工检查。")
            return False
        first = page.locator(".answerCard_list ul li").first
        if not await first.count():
            return False
        logger.info(f"平时测试补漏检查（第 {attempt + 2}/3 轮），保留已有答案。")
        if not await first.is_visible():
            await page.locator(".answerCard_tit").first.click(timeout=3000)
        await first.click(timeout=3000)
        await asyncio.sleep(0.3)
    return False


async def _study_work_round(page: Page, provider: AnswerProvider | None, cache: AnswerCache,
                           use_cache: bool, auto_submit: bool, should_stop) -> bool | None:
    try:
        await page.locator(ROW_SEL).first.wait_for(state="visible", timeout=12000)
    except Exception:
        logger.warn("智慧树测试/考试题目未出现；可能还在列表、需要验证或页面结构不同。")
        return False
    is_exam = "doexamination" in urlsplit(page.url).fragment.lower()
    label = "考试" if is_exam else "平时测试"
    logger.info(f"已识别智慧树{label}；只填写可识别的选择题。", shift=True)
    seen: set[tuple[str, ...]] = set()
    total = answered = 0
    paged = False
    final_saved = False
    for _ in range(300):
        if should_stop():
            return False
        questions = await extract_questions(page, capture_image=bool(provider))
        signature = tuple(question.fingerprint for question in questions)
        if not signature or signature in seen:
            logger.warn("测试/考试没有出现新题，停止翻页并保留页面供人工检查。")
            break
        seen.add(signature)
        for question in questions:
            if should_stop():
                return False
            total += 1
            if question.selected_keys:
                answered += 1
                logger.info(f"{label}第 {total} 题已有答案，保留不覆盖。")
                continue
            if not question.is_choice:
                logger.warn(f"{label}第 {total} 题题型未识别，留给人工。")
                continue
            result = cache.get(question) if use_cache else None
            if result is None and provider:
                solve_task = asyncio.create_task(provider.solve(question))
                try:
                    while not solve_task.done():
                        if should_stop():
                            return False
                        await asyncio.wait({solve_task}, timeout=0.25)
                    result = await solve_task
                finally:
                    if not solve_task.done():
                        solve_task.cancel()
                        await asyncio.gather(solve_task, return_exceptions=True)
            if should_stop():
                return False
            if not isinstance(result, AnswerResult) or result.empty:
                logger.warn(f"{label}第 {total} 题没有参考答案，留空。")
                continue
            keys = match_option(result, question)
            if question.qtype == "multiple" and len(keys) < 2:
                logger.warn(f"{label}第 {total} 题为多选，答案不足两项，留空。")
                continue
            if keys and await fill_answer(page, question, keys):
                answered += 1
                logger.info(f"{label}第 {total} 题已选中：{'+'.join(keys)}")
            else:
                logger.warn(f"{label}第 {total} 题未能确认页面选中状态，请人工核对。")
        if should_stop():
            return False
        next_button = page.locator(".switch-btn-box").get_by_role("button", name="下一题", exact=True).first
        if (len(questions) != 1 or not await next_button.count()
                or not await next_button.is_visible() or not await next_button.is_enabled()):
            save_button = page.get_by_role("button", name=re.compile(r"^(保存|暂存|保存答案)$")).first
            if should_stop():
                return False
            if await save_button.count() and await save_button.is_visible() and await save_button.is_enabled():
                await save_button.click(timeout=3000)
                final_saved = True
                logger.info("已点击页面的保存按钮；服务器是否落盘仍需在网页核对。")
            elif len(questions) == 1:
                logger.warn("最后一题没有明确保存按钮；页面选中不等于服务器已保存，请人工核对。")
            break
        paged = True
        if should_stop():
            return False
        await next_button.click(timeout=3000)  # 共享课逐题页靠“下一题”保存本题
        for _ in range(20):
            if should_stop():
                return False
            await asyncio.sleep(0.25)
            current = await extract_questions(page, capture_image=False)
            if current and tuple(q.fingerprint for q in current) != signature:
                break
        else:
            logger.warn("点击下一题后题目未变；最后一题是否保存需人工确认。")
            break
    logger.info(f"智慧树{label}本轮查看 {total} 题，确认选中 {answered} 题。", shift=True)
    if should_stop():
        return False
    if auto_submit:
        card_count = await page.locator(".answerCard_list ul li").count()
        if not is_exam and paged and not final_saved and answered == total:
            final_saved = await _save_answer_cards(page, total)
        if should_stop():
            return False
        if (total == 0 or answered != total or card_count != total
                or (paged and (is_exam or not final_saved))):
            logger.warn("无法确认整卷题数与已答题数一致，取消自动提交。")
            if is_homework_url(page.url) and total > 0 and card_count == total and answered < total:
                return None
        else:
            submitted = await _submit_work(page, label, should_stop)
            if not is_exam:
                return submitted
        if not is_exam:
            return False
    else:
        logger.info("未自动交卷/提交，请在网页核对并人工操作。")
    return True
