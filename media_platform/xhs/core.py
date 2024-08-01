import asyncio
import os
import random
from asyncio import Task
from typing import Dict, List, Optional, Tuple

from playwright.async_api import (BrowserContext, BrowserType, Page,
                                  async_playwright)

import config
from base.base_crawler import AbstractCrawler
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import utils
from var import crawler_type_var

from .client import XiaoHongShuClient
from .exception import DataFetchError
from .field import SearchSortType
from .login import XiaoHongShuLogin


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext

    def __init__(self) -> None:
        self.index_url = "https://www.xiaohongshu.com"
        self.user_agent = utils.get_user_agent()
        # self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = self.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            # Launch a browser context.
            chromium = playwright.chromium
            self.browser_context = await self.launch_browser(
                chromium,
                None,
                self.user_agent,
                headless=config.HEADLESS
            )
            # stealth.min.js is a js script to prevent the website from detecting the crawler.
            await self.browser_context.add_init_script(path="libs/stealth.min.js")
            # add a cookie attribute webId to avoid the appearance of a sliding captcha on the webpage
            await self.browser_context.add_cookies([{
                'name': "webId",
                'value': "xxx123",  # any value
                'domain': ".xiaohongshu.com",
                'path': "/"
            }])
            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)

            # Create a client to interact with the xiaohongshu website.
            self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
            if not await self.xhs_client.pong():
                login_obj = XiaoHongShuLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # input your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES
                )
                await login_obj.begin()
                await self.xhs_client.update_cookies(browser_context=self.browser_context)

            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_notes()
            elif config.CRAWLER_TYPE == "creator":
                # Get creator's information and their notes and comments
                await self.get_creators_and_notes()
            elif config.CRAWLER_TYPE == "follow":  # 一键关注
                await self.follow()
            elif config.CRAWLER_TYPE == "follow-by-api":  # 通过api一键关注
                await self.follow_by_api()
            else:
                pass

            utils.logger.info("[XiaoHongShuCrawler.start] Xhs Crawler finished ...")

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.search] Begin search xiaohongshu keywords")
        xhs_limit_count = 20  # xhs limit page fixed value
        if config.CRAWLER_MAX_NOTES_COUNT < xhs_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = xhs_limit_count
        start_page = config.START_PAGE
        for keyword in config.KEYWORDS.split(","):
            utils.logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            page = 1
            while (page - start_page + 1) * xhs_limit_count <= config.CRAWLER_MAX_NOTES_COUNT:
                if page < start_page:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Skip page {page}")
                    page += 1
                    continue

                try:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] search xhs keyword: {keyword}, page: {page}")
                    note_id_list: List[str] = []
                    notes_res = await self.xhs_client.get_note_by_keyword(
                        keyword=keyword,
                        page=page,
                        sort=SearchSortType(config.SORT_TYPE) if config.SORT_TYPE != '' else SearchSortType.GENERAL,
                    )
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Search notes res:{notes_res}")
                    if not notes_res or not notes_res.get('has_more', False):
                        utils.logger.info("No more content!")
                        break
                    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                    task_list = [
                        self.get_note_detail(
                            note_id=post_item.get("id"),
                            xsec_source=post_item.get("xsec_source"),
                            xsec_token=post_item.get("xsec_token"),
                            semaphore=semaphore
                        )
                        for post_item in notes_res.get("items", {})
                        if post_item.get('model_type') not in ('rec_query', 'hot_query')
                    ]
                    note_details = await asyncio.gather(*task_list)
                    for note_detail in note_details:
                        if note_detail:
                            await xhs_store.update_xhs_note(note_detail)
                            await self.get_notice_media(note_detail)
                            note_id_list.append(note_detail.get("note_id"))
                    page += 1
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Note details: {note_details}")
                    await self.batch_get_note_comments(note_id_list)
                except DataFetchError:
                    utils.logger.error("[XiaoHongShuCrawler.search] Get note detail error")
                    break

    async def get_creators_and_notes(self) -> None:
        """Get creator's notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.get_creators_and_notes] Begin get xiaohongshu creators")
        for user_id in config.XHS_CREATOR_ID_LIST:
            # get creator detail info from web html content
            createor_info: Dict = await self.xhs_client.get_creator_info(user_id=user_id)
            if createor_info:
                await xhs_store.save_creator(user_id, creator=createor_info)

            # Get all note information of the creator
            all_notes_list = await self.xhs_client.get_all_notes_by_creator(
                user_id=user_id,
                crawl_interval=random.random(),
                callback=self.fetch_creator_notes_detail
            )

            note_ids = [note_item.get("note_id") for note_item in all_notes_list]
            await self.batch_get_note_comments(note_ids)

    async def follow(self) -> None:
        """访问每个URL并点击关注按钮。"""
        utils.logger.info("[XiaoHongShuCrawler.follow] 开始关注流程")
        # 小红书基础URL
        base_url = "https://www.xiaohongshu.com/user/profile/"

        user_ids = config.XHS_USER_ID
        total_ids = len(user_ids)
        utils.logger.info(f"[XiaoHongShuCrawler.follow] 总共有 {total_ids} 个用户ID需要关注")

        if total_ids > 100:
            utils.logger.warning("[XiaoHongShuCrawler.follow] 用户ID列表超过100，将分批执行")

        start = 0
        total_processed = 0
        while start < total_ids:
            # 随机确定批次大小（最小1，最大100）
            batch_size = random.randint(1, 100)
            batch = user_ids[start:start + batch_size]
            batch_count = len(batch)
            utils.logger.info(
                f"[XiaoHongShuCrawler.follow] 处理从第 {start} 条到第 {start + batch_count - 1} 条共 {batch_count} 个用户")

            for user_id in batch:
                url = base_url + user_id
                utils.logger.info(f"[XiaoHongShuCrawler.follow] 访问URL: {url}")
                await self.context_page.goto(url)

                # 频繁操作可能会需要验证，这里验证通过后等待30秒再执行以避免频繁操作
                if "请通过验证" in await self.context_page.content():
                    utils.logger.info("[XiaoHongShuLogin.check_login_state] 登录过程中出现验证码，请手动验证")
                    await asyncio.sleep(30)

                try:
                    # 查找关注按钮
                    follow_button = await self.context_page.query_selector('.follow-button')
                    if not follow_button:
                        utils.logger.warning(f"[XiaoHongShuCrawler.follow] 在 {url} 未找到关注按钮")
                        continue

                    # 查找关注按钮内的文本内容
                    follow_text_element = await follow_button.query_selector('.reds-button-new-text')
                    if not follow_text_element:
                        utils.logger.warning(f"[XiaoHongShuCrawler.follow] 在 {url} 未找到关注按钮文本元素")
                        continue

                    follow_text = await follow_text_element.evaluate('node => node.textContent.trim()')
                    if follow_text != "关注":
                        utils.logger.info(f"[XiaoHongShuCrawler.follow] 已经关注了用户 {user_id} ")
                        continue

                    # 点击关注按钮
                    await follow_button.click()
                    utils.logger.info(f"[XiaoHongShuCrawler.follow] 点击了 {url} 的关注按钮")

                except Exception as e:
                    utils.logger.error(f"[XiaoHongShuCrawler.follow] 点击 {url} 的关注按钮时出错: {str(e)}")

            start += batch_count
            total_processed += batch_count
            remaining = total_ids - start
            utils.logger.info(
                f"[XiaoHongShuCrawler.follow] 本次循环执行了 {batch_count} 条，累计执行了 {total_processed} 条，还剩 {remaining} 条未执行")

            if remaining > 0:
                # 随机确定休息时间（最小1秒，最大180秒）
                pause_duration = random.randint(1, 180)
                utils.logger.info(f"[XiaoHongShuCrawler.follow] 休息 {pause_duration} 秒")
                await asyncio.sleep(pause_duration)

        utils.logger.info("[XiaoHongShuCrawler.follow] 关注流程结束")

    async def follow_by_api(self) -> None:
        utils.logger.info("[XiaoHongShuCrawler.follow] 开始关注流程")

        user_ids = config.XHS_USER_ID
        total_ids = len(user_ids)
        utils.logger.info(f"[XiaoHongShuCrawler.follow] 总共有 {total_ids} 个用户ID需要关注")

        if total_ids > 100:
            utils.logger.warning("[XiaoHongShuCrawler.follow] 用户ID列表超过100，将分批执行")

        start = 0
        total_processed = 0
        while start < total_ids:
            # 随机确定批次大小（最小1，最大100）
            batch_size = random.randint(1, 100)
            batch = user_ids[start:start + batch_size]
            batch_count = len(batch)
            utils.logger.info(
                f"[XiaoHongShuCrawler.follow] 处理从第 {start} 条到第 {start + batch_count - 1} 条共 {batch_count} 个用户")

            for user_id in batch:
                utils.logger.info(f"[XiaoHongShuCrawler.follow]: ID --> {user_id}")
                await self.xhs_client.follow_user(user_id)

            start += batch_count
            total_processed += batch_count
            remaining = total_ids - start
            utils.logger.info(
                f"[XiaoHongShuCrawler.follow] 本次循环执行了 {batch_count} 条，累计执行了 {total_processed} 条，还剩 {remaining} 条未执行")

            if remaining > 0:
                # 随机确定休息时间（最小1秒，最大180秒）
                pause_duration = random.randint(1, 180)
                utils.logger.info(f"[XiaoHongShuCrawler.follow] 休息 {pause_duration} 秒")
                await asyncio.sleep(pause_duration)

        utils.logger.info(f"[XiaoHongShuCrawler.follow] 关注流程结束，共关注了 {total_processed} 个用户")

    async def fetch_creator_notes_detail(self, note_list: List[Dict]):
        """
        Concurrently obtain the specified post list and save the data
        """
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            self.get_note_detail(
                note_id=post_item.get("note_id"),
                xsec_source=post_item.get("xsec_source"),
                xsec_token=post_item.get("xsec_token"),
                semaphore=semaphore
            )
            for post_item in note_list
        ]

        note_details = await asyncio.gather(*task_list)
        for note_detail in note_details:
            if note_detail:
                await xhs_store.update_xhs_note(note_detail)

    async def get_specified_notes(self):
        """Get the information and comments of the specified post"""
        # todo 指定帖子爬取暂时失效，xhs更新了帖子详情的请求参数，需要携带xsec_token，目前发现该参数只能在搜索场景下获取到
        raise Exception(
            "指定帖子爬取暂时失效，xhs更新了帖子详情的请求参数，需要携带xsec_token，目前发现只能在搜索场景下获取到")
        # semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        # task_list = [
        #     self.get_note_detail(note_id=note_id, xsec_token="", semaphore=semaphore) for note_id in config.XHS_SPECIFIED_ID_LIST
        # ]
        # note_details = await asyncio.gather(*task_list)
        # for note_detail in note_details:
        #     if note_detail is not None:
        #         await xhs_store.update_xhs_note(note_detail)
        #         await self.get_notice_media(note_detail)
        # await self.batch_get_note_comments(config.XHS_SPECIFIED_ID_LIST)

    async def get_note_detail(self, note_id: str, xsec_source: str, xsec_token: str, semaphore: asyncio.Semaphore) -> \
            Optional[Dict]:
        """Get note detail"""
        async with semaphore:
            try:
                return await self.xhs_client.get_note_by_id(note_id, xsec_source, xsec_token)
            except DataFetchError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(
                    f"[XiaoHongShuCrawler.get_note_detail] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def batch_get_note_comments(self, note_list: List[str]):
        """Batch get note comments"""
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(
            f"[XiaoHongShuCrawler.batch_get_note_comments] Begin batch get note comments, note list: {note_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for note_id in note_list:
            task = asyncio.create_task(self.get_comments(note_id, semaphore), name=note_id)
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(self, note_id: str, semaphore: asyncio.Semaphore):
        """Get note comments with keyword filtering and quantity limitation"""
        async with semaphore:
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Begin get note id comments {note_id}")
            await self.xhs_client.get_note_all_comments(
                note_id=note_id,
                crawl_interval=random.random(),
                callback=xhs_store.batch_update_xhs_note_comments
            )

    @staticmethod
    def format_proxy_info(ip_proxy_info: IpInfoModel) -> Tuple[Optional[Dict], Optional[Dict]]:
        """format proxy info for playwright and httpx"""
        playwright_proxy = {
            "server": f"{ip_proxy_info.protocol}{ip_proxy_info.ip}:{ip_proxy_info.port}",
            "username": ip_proxy_info.user,
            "password": ip_proxy_info.password,
        }
        httpx_proxy = {
            f"{ip_proxy_info.protocol}": f"http://{ip_proxy_info.user}:{ip_proxy_info.password}@{ip_proxy_info.ip}:{ip_proxy_info.port}"
        }
        return playwright_proxy, httpx_proxy

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create xhs client"""
        utils.logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create xiaohongshu API client ...")
        cookie_str, cookie_dict = utils.convert_cookies(await self.browser_context.cookies())
        xhs_client_obj = XiaoHongShuClient(
            proxies=httpx_proxy,
            headers={
                "User-Agent": self.user_agent,
                "Cookie": cookie_str,
                "Origin": "https://www.xiaohongshu.com",
                "Referer": "https://www.xiaohongshu.com",
                "Content-Type": "application/json;charset=UTF-8"
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
        )
        return xhs_client_obj

    async def launch_browser(
            self,
            chromium: BrowserType,
            playwright_proxy: Optional[Dict],
            user_agent: Optional[str],
            headless: bool = True
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(os.getcwd(), "browser_data",
                                         config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent
            )
            return browser_context

    async def close(self):
        """Close browser context"""
        await self.browser_context.close()
        utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")

    async def get_notice_media(self, note_detail: Dict):
        if not config.ENABLE_GET_IMAGES:
            utils.logger.info(f"[XiaoHongShuCrawler.get_notice_media] Crawling image mode is not enabled")
            return
        await self.get_note_images(note_detail)
        await self.get_notice_video(note_detail)

    async def get_note_images(self, note_item: Dict):
        """
        get note images. please use get_notice_media
        :param note_item:
        :return:
        """
        if not config.ENABLE_GET_IMAGES:
            return
        note_id = note_item.get("note_id")
        image_list: List[Dict] = note_item.get("image_list", [])

        for img in image_list:
            if img.get('url_default') != '':
                img.update({'url': img.get('url_default')})

        if not image_list:
            return
        picNum = 0
        for pic in image_list:
            url = pic.get("url")
            if not url:
                continue
            content = await self.xhs_client.get_note_media(url)
            if content is None:
                continue
            extension_file_name = f"{picNum}.jpg"
            picNum += 1
            await xhs_store.update_xhs_note_image(note_id, content, extension_file_name)

    async def get_notice_video(self, note_item: Dict):
        """
        get note images. please use get_notice_media
        :param note_item:
        :return:
        """
        if not config.ENABLE_GET_IMAGES:
            return
        note_id = note_item.get("note_id")

        videos = xhs_store.get_video_url_arr(note_item)

        if not videos:
            return
        videoNum = 0
        for url in videos:
            content = await self.xhs_client.get_note_media(url)
            if content is None:
                continue
            extension_file_name = f"{videoNum}.mp4"
            videoNum += 1
            await xhs_store.update_xhs_note_image(note_id, content, extension_file_name)
