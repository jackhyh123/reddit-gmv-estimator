#!/usr/bin/env python3
"""
Reddit GMV Estimator — 店铺分析本地服务器
用 Playwright 真实访问微店/淘宝/1688，抓取商品列表，输出品类匹配分析。

启动:
    pip install flask flask-cors playwright
    playwright install chromium
    python store_server.py

端口: 5678
"""
import asyncio
import re
import json
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
            'merino', 'cotton', 'luxury', 'premium', 'silk', 'vicuña',
            '羊绒', '羊毛', '针织', '亚麻', '丝绸', '高端', '奢侈', 'sweater',
            'cardigan', 'crewneck knit', 'round neck', '圆领', '开衫',
        ]
    },
    'streetwear': {
        'name': '潮牌/街头', 'emoji': '🧢',
        'keywords': [
            'hoodie', 'sweatshirt', 'graphic tee', 't-shirt', 'tee', 'crewneck',
            'supreme', 'stone island', 'off white', 'bape', 'palace',
            '卫衣', '连帽', '潮牌', '街头', 'fleece', 'zip-up',
        ]
    },
    'accessories': {
        'name': '配饰/皮具', 'emoji': '👜',
        'keywords': [
            'belt', 'wallet', 'bag', 'purse', 'card holder', 'leather',
            'gucci', 'louis vuitton', 'lv', 'prada', 'celine', 'clutch',
            'tote', 'backpack', 'crossbody',
            '皮带', '钱包', '手提包', '斜挎包', '皮具', '配饰', '腰带',
        ]
    },
    'footwear': {
        'name': '鞋类', 'emoji': '👟',
        'keywords': [
            'sneaker', 'shoe', 'boot', 'loafer', 'trainer', 'slipper',
            'nike', 'adidas', 'yeezy', 'jordan', 'new balance', 'nb',
            '运动鞋', '球鞋', '靴子', '鞋子', '拖鞋', '乐福鞋',
        ]
    },
}

def detect_categories(text: str) -> dict:
    """从文本中检测品类关键词命中数"""
    t = text.lower()
    result = {}
    for cat_id, cat in CATEGORIES.items():
        hits = [kw for kw in cat['keywords'] if kw in t]
        if hits:
            result[cat_id] = {'score': len(hits), 'hits': hits[:5]}
    return result


# ── 平台专属抓取器 ──────────────────────────────────────────

async def scrape_weidian(page) -> list[str]:
    """微店商品名抓取"""
    await page.wait_for_timeout(2000)

    # 尝试精确选择器
    for sel in ['.item-name', '.item-title', '[class*="item-name"]',
                '[class*="goods-name"]', '[class*="product-name"]',
                '.shopinfo-product-title', '.pro-name']:
        els = await page.query_selector_all(sel)
        names = []
        for el in els:
            t = (await el.inner_text()).strip()
            if 3 < len(t) < 120:
                names.append(t)
        if len(names) >= 3:
            return names[:40]

    # 通用 JS 提取
    return await _generic_extract(page)


async def scrape_taobao(page) -> list[str]:
    """淘宝/天猫店铺商品名抓取"""
    await page.wait_for_timeout(3000)

    for sel in [
        '.item-title a', '.goods-title', '[class*="item-title"]',
        '[class*="goods-title"]', '.ShopModule--itemTitle--',
        '.item-card-body h4', '.item-info h3',
    ]:
        els = await page.query_selector_all(sel)
        names = []
        for el in els:
            t = (await el.inner_text()).strip()
            if 3 < len(t) < 120:
                names.append(t)
        if len(names) >= 3:
            return names[:40]

    return await _generic_extract(page)


async def scrape_1688(page) -> list[str]:
    """1688 供应商商品名抓取"""
    await page.wait_for_timeout(3000)

    for sel in [
        '.offer-title a', '.product-name', '.offer-name',
        '[class*="offer-title"]', '[class*="product-title"]',
        '.subject-title', '[class*="subject"]',
    ]:
        els = await page.query_selector_all(sel)
        names = []
        for el in els:
            t = (await el.inner_text()).strip()
            if 3 < len(t) < 120:
                names.append(t)
        if len(names) >= 3:
            return names[:40]

    return await _generic_extract(page)


