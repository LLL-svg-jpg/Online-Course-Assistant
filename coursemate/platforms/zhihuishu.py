"""知道智慧树 (zhihuishu.com) 适配器。

选择器来源：CXRunfree/Autovisor (MIT)，那些是在真实页面上跑了三年的验证结果，
直接取用比自己猜靠谱得多。

与 Autovisor 的差别在答题：它检测到弹题后盲选前两个选项把弹窗关掉，
只求视频继续播；这里改成提取题干交给 AI 作答。
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page

from ..answer.base import AnswerResult, Option, Question
from ..logger import Logger
from .base import Lesson, PlatformAdapter, register

logger = Logger()


@register
class ZhihuishuAdapter(PlatformAdapter):
    name = "知道智慧树"
    # 旧的 passport.zhihuishu.com 只是跳板，会重定向到这里
    login_url = "https://login.zhihuishu.com/?origin=zhs"
    video_in_iframe = False

    # 判定"还在登录流程里"的域名前缀
    LOGIN_HOSTS = ("login.", "passport.")

    # 登录表单选择器（2026-09 在真实页面实测）。
    # 注意：新登录页的 input id 是 el-id-1784-18 这种每次加载都变的动态值，
    # 绝对不能拿 id 当选择器，只能用 name / type / class。
    USERNAME_SEL = 'input[name="mobile"]'
    PASSWORD_SEL = 'input[type="password"]'
    LOGIN_BTN_SEL = ".btn-block__grandient_login"
    AGREEMENT_SELECTORS = (
        '#agreement input[type="checkbox"]',
        '.agreement input[type="checkbox"]',
        '[class*="agreement" i] input[type="checkbox"]',
        'input[type="checkbox"][name*="agree" i]',
        'input[type="checkbox"][id*="agree" i]',
    )

    async def _accept_login_agreement(self, page: Page) -> bool:
        privacy = page.locator("label.privacy-checkbox").first
        box = privacy.locator('input[type="checkbox"]').first
        if await privacy.is_visible() and await box.count():
            if not await box.is_checked():
                control = privacy.locator(".el-checkbox__inner").first
                if await control.is_visible():
                    await control.click()
                else:
                    await privacy.click(position={"x": 5, "y": 5})
            return await box.is_checked()
        for sel in self.AGREEMENT_SELECTORS:
            agreement = page.locator(sel).first
            if (await agreement.count() and await agreement.is_visible()
                    and not await agreement.is_checked()):
                await agreement.check()
                return True
        return False

    # 智慧树同时在跑多套播放页：studyh5 / studyvideoh5 / fusioncourseh5 / hike，
    # DOM 结构并不一致。硬编码单个选择器必然在某些课上落空，
    # 因此这里按候选列表依次尝试，命中哪个就用哪个，并把结果记进日志便于反馈。
    LESSON_CANDIDATES = (
        "ul.list li.video",  # 共享课当前目录
        ".clearfix.video",      # 经典学分课，Autovisor 验证
        ".video-item",          # studyvideoh5 常见
        ".catalogue-item",
        ".chapter-item",
        ".lesson-item",
        "li[class*='video']",
    )
    LESSON_ACTIVE_MARKERS = ("current_play", "active", "on", "playing")
    HIKE_LESSON_SEL = ".file-item"
    HIKE_LESSON_ACTIVE = "active"
    FINISHED_MARKERS = (".time_icofinish", ".icon-finish", ".finished", ".complete")

    CAPTCHA_SEL = ".yidun_modal__title"
    DIALOG_SEL = ".el-dialog"
    POPUP_SEL = "#playTopic-dialog"
    QUESTION_TITLE_SEL = ".topic-title"
    QUESTION_LIST_SEL = ".el-scrollbar__view"
    QUESTION_NUMBER_SEL = ".number"
    OPTION_SEL = ".topic-item"
    ANSWERED_SEL = ".answer"

    def __init__(self) -> None:
        self.is_hike = False
        self.is_shared = False
        self.confirm_catalog_progress = False
        self.ui_playback = False
        # 实际命中的章节选择器，供日志与后续复用
        self.lesson_sel: str = ""
        self._last_selector_log: tuple[str, int] | None = None
        self._unsupported_speed: float | None = None
        self._popup_question_count = 0
        self._current_question_index = 0
        self._playback_lock = asyncio.Lock()
        self._chapter_test_rank: int | None = None
        self._chapter_test_key = ""
        self._confirmed_tests: set[str] = set()
        self.work_page: Page | None = None
        self.course_page_lost = False
        self.course_url = ""

    @classmethod
    def match(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == "zhihuishu.com" or host.endswith(".zhihuishu.com")

    # ---------- 刷课主线 ----------

    async def is_logged_in(self, page: Page) -> bool:
        """按域名判断，而不是找某个元素。

        以前用"登录框消失"来判定，结果智慧树换了登录页后旧选择器全部失效，
        元素不存在天然等于 hidden，程序当场认为登录成功，然后拿着未登录的
        会话去开课程页——平台回了 404。这类误判必须从判据上根除。
        """
        host = urlparse(page.url).hostname or ""
        if not host:  # about:blank 等尚未导航的状态
            return False
        if not host.endswith(".zhihuishu.com") or any(host.startswith(p) for p in self.LOGIN_HOSTS):
            return False
        try:
            username = page.locator(self.USERNAME_SEL).first
            return not (await username.count() and await username.is_visible())
        except Exception:
            return False

    async def login(self, page: Page, context: BrowserContext, username: str, password: str) -> None:
        task_url = getattr(self, "task_url", "")
        parsed = urlparse(task_url)
        login_url = task_url if (parsed.hostname == "onlineservice-api.zhihuishu.com"
                                 and parsed.path == "/gateway/f/v1/login/gologin") else self.login_url
        await page.goto(login_url, wait_until="domcontentloaded")
        # 登录页是 SPA，且 passport 域会重定向到 login 域，要等它渲染完
        await page.wait_for_timeout(3000)
        if await self.is_logged_in(page):
            logger.info("检测到已登录，跳过登录步骤。")
            return

        try:
            await page.wait_for_selector(self.USERNAME_SEL, state="visible", timeout=20000)
        except Exception:
            logger.warn(
                "没有认出登录表单，可能是智慧树又改版了。"
                "请在浏览器里手动完成登录，程序会等你。", shift=True)

        try:
            await self._accept_login_agreement(page)
        except Exception as exc:
            logger.warn(f"自动勾选登录协议未成功，请手动确认：{Logger.summarize(exc)}")

        if username and password:
            logger.info("正在自动填写账号密码...")
            try:
                await page.fill(self.USERNAME_SEL, username, timeout=10000)
                await page.fill(self.PASSWORD_SEL, password, timeout=10000)
                await page.wait_for_timeout(600)
                await page.click(self.LOGIN_BTN_SEL, timeout=10000)
                logger.info("已提交登录，等待跳转...")
            except Exception as exc:
                logger.warn(f"自动登录未能完成，请手动操作：{Logger.summarize(exc)}", shift=True)
            logger.warn(
                "若出现未识别的登录提示或验证未自动通过，请在等待时限内手动完成。", shift=True)
        else:
            logger.warn("未配置账号密码，请在浏览器窗口中手动登录...", shift=True)

        # 以"离开登录域名"作为成功判据，等人操作所以超时给足
        waited = 0
        while waited < 24 * 3600:
            await asyncio.sleep(2)
            waited += 2
            if await self.is_logged_in(page):
                return
            if waited % 60 == 0:
                logger.info(f"仍在等待登录完成...（已等 {waited // 60} 分钟）")

    # 打开课程页后用来识别"这不是课程页"的特征
    ERROR_HINTS = ("404", "页面不存在", "找不到", "无权限", "not found", "出错了")

    async def open_course(self, page: Page, url: str) -> str:
        self._confirmed_tests.clear()
        self.is_hike = "hike.zhihuishu.com" in url
        parsed = urlparse(url)
        self.is_shared = (parsed.hostname or "").lower() == "studyvideoh5.zhihuishu.com" \
            and parsed.path.lower().startswith("/stustudy")
        self.confirm_catalog_progress = self.is_shared
        self.ui_playback = self.is_shared
        if self.is_shared:
            self.QUESTION_TITLE_SEL = "#playTopic-dialog .topic-title"
            self.QUESTION_LIST_SEL = "#playTopic-dialog .el-pager"
            self.OPTION_SEL = "#playTopic-dialog .topic-item"
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # 播放页是 SPA，DOM 就绪不等于内容渲染完
        await page.wait_for_timeout(3500)

        status = response.status if response else 0
        if status >= 400:
            logger.error(f"课程页返回 HTTP {status}。请确认地址是否完整、是否已过期。")

        # 被踢回登录页说明会话没生效，这是 404 之外的另一种常见失败
        if not await self.is_logged_in(page):
            logger.error("打开课程页时被跳回登录页，说明登录状态未生效。")
            return "未登录"
        if self.is_shared:
            self.course_url = page.url

        title_sel = ".course-name" if self.is_hike else ".source-name"
        try:
            node = await page.wait_for_selector(title_sel, timeout=15000)
            title = (await node.text_content() or "").strip()
            if title:
                return title
        except Exception:
            pass

        # 读不到课程名时，检查是不是打开了错误页——否则后面会一路"没有章节"，
        # 让人以为是选择器问题，其实根本没进对页面
        try:
            body = (await page.inner_text("body"))[:400].lower()
        except Exception:
            body = ""
        if any(h in body for h in self.ERROR_HINTS):
            logger.error(
                "打开的页面像是错误页而不是课程页。请核对课程地址：\n"
                "  1) 必须是视频播放页，浏览器地址栏里带 courseId / classId 参数\n"
                "  2) 从课程列表点进去开始播放后，再复制地址栏的完整地址"
            )
            return "打开失败"
        return "未知课程"

    async def prepare_page(self, page: Page) -> None:
        """关掉进入课程时的引导弹窗，否则会挡住播放器。"""
        if not self.is_shared:
            try:
                if await page.locator(".courseRemind.khfaPop:visible").count():
                    warning = page.locator(".wxtsPop:visible .btn.primary").first
                    if await warning.count() and await warning.is_visible():
                        await warning.click(timeout=2000)
                        await page.locator(".el-dialog__wrapper.wxtsPop:visible").first.wait_for(
                            state="hidden", timeout=2000
                        )
            except Exception:
                pass
        selectors = ((".dialog-warn .talk-later-btn", ".dialog-warn .dialog-close",
                      ".dialog .dialog-read .iconguanbi")
                     if self.is_shared else (".courseRemind.khfaPop:visible .el-icon-error",
                                             ".courseRemind:visible .el-icon-error",
                                             ".iconfont.iconguanbi", ".dialog-close"))
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if await button.count() and await button.is_visible():
                    await button.click(timeout=2000)
            except Exception:
                pass
        if not self.is_shared:
            try:
                await page.locator(".courseRemind.khfaPop").first.wait_for(
                    state="hidden", timeout=2000
                )
            except Exception:
                logger.warn("学前必读弹窗仍遮挡页面，请手动关闭。")
        # 智慧树会校验播放器行为，不覆写 video.pause/play 等原生方法。

    async def list_lessons(self, page: Page) -> list[Lesson]:
        sel = await self._resolve_lesson_selector(page)
        if not sel:
            logger.error(
                "未能识别章节列表。可能是该课程使用了尚未适配的播放页版本。"
                "请把当前页面地址反馈给作者，或自行往 LESSON_CANDIDATES 里补充选择器。"
            )
            return []

        if sel == "ul.list li.video":
            sel = "ul.list li.video, ul.list li.chapter-test"
            self.lesson_sel = sel
        handles = await page.query_selector_all(sel)
        lessons: list[Lesson] = []
        for index, handle in enumerate(handles):
            classes = (await handle.get_attribute("class") or "").split()
            kind = "chapter" if "chapter-test" in classes else "video"
            title_node = await handle.query_selector("span.catalogue_title")
            if kind == "chapter":
                title_node = await handle.query_selector("span.name")
            title = " ".join((await (title_node or handle).text_content() or "").split())[:60]
            finished = False
            for marker in self.FINISHED_MARKERS:
                try:
                    if await handle.query_selector(marker):
                        finished = True
                        break
                except Exception:
                    continue
            if not finished:
                progress = await handle.query_selector(".progress-num")
                if progress and (await progress.text_content() or "").strip() == "100%":
                    finished = True
            if kind == "chapter" and str(index) in self._confirmed_tests:
                finished = True
            lessons.append(Lesson(title=title or "未命名小节", handle=handle,
                                  finished=finished, key=str(index), kind=kind))
        return lessons

    async def active_lesson_key(self, page: Page) -> str:
        if not self.lesson_sel:
            return ""
        handles = await page.query_selector_all(self.lesson_sel)
        markers = (self.HIKE_LESSON_ACTIVE,) if self.is_hike else self.LESSON_ACTIVE_MARKERS
        for index, handle in enumerate(handles):
            classes = (await handle.get_attribute("class") or "").split()
            if any(marker in classes for marker in markers):
                return str(index)
        return ""

    async def confirm_lesson_completion(self, page: Page, lesson: Lesson) -> bool:
        lessons = await self.list_lessons(page)
        return any(item.key == lesson.key and item.title == lesson.title
                   and item.finished for item in lessons)

    async def ensure_playing(self, page: Page) -> bool:
        """仅通过播放器可见控件续播，不注入 play/pause 守卫。"""
        async with self._playback_lock:
            if self.work_page is not None and self.work_page is not page:
                return False
            if await self.detect_question(page) or await self.detect_captcha(page):
                return False
            active_key = await self.active_lesson_key(page)
            if not active_key:
                return False
            lessons = await self.list_lessons(page)
            if not any(item.key == active_key and item.kind == "video"
                       and not item.finished for item in lessons):
                return False
            video = page.locator("video").first
            if not await video.count() or not await video.evaluate(
                "v => v.paused && !v.ended && !v.__coursemateManualHold"
            ):
                return False
            player = page.locator("#vjs_container").first
            await (player if await player.count() else video).hover(timeout=2000)
            for selector in ("#playButton .bigPlayButton, .bigPlayButton.pointer",
                             "#playButton", ".vjs-big-play-button", ".vjs-play-control"):
                button = page.locator(selector).first
                if not await button.count() or not await button.is_visible():
                    continue
                if await self.active_lesson_key(page) != active_key or not await video.evaluate(
                    "v => v.paused && !v.ended && !v.__coursemateManualHold"
                ):
                    return False
                handle = await video.element_handle()
                before = await video.evaluate("v => v.currentTime")
                await button.click(timeout=2000)
                try:
                    await page.wait_for_function(
                        "([v, before]) => v.isConnected && !v.paused && !v.ended "
                        "&& v.currentTime > before + 0.05",
                        arg=[handle, before], timeout=2000,
                    )
                    return await self.active_lesson_key(page) == active_key
                except Exception:
                    if not await video.evaluate("v => v.paused"):
                        return False
            return False

    async def tune_playback(self, page: Page, speed: float, mute: bool) -> None:
        """智慧树仅使用页面提供的倍速档；静音由浏览器启动参数处理。"""
        video = page.locator("video").first
        if not await video.count():
            return
        current = await video.evaluate("v => v.playbackRate")
        if abs(current - speed) < 0.01:
            return
        option = page.locator(f'.speedList .speedTab[rate="{speed:g}"]').first
        if not await option.count() and speed == 1:
            option = page.locator('.speedList .speedTab[rate="1.0"]').first
        if not await option.count():
            if self._unsupported_speed != speed:
                logger.warn(f"智慧树播放器没有 {speed:g}× 档位，保持网页当前倍速。")
                self._unsupported_speed = speed
            return
        box = page.locator(".speedBox").first
        if await box.count():
            await box.hover(timeout=3000)
        await option.click(timeout=3000)

    async def _resolve_lesson_selector(self, page: Page) -> str:
        """逐个试候选选择器，返回第一个真能选出元素的。

        智慧树同时在跑多套播放页，硬认一个选择器等于把成功率押在运气上。
        """
        if self.is_hike:
            try:
                await page.wait_for_selector(self.HIKE_LESSON_SEL, state="attached", timeout=20000)
                self.lesson_sel = self.HIKE_LESSON_SEL
                return self.lesson_sel
            except Exception:
                return ""

        # H5 播放页的目录是异步渲染的，先给它加载时间
        try:
            await page.wait_for_selector(
                ", ".join(self.LESSON_CANDIDATES), state="attached", timeout=30000
            )
        except Exception:
            logger.debug("等待章节列表超时，仍逐个候选试一次。")

        for candidate in self.LESSON_CANDIDATES:
            try:
                found = await page.query_selector_all(candidate)
            except Exception:
                continue
            if found:
                self.lesson_sel = candidate
                reported = (candidate, len(found))
                if reported != self._last_selector_log:
                    logger.info(f"章节列表命中选择器 {candidate}，共 {len(found)} 项。")
                    self._last_selector_log = reported
                return candidate
        return ""

    async def enter_lesson(self, page: Page, lesson: Lesson) -> bool:
        await self.prepare_page(page)
        if lesson.kind == "chapter":
            self._chapter_test_key = lesson.key
            self._chapter_test_rank = None
            label = await lesson.handle.evaluate("""el =>
                el.closest('ul.list')?.querySelector('.chapter .catalogue_title3 b')?.textContent.trim() || ''
            """)
            if label == "绪章":
                self._chapter_test_rank = 0
            else:
                match = re.fullmatch(r"第([\d一二三四五六七八九十]+)章", label)
                if match:
                    number = match.group(1)
                    digits = "零一二三四五六七八九"
                    if number.isdecimal():
                        self._chapter_test_rank = int(number)
                    elif "十" in number:
                        tens, units = number.split("十", 1)
                        self._chapter_test_rank = (digits.index(tens) if tens else 1) * 10 \
                            + (digits.index(units) if units else 0)
                    else:
                        self._chapter_test_rank = digits.index(number)
            before = set(page.context.pages)
            await lesson.handle.click(timeout=10000)
            await page.wait_for_timeout(1200)
            opened = [tab for tab in page.context.pages if tab not in before]
            self.work_page = opened[-1] if opened else page
            return True
        try:
            await lesson.handle.click(timeout=10000)
        except Exception as exc:
            logger.warn(f"点击小节失败：{Logger.summarize(exc)}")
            return False
        markers = (
            (self.HIKE_LESSON_ACTIVE,) if self.is_hike else self.LESSON_ACTIVE_MARKERS
        )
        try:
            await page.wait_for_selector(
                ", ".join(f".{m}" for m in markers), state="attached", timeout=15000
            )
        except Exception:
            # 等不到高亮标记不代表进不去，继续用 video 是否出现来判断
            pass
        if self.is_shared:
            for _ in range(16):
                if await self.active_lesson_key(page) == lesson.key:
                    break
                await page.wait_for_timeout(500)
            else:
                logger.warn(f"点击《{lesson.title}》后目录未切换到该小节，稍后按未完成补刷。")
                return False
        await page.wait_for_timeout(1000)
        try:
            await page.wait_for_selector("video", state="attached", timeout=20000)
        except Exception:
            logger.warn("本小节未找到视频元素，可能是文档或作业类任务点。")
            return False
        await self.prepare_page(page)
        return True

    async def process_chapter_test(self, page: Page, provider, cache, use_cache: bool,
                                   auto_submit: bool, should_stop) -> bool:
        from ..zhihuishu_work import is_work_list_url, study_tests

        work_page = self.work_page or page
        try:
            if is_work_list_url(work_page.url) and self._chapter_test_rank is None:
                logger.warn("未识别课程目录的章号，未选择其他章的平时测试。")
                return False
            processed = await study_tests(work_page, provider, cache, use_cache, should_stop,
                                          target_rank=self._chapter_test_rank)
            if processed:
                self._confirmed_tests.add(self._chapter_test_key)
                if work_page is not page and not work_page.is_closed():
                    await work_page.close()
            return processed
        finally:
            self.work_page = None
            if (not should_stop() and work_page is page and self.course_url
                    and page.url != self.course_url):
                try:
                    await page.goto(self.course_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_selector(", ".join(self.LESSON_CANDIDATES),
                                                 state="attached", timeout=20000)
                    await self.prepare_page(page)
                    logger.info("已从平时测试返回课程目录，继续处理后续小节。", shift=True)
                except Exception as exc:
                    self.course_page_lost = True
                    logger.error(f"平时测试后未能返回课程目录：{Logger.summarize(exc)}")

    async def get_progress(self, page: Page) -> str:
        """智慧树把学习进度写在 .percent / .study-percent 上。

        读不到就退回按视频播放比例估算——刷课主线不能因为读不到进度就停摆。
        """
        for sel in (".percent", ".study-percent", ".progress-num"):
            try:
                node = await page.query_selector(sel)
                if node:
                    text = (await node.text_content() or "").strip()
                    if text:
                        return text
            except Exception:
                continue
        try:
            ratio = await page.evaluate(
                "(() => { const v = document.querySelector('video');"
                " return v && v.duration ? Math.floor(v.currentTime / v.duration * 100) : null; })()"
            )
            if ratio is not None:
                return f"{ratio}%"
        except Exception:
            pass
        return ""

    async def lesson_finished(self, page: Page, lesson: Lesson) -> bool:
        """当前小节是否播完。

        共享课优先看目录完成标记；高亮移走可能是用户手动切课，不能当完成。
        其他旧播放页保留原有高亮判据，视频播到末尾作兜底。
        """
        if self.is_shared and await self.confirm_lesson_completion(page, lesson):
            return True
        if not self.is_shared:
            markers = ((self.HIKE_LESSON_ACTIVE,) if self.is_hike
                       else self.LESSON_ACTIVE_MARKERS)
            cls = await lesson.handle.get_attribute("class") or ""
            if cls and not any(marker in cls for marker in markers):
                return True
        try:
            done = await page.evaluate(
                "(() => { const v = document.querySelector('video');"
                " return !!(v && v.duration && v.currentTime >= v.duration - 1.5); })()"
            )
            return bool(done)
        except Exception:
            return False

    # ---------- 答题支线 ----------

    async def detect_question(self, page: Page) -> bool:
        try:
            dialog = page.locator(self.POPUP_SEL).first
            if await dialog.count():
                return await dialog.is_visible() and bool(await dialog.locator(".topic-title").count())
            if not self.is_shared:
                return await page.locator(self.QUESTION_TITLE_SEL).first.is_visible()
            return False
        except Exception:
            return False

    async def extract_questions(self, page: Page) -> list[Question]:
        """提取弹窗中所有题目。

        智慧树的弹题可能一次弹多道，用 .number 逐题切换，
        每次切换后重新读取当前题干与选项。
        """
        questions: list[Question] = []
        try:
            container = await page.query_selector(self.QUESTION_LIST_SEL)
            numbers = await container.query_selector_all(self.QUESTION_NUMBER_SEL) if container else []
        except Exception:
            numbers = []

        # 只有一道题时页面不渲染题号列表
        self._popup_question_count = len(numbers) or 1
        if not numbers:
            q = await self._read_current_question(page, 0)
            return [q] if q else []

        for index, number in enumerate(numbers):
            try:
                await number.click(timeout=3000)
                await asyncio.sleep(0.4)
            except Exception:
                continue
            q = await self._read_current_question(page, index)
            if q:
                questions.append(q)
        return questions

    async def _read_current_question(self, page: Page, index: int) -> Question | None:
        try:
            title_node = await page.query_selector(self.QUESTION_TITLE_SEL)
            if not title_node:
                return None
            stem = await self._question_stem(title_node)
            if not stem:
                return None
            option_nodes = await page.query_selector_all(self.OPTION_SEL)
            options: list[Option] = []
            for i, node in enumerate(option_nodes):
                text = (await node.text_content() or "").strip()
                text = " ".join(text.split())
                if not text:
                    continue
                # 选项文本常自带 "A." 前缀，剥掉以免重复
                key = chr(ord("A") + i)
                if len(text) > 2 and text[0].upper().isalpha() and text[1] in ".、．":
                    key = text[0].upper()
                    text = text[2:].strip()
                options.append(Option(key=key, text=text))
            type_node = await page.query_selector("#playTopic-dialog .title-tit")
            type_text = (await type_node.text_content() or "") if type_node else ""
            qtype = self._guess_type(type_text + stem, options)
            return Question(stem=stem, options=options, qtype=qtype, index=index)
        except Exception as exc:
            logger.debug(f"提取题目失败：{Logger.summarize(exc)}")
            return None

    @staticmethod
    async def _question_stem(title_node) -> str:
        return await title_node.evaluate(r"""el => {
            const copy = el.cloneNode(true);
            copy.querySelectorAll('.title-tit, .right, .error').forEach(node => node.remove());
            const first = copy.firstElementChild;
            if (first && /^(正确|错误)$/.test(first.textContent.trim())) first.remove();
            return copy.textContent.replace(/\s+/g, ' ').trim();
        }""")

    @staticmethod
    def _guess_type(stem: str, options: list[Option]) -> str:
        head = stem[:24]
        if "多选" in head:
            return "multiple"
        if "判断" in head:
            return "judge"
        if not options:
            return "fill"
        texts = {o.text.strip() for o in options}
        if texts <= {"正确", "错误", "对", "错", "√", "×", "A", "B"} and len(options) == 2:
            return "judge"
        return "single"

    async def fill_answer(
        self, page: Page, question: Question, keys: list[str], result: AnswerResult
    ) -> bool:
        if not keys:
            return False
        if not await self._activate_question(page, question):
            return False
        try:
            option_nodes = await page.query_selector_all(self.OPTION_SEL)
        except Exception:
            return False
        wanted = {k.upper() for k in keys}
        clicked = 0
        for i, node in enumerate(option_nodes):
            key = chr(ord("A") + i)
            if i < len(question.options):
                key = question.options[i].key.upper()
            if key not in wanted:
                continue
            try:
                await node.click(timeout=3000)
                clicked += 1
                await asyncio.sleep(0.2)
            except Exception as exc:
                logger.debug(f"点选选项 {key} 失败：{Logger.summarize(exc)}")
        return clicked > 0

    async def _activate_question(self, page: Page, question: Question) -> bool:
        if not self.is_shared:
            return True
        numbers = page.locator(self.POPUP_SEL).locator(".el-pager .number")
        if await numbers.count():
            if question.index < 0 or question.index >= await numbers.count():
                return False
            await numbers.nth(question.index).click(timeout=3000)
            await asyncio.sleep(0.2)
        self._current_question_index = question.index
        title = page.locator(self.QUESTION_TITLE_SEL).first
        return await title.count() > 0 and await self._question_stem(title) == question.stem

    async def question_already_correct(self, page: Page, question: Question) -> bool:
        return await self._activate_question(page, question) and await self.read_feedback(page) == "correct"

    def retry_without_feedback(self, question: Question) -> bool:
        return self.is_shared and question.qtype == "multiple"

    async def submit_answer(self, page: Page, auto_submit: bool) -> bool:
        if not auto_submit:
            logger.info("已填写答案但未提交（auto_submit = false），可自行确认后提交。")
            return True
        prefix = "#playTopic-dialog " if self.is_shared else ""
        for sel in (f"{prefix}.submit-btn", f"{prefix}.btn-submit",
                    f"{prefix}button.submit"):
            try:
                node = await page.query_selector(sel)
                if node:
                    await node.click(timeout=3000)
                    logger.info("答案已提交。")
                    return True
            except Exception:
                continue
        if await page.locator(self.POPUP_SEL).first.is_visible():
            # 两种智慧树视频弹题均可点选即判题，没有独立提交钮。
            return await self.detect_question(page)
        logger.warn("未找到提交按钮，答案保持已填写状态。")
        return False

    async def clear_selection(self, page: Page, question: Question) -> None:
        """取消已选中的选项。

        多选题不清空会越点越多，最后变成"全选"；
        单选题多数平台点新选项会自动换掉旧的，但不保证，所以一并处理。
        """
        if not await self._activate_question(page, question):
            return
        if question.qtype != "multiple":
            # 单选/判断点击新选项会替换原选项；重复点击旧选项会再次提交旧答案。
            return
        try:
            nodes = await page.query_selector_all(self.OPTION_SEL)
        except Exception:
            return
        for node in nodes:
            try:
                cls = await node.get_attribute("class") or ""
                selected = await node.query_selector(".active, .selected, .checked")
                # 只点掉当前处于选中态的，避免把未选的点成选中
                if selected or any(m in cls for m in ("active", "selected", "checked", "on")):
                    await node.click(timeout=2000)
                    await asyncio.sleep(0.12)
            except Exception:
                continue

    # 判定对错的 class 标记。平台改版时这里最容易失效，所以多列几个。
    RIGHT_MARKERS = ("right", "correct", "success", "is-right")
    WRONG_MARKERS = ("wrong", "error", "danger", "is-wrong")
    # 反馈提示所在的容器。不能全页搜文本——判断题的选项本身就叫"正确"，会误判
    FEEDBACK_SEL = ".el-message, .tips, .result-tip, .answer-result, .topic-result"

    async def read_feedback(self, page: Page) -> str:
        """判断平台是否认可本次作答。

        共享课必须看到明确判对标记；错题也可以手动关闭弹窗。
        其他播放页仍保留弹窗消失的判据。
        """
        await asyncio.sleep(0.8)  # 给平台一点渲染反馈的时间

        # 1. 弹窗消失 = 这题过了
        try:
            if not await self.detect_question(page):
                # 共享课的“关闭”按钮连错题也能关闭，不能据此判对。
                return "unknown" if self.is_shared else "correct"
        except Exception:
            return "unknown"

        dialog = page.locator(self.POPUP_SEL).first
        if await dialog.count() and await dialog.is_visible():
            if await dialog.locator(
                ".topic-title .error, .topic-title.error, "
                ".answer-zq .error, .answer-zq.error"
            ).count():
                return "wrong"
            if await dialog.locator(
                ".topic-title .right, .topic-title.right, "
                ".answer-zq .right, .answer-zq.right"
            ).count():
                return "correct"

        # 2. 选项上的对错标记
        try:
            for node in await page.query_selector_all(self.OPTION_SEL):
                cls = (await node.get_attribute("class") or "").lower()
                if any(m in cls for m in self.WRONG_MARKERS):
                    return "wrong"
                if any(m in cls for m in self.RIGHT_MARKERS):
                    return "correct"
        except Exception:
            pass

        # 3. 限定区域内的文字提示
        try:
            for node in await page.query_selector_all(self.FEEDBACK_SEL):
                text = (await node.text_content() or "").strip()
                if not text:
                    continue
                if any(w in text for w in ("错误", "答错", "不正确", "再想想", "重新")):
                    return "wrong"
                if any(w in text for w in ("正确", "答对", "回答对")):
                    return "correct"
        except Exception:
            pass

        return "unknown"

    async def confirm_and_close(self, page: Page) -> bool:
        """答对后点确认/继续，让视频接着播。"""
        if self.is_shared and self._current_question_index + 1 < self._popup_question_count:
            next_number = page.locator(self.POPUP_SEL).locator(".el-pager .number").nth(
                self._current_question_index + 1
            )
            if await next_number.count():
                await next_number.click(timeout=2000)
                return True
        footer = page.locator(f"{self.POPUP_SEL} .dialog-footer .btn").first
        if await footer.count() and await footer.is_visible():
            await footer.click(timeout=2000)
            await asyncio.sleep(0.4)
            if not await self.detect_question(page):
                return True
        prefix = "#playTopic-dialog " if self.is_shared else ""
        for sel in (f"{prefix}.confirm-btn", f"{prefix}.btn-confirm",
                    f"{prefix}.continue-btn", f"{prefix}.el-button--primary",
                    f"{prefix}.know-btn"):
            try:
                node = await page.query_selector(sel)
                if node:
                    await node.click(timeout=2000)
                    await asyncio.sleep(0.4)
                    if not await self.detect_question(page):
                        return True
            except Exception:
                continue
        await self.close_question(page)
        return not await self.detect_question(page)

    async def close_question(self, page: Page) -> None:
        """关闭弹窗。Escape 是智慧树最稳的关闭方式。"""
        dialog = page.locator(self.POPUP_SEL).first
        if await dialog.count() and await dialog.is_visible():
            result = dialog.locator(
                ".topic-title .error, .topic-title.error, .answer-zq .error, "
                ".answer-zq.error, .topic-title .right, .topic-title.right, "
                ".answer-zq .right, .answer-zq.right"
            )
            if not await result.count():
                logger.warn("视频弹题尚无判题结果，保留弹窗继续尝试作答。")
                return
        for attempt in (
            lambda: page.locator("#playTopic-dialog .close-btn, #playTopic-dialog .btn").first.click(timeout=2000),
            lambda: page.press(self.DIALOG_SEL, "Escape", timeout=2000),
            lambda: page.evaluate(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{bubbles:true, keyCode:27}));"
            ),
            lambda: page.click(".el-message-box__headerbtn", timeout=2000),
        ):
            try:
                await attempt()
                await asyncio.sleep(0.3)
                if not await self.detect_question(page):
                    return
            except Exception:
                continue

    # ---------- 风控 ----------

    async def detect_captcha(self, page: Page) -> bool:
        try:
            node = page.locator(self.CAPTCHA_SEL).first
            return bool(await node.count() and await node.is_visible())
        except Exception:
            return False

    async def captcha_cleared(self, page: Page) -> bool:
        return not await self.detect_captcha(page)
