#!/usr/bin/env python3
"""
Reddit GMV Estimator — 店铺分析本地服务器

启动:
    pip install flask flask-cors playwright playwright-stealth requests
    playwright install chromium
    python store_server.py

Reddit API 配置（可选，大幅提升抓取成功率）:
  1. 访问 https://www.reddit.com/prefs/apps → 创建 "script" 类型应用
  2. 复制 client_id（应用名下方的短字符串）和 client_secret
  3. 在本文件同目录创建 reddit_credentials.json：
     {"client_id": "xxx", "client_secret": "yyy", "username": "你的Reddit用户名"}
  4. 重启服务器，Reddit 数据抓取将切换为 OAuth2 认证模式（不受 IP 封锁影响）
"""
import asyncio
import json
import os
import re
import sys
import time
import urllib.request
import requests as _requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── 品类关键词库 ────────────────────────────────────────────
CATEGORIES = {
    'luxury-basics': {
        'name': '奢侈基础款', 'emoji': '👔',
        'keywords': [
            'cashmere', 'wool', 'knit', 'loro piana', 'polo', 'linen',
            'merino', 'cotton', 'luxury', 'premium', 'silk', 'vicuna',
            'sweater', 'cardigan', 'crewneck', 'round neck',
            '羊绒', '羊毛', '针织', '亚麻', '丝绸', '高端', '奢侈',
            '圆领', '开衫', '毛衣', '衬衫', '西装',
        ]
    },
    'streetwear': {
        'name': '潮牌/街头', 'emoji': '🧢',
        'keywords': [
            'hoodie', 'sweatshirt', 'graphic tee', 't-shirt', 'tee', 'crewneck',
            'supreme', 'stone island', 'off white', 'bape', 'palace', 'fleece',
            '卫衣', '连帽', '潮牌', '街头', '帽衫', '宽松',
        ]
    },
    'accessories': {
        'name': '配饰/皮具', 'emoji': '👜',
        'keywords': [
            'belt', 'wallet', 'bag', 'purse', 'card holder', 'leather',
            'gucci', 'louis vuitton', 'lv', 'prada', 'celine',
            'clutch', 'tote', 'backpack', 'crossbody', 'handbag',
            '皮带', '钱包', '手提包', '斜挎包', '皮具', '配饰', '腰带', '包包',
        ]
    },
    'footwear': {
        'name': '鞋类', 'emoji': '👟',
        'keywords': [
            'sneaker', 'shoe', 'boot', 'loafer', 'trainer', 'slipper',
            'nike', 'adidas', 'yeezy', 'jordan', 'new balance',
            '运动鞋', '球鞋', '靴子', '鞋子', '拖鞋', '乐福鞋', '板鞋',
        ]
    },
    'sportswear': {
        'name': '球衣/运动服', 'emoji': '⚽',
        'keywords': [
            # 英文
            'jersey', 'jerseys', 'kit', 'football shirt', 'soccer jersey',
            'basketball jersey', 'football jersey', 'away kit', 'home kit',
            'thai jersey', 'thai quality', 'player version', 'match jersey',
            'nba', 'nfl', 'mlb', 'nhl', 'ucl', 'world cup', 'champions league',
            'real madrid', 'barcelona', 'manchester', 'arsenal', 'chelsea',
            'psg', 'juventus', 'inter milan', 'ac milan', 'liverpool',
            'brazil', 'argentina', 'france', 'germany', 'england',
            # 中文
            '球衣', '足球衣', '篮球衣', '运动服', '足球服', '篮球服',
            '主场', '客场', '泰版', '泰国版', '球迷版', '球员版',
            '世界杯', '欧冠', '英超', '西甲', '意甲', '德甲', '法甲',
            '巴西', '阿根廷', '法国', '德国', '西班牙', '葡萄牙',
            '皇马', '巴萨', '曼联', '利物浦', '阿森纳', '拜仁',
            '耐克', '阿迪达斯', '彪马',
        ]
    },
}

# ── Reddit OAuth2 ────────────────────────────────────────────

