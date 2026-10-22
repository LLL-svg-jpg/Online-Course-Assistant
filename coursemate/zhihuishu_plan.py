"""智慧树每日学习与课程提问计划；本机动作不替代网站计分。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse
from playwright.async_api import Error as PlaywrightError

from .logger import Logger
from .paths import user_file
from .zhihuishu_knowledge import pause_video
from .zhihuishu_qa import publish_questions
from .zhihuishu_answers import publish_answers

logger = Logger()
BEIJING = timezone(timedelta(hours=8))


def now():
    return datetime.now(BEIJING)


class PlanStore:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else user_file("runtime", "zhihuishu-plan.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.pending = False

    def course(self, account, url):
        course_id = parse_qs(urlparse(url).query).get("recruitAndCourseId", [""])[0]
        if not course_id:
            raise ValueError("课程地址缺少课程身份，未建立学习计划")
        identity = json.dumps([account["id"], account.get("username", ""), course_id], ensure_ascii=False)
        key = hashlib.sha256(identity.encode()).hexdigest()
        return self.data.setdefault(key, {"days": {}, "questions": []})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def playback_seconds(previous, current, interval):
    """用连续媒体时间核对真实播放，暂停/跳播/换媒体/长时间失去观测不计。"""
    if not previous or not current or not 0 < interval <= 5:
        return 0.0
    if (previous["identity"] != current["identity"] or previous["paused"] or current["paused"]
            or previous["ready"] < 2 or current["ready"] < 2):
        return 0.0
    delta = current["time"] - previous["time"]
    rate = max(previous["rate"], current["rate"], .1)
    if not 0 < delta <= interval * rate * 1.8 + .5:
        return 0.0
    return min(interval, delta / rate)


async def open_menu_page(page, label):
    choice = page.get_by_text(label, exact=True)
    if not await choice.count() or not await choice.first.is_visible():
        await page.get_by_text("菜单", exact=True).first.click(timeout=5000)
    async with page.expect_popup(timeout=15000) as opened:
        await page.get_by_text(label, exact=True).click(timeout=5000)
    result = await opened.value
    await result.wait_for_load_state("domcontentloaded", timeout=30000)
    return result


def integer(value, name):
    number = int(value)
    if number < 0:
        raise ValueError(f"网站{name}为负数，未采用")
    return number


class CoursePlan:
    def __init__(self, store, account, url, config):
        self.store, self.config = store, config
        self.record = store.course(account, url)
        self.title, self.topics = "", []
        self.minutes, self.remaining = 0, 0
        self.ready = False
        self.outside_period = False
        self.day = now().date().isoformat()
        self.limited = False
        self.tracking_error = None

    def quota_reached(self):
        return (self.ready and self.config.zhihuishu_daily_enabled and self.minutes > 0
                and self.watched() >= self.minutes * 60)

    def watched(self):
        return float(self.record["days"].get(now().date().isoformat(), {}).get("seconds", 0))

    def pause_reason(self):
        if not self.ready:
            return ""
        if self.tracking_error is not None:
            return "视频计时未能继续确认，先暂停供检查"
        if now().date().isoformat() != self.day:
            return "日期已变化，先暂停并重新核对网站规则"
        if now() > datetime.fromisoformat(self.record["period"][1]):
            return "课程学习周期已结束，先暂停"
        return "今日实际播放目标已达，剩余课件留待后续处理" if self.quota_reached() else ""

    async def track(self, page, adapter):
        previous, previous_tick, previous_day = None, time.monotonic(), now().date()
        last_save = previous_tick
        try:
            while True:
                current = None
                tick, date = time.monotonic(), now().date()
                if (self.ready and self.minutes and self.config.zhihuishu_daily_enabled
                        and not adapter._knowledge_busy and not adapter._playback_recovering
                        and adapter._on_mooc_course(page)):
                    try:
                        current = await page.evaluate("""() => {
                            const videos=[...document.querySelectorAll('video')];
                            const v=videos.length===1 ? videos[0] : null;
                            const key=document.querySelector('.catalog-com .current .item-num')?.textContent.trim();
                            return v && key ? {identity:key+'|'+v.currentSrc,time:v.currentTime,
                                paused:v.paused,rate:v.playbackRate,ready:v.readyState} : null;
                        }""")
                    except PlaywrightError as exc:
                        if page.is_closed():
                            raise
                        logger.debug(f"每日学习 [{self.title}]：本次媒体观测中断，未计时：{exc}")
                seconds = playback_seconds(previous, current, tick - previous_tick) if date == previous_day else 0
                if seconds:
                    day = self.record["days"].setdefault(date.isoformat(), {})
                    day["seconds"] = float(day.get("seconds", 0)) + seconds
                    if tick - last_save >= 10:
                        self.store.save()
                        last_save = tick
                previous, previous_tick, previous_day = current, tick, date
                await asyncio.sleep(.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.tracking_error = exc
            logger.log_exception(f"每日学习 [{self.title}]：视频计时中断，未记达标。", exc)
            raise
        finally:
            self.store.save()

    async def prepare(self, page, adapter, title, lessons):
        self.title = title
        self.topics = [lesson.title for lesson in lessons if lesson.kind == "video"]
        self.record["title"] = title
        report = None
        try:
            async with adapter._playback_lock:
                adapter._knowledge_busy = True
                await pause_video(page)
            report = await open_menu_page(page, "成绩分析")
            parsed = urlparse(report.url)
            if parsed.hostname != "stuonline.zhihuishu.com" or parsed.path != "/stuonline/stuLearnReportNew/index":
                raise RuntimeError("未到达可识别的本课程成绩分析页")
            params = parse_qs(parsed.query)
            self.record["report_identity"] = {k: params[k][0] for k in ("courseId", "recruitId")}
            link = await report.get_by_role("link", name="课程学习", exact=True).get_attribute("href")
            expected = parse_qs(urlparse(adapter.course_url).query).get("recruitAndCourseId")
            if not expected or parse_qs(urlparse(link or "").query).get("recruitAndCourseId") != expected:
                raise RuntimeError("成绩分析页与当前课程身份不一致")
            await report.get_by_role("link", name="查看详细计分规则", exact=True).click(timeout=5000)
            modal = report.locator('[id^="tm_dialog_win_"]').filter(has_text="学/习/时/间")
            await modal.wait_for(state="visible", timeout=15000)
            text = await modal.inner_text(timeout=5000)
            period = re.search(r"学/习/时/间\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*至\s*"
                               r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", text)
            if not period:
                raise RuntimeError("未读到课程学习起止时间，未推断可学习或可提问")
            start, end = [datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
                          for value in period.groups()]
            end += timedelta(minutes=1) - timedelta(microseconds=1)
            self.record["period"] = [start.isoformat(), end.isoformat()]
            await modal.locator("#close_windowa").click(timeout=5000)
            await modal.wait_for(state="hidden", timeout=5000)
            if not start <= now() <= end:
                self.outside_period = True
                self.store.save()
                logger.warn(f"智慧树计划 [{title}]：不在课程学习周期内，未开始视频或提问。")
                return False
            if self.config.zhihuishu_daily_enabled:
                async with report.expect_response(lambda r: urlparse(r.url).path.rstrip("/").endswith(
                        "/stuLearnReportNew/loadStuHabit"), timeout=15000) as receipt:
                    await report.locator("#LR_StudyHabit_pag").click(timeout=5000)
                data = await (await receipt.value).json()
                habit = data.get("stuStudyRecord")
                if not isinstance(habit, dict):
                    raise RuntimeError("网站没有返回学习习惯规则，未使用加载中的零值")
                completed = integer(habit["studyDay"], "已学习天数")
                total = integer(habit["totalStudyDay"], "要求天数")
                self.remaining = max(0, total - completed)
                self.minutes = integer(habit["suggestTime"], "建议分钟数") if self.remaining else 0
                if self.remaining and not self.minutes:
                    raise RuntimeError("还需学习但未读到有效分钟数，未推断已经达标")
                self.record["habit"] = {"day": now().date().isoformat(), "completed": completed,
                                        "total": total, "minutes": self.minutes}
                self.store.pending |= self.remaining > 0
                logger.info(f"每日学习 [{title}]：网站已计 {completed}/{total} 天，尚需 {self.remaining} 天；"
                            f"今日本机实际播放 {self.watched()/60:.1f}/{self.minutes} 分钟，网站通常次日上午更新。")
            if self.config.zhihuishu_questions_enabled or getattr(self.config, "zhihuishu_answers_enabled", False):
                async with report.expect_response(lambda r: urlparse(r.url).path.rstrip("/").endswith(
                        "/stuLearnReportNew/loadCourseForum"), timeout=15000) as receipt:
                    await report.locator("#LRCourseForum_pag").click(timeout=5000)
                data = await (await receipt.value).json()
                interaction = data["qaStatisticsMap"]["myInteractionDto"]
                if self.config.zhihuishu_questions_enabled:
                    self.record["valid_questions"] = integer(interaction["effectiveQuestion"] or 0, "有效提问数")
                    self.record["invalid_questions"] = integer(interaction["ineffectQuestion"] or 0, "无效提问数")
                    self.store.pending |= self.record["valid_questions"] < self.config.zhihuishu_question_count
                    logger.info(f"AI 提问 [{title}]：网站已统计有效 {self.record['valid_questions']} 条、"
                                f"无效 {self.record['invalid_questions']} 条；本轮目标 {self.config.zhihuishu_question_count} 条。")
                if getattr(self.config, "zhihuishu_answers_enabled", False):
                    self.record["valid_answers"] = integer(interaction["effectiveAnswer"] or 0, "有效回答数")
                    self.record["invalid_answers"] = integer(interaction["ineffectAnswer"] or 0, "无效回答数")
                    self.store.pending |= self.record["valid_answers"] < self.config.zhihuishu_answer_count
                    logger.info(f"AI 回答 [{title}]：网站已统计有效 {self.record['valid_answers']} 条、"
                                f"无效 {self.record['invalid_answers']} 条；本轮目标 {self.config.zhihuishu_answer_count} 条。")
            self.ready = True
            self.day = now().date().isoformat()
            self.store.save()
            return True
        finally:
            if report is not None and not report.is_closed():
                await report.close()
            adapter._knowledge_busy = False

    async def finish(self, page, adapter, provider, clock, should_stop):
        if not self.ready or should_stop():
            return False
        if self.tracking_error is not None:
            async with adapter._playback_lock:
                adapter._knowledge_busy = True
                try:
                    await pause_video(page)
                finally:
                    adapter._knowledge_busy = False
            return False
        if now().date().isoformat() != self.day or now() > datetime.fromisoformat(self.record["period"][1]):
            async with adapter._playback_lock:
                adapter._knowledge_busy = True
                try:
                    await pause_video(page)
                finally:
                    adapter._knowledge_busy = False
            logger.info(f"智慧树计划 [{self.title}]：日期或学习周期变化，已暂停，未沿用旧规则继续发问。")
            return True
        logger.info(f"智慧树计划 [{self.title}]：课程检查结束，开始执行已勾选的计划。", shift=True)
        interactions_ok = True
        async with adapter._playback_lock:
            adapter._knowledge_busy = True
            await pause_video(page)
        try:
            if now().date().isoformat() != self.day or now() > datetime.fromisoformat(self.record["period"][1]):
                logger.info(f"智慧树计划 [{self.title}]：日期或学习周期变化，已暂停，等待重新核对。")
                return True
            # 先执行课程问答，避免提问和回答被长时间补时挡住。
            for enabled, publish, target, label in (
                    (self.config.zhihuishu_questions_enabled, publish_questions,
                     getattr(self.config, "zhihuishu_question_count", 5), "提问"),
                    (getattr(self.config, "zhihuishu_answers_enabled", False), publish_answers,
                     getattr(self.config, "zhihuishu_answer_count", 5), "回答")):
                if not enabled:
                    continue
                def stopped():
                    return (should_stop() or now().date().isoformat() != self.day
                            or now() > datetime.fromisoformat(self.record["period"][1]))
                if stopped():
                    interactions_ok = False
                    break
                qa = None
                success = False
                try:
                    # 每项使用独立问答页，失败表单留给用户检查，不挡住后续回答。
                    qa = await open_menu_page(page, "课程问答")
                    parsed = urlparse(qa.url)
                    route, _, query = parsed.fragment.partition("?")
                    identity = self.record["report_identity"]
                    if (parsed.hostname != "qah5.zhihuishu.com" or route.rstrip("/") != "/web/home/" + identity["courseId"]
                            or parse_qs(query).get("recruitId") != [identity["recruitId"]]):
                        raise RuntimeError("课程问答页与当前课程身份不一致，未发布")
                    await qa.locator(".ask-btn").wait_for(state="visible", timeout=15000)
                    await qa.get_by_role("heading", name=self.title, exact=True).wait_for(
                        state="visible", timeout=15000)
                    success = await publish(qa, provider, self.record, target, self.title, self.topics,
                        self.store.save, now().date().isoformat(), stopped)
                except Exception as exc:
                    success = False
                    logger.log_exception(f"AI {label} [{self.title}]：本轮未完成，继续独立的后续计划。", exc)
                finally:
                    if qa is not None and success and not qa.is_closed():
                        await qa.close()
                    elif qa is not None and not qa.is_closed():
                        logger.warn(f"AI {label} [{self.title}]：页面已保留供检查。")
                if not success:
                    interactions_ok = False
        finally:
            adapter._knowledge_busy = False

        if should_stop():
            return False
        if self.config.zhihuishu_daily_enabled and self.minutes and not self.quota_reached():
            if clock.reached(self.config.limit_max_minutes):
                logger.warn(f"每日学习 [{self.title}]：已达本轮限时，尚未满足网站时长，未记达标。")
                self.limited = True
            elif not await self.review(page, adapter, clock, should_stop):
                return False
        if self.minutes and self.quota_reached():
            # 补时结束后暂停播放器，再记录本机目标达成；不代替网站次日统计。
            async with adapter._playback_lock:
                adapter._knowledge_busy = True
                try:
                    await pause_video(page)
                finally:
                    adapter._knowledge_busy = False
            day = self.record["days"].setdefault(now().date().isoformat(), {})
            day["local_target_met"] = True
            self.store.save()
            logger.info(f"每日学习 [{self.title}]：今日实际播放已达 {self.minutes} 分钟，已暂停；"
                        "这里只确认本机播放，规律天数等待网站次日统计。", shift=True)
        return interactions_ok and not self.limited

    async def review(self, page, adapter, clock, should_stop):
        """通过正常目录及播放器复习，补足实际播放时间，不改完成状态/计时上报。"""
        from .platforms.zhihuishu import player_pointer

        adapter._daily_replay = True
        last_progress, last_seconds = time.monotonic(), self.watched()
        last_log = last_progress
        logger.info(f"每日学习 [{self.title}]：开始补足实际播放时长，已记录 "
                    f"{self.watched()/60:.1f}/{self.minutes} 分钟；已完成视频会正常复习。", shift=True)
        try:
            while not self.quota_reached():
                if should_stop():
                    return False
                if not self.config.zhihuishu_daily_enabled:
                    return True
                if clock.reached(self.config.limit_max_minutes):
                    self.limited = True
                    logger.warn(f"每日学习 [{self.title}]：已达本轮限时，网站每日时长尚未满足，未记达标。")
                    return False
                if now().date().isoformat() != self.day:
                    logger.info(f"每日学习 [{self.title}]：已跨天，结束本轮，重新核对网站规则。")
                    return True
                if adapter._knowledge_busy or adapter._playback_recovering:
                    last_progress = time.monotonic()
                    await asyncio.sleep(.5)
                    continue
                async with adapter._playback_lock:
                    # 与续播恢复、倍速调节串行，避免页面重载中误读空目录并抢点第一节。
                    if adapter._knowledge_busy or adapter._playback_recovering:
                        continue
                    if await adapter.detect_question(page) or await adapter.detect_captcha(page):
                        last_progress = time.monotonic()
                        await asyncio.sleep(.5)
                        continue
                    lessons = [item for item in await adapter.list_lessons(page) if item.kind == "video"]
                    active = await adapter.active_lesson_key(page)
                    manual = adapter._manual_lesson_key
                    if manual:
                        if manual != active:
                            await asyncio.sleep(.1)
                            continue
                        adapter._manual_lesson_key = ""
                        logger.info(f"每日学习 [{self.title}]：尊重手动选择，继续当前小节复习。")
                    index = next((i for i, item in enumerate(lessons) if item.key == active), None)
                    if index is None:
                        if not lessons or not await adapter.enter_lesson(page, lessons[0]):
                            return False
                    video = page.locator("video").first
                    state = await video.evaluate("v => ({ended:v.ended,hold:!!v.__coursemateManualHold})", timeout=5000)
                    if state["hold"]:
                        last_progress = time.monotonic()
                        await asyncio.sleep(.5)
                        continue
                    if state["ended"]:
                        if adapter._manual_lesson_key or await adapter.active_lesson_key(page) != active:
                            continue
                        if index is not None:
                            following = lessons[(index + 1) % len(lessons)]
                            if following.key != active:
                                if not await adapter.enter_lesson(page, following):
                                    return False
                        # 正常重播按钮会从结尾重新开始，不能通过修改 currentTime 伪造学习。
                        if await video.evaluate("v => v.ended", timeout=5000):
                            await player_pointer(page, page.locator(".vjs-play-control").first)
                seconds = self.watched()
                if time.monotonic() - last_log >= 60:
                    logger.info(f"每日学习 [{self.title}]：实际播放进度 {seconds/60:.1f}/{self.minutes} 分钟，"
                                "网站学习习惯得分待次日核对。")
                    last_log = time.monotonic()
                if seconds > last_seconds:
                    last_progress, last_seconds = time.monotonic(), seconds
                elif time.monotonic() - last_progress > 90:
                    logger.warn(f"每日学习 [{self.title}]：90秒未确认有效播放，保留页面，未记达标。")
                    return False
                await asyncio.sleep(.5)
            return True
        finally:
            adapter._daily_replay = False


async def wait_next_day(config, should_stop, round_day=None):
    base = round_day if round_day is not None else now()
    target = (base + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    logger.info(f"今日计划结束，等待北京时间 {target:%m-%d %H:%M} 重新核对并继续；点停止可结束等待。", shift=True)
    while now() < target:
        if (should_stop() or config.zhihuishu_daily_action != "wait"
                or not (config.zhihuishu_daily_enabled or config.zhihuishu_questions_enabled
                        or getattr(config, "zhihuishu_answers_enabled", False))):
            return False
        await asyncio.sleep(.5)
    return not should_stop()
