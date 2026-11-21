"""真实浏览器 + 全假网络：验证前端请求乱序、切视图计数与 JS 错误。"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='msedge', headless=True, chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.clock.install()
            errors = []
            page.on('pageerror', lambda err: errors.append(type(err).__name__ + ':' + str(err)))
            pending = []
            block_next = [False]
            status = dict(running=False,logged_in=True,total=2,phase='idle',progress={},cooldown_until=0)
            items = [dict(item_id=str(i),title='商品'+str(i),price=i*100,seller='测试',seller_type='personal',days_ago=0,
                          city='深圳',dist='南山区',keyword='笔记本') for i in (1,2)]
            favorites = [dict(items[0],item_id='fav'+str(i)) for i in range(4)]
            deletions = []
            submitted_windows = []
            async def route(r):
                from urllib.parse import urlparse,parse_qs
                parsed = urlparse(r.request.url)
                if parsed.hostname not in ('localhost','127.0.0.1'):
                    await r.abort()
                    return
                path = parsed.path
                if path == '/':
                    await r.fulfill(status=200,content_type='text/html',body=(ROOT/'index.html').read_text(encoding='utf-8'))
                    return
                data = {}
                if path == '/api/status':
                    data = status
                elif path == '/api/scrape':
                    submitted_windows.append(r.request.post_data_json['days'])
                    data = dict(ok=True,msg='started')
                elif path == '/api/items' and r.request.method == 'DELETE':
                    scope = r.request.post_data_json
                    deletions.append(scope)
                    assert scope == dict(kind='keyword',value='笔记本',city='')
                    deleted, deleted_favorites = len(items), len(favorites)
                    items.clear()
                    favorites.clear()
                    data = dict(ok=True,deleted=deleted,deleted_favorites=deleted_favorites,total=0)
                elif path == '/api/items':
                    sort = parse_qs(parsed.query).get('sort',['new'])[0]
                    rows = sorted(items,key=lambda i:i['price'],reverse=sort=='price-desc')
                    data = dict(items=rows,keywords=list({i['keyword'] for i in items}),
                                dists=list({i['dist'] for i in items}),cities=list({i['city'] for i in items}))
                    if block_next[0]:
                        block_next[0] = False
                        event = asyncio.Event()
                        pending.append(event)
                        await event.wait()
                elif path == '/api/favorites':
                    data = dict(favorites=favorites,total=4)
                elif path == '/api/sellers':
                    data = dict(sellers=[],total=0)
                elif path == '/api/settings':
                    data = dict(keep_days=30)
                elif path == '/api/schedules':
                    data = dict(schedules=[])
                elif path == '/api/skins':
                    data = dict(skins=[],roles=[])
                await r.fulfill(status=200,content_type='application/json',body=json.dumps(data))
            await page.route('**/*',route)
            await page.goto('http://localhost:8000/')
            await page.wait_for_function("document.querySelectorAll('#items .item').length === 2")
            await page.locator('#keyword').fill('测试')
            for value in ('0.041666666666666664','0.08333333333333333','0.125','0.5'):
                await page.select_option('#days',value)
                assert await page.locator('#days').input_value() == value
                async with page.expect_response(lambda r: '/api/scrape' in r.url):
                    await page.locator('#btn-scrape').click()
            assert submitted_windows == [1/24,2/24,3/24,0.5]
            # 手动筛选新响应先回来，旧响应不能覆盖新排序。
            block_next[0] = True
            await page.select_option('#f-sort','price-asc')
            while not pending:
                await asyncio.sleep(.01)
            await page.select_option('#f-sort','price-desc')
            await page.wait_for_function("document.querySelector('#items .item-price').textContent.includes('200')")
            pending.pop(0).set()
            await asyncio.sleep(.1)
            assert '200' in await page.locator('#items .item-price').first.inner_text()
            # 轮询发出后切收藏，旧商品响应不能改写收藏计数。
            block_next[0] = True
            await page.clock.fast_forward(15000)
            while not pending:
                await asyncio.sleep(.01)
            await page.locator('#view-toggle [data-v="favorites"]').click()
            await page.wait_for_function("document.querySelectorAll('#favorites .item').length === 4")
            pending.pop(0).set()
            await asyncio.sleep(.1)
            assert '4' in await page.locator('#count-label').inner_text()
            assert '收藏' in await page.locator('#count-label').inner_text()
            # 正式任务没有固定总页数，仍须显示真实进度，不展示虚假的分母。
            status.update(running=True,phase='collecting',progress=dict(page=45,pages=0,collected=1350))
            await page.clock.fast_forward(15000)
            await page.wait_for_function("document.querySelector('#status-text').textContent.includes('已采45页 1350条')")
            status.update(phase='resting')
            await page.clock.fast_forward(15000)
            await page.wait_for_function("document.querySelector('#status-text').textContent.includes('采集间歇休息（至少10秒）')")
            status.update(running=False,logging_in=True,phase='waiting_login')
            await page.clock.fast_forward(15000)
            await page.wait_for_function("document.querySelector('#status-text').textContent.includes('等待登录')")
            assert await page.locator('#btn-scrape').is_disabled()
            status.update(running=False,logging_in=False,phase='idle',progress={},total=0)
            await page.locator('#view-toggle [data-v="items"]').click()
            await page.wait_for_function("document.querySelectorAll('#items .item').length === 2")
            assert await page.locator('#f-city .chip-delete').count() == 1  # 单城市也能删除。
            assert await page.locator('#f-price .chip-delete').count() == 0
            assert await page.locator('#f-cat .chip[data-v=""] .chip-delete').count() == 0
            page.once('dialog', lambda d: d.dismiss())
            await page.locator('#f-city .chip-delete').click()
            assert not deletions
            assert await page.locator('#f-cat .chip.on').get_attribute('data-v') == ''
            # 在旧筛选响应尚未返回时删除，旧响应不得让已删卡片复活。
            block_next[0] = True
            await page.select_option('#f-sort','price-asc')
            while not pending:
                await asyncio.sleep(.01)
            page.once('dialog', lambda d: d.accept())
            await page.locator('#f-cat .chip-delete').click()
            await page.wait_for_function("document.querySelectorAll('#f-cat .chip-delete').length === 0")
            pending.pop(0).set()
            await asyncio.sleep(.1)
            assert len(deletions) == 1
            assert await page.locator('#items .item').count() == 0
            assert '0' in await page.locator('#count-label').inner_text()
            assert await page.locator('#f-dist .chip-delete').count() == 0
            await page.locator('#view-toggle [data-v="favorites"]').click()
            await page.wait_for_function("document.querySelector('#count-label').textContent.includes('0') && document.querySelectorAll('#favorites .item').length === 0")
            assert await page.locator('#favorites .item').count() == 0
            assert not errors, errors
            print(json.dumps({'frontend':'PASS','checks':['reversed responses','poll after view switch','unknown total pages progress','tag delete cancel/confirm','single city delete','stale response after delete','favorites removed','no JavaScript errors']},ensure_ascii=True))
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(),60))
