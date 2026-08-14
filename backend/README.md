# AI Customer Assistant — Backend

## Crawler v2 setup (Playwright)

The crawler v2 (sitemap-first discovery + Playwright page rendering) requires a
Chromium binary in addition to the Python package. Install the dependency and
the browser once:

```bash
uv add playwright
uv run playwright install --with-deps chromium
```

`--with-deps` installs the OS libraries Chromium needs (requires root/sudo on
Linux). This is a new environment requirement beyond the v1 (httpx-only)
crawler — run it in every environment that executes a crawl (local dev, CI).

## Tests

```bash
uv run pytest
```