_REDDIT_TOKEN: dict = {}   # {'token': str, 'expires_at': float}
_REDDIT_CREDS: dict = {}   # client_id / client_secret / username

def _load_reddit_creds():
    """从同目录 reddit_credentials.json 加载凭据。"""
    global _REDDIT_CREDS
    path = os.path.join(os.path.dirname(__file__), 'reddit_credentials.json')
    if os.path.exists(path):
        with open(path) as f:
            _REDDIT_CREDS = json.load(f)

_load_reddit_creds()

def _reddit_oauth_token() -> str | None:
    """返回有效的 OAuth Bearer token，过期自动刷新。未配置凭据返回 None。"""
    if not _REDDIT_CREDS.get('client_id'):
        return None
    now = time.time()
    if _REDDIT_TOKEN.get('token') and _REDDIT_TOKEN.get('expires_at', 0) > now + 60:
        return _REDDIT_TOKEN['token']

    proxies = {'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'}
    ua = f"RedditGMV/1.0 (by /u/{_REDDIT_CREDS.get('username','bot')})"

    resp = _requests.post(
        'https://www.reddit.com/api/v1/access_token',
        auth=(_REDDIT_CREDS['client_id'], _REDDIT_CREDS.get('client_secret', '')),
        data={'grant_type': 'client_credentials'},
        headers={'User-Agent': ua},
        proxies=proxies,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _REDDIT_TOKEN['token'] = data['access_token']
    _REDDIT_TOKEN['expires_at'] = now + data.get('expires_in', 3600)
    return _REDDIT_TOKEN['token']


def _reddit_api_get(path: str, params: dict = None) -> dict:
    """
    用 OAuth token 请求 oauth.reddit.com（认证请求，不受 IP 封锁影响）。
    path: 如 /user/foo/submitted
    """
    token = _reddit_oauth_token()
    if token is None:
        raise RuntimeError('NO_CREDENTIALS')

    proxies = {'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'}
    ua = f"RedditGMV/1.0 (by /u/{_REDDIT_CREDS.get('username','bot')})"
    url = f'https://oauth.reddit.com{path}'

    resp = _requests.get(
        url,
        params=params or {},
        headers={'Authorization': f'Bearer {token}', 'User-Agent': ua},
        proxies=proxies,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Chrome Cookies 方案（使用你的 Reddit 登录态） ────────────
_CHROME_COOKIES = None
_CHROME_COOKIES_LOADED = False

def _get_chrome_cookies():
    """延迟加载 Chrome 里的 reddit.com cookies（仅第一次调用时读取）。"""
    global _CHROME_COOKIES, _CHROME_COOKIES_LOADED
    if _CHROME_COOKIES_LOADED:
        return _CHROME_COOKIES
    _CHROME_COOKIES_LOADED = True
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name='reddit.com')
        # 验证是否有登录 cookie
        names = {c.name for c in cj}
        if 'reddit_session' in names or 'token_v2' in names:
            _CHROME_COOKIES = cj
            print(f'[Reddit] 已从 Chrome 读取登录态 ({len(names)} cookies)')
        else:
            print('[Reddit] Chrome 中未发现 Reddit 登录态')
    except Exception as e:
        print(f'[Reddit] 读取 Chrome cookies 失败: {e}')
    return _CHROME_COOKIES


def _reddit_fetch_with_chrome_cookies(path: str) -> dict | list:
    """
    带 Chrome Reddit 登录 cookies 请求 www.reddit.com/.../.json
    path: 如 /user/foo/submitted.json?limit=100&raw_json=1
    """
    cookies = _get_chrome_cookies()
    if cookies is None:
        raise RuntimeError('NO_CHROME_COOKIES')
    proxies = {'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    resp = _requests.get(
        f'https://www.reddit.com{path}',
        cookies=cookies,
        headers=headers,
        proxies=proxies,
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f'HTTP {resp.status_code}')
    text = resp.text
    if text.lstrip().startswith('<'):
        raise RuntimeError('HTML returned (blocked)')
    return json.loads(text)


def normalize_text(text: str) -> str:
    """
    清除微店/淘宝常见反爬干扰字符：
      "N.ike" → "nike"
      "A.ir F.or.ce" → "air force"
      "ad.idas Y.-3" → "adidas y-3"
      "G.el-Ka.hana" → "gel-kahana"
    """
    # 去掉字母/汉字之间的点（保留品牌连字符如 Y-3）
    t = re.sub(r'(?<=[a-zA-Z\u4e00-\u9fff])\.(?=[a-zA-Z\u4e00-\u9fff])', '', text)
    # 压缩多余空格
    t = re.sub(r'\s+', ' ', t)
    return t.lower()


def detect_categories(text: str) -> dict:
    # 同时用原文 + 清洗版，两路命中
    t_raw    = text.lower()
    t_clean  = normalize_text(text)
    result   = {}
    for cat_id, cat in CATEGORIES.items():
        hits = list({kw for kw in cat['keywords']
                     if kw in t_raw or kw in t_clean})
        if hits:
            result[cat_id] = {'score': len(hits), 'hits': hits[:5]}
    return result


# ── 平台专属抓取器 ──────────────────────────────────────────

async def scrape_weidian(page, url: str) -> list[str]:
    """
    微店：拦截 thor.weidian.com 商品列表 API，100% 可靠。
    需在 page 上设置 response 监听后再调用 goto。
    """
    items_collected = []

    async def on_resp(resp):
        if 'getCateItemListForCommonItemSection' in resp.url or \
           'getItemListForCommonItemSection' in resp.url or \
           'itemList' in resp.url:
            try:
                data = await resp.json()
                item_list = data.get('result', {}).get('itemList', [])
                for item in item_list:
                    name = (item.get('itemName') or item.get('name') or
                            item.get('title') or '').strip()
                    if name and len(name) > 2:
                        items_collected.append(name)
            except Exception:
                pass

    page.on('response', on_resp)

    await page.goto(url, wait_until='domcontentloaded', timeout=25000)
    await page.wait_for_timeout(2000)
    await page.keyboard.press('Escape')   # 关闭弹窗

    # 滚动触发懒加载（通常需要 2–3 屏）
    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 600)")
        await page.wait_for_timeout(500)

    await page.wait_for_timeout(1500)
    return list(dict.fromkeys(items_collected))  # 去重保序


