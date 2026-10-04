"""四个常驻后台协程。

这是"陪伴"的实质：主循环只管推进章节，这四个协程各自盯住一件事，
让播放不中断、题目被处理、异常被看见。

并发模型借鉴 Autovisor：每个协程独立轮询、独立容错，
任何一个挂掉都不会拖垮其余部分，由 task_monitor 统一上报。
"""
from __future__ import annotations

import asyncio

from playwright.async_api import Frame, Page

from .answer.ai import match_option
from .answer.base import AnswerProvider, AnswerResult
from .answer.cache import AnswerCache
from .answer.strategy import build_attempts, describe_attempt
from .config import Config
from .events import StudyClock, VerificationTimeout
from .verification import has_verification, wait_verification
from .logger import Logger
from .platforms.base import PlatformAdapter

logger = Logger()

_KEEP_PLAYING_JS = """
() => {
    const v = document.querySelector('video');
    if (!v) return false;
    v.__coursemateDoneStopped = false;

    const resume = () => {
        if (v.__coursemateManualHold || v.ended || !v.isConnected) return;
        const result = v.play();
        if (result && typeof result.catch === 'function') result.catch(() => {});
    };

    if (v.__coursemateManualHold) return false;

    // 平台可能每隔几秒主动 pause。只靠 Python 轮询续播会留下最多 2 秒的
    // 可见停顿；给当前 video 装一次事件守卫，暂停发生后立刻恢复。
    if (!v.__coursemateKeepPlaying) {
        v.__coursemateKeepPlaying = true;
        v.__coursemateResume = resume;
        v.addEventListener('pause', resume);
    }

    const shouldResume = v.paused && !v.ended;
    if (shouldResume) resume();
    return shouldResume;
}
"""

# 单个弹题的总耗时上限。超过就强行关窗继续播——
# 卡在一道题上不动，比这道题没答对严重得多。
QUESTION_TOTAL_TIMEOUT = 300

# 浏览器被用户关掉时，各协程应当安静退出而不是刷屏报错
_CLOSED_SIGNALS = (
    "Target closed",
    "Target page, context or browser has been closed",
    "Browser closed",
    "Connection closed",
)
# 高频轮询中的常规未命中，降级为 DEBUG，避免污染控制台
_EXPECTED_SIGNALS = (
    "waiting for locator",
    "waiting for selector",
    "No node found for selector",
    "Execution context was destroyed",
    "frame was detached",
    "Timeout",
)


def is_closed(exc: BaseException) -> bool:
    text = str(exc)
    return any(sig in text for sig in _CLOSED_SIGNALS)


def is_expected(exc: BaseException) -> bool:
    text = str(exc)
    return any(sig in text for sig in _EXPECTED_SIGNALS)


def _handle(exc: BaseException, who: str) -> bool:
    """统一的协程异常处理。返回 True 表示应当退出协程。"""
    if is_closed(exc):
        logger.debug(f"浏览器已关闭，{who} 停止运行。")
        return True
    if is_expected(exc):
        logger.debug(f"{who} 轮询未命中：{Logger.summarize(exc)}")
    else:
        logger.log_exception(f"{who} 执行异常。", exc)
    return False


async def ensure_playing(frame: Page | Frame) -> bool:
    """立即续播，并防止页面脚本造成肉眼可见的反复暂停。"""
    return bool(await frame.evaluate(_KEEP_PLAYING_JS))


async def stop_completed_playback(frame: Page | Frame) -> None:
    """视频任务已完成时撤销自动续播，避免重新打开后从头播放。"""
    await frame.evaluate("""() => {
        const v = document.querySelector('video');
        if (!v || v.__coursemateDoneStopped) return;
        if (v.__coursemateResume) v.removeEventListener('pause', v.__coursemateResume);
        v.__coursemateKeepPlaying = false;
        v.__coursemateDoneStopped = true;
        if (v.currentTime < 5 && !v.paused) v.pause();
    }""")


async def hold_playback_for_manual_check(
    page: Page, adapter: PlatformAdapter, holding: bool, reason: str = "captcha"
) -> None:
    """人工处理验证码或弹题时暂停视频，避免续播守卫把它重新拉起。"""
    try:
        frame = await adapter.video_frame(page)
        await frame.evaluate(
            """([holding, reason]) => {
                const v = document.querySelector('video');
                if (!v) return false;
                const reasons = v.__coursemateManualHoldReasons ||= new Set();
                if (holding) reasons.add(reason); else reasons.delete(reason);
                v.__coursemateManualHold = reasons.size > 0;
                if (v.__coursemateManualHold && !v.paused) v.pause();
                return true;
            }""",
            [holding, reason],
        )
    except Exception:
        # 验证页有时已跳离播放器；此时没有视频可暂停也不影响人工处理。
        pass


