"""离线验证旧商品、收藏和卖家卡片的链接及图片处理。"""
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parents[2]
    page_html = (root / 'index.html').read_text(encoding='utf-8')
    allowed_image = 'https://img.alicdn.com/sample.webp'
    other_site = 'https://example.org/'
    item = dict(item_id='42', title='本地测试', seller='卖家', price=1)
    products = [
        dict(item, url='http://www.goofish.com/item?id=42', pic_url=allowed_image),
        dict(item, item_id='43', url=other_site, pic_url=other_site + 'sample.webp'),
    ]
    payloads = {
        '/api/status': dict(logged_in=True, total=2),
        '/api/items': dict(items=products),
        '/api/favorites': dict(favorites=[products[1]]),
        '/api/sellers': dict(sellers=[dict(key='one', name='卖家', score=5, count=1,
                                        sample_pic=other_site + 'seller.webp')]),
    }
    external_requests = []

    def route(request):
        url = request.request.url
        parsed = urlsplit(url)
        if parsed.netloc == 'localhost:8000':
            if parsed.path == '/':
                return request.fulfill(content_type='text/html', body=page_html)
            return request.fulfill(json=payloads.get(parsed.path, {}))
        external_requests.append(url)
        if url == allowed_image:
            return request.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg"/>')
        request.abort()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True, chromium_sandbox=True)
        try:
            page = browser.new_page()
            page.route('**/*', route)
            page.add_init_script('window.openCalls = []; window.open = (...args) => window.openCalls.push(args);')
            page.goto('http://localhost:8000/')
            cards = page.locator('#items .item')
            expect(cards).to_have_count(2)
            expect(cards.first.locator('img')).to_have_attribute('src', allowed_image)
            placeholder = page.evaluate('PLACEHOLDER_IMG')
            expect(cards.last.locator('img')).to_have_attribute('src', placeholder)
            cards.first.click()
            cards.last.click()
            page.locator('#view-toggle [data-v="favorites"]').click()
            favorite = page.locator('#favorites .item')
            expect(favorite.locator('img')).to_have_attribute('src', placeholder)
            favorite.click()
            assert page.evaluate('window.openCalls') == [
                ['https://www.goofish.com/item?id=42', '_blank', 'noopener,noreferrer']
            ]
            page.locator('#view-toggle [data-v="sellers"]').click()
            expect(page.locator('#sellers .seller-pic')).to_have_attribute('src', placeholder)
            assert set(external_requests) <= {allowed_image}, external_requests
            print('FRONTEND_SECURITY_PASS')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