async def scrape_taobao(page, url: str) -> list[str]:
    """
    淘宝：拦截 mtop API 商品列表响应。
    需要在启动时传入浏览器 cookie（用户已登录状态）以获得最佳效果。
    无 cookie 时仍可拿到部分公开商品。
    """
    items_collected = []

    async def on_resp(resp):
        if any(k in resp.url for k in ['mtop.taobao', 'recommend', 'item_search',
                                        'itemsearch', 'shopItem', 'searchAuction']):
            try:
                b = await resp.text()
                # mtop 返回格式: {"data": {"auctions": [...]}}
                data = json.loads(b)
                auctions = (data.get('data', {}).get('auctions') or
                            data.get('data', {}).get('itemDOs') or
                            data.get('data', {}).get('result', {}).get('items') or [])
                for a in auctions:
                    name = (a.get('title') or a.get('raw_title') or
                            a.get('itemTitle') or a.get('name') or '').strip()
                    if name and len(name) > 2:
                        items_collected.append(name)
            except Exception:
                pass

    page.on('response', on_resp)
    await page.goto(url, wait_until='domcontentloaded', timeout=25000)
    await page.wait_for_timeout(3000)

    for _ in range(5):
        await page.evaluate("window.scrollBy(0, 700)")
        await page.wait_for_timeout(700)

    # DOM 兜底
    if not items_collected:
        dom_items = await page.evaluate("""(function() {
            var sels = ['[class*="itemTitle"]','[class*="item-title"]',
                        '[class*="goods-title"]','.title a','a[title]','h3','h4'];
            var res = [], seen = {};
            for (var s of sels) {
                for (var el of document.querySelectorAll(s)) {
                    var t = (el.getAttribute('title') || el.innerText || '').trim();
                    if (t.length > 3 && t.length < 80 && !seen[t]) {
                        seen[t] = 1; res.push(t);
                    }
                }
                if (res.length >= 5) break;
            }
            return res.slice(0, 30);
        })()""")
        items_collected.extend(dom_items)

    return list(dict.fromkeys(items_collected))