async def _generic_extract(page) -> list[str]:
    """兜底：提取页面所有有意义的文本节点"""
    items = await page.evaluate("""
        () => {
            const candidates = document.querySelectorAll(
                'h1,h2,h3,h4,a[title],[class*="title"],[class*="name"],[class*="product"],[class*="goods"],[class*="item"]'
            );
            const seen = new Set();
            const results = [];
            for (const el of candidates) {
                const t = (el.getAttribute('title') || el.innerText || '').trim();
                if (t.length > 3 && t.length < 120 && !seen.has(t)) {
                    seen.add(t);
                    results.push(t);
                }
                if (results.length >= 60) break;
            }
            return results;
        }
    """)
    return items


# ── 匹配分计算 ──────────────────────────────────────────────

def compute_match(products: list[str], blogger_cats: dict) -> dict:
    """
    blogger_cats: {cat_id: post_count, ...}  来自前端的博主品类分布
    返回 match_score (0-100) + 分析说明
    """
    all_text = ' '.join(products)
    store_cats = detect_categories(all_text)

    if not store_cats:
        return {
            'match_score': 20,
            'store_top_cats': [],
            'reason': '无法从商品名称中识别出明确品类，建议人工确认店铺主营方向。',
        }

    # 店铺主要品类（按命中数排序）
    store_ranked = sorted(store_cats.items(), key=lambda x: x[1]['score'], reverse=True)
    store_top    = [k for k, _ in store_ranked[:3]]

    # 博主主要品类（按发帖数排序）
    blogger_top  = sorted(blogger_cats.items(), key=lambda x: x[1], reverse=True) if blogger_cats else []
    blogger_top  = [k for k, _ in blogger_top[:3]]

    # 重叠品类
    overlap = [c for c in store_top if c in blogger_top]
    overlap_score = len(overlap) / max(len(store_top), 1)

    # 加权：主品类完全重叠 → 最高分
    if store_top and blogger_top and store_top[0] == blogger_top[0]:
        base = 85
    elif overlap:
        base = 60 + int(overlap_score * 20)
    else:
        base = 30

    # 品类描述
    store_cat_labels = [
        f"{CATEGORIES[k]['emoji']} {CATEGORIES[k]['name']} ({v['score']}个关键词)"
        for k, v in store_ranked[:3]
    ]
    overlap_labels = [f"{CATEGORIES[k]['emoji']} {CATEGORIES[k]['name']}" for k in overlap]

    if overlap:
        reason = (
            f"店铺主营 {' / '.join(store_cat_labels[:2])}，"
            f"与博主内容重叠品类：{' · '.join(overlap_labels)}，匹配度高。"
        )
    else:
        reason = (
            f"店铺主营 {' / '.join(store_cat_labels[:2])}，"
            f"博主内容偏向 {' / '.join(blogger_top[:2]) if blogger_top else '未知'}，"
            f"品类存在差异，建议调整选品方向。"
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


# ── 主抓取流程 ──────────────────────────────────────────────

async def do_scrape(url: str, blogger_cats: dict) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
        )
        ctx = await browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/122.0.0.0 Safari/537.36'
            ),
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
        )
        page = await ctx.new_page()

        # 屏蔽图片/字体加速加载
        await page.route('**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf}',
                         lambda route: route.abort())

        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=25000)
        except Exception as e:
            await browser.close()
            raise RuntimeError(f'页面加载超时或被拦截: {e}')

        shop_name = (await page.title()).strip() or url

        if 'weidian.com' in url:
            platform, products = '微店', await scrape_weidian(page)
        elif 'taobao.com' in url or 'tmall.com' in url:
            platform, products = '淘宝', await scrape_taobao(page)
        elif '1688.com' in url:
            platform, products = '1688', await scrape_1688(page)
        else:
            platform, products = '其他', await _generic_extract(page)

        await browser.close()

    products = list(dict.fromkeys(p for p in products if p))  # 去重保序
    match    = compute_match(products, blogger_cats)

    return {
        'success':        True,
        'platform':       platform,
        'shop_name':      shop_name,
        'product_count':  len(products),
        'sample_products': products[:12],
        **match,
    }


# ── Flask 路由 ──────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


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
    print('\n' + '='*50)
    print('  Reddit GMV — 店铺分析服务器')
    print('  http://127.0.0.1:5678')
    print('  保持此窗口运行，然后在浏览器工具中使用店铺匹配分析')
    print('='*50 + '\n')
    app.run(host='127.0.0.1', port=5678, debug=False)
