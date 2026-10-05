"""F2 adapter: only this module imports F2 or interprets its response schema."""
import asyncio
from datetime import datetime, timezone

from ..safe_network import client, video_id
from .contracts import ConfirmationRequired, LoginRequired, SourceMedia


class F2Source:
    name = "f2"

    def __init__(self, options=None):
        # Lazy import lets other providers run without installing F2.
        from f2.apps.douyin.utils import TokenManager, ClientConfManager
        from f2.apps.douyin.crawler import DouyinCrawler
        from f2.apps.douyin.model import PostDetail
        self.tokens, self.config = TokenManager, ClientConfManager
        self.crawler, self.detail = DouyinCrawler, PostDetail

    def headers(self):
        return self.config.headers()

    async def resolve_id(self, url):
        return await video_id(url, self.headers())

    async def describe(self, media_id, max_short_edge=1080):
        cookie = "ttwid=" + await asyncio.to_thread(self.tokens.gen_ttwid) + ";"
        async with self.crawler({"cookie": cookie, "headers": self.headers(),
                                 "timeout": 30, "max_retries": 2}) as crawler:
            crawler._aclient = client(headers=crawler.crawler_headers, timeout=30, follow_redirects=True)
            raw = await crawler.fetch_post_detail(self.detail(aweme_id=media_id))
        return self.normalize(media_id, raw, max_short_edge)

    @staticmethod
    def normalize(media_id, raw, max_short_edge=1080):
        item = raw.get("aweme_detail")
        if not item or not item.get("author"):
            raise LoginRequired("Public visitor metadata unavailable")
        video = item["video"]
        if video.get("duration", 0) > 1800000:
            raise ConfirmationRequired("Video exceeds 30 minutes")
        candidates = []
        for candidate in video.get("bit_rate", []):
            address = candidate.get("play_addr", {})
            width, height = address.get("width", video.get("width", 0)), address.get("height", video.get("height", 0))
            if width and height and min(width, height) <= max_short_edge and address.get("url_list"):
                candidates.append((min(width, height), candidate.get("bit_rate", 0), candidate))
        if not candidates:
            raise RuntimeError("No verified video candidate at or below resolution policy")
        selected = max(candidates, key=lambda value: value[:2])[2]
        description = item.get("desc", "")
        return SourceMedia(
            media_id=media_id, title=item.get("item_title") or item.get("preview_title") or description.split("\n")[0],
            description=description, author=item["author"]["nickname"],
            published_at=datetime.fromtimestamp(item["create_time"], timezone.utc).isoformat(),
            duration_seconds=video["duration"] / 1000,
            canonical_url=f"https://www.douyin.com/video/{media_id}",
            video_urls=tuple(selected["play_addr"]["url_list"]),
            cover_urls=tuple((video.get("origin_cover") or video["cover"])["url_list"]),
            selection={"gear": selected.get("gear_name"), "bit_rate": selected.get("bit_rate"),
                       "address": selected["play_addr"]},
            expected_bytes=selected["play_addr"].get("data_size", 0), raw_metadata=raw,
        )