async def scrape_1688(page, url: str) -> list[str]:
    """
    1688：移动版绕过桌面 CAPTCHA，拦截 offerlist/product API。
    """
    items_collected = []

    async def on_resp(resp):
        if any(k in resp.url for k in ['offerresult', 'offerList', 'getProducts',
                                        'offerinfo', 'productList']):
            try:
                b = await resp.text()
                data = json.loads(b)
                offers = (data.get('data', {}).get('data', {}).get('offerList') or
                          data.get('data', {}).get('offerList') or
                          data.get('result', {}).get('offerList') or [])
                for o in offers:
                    name = (o.get('subject') or o.get('offerSubject') or
                            o.get('title') or o.get('name') or '').strip()
                    if name and len(name) > 2:
                        items_collected.append(name)
            except Exception:
                pass

    page.on('response', on_resp)

    # 转换成移动版 URL（绕 CAPTCHA）
    mobile_url = re.sub(r'https?://([\w-]+)\.1688\.com',
                        lambda m: f'https://m.1688.com/sellerInfo/{m.group(1)}', url)
    if 'm.1688.com' not in mobile_url:
        mobile_url = url  # fallback 原始

    try:
        await page.goto(mobile_url, wait_until='domcontentloaded', timeout=25000)
    except Exception:
        await page.goto(url, wait_until='domcontentloaded', timeout=25000)

    await page.wait_for_timeout(3000)
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, 700)")
        await page.wait_for_timeout(600)

    # DOM 兜底
    if not items_collected:
        dom_items = await page.evaluate("""(function() {
            var sels = ['.offer-title a','[class*="offer-title"]','[class*="subject"]',
                        '.product-name','h3','h4','a[title]'];
            var res = [], seen = {};
            for (var s of sels) {
                for (var el of document.querySelectorAll(s)) {
                    var t = (el.getAttribute('title') || el.innerText || '').trim();
                    if (t.length > 3 && t.length < 100 && !seen[t]) {
                        seen[t] = 1; res.push(t);
                    }
                }
                if (res.length >= 5) break;
            }
            return res.slice(0, 30);
        })()""")
        items_collected.extend(dom_items)

    return list(dict.fromkeys(items_collected))


async def _generic_scrape(page, url: str) -> list[str]:
    await page.goto(url, wait_until='domcontentloaded', timeout=25000)
    await page.wait_for_timeout(3000)
    return await page.evaluate("""(function() {
        var sels = ['h1','h2','h3','h4','[class*="title"]','[class*="name"]',
                    '[class*="product"]','a[title]'];
        var res = [], seen = {};
        for (var s of sels) {
            for (var el of document.querySelectorAll(s)) {
                var t = (el.getAttribute('title') || el.innerText || '').trim();
                if (t.length > 3 && t.length < 100 && !seen[t]) {
                    seen[t] = 1; res.push(t);
                }
            }
        }
        return res.slice(0, 40);
    })()""")


# ── 匹配分计算 ──────────────────────────────────────────────

