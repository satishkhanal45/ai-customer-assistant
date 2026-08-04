from dataclasses import dataclass, field
from enum import Enum

class CrawlMode(str, Enum):
    PAGE = "PAGE"
    SITE = "SITE"

@dataclass(frozen=True)
class CrawlConfig:
    mode: CrawlMode = CrawlMode.PAGE
    max_depth: int = 2
    max_pages: int = 50
    request_timeout: float = 15.0
    concurrent_requests: int = 5
    retry_count: int = 2
    delay_between_requests: float = 0.0
    user_agent: str = "ai-customer-assistant-crawler/1.0"
    respect_robots_txt: bool = False
    allowed_domains: tuple[str, ...] = field(default_factory=tuple)
    follow_redirects: bool = True
