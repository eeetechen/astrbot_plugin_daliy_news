import asyncio
import datetime
import os
import traceback
from pathlib import Path
from typing import Tuple

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

# 保存新闻的目录
SAVED_NEWS_DIR = Path("data", "plugin_data", "astrbot_plugin_daily_60s_news", "news")
SAVED_NEWS_DIR.mkdir(parents=True, exist_ok=True)


def _file_exists(path: str) -> bool:
    """
    判断新闻文件是否存在
    """
    return os.path.exists(path)


@register(
    "每日60s读懂世界",
    "eaton",
    "这是 AstrBot 的一个每日60s新闻插件。支持定时发送和命令发送",
    "0.0.2",
)
class Daily60sNewsPlugin(Star):
    """
    AstrBot 每日60s新闻插件，支持定时推送和命令获取。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.news_type = config.get("news_type", "indirect")
        self.news_path = SAVED_NEWS_DIR
        self.groups = self.config.groups
        self.push_time = self.config.push_time
        if self.news_type == "vikiboss_api":
            self.api = self.config.vikiboss_api
        elif self.news_type == "indirect":
            self.api = self.config.indirect
            self.img_key = self.config.img_key
            self.date_key = self.config.date_key
        elif self.news_type == "direct":
            self.img_url = self.config.direct
        self.is_debug = self.config.is_debug
        logger.info(f"插件配置: {self.config}")

        # 启动定时任务
        self._monitoring_task = asyncio.create_task(self._daily_task())

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_message(self, event: AstrMessageEvent):
        if self.is_debug:
            logger.debug("now print messages: "+ event.get_messages())
            logger.debug("now print message_type: " + event.get_message_type())
            logger.debug("now print raw_message: " + event.message_obj.raw_message())



    @filter.command_group("新闻")
    def mnews(self):
        """新闻命令分组"""
        pass

    @mnews.command("news", alias={"早报", "新闻"})
    async def daily_60s_news(self, event: AstrMessageEvent):
        """
        在当前聊天页面获取今日60s新闻（根据配置类型返回文本或图片）,
        别名：早报，新闻
        """
        news_path, _ = await self._get_image_news()
        yield event.image_result(news_path)


    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("status")
    async def check_status(self, event: AstrMessageEvent):
        """
        检查插件状态（仅管理员）
        """
        sleep_time = self._calculate_sleep_time()
        hours = int(sleep_time / 3600)
        minutes = int((sleep_time % 3600) / 60)

        yield event.plain_result(
            f"每日60s新闻插件正在运行\n"
            f"推送时间: {self.push_time}\n"
            f"距离下次推送还有: {hours}小时{minutes}分钟"
        )


    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("clean")
    async def clean_news(self, event: AstrMessageEvent):
        """
        清理过期新闻文件（仅管理员）
        """
        await self._delete_expired_news_files()
        yield event.plain_result(
            f"{event.get_sender_name()}: 过期({self.config.save_days}前)新闻文件已清理。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("push")
    async def push_news(self, event: AstrMessageEvent):
        """
        手动向目标群组推送今日60s新闻（仅管理员）
        """
        await self._send_daily_news_to_groups()
        yield event.plain_result(f"{event.get_sender_name()}:已成功向群组推送新闻")

    @mnews.command("image")
    async def push_image_news(self, event: AstrMessageEvent):
        """
        在当前聊天页面获取今日60s新闻-图片
        """
        news_path, _ = await self._get_image_news()
        yield event.image_result(news_path)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("update_news")
    async def update_news_files(self, event: AstrMessageEvent):
        """
        强制更新新闻文件（仅管理员）
        """
        await self._update_news_files()
        yield event.plain_result(
            f"{event.get_sender_name()}:今日新闻文件已更新..."
        )

    async def terminate(self):
        """插件卸载时调用"""
        if self._monitoring_task:
            self._monitoring_task.cancel()
        logger.info("每日60s新闻插件: 定时任务已停止")

    async def _update_news_files(self):
        logger.info("开始强制更新新闻文件...")
        image_path, _ = self._get_news_file_path()
        await self._download_news(path=image_path)

    def _get_news_file_path(self) -> Tuple[str, str]:
        """
        获取今日新闻文件的绝对路径和文件名
        :return: (文件绝对路径, 文件名)
        """
        current_date = datetime.datetime.now().strftime("%Y%m%d")
        name = f"{current_date}.jpeg"
        path = os.path.join(self.news_path, name)
        logger.info(f"mnews path: {path}")
        return path, name

    # async def _get_text_news(self) -> Tuple[str, bool]:
    #     """
    #     获取文本新闻内容，若本地无则下载
    #     :return: (新闻内容, 是否成功)
    #     """
    #     path, _ = self._get_news_file_path(news_type="text")
    #     if self._file_exists(path):
    #         with open(path, "r", encoding="utf-8") as f:
    #             data = f.read()
    #         return data, True
    #     else:
    #         return await self._download_news(path, news_type="text")

    async def _get_image_news(self) -> Tuple[str, bool]:
        """
        获取图片新闻路径，若本地无则下载
        :return: (图片路径, 是否成功)
        """
        path, _ = self._get_news_file_path()
        if _file_exists(path):
            return path, True
        else:
            return await self._download_news(path)

    async def _download_news(self, path: str) -> tuple[str, bool] | None:
        """
        下载今日新闻（图片），失败自动重试
        :param path: 保存路径
        :return: (内容或路径, 是否成功)
        """
        retries = 3
        timeout = 5
        date = datetime.datetime.now().strftime("%Y-%m-%d")

        for attempt in range(retries):
            try:
                if self.news_type == "vikiboss_api":
                    url = f"https://60s-api.viki.moe/v2/60s?date={date}&encoding=image-proxy"
                    logger.info(f"开始下载新闻文件:{url}...")
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=timeout) as response:
                            if response.status == 200:
                                content = await response.read()
                                with open(path, "wb") as f:
                                    f.write(content)
                                    return path, True
                            else:
                                raise Exception(f"API返回错误代码: {response.status}")
                elif self.news_type == "indirect":
                    url = self.api
                    logger.info(f"开始获取新闻数据:{url}...")
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=timeout) as response:
                            if response.status == 200:
                                # 解析JSON响应
                                data = await response.json()
                                if data.get("code") == 200:
                                    image_url = data.get("imageUrl")
                                    if not image_url:
                                        raise Exception("响应中未找到imageUrl字段")

                                    # 下载图片
                                    logger.info(f"开始下载图片:{image_url}...")
                                    async with session.get(image_url, timeout=timeout) as img_response:
                                        if img_response.status == 200:
                                            img_content = await img_response.read()
                                            with open(path, "wb") as f:
                                                f.write(img_content)
                                            return path, True
                                        else:
                                            raise Exception(f"图片下载失败: HTTP {img_response.status}")
                                else:
                                    raise Exception(f"API返回错误: {data.get('msg', '未知错误')}")
                            else:
                                raise Exception(f"API请求失败: HTTP {response.status}")
                elif self.news_type == "direct":
                    url = self.img_url
                    logger.info(f"开始下载新闻文件:{url}...")
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=timeout) as response:
                            if response.status == 200:
                                content = await response.read()
                                with open(path, "wb") as f:
                                    f.write(content)
                                    return path, True
                            else:
                                raise Exception(f"API返回错误代码: {response.status}")

            except Exception as e:
                logger.error(
                    f"[mnews] 请求失败，正在重试 {attempt + 1}/{retries} 次: {e}"
                )
                if attempt == retries - 1:
                    logger.error(f"[mnews] 请求新闻接口失败: {e}")
                    content = f"接口报错，请联系管理员:{e}"
                    return content, False
                await asyncio.sleep(1)
        return None

    async def _send_daily_news_to_groups(self):
        """
        推送新闻到所有目标群组
        """
        for target in self.config.groups:
            try:

                news_path, _ = await self._get_image_news()
                message_chain = (
                    MessageChain().message("每日新闻播报：").file_image(news_path)
                )
                logger.info(f"[每日新闻] 推送图片新闻: {news_path}")
                await self.context.send_message(target, message_chain)
                logger.info(f"[每日新闻] 已向{target}推送定时新闻。")
                await asyncio.sleep(2)  # 防止推送过快
            except Exception as e:
                error_message = str(e) if str(e) else "未知错误"
                logger.error(f"[每日新闻] 推送新闻失败: {error_message}")
                # 可选：记录堆栈跟踪信息
                logger.exception("详细错误信息：")
                await asyncio.sleep(2)  # 防止推送过快

    def _calculate_sleep_time(self) -> float:
        """
        计算距离下次推送的秒数
        :return: 距离下次推送的秒数
        """
        now = datetime.datetime.now()
        hour, minute = map(int, self.push_time.split(":"))
        next_push = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_push <= now:
            next_push += datetime.timedelta(days=1)
        return (next_push - now).total_seconds()

    async def _delete_expired_news_files(self):
        """
        删除过期新闻文件
        """
        save_days = self.config.save_days
        if save_days <= 0:
            raise ValueError("保存天数不能小于0")
        for filename in os.listdir(self.news_path):
            try:
                file_date = datetime.datetime.strptime(filename[:8], "%Y%m%d").date()
                if (datetime.date.today() - file_date).days >= save_days:
                    file_path = os.path.join(self.news_path, filename)
                    os.remove(file_path)
            except Exception:
                continue

    async def _daily_task(self):
        """
        定时任务主循环，定时推送新闻
        """
        while True:
            try:
                sleep_time = self._calculate_sleep_time()
                logger.info(f"[每日新闻] 下次推送将在 {sleep_time / 3600:.2f} 小时后")
                await asyncio.sleep(sleep_time)
                await self._update_news_files()
                await self._delete_expired_news_files()
                await self._send_daily_news_to_groups()
                await asyncio.sleep(60)  # 避免重复推送
            except Exception as e:
                logger.error(f"[每日新闻] 定时任务出错: {e}")
                traceback.print_exc()
                await asyncio.sleep(300)