def compute_match(products: list[str], blogger_cats: dict) -> dict:
    all_text = ' '.join(products)
    store_cats = detect_categories(all_text)

    if not store_cats:
        return {
            'match_score': 20,
            'store_top_cats': [],
            'overlap_cats': [],
            'reason': '无法从商品名称中识别出明确品类，建议人工确认店铺主营方向。',
        }

    store_ranked = sorted(store_cats.items(), key=lambda x: x[1]['score'], reverse=True)
    store_top    = [k for k, _ in store_ranked[:3]]
    blogger_top  = sorted(blogger_cats.items(), key=lambda x: x[1], reverse=True) if blogger_cats else []
    blogger_top  = [k for k, _ in blogger_top[:3]]

    overlap = [c for c in store_top if c in blogger_top]

    if store_top and blogger_top and store_top[0] == blogger_top[0]:
        base = 85
    elif overlap:
        base = 60 + int(len(overlap) / max(len(store_top), 1) * 20)
    else:
        base = 30

    store_cat_labels = [
        f"{CATEGORIES[k]['emoji']} {CATEGORIES[k]['name']} ({v['score']}词)"
        for k, v in store_ranked[:2]
    ]
    overlap_labels = [f"{CATEGORIES[k]['emoji']} {CATEGORIES[k]['name']}" for k in overlap]

    if overlap:
        reason = (
            f"店铺主营 {' / '.join(store_cat_labels)}，"
            f"与博主内容重叠：{' · '.join(overlap_labels)}，品类高度契合。"
        )
    else:
        reason = (
            f"店铺主营 {' / '.join(store_cat_labels)}，"
            f"博主内容集中在 {' / '.join(CATEGORIES.get(k, {}).get('name','?') for k in blogger_top[:2])} 方向，"
            f"建议调整选品或寻找更匹配的博主。"
        )

    return {
        'match_score': min(95, base),
        'store_top_cats': [
            {'id': k, 'name': CATEGORIES[k]['name'], 'emoji': CATEGORIES[k]['emoji'],
             'score': v['score'], 'hits': v['hits']}
            for k, v in store_ranked[:3]
        ],
        'overlap_cats': overlap_labels,
        'reason': reason,
    }


# ── Reddit 抓取（Playwright，绕过 403 拦截） ─────────────────

REDDIT_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)
STEALTH_PATCH = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.chrome={runtime:{}};"
)

async def _reddit_fetch_via_playwright(api_path: str, timeout: int = 30000) -> str:
    """
    用 Playwright 直接访问 old.reddit.com 的 .json 端点。
    old.reddit.com 在真实浏览器环境下返回纯 JSON，不受新版 UI 限制。
    返回原始 JSON 字符串（<pre> 标签包裹的内容或直接 JSON）。
    """
    from playwright.async_api import async_playwright

    # old.reddit.com 对真实浏览器直接返回 JSON
    url = f'https://old.reddit.com{api_path}'

    try:
        from playwright_stealth import Stealth
        _stealth = Stealth()
    except ImportError:
        _stealth = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ],
        )
        ctx = await browser.new_context(
            user_agent=REDDIT_UA,
            locale='en-US',
            timezone_id='America/New_York',
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"macOS"',
            },
        )
        await ctx.add_init_script(STEALTH_PATCH)
        page = await ctx.new_page()

        if _stealth:
            try:
                await _stealth.apply_stealth_async(page)
            except Exception:
                pass

        await page.route('**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf,ico,mp4,webm}',
                         lambda r: r.abort())

        resp = await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        if not resp:
            await browser.close()
            raise RuntimeError('无响应')

        # old.reddit.com 的 .json 在浏览器里以 <pre>JSON</pre> 形式呈现
        content = await page.content()
        await browser.close()

    # 提取 <pre> 内容
    m = re.search(r'<pre[^>]*>([\s\S]+?)</pre>', content, re.I)
    if m:
        return m.group(1).strip()
    # 某些情况直接是纯文本 JSON
    body_m = re.search(r'<body[^>]*>([\s\S]+?)</body>', content, re.I)
    raw = (body_m.group(1) if body_m else content).strip()
    if raw.startswith('{') or raw.startswith('['):
        return raw
    raise RuntimeError(f'无法从响应中提取 JSON（前80字符：{content[:80]}）')