async def task_monitor(tasks: list[asyncio.Task]) -> None:
    """监控其余协程，任何一个异常退出都要让用户知道。"""
    reported: set[asyncio.Task] = set()
    while any(not t.done() for t in tasks):
        for task in tasks:
            if task.done() and task not in reported:
                reported.add(task)
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    name = getattr(task.get_coro(), "__name__", "后台任务")
                    logger.log_exception(f"后台任务 {name} 异常结束。", exc, shift=True)
        await asyncio.sleep(1)


async def playback_worker(page: Page, adapter: PlatformAdapter) -> None:
    """续播：视频被暂停就恢复。

    平台会因为弹窗、失焦、切集等原因暂停视频，没有这个协程，
    程序会安静地"挂"在那里，进度一动不动。
    """
    while True:
        try:
            frame = await adapter.video_frame(page)
            completed = getattr(adapter, "video_task_completed", None)
            if completed is not None and await completed(page):
                await stop_completed_playback(frame)
            elif getattr(adapter, "ui_playback", False):
                if await adapter.ensure_playing(page):
                    logger.debug("检测到视频暂停，已通过播放器按钮恢复播放。")
            elif await ensure_playing(frame):
                logger.debug("检测到视频暂停，已恢复播放。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _handle(exc, "续播模块"):
                return
        await asyncio.sleep(2)


async def tuning_worker(page: Page, adapter: PlatformAdapter, config: Config) -> None:
    """锁定倍速与静音。

    平台脚本会在切集、缓冲后把 playbackRate 重置回 1.0，
    所以必须持续巡检而不是设置一次了事。
    """
    while True:
        try:
            await asyncio.sleep(3)
            frame = await adapter.video_frame(page)
            speed = config.speed  # 每次读取，支持运行中改配置
            mute = config.mute
            if getattr(adapter, "ui_playback", False):
                await adapter.tune_playback(page, speed, mute)
                continue
            await frame.evaluate(
                """([speed, mute]) => {
                    const v = document.querySelector('video');
                    if (!v) return;
                    if (Math.abs(v.playbackRate - speed) > 0.01) v.playbackRate = speed;
                    if (mute && v.volume !== 0) { v.volume = 0; v.muted = true; }
                }""",
                [speed, mute],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _handle(exc, "播放调节模块"):
                return


async def captcha_worker(
    page: Page, adapter: PlatformAdapter, config: Config, clock: StudyClock
) -> None:
    """人机验证监视。

    已识别的控件尝试本地识别，未通过时限时等待人工。等待时间从有效学习时长里扣除，
    否则限时刷课会被验证等待时间白白吃掉。
    """
    while True:
        try:
            await asyncio.sleep(3)
            if not await adapter.detect_captcha(page) and not await has_verification(page):
                continue

            # 这条带 [需要你处理] 前缀，界面据此把窗口叫到最前。
            # 本地验证未通过时，及时叫人接管：
            # 窗口收在托盘里、被别的程序挡着时，光响一声铃很容易错过，
            # 一错过就白等在那儿，限时刷课的时间也跟着耗掉
            logger.warn("[需要你处理] 检测到人机验证，尝试本地识别；未通过时请手动处理。",
                        shift=True)
            if config.beep_on_captcha:
                print("\a", end="", flush=True)

            await hold_playback_for_manual_check(page, adapter, True)
            clock.pause()
            cleared = False
            try:
                try:
                    await asyncio.wait_for(wait_verification(page, adapter),
                                           timeout=config.login_timeout_seconds)
                    cleared = True
                except asyncio.TimeoutError:
                    raise VerificationTimeout from None
            finally:
                waited = clock.resume()
                if cleared:
                    await hold_playback_for_manual_check(page, adapter, False)
            logger.info(f"验证控件已消失，本次等待 {waited:.0f} 秒不计入学习时长。", shift=True)
        except asyncio.CancelledError:
            raise
        except VerificationTimeout:
            raise
        except Exception as exc:
            if _handle(exc, "人机验证模块"):
                return


async def solve_one_question(
    page: Page,
    adapter: PlatformAdapter,
    config: Config,
    question,
    provider: AnswerProvider | None,
    cache: AnswerCache,
) -> bool:
    """作答一道题，必要时反复尝试直到平台判定正确。返回是否答对。

    试错开关开启时仅对视频弹窗按选项重试；关闭时用参考答案填写一次。
    """
    logger.info(f"  题目：{question.describe()}")

    if not question.is_choice:
        logger.info("  非选择题，本程序不自动填写，需要你手动处理。")
        return False

    if await adapter.question_already_correct(page, question):
        logger.info("  平台已判定本题正确，保留现有答案。")
        await adapter.confirm_and_close(page)
        return True

    # 只有视频弹窗能依据平台对错反馈试错；开启试错时先走选项序列，
    # 独立测验/考试在各自流程里仍只用题库或 AI 答案。
    result = AnswerResult()
    if not config.retry_until_correct:
        result = cache.get(question) if config.answer_cache else None
        if result is None and provider is not None:
            result = await provider.solve(question)
        if result is None:
            result = AnswerResult()

    ai_keys = match_option(result, question) if not result.empty else []
    if ai_keys:
        logger.info(f"  参考答案：{'+'.join(ai_keys)}（{result.source}，"
                    f"置信度 {result.confidence:.0%}）")
    else:
        logger.info("  视频弹题按选项顺序试错。" if config.retry_until_correct
                    else "  无参考答案，不自动填写。")

    if not config.retry_until_correct:
        # 旧行为：只填一次，由用户决定是否提交
        if not ai_keys:
            logger.warn("  没有可填的答案，跳过本题。")
            return False
        await adapter.clear_selection(page, question)
        if await adapter.fill_answer(page, question, ai_keys, result):
            await adapter.submit_answer(page, config.auto_submit)
            return True
        logger.warn("  填写答案失败。")
        return False

    attempts = build_attempts(question)
    if not attempts:
        logger.warn("  无法生成候选答案，跳过本题。")
        return False

    for index, attempt in enumerate(attempts, 1):
        if not await adapter.detect_question(page):
            if await adapter.read_feedback(page) == "correct":
                logger.info("  题目已关闭，平台判定为通过。")
                return True
            logger.warn("  弹窗已关闭，但平台没有给出正确反馈；不记为答对。")
            return False

        logger.info(f"  {describe_attempt(attempt, index, len(attempts))}")
        await adapter.clear_selection(page, question)
        if not await adapter.fill_answer(page, question, attempt, result):
            logger.debug("  本次点选未生效，换下一组。")
            continue

        # 试错必须真提交，否则拿不到平台的对错反馈
        await adapter.submit_answer(page, auto_submit=True)
        feedback = await adapter.read_feedback(page)
        if feedback == "unknown" and adapter.retry_without_feedback(question):
            await asyncio.sleep(1.0)
            feedback = await adapter.read_feedback(page)

        if feedback == "correct":
            logger.info(f"  ✓ 答对了（第 {index} 次尝试）。")
            # 验证过的答案比 AI 猜的可信得多，覆盖写回缓存
            if config.answer_cache:
                verified = AnswerResult(
                    option_keys=attempt,
                    option_texts=[o.text for o in question.options
                                  if o.key.upper() in attempt],
                    confidence=1.0,
                    reasoning="平台判定正确",
                    source="verified",
                )
                cache.put(question, verified)
            await adapter.confirm_and_close(page)
            return True

        if feedback == "unknown":
            if adapter.retry_without_feedback(question):
                logger.info("  平台尚无判题反馈，继续有限次候选尝试；未记为答对。")
                continue
            logger.warn("  无法判断对错，停止尝试以免乱点。请留意这道题。")
            return False

        logger.debug(f"  第 {index} 次尝试判定为错误。")
        await asyncio.sleep(0.4)

    logger.warn(f"  已尝试 {len(attempts)} 次仍未答对，放弃本题以免卡住播放。")
    return False


async def solve_chapter_test_once(
    page: Page,
    adapter: PlatformAdapter,
    config: Config,
    provider: AnswerProvider | None,
    cache: AnswerCache,
) -> bool:
    """处理一次独立章节测验；返回当前页面是否可以安全离开。"""
    questions = await adapter.extract_questions(page)
    if not questions:
        logger.warn("检测到章节测验，但未能提取题目；已跳过，不会乱点。")
        return True

    logger.info(
        f"检测到独立章节测验，共 {len(questions)} 题。"
        "此处只采用题库/AI答案一次，不执行视频弹题的换答案试错。",
        shift=True,
    )
    answered = 0
    preserved = 0
    for question in questions:
        if not question.is_choice:
            logger.warn(f"  {question.describe()} 不是选择题，已留给你手动处理。")
            continue
        if question.selected_keys:
            preserved += 1
            logger.info(
                f"  第 {question.index + 1} 题已有答案 "
                f"{'+'.join(question.selected_keys)}，已保留并跳过。"
            )
            continue
        result = cache.get(question) if config.answer_cache else None
        if result is None and provider is not None:
            result = await provider.solve(question)
        if result is None or result.empty:
            logger.warn(f"  {question.describe()} 没有可靠答案，未填写。")
            continue
        keys = match_option(result, question)
        if not keys:
            logger.warn(f"  {question.describe()} 的 AI 答案无法匹配页面选项，未填写。")
            continue
        if await adapter.fill_answer(page, question, keys, result):
            answered += 1
            logger.info(f"  第 {question.index + 1} 题已填写：{'+'.join(keys)}")

    if answered == 0 and preserved == 0:
        logger.warn("章节测验没有任何题被可靠填写，已跳过且未提交。")
        return True

    completed = answered + preserved
    all_answered = completed == len(questions)
    auto_submit = bool(config.auto_submit and all_answered)
    action_ok = await adapter.submit_answer(page, auto_submit=auto_submit)
    if auto_submit and action_ok:
        logger.info(
            f"章节测验 {completed}/{len(questions)} 题已有答案并确认提交。", shift=True
        )
    elif auto_submit:
        saved = await adapter.submit_answer(page, auto_submit=False)
        if saved:
            logger.warn(
                f"章节测验 {completed}/{len(questions)} 题未确认提交成功，"
                "已改为暂存并继续下一章。",
                shift=True,
            )
        else:
            logger.warn(
                f"章节测验 {completed}/{len(questions)} 题未确认提交成功，"
                "再次暂存也未获成功证据；为保持无人值守，将继续下一章。",
                shift=True,
            )
    else:
        why = "存在未能可靠作答的题" if not all_answered else "未开启章节测验自动提交"
        if action_ok:
            logger.info(
                f"章节测验已有答案 {completed}/{len(questions)} 题并暂存（{why}），"
                "不会自动试错。",
                shift=True,
            )
        else:
            retry_saved = await adapter.submit_answer(page, auto_submit=False)
            if retry_saved:
                logger.warn("章节测验首次暂存未确认，重试暂存成功，继续下一章。", shift=True)
            else:
                logger.warn(
                    "章节测验两次暂存均未获成功证据；为保持无人值守，将继续下一章。",
                    shift=True,
                )
    # 无人值守模式不能因平台没有回执而卡在本章；失败已尽力暂存并明确报警。
    return True


async def question_worker(
    page: Page,
    adapter: PlatformAdapter,
    config: Config,
    clock: StudyClock,
    provider: AnswerProvider | None,
    cache: AnswerCache,
) -> None:
    """答题闭环：检测弹题 → 试错作答 → 确认关闭 → 让播放继续。

    视频弹窗开启试错时先按选项尝试；独立测验和考试仍走 AI 答题流程。

    自动答题有总超时兜底；人工处理模式保留弹窗，等用户处理完再续播。
    """
    import time

    while True:
        try:
            await asyncio.sleep(2)
            if not await adapter.detect_question(page):
                continue

            paused_at = time.time()
            if not config.answer_enabled:
                logger.warn("[需要你处理] 检测到视频弹题，自动答题已关闭，请在浏览器手动处理。",
                            shift=True)
                try:
                    await hold_playback_for_manual_check(page, adapter, True, "question")
                    while await adapter.detect_question(page):
                        await asyncio.sleep(2)
                finally:
                    await hold_playback_for_manual_check(page, adapter, False, "question")
                    clock.add_paused(time.time() - paused_at)
                if getattr(adapter, "ui_playback", False):
                    await adapter.ensure_playing(page)
                continue
            try:
                await asyncio.wait_for(
                    _handle_question_popup(page, adapter, config, provider, cache),
                    timeout=QUESTION_TOTAL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warn(
                    f"答题超过 {QUESTION_TOTAL_TIMEOUT // 60} 分钟仍未结束，"
                    "强制关闭弹窗继续播放。", shift=True)
            finally:
                # 无论成功失败都要确保弹窗被关掉
                try:
                    if await adapter.detect_question(page):
                        await adapter.close_question(page)
                except Exception:
                    pass

            elapsed = time.time() - paused_at
            clock.add_paused(elapsed)
            playback = "继续播放。"
            if getattr(adapter, "ui_playback", False):
                resumed = await adapter.ensure_playing(page)
                playback = "继续播放。" if resumed else "续播任务将继续检查播放状态。"
            logger.info(f"答题结束，耗时 {elapsed:.0f} 秒不计入学习时长，{playback}", shift=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _handle(exc, "答题模块"):
                return
            if config.answer_enabled:
                try:
                    await adapter.close_question(page)
                except Exception:
                    pass


async def _handle_question_popup(
    page: Page,
    adapter: PlatformAdapter,
    config: Config,
    provider: AnswerProvider | None,
    cache: AnswerCache,
) -> None:
    if not config.answer_enabled:
        return
    questions = await adapter.extract_questions(page)
    if not questions:
        # 检测到弹窗但抠不出题目，至少要把它关掉，否则播放永久卡住
        logger.warn("检测到弹题但未能提取题干，直接关闭弹窗。")
        await adapter.close_question(page)
        return

    logger.info(f"检测到 {len(questions)} 道题目，暂停计时开始作答。", shift=True)
    solved = 0
    for question in questions:
        if await solve_one_question(page, adapter, config, question, provider, cache):
            solved += 1
    logger.info(f"本轮答对 {solved}/{len(questions)} 题。")
