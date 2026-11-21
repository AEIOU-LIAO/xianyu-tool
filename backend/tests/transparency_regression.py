"""真实沙箱Edge验证内容透明度、壁纸独立性和刷新保存；全假网络。"""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = Path(sys.argv[1]) if len(sys.argv) > 1 else None
wallpaper = '''<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="1000"><defs>
<linearGradient id="g" x2="1" y2="1"><stop stop-color="#72d6d1"/><stop offset=".5" stop-color="#a9aff4"/>
<stop offset="1" stop-color="#ffcca1"/></linearGradient></defs><rect width="1400" height="1000" fill="url(#g)"/>
<circle cx="1050" cy="250" r="310" fill="#ffc3cc" fill-opacity=".5"/></svg>'''

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel='msedge',headless=True,chromium_sandbox=True)
    try:
        page = browser.new_page(viewport={'width':1280,'height':1000})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def route(r):
            path=r.request.url.split('localhost:8000')[-1].split('?')[0]
            if path == '/':
                return r.fulfill(content_type='text/html; charset=utf-8',body=(ROOT/'index.html').read_text(encoding='utf-8'))
            if path == '/wallpaper.svg':
                return r.fulfill(content_type='image/svg+xml',body=wallpaper)
            if path == '/api/status':
                data={'running':False,'logged_in':True,'total':1,'phase':'idle','progress':{}}
            elif path == '/api/items':
                data={'items':[{'item_id':'test','title':'商品内容保持清晰','price':999,'seller':'测试卖家',
                                'seller_type':'personal','days_ago':0,'keyword':'笔记本','city':'深圳','dist':'南山区'}],
                      'keywords':['笔记本'],'cities':['深圳'],'dists':['南山区']}
            elif path == '/api/skins':
                data={'skins':[{'id':'test','name':'测试壁纸','roleName':'测试','web':'/wallpaper.svg','thumb':'/wallpaper.svg'}]}
            else:
                data={}
            r.fulfill(content_type='application/json',body=json.dumps(data))
        page.route('**/*',route)
        page.goto('http://localhost:8000/')
        page.wait_for_selector('#items .item')
        page.locator('#btn-open-skin').click()
        page.locator('.skin-item').click()
        page.locator('#skin-veil').focus()
        page.locator('#skin-veil').press('Home')
        slider=page.locator('#skin-content-transparency')
        slider.focus()
        slider.press('End')
        assert slider.input_value() == '100'
        assert page.locator('.scrape-card').evaluate("e=>getComputedStyle(e).backgroundColor") == 'rgba(255, 255, 255, 0)'
        assert page.locator('#items .item').first.evaluate("e=>getComputedStyle(e).opacity") == '1'
        assert page.locator('.item-title').evaluate("e=>getComputedStyle(e).opacity") == '1'
        slider.press('Home')
        assert page.locator('.filter-card').evaluate("e=>getComputedStyle(e).backgroundColor") == 'rgb(255, 255, 255)'
        box=slider.bounding_box()
        page.mouse.click(box['x']+8+(box['width']-16)*.65,box['y']+box['height']/2)
        selected=slider.input_value()
        assert 60 <= int(selected) <= 70
        assert page.locator('#skin-veil').input_value() == '0'
        page.locator('#btn-close-skin').click()
        if PREVIEW:
            PREVIEW.parent.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(PREVIEW),full_page=True)
        page.reload()
        page.wait_for_selector('#items .item')
        assert page.locator('body').evaluate("e=>e.classList.contains('skin-on')")
        assert slider.input_value() == selected
        assert page.locator('#skin-veil').input_value() == '0'
        page.locator('#btn-open-skin').click()
        page.set_viewport_size({'width':390,'height':844})
        box=slider.bounding_box()
        assert box['x'] >= 0 and box['x']+box['width'] <= 390
        page.locator('#btn-skin-reset').click()
        assert slider.input_value() == '20'
        assert not page.locator('body').evaluate("e=>e.classList.contains('skin-on')")
        assert not errors, errors
        print('TRANSPARENCY_PASS: native slider 0/100/intermediate, text opacity, independent veil, reload persistence, mobile, reset')
    finally:
        browser.close()