async def fetch_reddit_post(sub: str, post_id: str) -> dict:
    errors = []

    # 方案1：Chrome 登录 cookies（最可靠，无需额外配置）
    try:
        data = _reddit_fetch_with_chrome_cookies(
            f'/r/{sub}/comments/{post_id}.json?raw_json=1&limit=1'
        )
        return data[0]['data']['children'][0]['data']
    except Exception as e:
        errors.append(f'Chrome cookies: {e}')

    # 方案2：OAuth API（已配置凭据时）
    try:
        data = _reddit_api_get(f'/r/{sub}/comments/{post_id}', {'raw_json': 1, 'limit': 1})
        return data[0]['data']['children'][0]['data']
    except Exception as e:
        errors.append(f'OAuth: {e}')

    # 方案3：Playwright（慢，最后降级）
    try:
        path = f'/r/{sub}/comments/{post_id}.json?raw_json=1&limit=1'
        text = await _reddit_fetch_via_playwright(path)
        data = json.loads(text)
        return data[0]['data']['children'][0]['data']
    except Exception as e:
        errors.append(f'Playwright: {e}')

    raise RuntimeError('全部方案失败；' + ' | '.join(errors))


async def fetch_reddit_user_posts(username: str, limit: int = 100) -> list:
    errors = []

    # 方案1：Chrome 登录 cookies
    try:
        data = _reddit_fetch_with_chrome_cookies(
            f'/user/{username}/submitted.json?limit={limit}&raw_json=1'
        )
        return [c['data'] for c in data['data']['children']]
    except Exception as e:
        errors.append(f'Chrome cookies: {e}')

    # 方案2：OAuth
    try:
        data = _reddit_api_get(f'/user/{username}/submitted', {'raw_json': 1, 'limit': limit})
        return [c['data'] for c in data['data']['children']]
    except Exception as e:
        errors.append(f'OAuth: {e}')

    # 方案3：Playwright
    try:
        path = f'/user/{username}/submitted.json?limit={limit}&raw_json=1'
        text = await _reddit_fetch_via_playwright(path)
        data = json.loads(text)
        return [c['data'] for c in data['data']['children']]
    except Exception as e:
        errors.append(f'Playwright: {e}')

    raise RuntimeError('全部方案失败；' + ' | '.join(errors))


# ── 主抓取流程 ──────────────────────────────────────────────

async def do_scrape(url: str, blogger_cats: dict) -> dict:
    from playwright.async_api import async_playwright
    try:
        from playwright_stealth import Stealth
        use_stealth = True
    except ImportError:
        use_stealth = False

    STEALTH_PATCH = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
    )

    # 平台检测
    if 'weidian.com' in url:
        platform = '微店'
        ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    elif 'taobao.com' in url or 'tmall.com' in url:
        platform = '淘宝'
        ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    elif '1688.com' in url:
        platform = '1688'
        ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
    else:
        platform = '其他'
        ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
        )
        ctx = await browser.new_context(
            user_agent=ua,
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
        )
        await ctx.add_init_script(STEALTH_PATCH)

        # 屏蔽图片/字体加速加载（不影响 XHR/API）
        page = await ctx.new_page()
        await page.route(
            '**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf,ico}',
            lambda r: r.abort()
        )

        if use_stealth:
            try:
                await Stealth().apply_stealth_async(page)
            except Exception:
                pass

        try:
            if platform == '微店':
                products = await scrape_weidian(page, url)
            elif platform == '淘宝':
                products = await scrape_taobao(page, url)
            elif platform == '1688':
                products = await scrape_1688(page, url)
            else:
                products = await _generic_scrape(page, url)

            shop_name = (await page.title()).strip() or url
        except Exception as e:
            await browser.close()
            raise RuntimeError(f'页面加载失败: {e}')

        await browser.close()

    products = [p for p in products if p]
    match    = compute_match(products, blogger_cats)

    # 无商品时给出平台相关提示
    tip = None
    if not products:
        tips = {
            '淘宝': '淘宝店铺内容需要登录态。如果你已在 Chrome 登录淘宝，请使用 --use-browser-cookies 参数重启服务器，或直接在工具中手动输入主营品类关键词。',
            '1688': '1688 有验证码防护，建议在工具中手动输入主营品类关键词，或确认链接格式为 https://公司名.1688.com/。',
            '微店': '未抓到商品，请确认微店链接格式为 https://weidian.com/?userid=XXXXXX。',
            '其他': '未识别到商品信息，请确认链接正确。',
        }
        tip = tips.get(platform, tips['其他'])

    return {
        'success': True,
        'platform': platform,
        'shop_name': shop_name,
        'product_count': len(products),
        'sample_products': products[:12],
        'tip': tip,
        **match,
    }


