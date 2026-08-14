from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

class CrawlMode(str, Enum):
    PAGE = "PAGE"
    SITE = "SITE"

WaitStrategy = Literal["fixed_timeout", "networkidle", "selector"]

_VALID_WAIT_STRATEGIES = ("fixed_timeout", "networkidle", "selector")

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
    # How long to wait for a page to finish rendering before capturing HTML
    # (decision 7). fixed_timeout captures right after the load event --
    # unsuitable for SPA/client-rendered sites, where the DOM is still the
    # loading shell. networkidle waits for the network to go quiet (covers
    # delayed fetches); selector waits for a caller-specified element to
    # appear. Retry/backoff behaviour is identical across all three -- only
    # the wait condition changes.
    wait_strategy: WaitStrategy = "fixed_timeout"
    wait_selector: str | None = None  # required only when wait_strategy == "selector"

    def __post_init__(self) -> None:
        if self.wait_strategy not in _VALID_WAIT_STRATEGIES:
            raise ValueError(
                f"Invalid wait_strategy: {self.wait_strategy!r}. "
                f"Expected one of {_VALID_WAIT_STRATEGIES}."
            )
        if self.wait_strategy == "selector" and not self.wait_selector:
            raise ValueError(
                "wait_selector is required when wait_strategy == 'selector'"
            )
