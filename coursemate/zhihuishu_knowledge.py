"""智慧树新版知识点练习：免考跳过，仅用 AI 作答，提交以网站回执为准。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

from .answer.ai import match_option
from .answer.base import AnswerResult, Option, Question, normalize
from .logger import Logger
from .paths import user_file

logger = Logger()
MENU = "#current-node-box .node-hover-list .item"
QUESTIONS = ".exam .exam-item .question-item"
SUBMIT_API = "https://aistudy.zhihuishu.com/gateway/t/v1/exam/submit"


class KnowledgeFeedback:
    """仅记录网站逐题判定；按题干和选项内容匹配，避免换序后重犯。"""
    def __init__(self, path=None):
        self.path = path if path is not None else user_file("runtime", "knowledge-feedback.json")
        self.items = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def identity(question):
        content = [normalize(question.stem), question.qtype,
                   sorted(normalize(option.text) for option in question.options)]
        return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def selection(question, keys):
        return sorted(normalize(opt.text) for opt in question.options if opt.key in keys)

    def correct(self, question):
        texts = self.items.get(self.identity(question), {}).get("correct", [])
        keys = [opt.key for opt in question.options if normalize(opt.text) in texts]
        return keys if texts and len(keys) == len(texts) else []

    def rejected(self, question, keys):
        return self.selection(question, keys) in self.items.get(self.identity(question), {}).get("wrong", [])

    def hint(self, question):
        wrong = self.items.get(self.identity(question), {}).get("wrong", [])
        return "\n".join("已被网站判错的完整选项组合：" + "；".join(texts) for texts in wrong)

    def record(self, question, correct):
        choice = self.selection(question, question.selected_keys)
        if not choice:
            return
        item = self.items.setdefault(self.identity(question), {"wrong": []})
        if correct:
            item["correct"] = choice
            item["wrong"] = [old for old in item["wrong"] if old != choice]
        else:
            if choice not in item["wrong"]:
                item["wrong"].append(choice)
            if item.get("correct") == choice:
                item.pop("correct")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".pending")
        pending.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(self.path)


def is_knowledge_url(url: str) -> bool:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    return (parsed.hostname == "wisdom-mooc.zhihuishu.com" and parsed.path == "/exam"
            and all(query.get(key) for key in ("knowledgeId", "idStr", "recruitAndCourseId")))


async def pending_entries(page) -> list[int]:
    pending = []
    rows = page.locator(MENU)
    for index in range(await rows.count()):
        button = rows.nth(index).locator(".mastery-btn")
        status = await button.inner_text()
        classes = await button.get_attribute("class") or ""
        if "免考" in status or "mastery-btn-free-exam" in classes:
            continue
        if re.search(r"(?<![\d.])100(?:\.0+)?\s*%", status):
            continue
        pending.append(index)
    return pending


async def read_question(row, index: int) -> Question:
    kind = await row.locator(".quest-type").inner_text()
    qtype = ("multiple" if "多选" in kind else "judge" if "判断" in kind
             else "single" if "单选" in kind else "unknown")
    stem = " ".join((await row.locator(".quest-title .option-name").inner_text()).split())
    options, selected = [], []
    labels = row.locator("label.el-radio, label.el-checkbox")
    for i in range(await labels.count()):
        label = labels.nth(i)
        key = chr(65 + i)
        options.append(Option(key, " ".join((await label.locator(".preStyle").inner_text()).split())))
        if await label.locator("input").is_checked():
            selected.append(key)
    return Question(stem, options, qtype=qtype, context="chapter", index=index,
                    selected_keys=selected)


async def solve_practice(page, provider, should_stop, feedback=None, label="") -> bool:
    prefix = f"知识点练习 [{label}]" if label else "知识点练习"
    if not is_knowledge_url(page.url) or provider is None or should_stop():
        return False
    await page.locator(QUESTIONS).first.wait_for(state="attached", timeout=20000)
    rows = page.locator(QUESTIONS)
    total = await rows.count()
    if not total or await page.locator(".exam .answer-card .list .item").count() != total:
        return False
    fingerprints = []
    for index in range(total):
        if should_stop() or not is_knowledge_url(page.url):
            return False
        # 题号卡可回到漏题，之后仍通过网站的“下一题”逐题推进。
        row = rows.nth(index)
        if not await row.is_visible():
            await page.locator(".exam .answer-card .list .item").nth(index).click(timeout=3000)
        question = await read_question(row, index)
        fingerprints.append(question.fingerprint)
        if not question.is_choice or not question.stem:
            logger.warn(f"{prefix}：题型未识别，未提交，保留练习页。")
            return False
        known = feedback.correct(question) if feedback is not None else []
        if known:
            result = AnswerResult(option_keys=known, source="网站已判对记录")
        else:
            question.feedback = feedback.hint(question) if feedback is not None else ""
            task = asyncio.create_task(provider.solve(question))
            try:
                while not task.done():
                    if should_stop():
                        return False
                    await asyncio.wait({task}, timeout=0.25)
                result = await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if should_stop() or not isinstance(result, AnswerResult):
            return False
        keys = set(match_option(result, question))
        valid = {opt.key for opt in question.options}
        if (not keys or not keys <= valid or
                (question.qtype != "multiple" and len(keys) != 1)):
            logger.warn(f"{prefix}：AI 未给出可匹配答案，未提交，保留练习页。")
            return False
        if feedback is not None and feedback.rejected(question, keys):
            logger.warn(f"{prefix}：AI 仍返回网站已判错的选项组合，已拦截，未重复提交，保留练习页。")
            return False
        if (await read_question(row, index)).fingerprint != question.fingerprint:
            return False
        labels = row.locator("label.el-radio, label.el-checkbox")
        for i, option in enumerate(question.options):
            if should_stop():
                return False
            checked = await labels.nth(i).locator("input").is_checked()
            if (option.key in keys) != checked and (question.qtype == "multiple" or option.key in keys):
                await labels.nth(i).click(timeout=3000)
        if set((await read_question(row, index)).selected_keys) != keys:
            return False
        source = "网站已判对记录" if known else "AI"
        logger.info(f"{prefix}：{source}已填写第 {index + 1}/{total} 题。")
        if index + 1 < total:
            await page.locator(".exam .next-btn:not(.disabled)").click(timeout=3000)
            await rows.nth(index + 1).wait_for(state="visible", timeout=3000)

    # 所有题的原题身份与原生选中状态均需确认，不能把点击成功当成作答成功。
    if await rows.count() != total:
        return False
    for index in range(total):
        question = await read_question(rows.nth(index), index)
        if question.fingerprint != fingerprints[index] or not question.selected_keys:
            return False
    if should_stop():
        return False
    try:
        async with page.expect_response(
            lambda r: r.url.split("?", 1)[0] == SUBMIT_API and r.request.method == "POST",
            timeout=15000,
        ) as receipt:
            await page.locator(".exam .header .submit").click(timeout=3000)
        data = await (await receipt.value).json()
        if str(data.get("code")) != "200" or not data.get("data"):
            return False
        await page.wait_for_url("**/pointOfMastery/**", timeout=15000)
        logger.info(f"{prefix}：{total} 题取得网站提交成功回执；正确率以网站结果为准。")
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warn(f"{prefix}：提交未确认（可能仍有漏题），未确认强交，保留练习页。")
        return False


async def read_review(page, feedback, should_stop) -> bool:
    await page.locator(".line1-count-link").click(timeout=5000)
    await page.wait_for_url("**/examAnalysis?**", timeout=15000)
    if urlsplit(page.url).hostname != "wisdom-mooc.zhihuishu.com":
        return False
    rows = page.locator(".exam-preview .question-item")
    await rows.first.wait_for(state="visible", timeout=15000)
    for index in range(await rows.count()):
        if should_stop():
            return False
        row = rows.nth(index)
        question = await read_question(row, index)
        verdict = row.locator(".question-result")
        text = await verdict.inner_text()
        if "回答错误" in text:
            feedback.record(question, False)
        elif "回答正确" in text:
            feedback.record(question, True)
        else:
            return False
    feedback.save()
    return True


async def retry_practice(page, provider, should_stop, feedback, label) -> bool:
    for attempt in range(1, 4):
        current = f"{label}／第 {attempt}/3 次"
        if not await solve_practice(page, provider, should_stop, feedback, current):
            return False
        rate = page.locator(".charts-label-rate")
        await rate.wait_for(state="visible", timeout=15000)
        # 结果组件先出现，统计接口随后才填入题数/掌握度；不能读取 NaN/占位值。
        try:
            await page.wait_for_function(r"""() => {
                const rate = document.querySelector('.charts-label-rate')?.textContent.trim();
                const counts = [...document.querySelectorAll('.line1-count-total-num')]
                    .map(e => Number(e.textContent.trim()));
                return /^\d+(?:\.\d+)?\s*%$/.test(rate || '') && counts.length === 2
                    && counts[0] > 0 && counts[1] >= 0 && counts[1] <= counts[0];
            }""", timeout=15000)
        except Exception:
            logger.warn(f"知识点练习 [{current}]：网站结果统计未就绪，保留页面。")
            return False
        value = (await rate.inner_text()).replace(" ", "").strip()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", value)
        if not match or not 0 <= float(match[1]) <= 100:
            logger.warn(f"知识点练习 [{current}]：未识别网站掌握度，保留页面。")
            return False
        score = float(match[1])
        logger.info(f"知识点练习 [{current}]：网站掌握度 {score:g}%。")
        if should_stop():
            return False
        if await page.locator(".line1-count-link").count():
            if not await read_review(page, feedback, should_stop):
                return False
        elif score < 100:
            logger.warn(f"知识点练习 [{current}]：无法取得逐题反馈，停止重做以免重复错答，保留页面。")
            return False
        if score == 100:
            return True
        if attempt == 3:
            logger.warn(f"知识点练习 [{label}]：已作答 3 次仍未到 100%，跳过并继续后续任务。")
            return True
        if should_stop():
            return False
        await page.locator(".exam-preview .header .submit").click(timeout=5000)
        await page.wait_for_url(is_knowledge_url, timeout=15000)
    return False


async def process_lesson(page, adapter, lesson, provider, should_stop, feedback=None) -> bool:
    """独立页面执行知识点；失败页留在原位，主课程可继续后续任务。"""
    from .platforms.zhihuishu import ZhihuishuAdapter, player_pointer

    if provider is None or should_stop():
        return False
    active_key = await adapter.active_lesson_key(page)
    # 暂停仍保留学习会话；先卸载原播放器，再让练习页取得课程会话。
    practice = None
    succeeded = False
    label = lesson.title
    stage = "打开独立课程页"
    child = ZhihuishuAdapter()
    child._navigation_owner = adapter
    try:
        await page.goto("about:blank")
        practice = await page.context.new_page()
        feedback = feedback if feedback is not None else KnowledgeFeedback()
        if await child.open_course(practice, adapter.course_url) in ("未登录", "打开失败"):
            return False
        await child.prepare_page(practice)
        current = next((item for item in await child.list_lessons(practice)
                        if item.key == lesson.key), None)
        if current is None or should_stop() or not await child.enter_lesson(practice, current):
            return False
        stage = "暂停练习前视频"
        await pause_video(practice)
        stage = "等待知识点菜单"
        await practice.locator("#current-node-box").wait_for(state="attached", timeout=15000)
        # 菜单数据异步加载；播放器菜单出现前不能把空列表认作免考。
        await practice.locator(MENU).first.wait_for(state="attached", timeout=15000)
        pending = await pending_entries(practice)
        for number, index in enumerate(pending):
            if should_stop():
                return False
            stage = "打开知识点菜单"
            await player_pointer(practice, practice.locator("#vjs_container"), hover=True)
            await player_pointer(practice, practice.locator("#current-node-box .title-box"), hover=True)
            button = practice.locator(MENU).nth(index).locator(".mastery-btn")
            if "免考" in await button.inner_text():
                continue
            title = practice.locator(MENU).nth(index).locator(".title")
            name = (await title.get_attribute("title") or await title.inner_text()) if await title.count() else f"知识点 {index + 1}"
            label = f"{lesson.title}／{name}"
            stage = "进入知识点练习"
            await player_pointer(practice, button, reveal=practice.locator("#current-node-box .title-box"))
            await practice.wait_for_url(is_knowledge_url, timeout=15000)
            knowledge = parse_qs(urlsplit(practice.url).query)["knowledgeId"][0]
            if knowledge not in adapter._knowledge_done:
                stage = "作答知识点练习"
                if not await retry_practice(practice, provider, should_stop, feedback, label):
                    return False
                adapter._knowledge_done.add(knowledge)
            if number + 1 == len(pending):
                break
            # 重做会叠加结果/解析历史，回原课程并重新定位当前视频。
            stage = "返回课程继续下一知识点"
            if await child.open_course(practice, adapter.course_url) in ("未登录", "打开失败"):
                return False
            current = next((item for item in await child.list_lessons(practice)
                            if item.key == lesson.key), None)
            if current is None or not await child.enter_lesson(practice, current):
                return False
            await practice.locator(MENU).first.wait_for(state="attached", timeout=15000)
            await pause_video(practice)
        succeeded = True
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warn(f"知识点练习 [{label}] 在{stage}未完成：{Logger.summarize(exc)}")
        logger.debug(f"知识点练习详情 [{label}／{stage}]：{exc}")
        return False
    finally:
        # 仅保留有答题内容的失败页，不能留下另一个课程播放器与主页面抢会话。
        if practice is not None and (succeeded or should_stop() or child._on_mooc_course(practice)):
            await practice.close()
        manual = adapter._manual_lesson_key
        if not should_stop() and not (manual and adapter._on_mooc_course(page)):
            if await child.open_course(page, adapter.course_url) in ("未登录", "打开失败"):
                raise RuntimeError("知识点练习后恢复课程页失败")
            current = next((item for item in await child.list_lessons(page)
                            if item.key == (adapter._manual_lesson_key or active_key)), None)
            if current is None or not await child.enter_lesson(page, current):
                raise RuntimeError("知识点练习后恢复原小节失败")
            if current.finished:
                await pause_video(page)


async def pause_video(page) -> None:
    from .platforms.zhihuishu import player_pointer

    video = page.locator("video").first
    if not await video.count():
        return
    await video.evaluate("v => { v.muted = true; v.volume = 0; }")
    if not await video.evaluate("v => v.paused"):
        saves = []
        def on_request(request):
            url = urlsplit(request.url)
            if (url.hostname == "studyservice-api.zhihuishu.com"
                    and url.path.endswith(("/learning/saveDatabaseIntervalTimeV2",
                                           "/learning/saveCacheIntervalTimeV2"))):
                saves.append(request)
        page.on("request", on_request)
        try:
            await player_pointer(page, page.locator("#vjs_container"), hover=True)
            if not await video.evaluate("v => v.paused"):
                await player_pointer(page, page.locator("#playButton"))
            await page.wait_for_function("() => document.querySelector('video')?.paused", polling=100, timeout=3000)
            # 原生暂停会保存进度，等这次保存结束再打开下一播放器，避免旧请求迟到。
            for request in saves:
                response = await asyncio.wait_for(request.response(), timeout=15000)
                if response is not None:
                    await asyncio.wait_for(response.body(), timeout=15000)
        finally:
            page.remove_listener("request", on_request)