# ── Flask 路由 ──────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'version': '1.4',
        'reddit_oauth': bool(_REDDIT_CREDS.get('client_id')),
        'reddit_chrome_cookies': _get_chrome_cookies() is not None,
    })


@app.route('/resolve', methods=['GET', 'OPTIONS'])
def resolve_reddit_url():
    """
    解析 Reddit 短链接（/r/sub/s/code）→ 返回完整帖子 ID 和子版块。
    绕过浏览器 CORS 限制，由本地 Python 服务器代理请求。
    """
    if request.method == 'OPTIONS':
        return '', 200

    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'success': False, 'error': '缺少 url 参数'}), 400

    try:
        # 使用 urllib 跟随重定向，读取最终 URL
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/124.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            final_url = resp.geturl()

        m = re.search(r'reddit\.com/r/([^/?#]+)/comments/([a-z0-9]+)', final_url, re.I)
        if not m:
            return jsonify({'success': False, 'error': f'无法从重定向 URL 解析帖子 ID: {final_url[:120]}'}), 400

        return jsonify({'success': True, 'sub': m.group(1), 'id': m.group(2), 'final_url': final_url})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/reddit/post/<sub>/<post_id>', methods=['GET'])
def reddit_post(sub, post_id):
    """代理抓取 Reddit 帖子数据（绕过浏览器 403）。"""
    try:
        loop   = asyncio.new_event_loop()
        result = loop.run_until_complete(fetch_reddit_post(sub, post_id))
        loop.close()
        return jsonify({'success': True, 'post': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/reddit/user/<username>/posts', methods=['GET'])
def reddit_user_posts(username):
    """代理抓取 Reddit 用户近期帖子（绕过浏览器 403）。"""
    limit = int(request.args.get('limit', 100))
    try:
        loop   = asyncio.new_event_loop()
        posts  = loop.run_until_complete(fetch_reddit_user_posts(username, limit))
        loop.close()
        return jsonify({'success': True, 'posts': posts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/analyze', methods=['POST', 'OPTIONS'])
def analyze():
    if request.method == 'OPTIONS':
        return '', 200
    data         = request.get_json(force=True) or {}
    store_url    = data.get('store_url', '').strip()
    blogger_cats = data.get('blogger_categories', {})

    if not store_url:
        return jsonify({'success': False, 'error': '请提供店铺链接'}), 400

    try:
        loop   = asyncio.new_event_loop()
        result = loop.run_until_complete(do_scrape(store_url, blogger_cats))
        loop.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    print('\n' + '='*52)
    print('  Reddit GMV — 店铺分析服务器 v1.4')
    print('  http://127.0.0.1:5678')
    print()
    print('  平台支持:')
    print('  微店  ✅ 全自动（无需登录）')
    print('  淘宝  ⚠️  建议在 Chrome 中已登录淘宝')
    print('  1688  ⚠️  移动版自动绕 CAPTCHA')
    # Reddit 多路径尝试
    print()
    cc = _get_chrome_cookies()
    if cc:
        print('  Reddit ✅ 使用 Chrome 登录态抓取（推荐）')
    elif _REDDIT_CREDS.get('client_id'):
        print('  Reddit ✅ OAuth2 已配置')
    else:
        print('  Reddit ⚠️  未检测到可用登录态，将用 Playwright 降级')
        print('  提示：在 Chrome 中登录 reddit.com 即可自动启用高可靠抓取')
    print('='*52 + '\n')
    app.run(host='127.0.0.1', port=5678, debug=False)
