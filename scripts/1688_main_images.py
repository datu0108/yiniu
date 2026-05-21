#!/usr/bin/env python3
"""下载 1688 商品主图。

推荐用法（半自动，滑块可正常拖动）：
    bash scripts/open_chrome_debug.sh   # 1. 打开「真·Chrome」（非自动化窗口）
    # 2. 在 Chrome 里手动搜索关键词，通过滑块
    .venv/bin/python scripts/1688_main_images.py -k 纯棉毛巾 -n 20 --all-main   # 全部主图
    # 默认只下列表首图；加 --all-main 会逐个进详情页下载 main_01~05.jpg

全自动（易被 1688 识别，滑块可能失败）：
    .venv/bin/python scripts/1688_main_images.py -k 关键词 -n 20 --auto-browser
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse

import requests

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "请先安装依赖:\n"
        "  bash scripts/setup_1688.sh\n"
        "或: python3 -m pip install -r requirements-scrape.txt"
    ) from exc

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
OFFER_ID_RE = re.compile(r"(?:offer/|offerId=)(\d{8,})")
LIST_PAGE_HINTS = ("offer_search.htm", "offerlist", "search.htm", "selloffer")
ALICDN_IMG_RE = re.compile(
    r"https?://[^\"'\s]*?alicdn\.com/img/ibank/O1CN01[\w-]+\.(?:jpg|jpeg|png|webp)(?:_\.\w+)?",
    re.IGNORECASE,
)
LIST_JSON_OFFER_RE = re.compile(
    r'"offerId"\s*:\s*"?(\d{8,})"?\s*',
    re.IGNORECASE,
)
LIST_JSON_IMG_RE = re.compile(
    r'"(?:imageUrl|picUrl|odPicUrl|imgUrl|originalUrl|summImageUrl)"\s*:\s*"(//[^"]+|https?://[^"]+)"',
    re.IGNORECASE,
)


@dataclass
class ListProduct:
    offer_id: str
    thumb_url: str | None
    title: str | None
    detail_url: str


def parse_offer_id(value: str) -> str:
    """从 URL 或纯数字中提取 offerId。"""
    value = value.strip()
    if value.isdigit():
        return value
    m = OFFER_ID_RE.search(value)
    if m:
        return m.group(1)
    path = urlparse(value).path
    m = re.search(r"/(\d{8,})\.html?", path)
    if m:
        return m.group(1)
    raise ValueError(f"无法解析商品 ID: {value}")


def offer_detail_url(offer_id: str) -> str:
    return f"https://detail.1688.com/offer/{offer_id}.html"


def is_list_page_url(url: str) -> bool:
    lower = url.lower()
    return any(h in lower for h in LIST_PAGE_HINTS)


def build_search_url(keyword: str) -> str:
    return f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(keyword.strip())}"


def safe_dirname(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    return (cleaned[:80] or "search").strip("_")


def page_needs_captcha(html: str) -> bool:
    return "x5secdata" in html or ("punish" in html and "captcha" in html.lower())


def default_browser_data_dir() -> Path:
    return Path.home() / ".1688_main_images_browser"


DEFAULT_CDP_URL = "http://127.0.0.1:9222"

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
"""


def system_browser_channel() -> str | None:
    """优先用本机已安装的 Chrome/Edge，免下载 170MB Chromium。"""
    if Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists():
        return "chrome"
    if Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge").exists():
        return "msedge"
    return None


def _playwright_launch_kwargs() -> dict:
    """降低被识别为自动化的概率（仍不如「连接真 Chrome」可靠）。"""
    channel = system_browser_channel()
    kwargs: dict = {
        "headless": False,
        "viewport": {"width": 1400, "height": 900},
        "ignore_default_args": ["--enable-automation", "--no-sandbox"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if channel:
        kwargs["channel"] = channel
    else:
        kwargs["user_agent"] = DEFAULT_UA
    return kwargs


def _apply_stealth(context) -> None:
    try:
        context.add_init_script(STEALTH_INIT_SCRIPT)
    except Exception:
        pass


def _upgrade_list_thumb(url: str) -> str:
    """列表缩略图 URL 尽量还原为较大尺寸。"""
    url = normalize_image_url(url)
    url = re.sub(r"_\d+x\d+\.(jpg|jpeg|png|webp)", r".\1", url, flags=re.IGNORECASE)
    url = re.sub(r"\.sum\.(jpg|jpeg|png)", r".\1", url, flags=re.IGNORECASE)
    url = url.replace(".jpg_sum", ".jpg")
    return url


def _extract_list_from_json(html: str) -> dict[str, ListProduct]:
    """从页面内嵌 JSON（搜索/店铺列表接口数据）提取商品。"""
    found: dict[str, ListProduct] = {}
    for m in LIST_JSON_OFFER_RE.finditer(html):
        offer_id = m.group(1)
        if offer_id in found:
            continue
        chunk = html[m.start() : m.start() + 1200]
        img_m = LIST_JSON_IMG_RE.search(chunk)
        thumb = _upgrade_list_thumb(img_m.group(1)) if img_m else None
        title_m = re.search(r'"(?:title|subject|offerTitle)"\s*:\s*"([^"]{2,200})"', chunk)
        title = title_m.group(1) if title_m else None
        found[offer_id] = ListProduct(
            offer_id=offer_id,
            thumb_url=thumb,
            title=title,
            detail_url=offer_detail_url(offer_id),
        )
    return found


def _card_image_from_anchor(anchor) -> str | None:
    node = anchor
    for _ in range(10):
        if node is None:
            break
        for img in node.select("img"):
            src = (
                img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-sf-original-src")
                or img.get("src")
            )
            if src and "alicdn.com" in src and not src.startswith("data:"):
                return _upgrade_list_thumb(src)
        node = node.parent
    return None


def extract_list_products(html: str) -> list[ListProduct]:
    """从搜索/店铺列表页 HTML 解析商品（offerId + 列表首图）。"""
    by_id = _extract_list_from_json(html)
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "offer/" not in href and "offerId=" not in href:
            continue
        try:
            offer_id = parse_offer_id(href)
        except ValueError:
            continue
        if offer_id in by_id and by_id[offer_id].thumb_url:
            continue

        thumb = _card_image_from_anchor(a)
        title = (a.get("title") or "").strip() or None
        if not title:
            t = a.find(string=True, recursive=False)
            if t and len(str(t).strip()) > 2:
                title = str(t).strip()[:200]

        detail_url = href if href.startswith("http") else urljoin("https://detail.1688.com/", href)
        by_id[offer_id] = ListProduct(
            offer_id=offer_id,
            thumb_url=thumb or (by_id[offer_id].thumb_url if offer_id in by_id else None),
            title=title or (by_id[offer_id].title if offer_id in by_id else None),
            detail_url=detail_url,
        )

    items = list(by_id.values())
    items.sort(key=lambda x: x.offer_id)
    return items


def normalize_image_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if url.endswith("_.webp"):
        url = url[:-6]
    url = url.replace(".jpg_sum", "")
    return url


def _image_id(url: str) -> str | None:
    m = re.search(r"O1CN01\w+", url)
    return m.group(0) if m else None


def _extract_offer_img_list_from_context(html: str) -> list[str]:
    """从 window.context 的 gallery.offerImgList 提取主图。"""
    start_marker = "window.context=(function(b,d){"
    end_marker = "})(window.contextPath,"
    start_idx = html.find(start_marker)
    if start_idx == -1:
        return []
    json_start = html.find(end_marker, start_idx)
    if json_start == -1:
        return []
    json_start += len(end_marker)

    brace = 0
    json_end = json_start
    for i in range(json_start, len(html)):
        ch = html[i]
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace == 0:
                json_end = i + 1
                break
    else:
        return []

    try:
        data = json.loads(html[json_start:json_end])
    except json.JSONDecodeError:
        return []

    gallery = (
        data.get("result", {})
        .get("data", {})
        .get("gallery", {})
        .get("fields", {})
    )
    imgs = gallery.get("offerImgList") or gallery.get("mainImage") or []
    if isinstance(imgs, str):
        imgs = [imgs]
    return [normalize_image_url(u) for u in imgs if isinstance(u, str) and u]


def _extract_from_gallery_dom(soup: BeautifulSoup) -> list[str]:
    """从详情页 DOM 画廊区域提取主图 URL。"""
    urls: list[str] = []
    selectors = [
        "motion.div.img-list-wrapper",
        "div.img-list-wrapper",
        "ul.od-gallery-list",
        "motion.div.module-od-picture-gallery",
        "div.module-od-picture-gallery",
    ]

    for selector in selectors:
        for element in soup.select(selector):
            classes = element.get("class") or []
            if any("recommend-gallery" in c for c in classes):
                continue

            for wrapper in element.select("div.od-gallery-turn-item-wrapper"):
                if wrapper.select_one(".od-video-wrapper, .prepic-video, img.video-icon"):
                    continue
                img = wrapper.select_one("img.od-gallery-img")
                if not img:
                    continue
                src = img.get("data-sf-original-src") or img.get("src")
                if src and not src.startswith("data:"):
                    urls.append(normalize_image_url(src))

            if not urls:
                for img in element.select("img.ant-image-img"):
                    if "video-icon" in (img.get("class") or []):
                        continue
                    src = img.get("data-sf-original-src") or img.get("src")
                    if src and not src.startswith("data:"):
                        urls.append(normalize_image_url(src))

            if urls:
                break
        if urls:
            break

    # 去重且保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def _dedupe_main_vs_sku(main_urls: list[str], soup: BeautifulSoup) -> list[str]:
    """尽量排除 SKU 色卡小图，保留主图轮播。"""
    sku_ids: set[str] = set()
    for img in soup.select("button.sku-filter-button img, div.expand-view-item img"):
        src = img.get("data-sf-original-src") or img.get("src")
        if not src:
            continue
        img_id = _image_id(normalize_image_url(src))
        if img_id:
            sku_ids.add(img_id)

    if not sku_ids:
        return main_urls[:5] if len(main_urls) > 5 else main_urls

    non_sku = [u for u in main_urls if _image_id(u) not in sku_ids]
    if len(non_sku) >= 5:
        return non_sku[:5]
    if len(main_urls) == 5:
        return main_urls
    result = list(non_sku)
    for u in main_urls:
        if u not in result:
            result.append(u)
        if len(result) >= 5:
            break
    return result


def extract_main_images(html: str) -> list[str]:
    """从详情页 HTML 解析主图 URL 列表。"""
    from_context = _extract_offer_img_list_from_context(html)
    if from_context:
        return from_context

    soup = BeautifulSoup(html, "html.parser")
    dom_urls = _extract_from_gallery_dom(soup)
    if dom_urls:
        return _dedupe_main_vs_sku(dom_urls, soup)

    # 兜底：页面内所有 ibank 主图（可能含详情图，仅作最后手段）
    fallback = []
    seen: set[str] = set()
    for m in ALICDN_IMG_RE.finditer(html):
        url = normalize_image_url(m.group(0))
        if url not in seen:
            seen.add(url)
            fallback.append(url)
    return fallback[:5]


def load_cookie_string(cookie_arg: str | None, cookie_file: Path | None) -> str | None:
    if cookie_arg:
        return cookie_arg.strip()
    if cookie_file and cookie_file.is_file():
        return cookie_file.read_text(encoding="utf-8").strip()
    return None


def fetch_html_requests(
    url: str,
    cookie: str | None,
    timeout: float,
) -> str:
    headers = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["Cookie"] = cookie
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    text = resp.text
    if "punish" in text and ("captcha" in text.lower() or "x5secdata" in text):
        raise RuntimeError(
            "触发 1688 验证码/风控。请改用 --html 保存的页面，或 --playwright，或更新 Cookie。"
        )
    return text


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "一键搜索需要 Playwright 库：\n"
            "  bash scripts/setup_1688.sh\n"
            "若本机已装 Google Chrome，无需再下载 Chromium。"
        ) from exc
    from playwright.sync_api import sync_playwright

    return sync_playwright


def _launch_browser(p, *, browser_data_dir: Path | None = None):
    """启动浏览器：优先本机 Chrome，否则用 Playwright 自带的 Chromium。"""
    channel = system_browser_channel()
    if channel:
        print(f"使用本机浏览器（channel={channel}）")
    if browser_data_dir is not None:
        context = p.chromium.launch_persistent_context(
            str(browser_data_dir),
            **_playwright_launch_kwargs(),
        )
        _apply_stealth(context)
        return context
    launch_kw = _playwright_launch_kwargs()
    launch_kw.pop("viewport", None)
    browser = p.chromium.launch(**launch_kw)
    return browser


def _pick_1688_page(browser) -> object:
    """从已连接的 Chrome 里选 1688 搜索结果页。"""
    for context in browser.contexts:
        for page in context.pages:
            if "1688.com" in (page.url or ""):
                return page
    if browser.contexts and browser.contexts[0].pages:
        return browser.contexts[0].pages[-1]
    raise RuntimeError("Chrome 里没有打开任何页面")


def _collect_products_from_page(page, count: int) -> list[ListProduct]:
    """在当前列表页滚动并解析商品。"""
    products: dict[str, ListProduct] = {}
    stagnant = 0
    max_rounds = max(40, count // 8 + 10)
    for _ in range(max_rounds):
        for item in extract_list_products(page.content()):
            products[item.offer_id] = item
        n = len(products)
        print(f"  已识别 {n} 个商品…")
        if n >= count:
            break
        prev = n
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
        for item in extract_list_products(page.content()):
            products[item.offer_id] = item
        if len(products) == prev:
            stagnant += 1
            if stagnant >= 3:
                print("  列表似乎已到底，停止滚动")
                break
        else:
            stagnant = 0

    result = sorted(products.values(), key=lambda x: x.offer_id)
    if not result:
        raise RuntimeError("当前页面未解析到商品，请确认已在 1688 打开搜索结果列表")
    if len(result) < count:
        print(f"提示: 只找到 {len(result)} 个商品（少于目标的 {count}）")
    return result[:count]


def _cookies_from_browser(browser) -> str | None:
    if not browser.contexts:
        return None
    parts = [
        f"{c['name']}={c['value']}"
        for c in browser.contexts[0].cookies()
        if c.get("name") and c.get("value") is not None
    ]
    return "; ".join(parts) if parts else None


def _download_all_main_via_browser(
    page,
    products: list[ListProduct],
    save_root: Path,
    *,
    cookie: str | None,
    timeout: float,
    delay: float,
    download: bool,
) -> int:
    """在已连接的 Chrome 里逐个打开详情页，解析并下载全部主图。"""
    timeout_ms = int(timeout * 1000)
    print(f"\n逐个打开详情页，下载全部主图（共 {len(products)} 个商品，间隔 {delay}s）…")
    errors = 0
    for i, prod in enumerate(products, 1):
        label = (prod.title or prod.offer_id)[:50]
        url = offer_detail_url(prod.offer_id)
        print(f"\n[{i}/{len(products)}] {label}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)
            html = page.content()
            if page_needs_captcha(html):
                print("  详情页需验证，请在 Chrome 完成操作后按 Enter …")
                input()
                html = page.content()
            images = extract_main_images(html)
            if not images:
                print("  未解析到主图，跳过")
                continue
            for n, img_url in enumerate(images, 1):
                print(f"  [{n}] {img_url[:90]}…")
            if download:
                out_dir = save_root / prod.offer_id
                download_all(images, out_dir, cookie, timeout)
        except Exception as exc:
            errors += 1
            print(f"  失败: {exc}", file=sys.stderr)
        if i < len(products):
            time.sleep(delay)
    return errors


def fetch_search_connect_session(
    keyword: str,
    count: int,
    *,
    cdp_url: str,
    full_detail: bool,
    save_root: Path,
    timeout: float,
    delay: float,
    download: bool,
) -> list[ListProduct]:
    """连接 Chrome：解析列表；可选在同一浏览器会话中抓取全部主图。"""
    sync_playwright = _require_playwright()
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(
                f"无法连接 Chrome（{cdp_url}）。请先执行: bash scripts/open_chrome_debug.sh"
            ) from exc

        page = _pick_1688_page(browser)
        print(f"已连接 Chrome，当前页: {page.url[:80]}…")
        print(f"\n请在 Chrome 里搜索「{keyword}」，商品列表全部出来后，回到终端按 Enter …")
        input()
        page.wait_for_timeout(1500)

        if page_needs_captcha(page.content()):
            print("当前仍是验证页，请先在 Chrome 里完成滑块，再按 Enter …")
            input()

        products = _collect_products_from_page(page, count)
        cookie = _cookies_from_browser(browser)

        if full_detail:
            errors = _download_all_main_via_browser(
                page,
                products,
                save_root,
                cookie=cookie,
                timeout=timeout,
                delay=delay,
                download=download,
            )
            if errors:
                print(f"\n完成，{errors} 个商品失败")
            elif download:
                print(f"\n完成 -> {save_root}/<offerId>/main_01.jpg …")
        elif download:
            session = requests.Session()
            session.headers.update(
                {"User-Agent": DEFAULT_UA, "Referer": "https://s.1688.com/"}
            )
            if cookie:
                session.headers["Cookie"] = cookie
            saved = 0
            for i, prod in enumerate(products, 1):
                if not prod.thumb_url:
                    continue
                dest = save_root / prod.offer_id / "main_01.jpg"
                print(f"[{i}/{len(products)}] {prod.offer_id}")
                download_image(session, prod.thumb_url, dest, timeout)
                saved += 1
                time.sleep(0.2)
            print(f"\n已下载列表首图 {saved} 张（每张仅 1 张；要全部主图请加 --all-main）")

        browser.close()
        return products


def fetch_html_playwright(url: str, timeout_ms: int, *, scroll_times: int = 0) -> str:
    sync_playwright = _require_playwright()
    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(5000)
        for _ in range(scroll_times):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
        html = page.content()
        browser.close()
    return html


def fetch_search_products_playwright(
    keyword: str,
    count: int,
    *,
    timeout: float,
    browser_data_dir: Path,
    wait_captcha: bool,
) -> list[ListProduct]:
    """打开 1688 搜索页，滚动加载并解析商品列表。"""
    sync_playwright = _require_playwright()
    url = build_search_url(keyword)
    timeout_ms = int(timeout * 1000)
    browser_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = _launch_browser(p, browser_data_dir=browser_data_dir)
        page = context.pages[0] if context.pages else context.new_page()
        print(f"搜索关键词: {keyword}")
        print(f"目标数量: {count}")
        print(f"打开: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3000)

        if page_needs_captcha(page.content()):
            if wait_captcha:
                print(
                    "\n检测到验证码。全自动模式下滑块常无法拖动。\n"
                    "自动化模式：等待 10 秒尝试自动绕过（或请人工干预）…\n"
                )
                page.wait_for_timeout(10000)
                # input() # 已移除人工回车
                page.wait_for_timeout(2000)
            else:
                raise RuntimeError(
                    "遇到验证码。请用半自动模式（不要加 --auto-browser），见 scripts/open_chrome_debug.sh"
                )

        result = _collect_products_from_page(page, count)
        context.close()
        return result


def fetch_page_html(
    url: str,
    *,
    cookie: str | None,
    use_playwright: bool,
    timeout: float,
    scroll_times: int = 0,
) -> str:
    if use_playwright:
        return fetch_html_playwright(url, int(timeout * 1000), scroll_times=scroll_times)
    return fetch_html_requests(url, cookie, timeout)


def download_image(session: requests.Session, url: str, dest: Path, timeout: float) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def download_all(
    urls: Iterable[str],
    out_dir: Path,
    cookie: str | None,
    timeout: float,
) -> list[Path]:
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Referer": "https://detail.1688.com/"})
    if cookie:
        session.headers["Cookie"] = cookie

    saved: list[Path] = []
    for idx, url in enumerate(urls, start=1):
        ext = ".jpg"
        if ".png" in url.lower():
            ext = ".png"
        elif ".webp" in url.lower():
            ext = ".webp"
        dest = out_dir / f"main_{idx:02d}{ext}"
        download_image(session, url, dest, timeout)
        saved.append(dest)
        print(f"  已保存: {dest.name} <- {url[:80]}...")
        time.sleep(0.3)
    return saved


def process_one(
    source: str,
    *,
    html_path: Path | None,
    out_root: Path,
    cookie: str | None,
    use_playwright: bool,
    timeout: float,
    download: bool,
) -> list[str]:
    if html_path:
        html = html_path.read_text(encoding="utf-8", errors="replace")
        offer_id = parse_offer_id(source) if source else _guess_offer_id(html)
        if not offer_id:
            offer_id = html_path.stem
    else:
        offer_id = parse_offer_id(source)
        url = offer_detail_url(offer_id)
        print(f"抓取详情: {url}")
        html = fetch_page_html(
            url,
            cookie=cookie,
            use_playwright=use_playwright,
            timeout=timeout,
        )

    images = extract_main_images(html)
    if not images:
        raise RuntimeError(f"未解析到主图 (offerId={offer_id})")

    print(f"商品 {offer_id} 共 {len(images)} 张主图:")
    for i, u in enumerate(images, 1):
        print(f"  [{i}] {u}")

    if download:
        out_dir = out_root / offer_id
        print(f"下载到: {out_dir}")
        download_all(images, out_dir, cookie, timeout)

    return images


def process_list(
    source: str,
    *,
    list_html_path: Path | None,
    out_root: Path,
    cookie: str | None,
    use_playwright: bool,
    timeout: float,
    download: bool,
    full_detail: bool,
    delay: float,
    scroll_times: int,
    export_offers: Path | None,
) -> list[ListProduct]:
    if list_html_path:
        html = list_html_path.read_text(encoding="utf-8", errors="replace")
    else:
        url = source.strip()
        if not url.startswith("http"):
            raise ValueError("列表模式请提供完整 URL，或改用 --list-html 本地 HTML")
        print(f"抓取列表: {url}")
        html = fetch_page_html(
            url,
            cookie=cookie,
            use_playwright=use_playwright,
            timeout=timeout,
            scroll_times=scroll_times,
        )

    products = extract_list_products(html)
    if not products:
        raise RuntimeError(
            "未从列表页解析到商品。请用 SingleFile 保存「已加载完成」的搜索/店铺列表页后再 --list-html。"
        )

    print(f"列表共 {len(products)} 个商品")
    if export_offers:
        export_offers.parent.mkdir(parents=True, exist_ok=True)
        export_offers.write_text(
            "\n".join(p.offer_id for p in products) + "\n",
            encoding="utf-8",
        )
        print(f"已导出 offerId 列表: {export_offers}")

    if full_detail:
        print("逐个抓取详情页主图（较慢）…")
        errors = 0
        for i, p in enumerate(products, 1):
            print(f"\n[{i}/{len(products)}] offerId={p.offer_id}")
            try:
                process_one(
                    p.offer_id,
                    html_path=None,
                    out_root=out_root,
                    cookie=cookie,
                    use_playwright=use_playwright,
                    timeout=timeout,
                    download=download,
                )
            except Exception as exc:
                errors += 1
                print(f"  失败: {exc}", file=sys.stderr)
            if i < len(products):
                time.sleep(delay)
        if errors:
            raise RuntimeError(f"详情抓取完成，{errors} 个商品失败")
        return products

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Referer": "https://s.1688.com/"})
    if cookie:
        session.headers["Cookie"] = cookie

    saved = 0
    for p in products:
        label = (p.title or p.offer_id)[:40]
        if not p.thumb_url:
            print(f"  跳过（无列表图）: {label}")
            continue
        print(f"  {p.offer_id} | {label}")
        print(f"    {p.thumb_url}")
        if download:
            out_dir = out_root / p.offer_id
            dest = out_dir / "main_01.jpg"
            download_image(session, p.thumb_url, dest, timeout)
            saved += 1
            time.sleep(0.2)

    if download:
        print(f"\n已下载列表首图 {saved} 张 -> {out_root}/<offerId>/main_01.jpg")
    return products


def process_search(
    keyword: str,
    count: int,
    *,
    out_root: Path,
    timeout: float,
    browser_data_dir: Path,
    wait_captcha: bool,
    download: bool,
    use_connect: bool,
    cdp_url: str,
    full_detail: bool,
    delay: float,
    cookie: str | None,
    use_playwright: bool,
) -> list[ListProduct]:
    """一键：解析搜索列表 → 下载主图（默认列表首图；--all-main 为详情页全部主图）。"""
    save_root = out_root / safe_dirname(keyword)

    if use_connect:
        return fetch_search_connect_session(
            keyword,
            count,
            cdp_url=cdp_url,
            full_detail=full_detail,
            save_root=save_root,
            timeout=timeout,
            delay=delay,
            download=download,
        )

    print("全自动打开浏览器（易被风控，滑块可能失败）…")
    products = fetch_search_products_playwright(
        keyword,
        count,
        timeout=timeout,
        browser_data_dir=browser_data_dir,
        wait_captcha=wait_captcha,
    )
    print(f"\n共 {len(products)} 个商品，保存目录: {save_root}")

    if not download and not full_detail:
        for p in products:
            print(f"  {p.offer_id} | {(p.title or '')[:50]} | {p.thumb_url}")
        return products

    if full_detail:
        print("逐个抓取详情页全部主图（较慢）…")
        errors = 0
        for i, p in enumerate(products, 1):
            print(f"\n[{i}/{len(products)}] offerId={p.offer_id}")
            try:
                process_one(
                    p.offer_id,
                    html_path=None,
                    out_root=save_root,
                    cookie=cookie,
                    use_playwright=use_playwright,
                    timeout=timeout,
                    download=True,
                )
            except Exception as exc:
                errors += 1
                print(f"  失败: {exc}", file=sys.stderr)
            if i < len(products):
                time.sleep(delay)
        if errors:
            raise RuntimeError(f"详情抓取完成，{errors} 个商品失败")
        return products

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Referer": "https://s.1688.com/"})
    if cookie:
        session.headers["Cookie"] = cookie
    saved = 0
    for i, p in enumerate(products, 1):
        label = (p.title or p.offer_id)[:50]
        if not p.thumb_url:
            print(f"[{i}/{len(products)}] 跳过（无图）: {label}")
            continue
        dest = save_root / p.offer_id / "main_01.jpg"
        print(f"[{i}/{len(products)}] {label}")
        download_image(session, p.thumb_url, dest, timeout)
        saved += 1
        time.sleep(0.25)

    print(f"\n完成：已下载列表首图 {saved} 张（要全部主图请加 --all-main）")
    return products


def prompt_keyword_and_count() -> tuple[str, int]:
    print("1688 商品主图 · 一键下载\n")
    keyword = input("请输入商品关键词: ").strip()
    if not keyword:
        raise ValueError("关键词不能为空")
    raw = input("请输入需要下载的商品数量 [默认 20]: ").strip() or "20"
    count = int(raw)
    if count < 1:
        raise ValueError("数量至少为 1")
    return keyword, count


def _guess_offer_id(html: str) -> str | None:
    m = OFFER_ID_RE.search(html)
    return m.group(1) if m else None


def read_url_list(path: Path) -> list[str]:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="下载 1688 商品主图。推荐: python %(prog)s -k 关键词 -n 数量",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "推荐（半自动，滑块正常）:\n"
            "  bash scripts/open_chrome_debug.sh\n"
            "  %(prog)s -k 键盘 -n 10 --all-main\n"
            "  # 在 Chrome 里手动搜索，列表出现后按 Enter；--all-main 下载每个商品全部主图\n"
        ),
    )
    p.add_argument("-k", "--keyword", help="搜索关键词（一键模式）")
    p.add_argument(
        "-n",
        "--count",
        type=int,
        default=20,
        help="需要下载的商品数量（默认 20，配合 -k 使用）",
    )

    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("url", nargs="?", help="商品链接、列表页 URL 或 offerId（高级）")
    src.add_argument("-f", "--file", type=Path, help="每行一个链接/offerId 的文本文件")

    p.add_argument("-o", "--output", type=Path, default=Path("1688_images"), help="图片输出目录")
    p.add_argument(
        "--browser-data",
        type=Path,
        default=None,
        help=f"Playwright 登录态目录（默认 {default_browser_data_dir()}）",
    )
    p.add_argument(
        "--no-wait-captcha",
        action="store_true",
        help="遇到验证码立即失败，不等待手动处理",
    )
    p.add_argument(
        "--auto-browser",
        action="store_true",
        help="全自动打开浏览器（易被 1688 识别，滑块常失败；默认用连接真 Chrome）",
    )
    p.add_argument(
        "--cdp-url",
        default=DEFAULT_CDP_URL,
        help=f"半自动模式连接的 Chrome 调试地址（默认 {DEFAULT_CDP_URL}）",
    )
    p.add_argument("--html", type=Path, help="本地已保存的详情页 HTML")
    p.add_argument(
        "--list",
        action="store_true",
        help="列表模式：从搜索/店铺列表页批量取每个商品的首图",
    )
    p.add_argument("--list-html", type=Path, help="本地已保存的列表页 HTML（推荐）")
    p.add_argument(
        "--full-detail",
        "--all-main",
        action="store_true",
        dest="full_detail",
        help="逐个打开商品详情页，下载全部主图 main_01~05（默认只下列表首图）",
    )
    p.add_argument(
        "--export-offers",
        type=Path,
        help="列表模式下导出 offerId 到文本文件",
    )
    p.add_argument("--delay", type=float, default=2.0, help="--full-detail 时每个商品间隔秒数")
    p.add_argument(
        "--scroll",
        type=int,
        default=0,
        metavar="N",
        help="playwright 抓取列表时向下滚动 N 次以加载更多商品",
    )
    p.add_argument("--cookie", help="浏览器 Cookie 字符串")
    p.add_argument("--cookie-file", type=Path, help="Cookie 文件")
    p.add_argument("--playwright", action="store_true", help="用 Chromium 打开页面（可手动过验证码）")
    p.add_argument("--no-download", action="store_true", help="只打印图片 URL，不下载")
    p.add_argument("--timeout", type=float, default=30.0, help="请求超时秒数")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cookie = load_cookie_string(args.cookie, args.cookie_file)
    out_root: Path = args.output
    download = not args.no_download
    browser_data = args.browser_data or default_browser_data_dir()

    keyword = args.keyword
    count = args.count
    if keyword is None and len(sys.argv) <= 1:
        try:
            keyword, count = prompt_keyword_and_count()
        except (ValueError, KeyboardInterrupt) as exc:
            print(f"已取消: {exc}", file=sys.stderr)
            return 2

    if keyword:
        try:
            process_search(
                keyword,
                count,
                out_root=out_root,
                timeout=args.timeout,
                browser_data_dir=browser_data,
                wait_captcha=not args.no_wait_captcha,
                download=download,
                use_connect=not args.auto_browser,
                cdp_url=args.cdp_url,
                full_detail=args.full_detail,
                delay=args.delay,
                cookie=cookie,
                use_playwright=args.playwright or args.auto_browser,
            )
        except Exception as exc:
            print(f"失败: {exc}", file=sys.stderr)
            return 1
        return 0

    list_mode = args.list or args.list_html is not None
    if not list_mode and args.url and is_list_page_url(args.url):
        list_mode = True

    if list_mode:
        source = args.url or ""
        try:
            process_list(
                source,
                list_html_path=args.list_html,
                out_root=out_root,
                cookie=cookie,
                use_playwright=args.playwright,
                timeout=args.timeout,
                download=download,
                full_detail=args.full_detail,
                delay=args.delay,
                scroll_times=args.scroll,
                export_offers=args.export_offers,
            )
        except Exception as exc:
            print(f"失败: {exc}", file=sys.stderr)
            return 1
        return 0

    targets: list[str] = []
    if args.file:
        targets = read_url_list(args.file)
    elif args.url:
        targets = [args.url]
    else:
        print("请使用 -k 关键词 -n 数量，或提供 url / -f 文件", file=sys.stderr)
        return 2

    errors = 0
    for item in targets:
        try:
            process_one(
                item,
                html_path=args.html,
                out_root=out_root,
                cookie=cookie,
                use_playwright=args.playwright,
                timeout=args.timeout,
                download=download,
            )
        except Exception as exc:
            errors += 1
            print(f"失败 [{item}]: {exc}", file=sys.stderr)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
", file=sys.stderr)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
