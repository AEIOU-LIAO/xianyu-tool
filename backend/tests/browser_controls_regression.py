"""基于实测页面结构的省/市弹层和图标翻页回归；全程不联网。"""
import json
import sys
from pathlib import Path
from unittest.mock import Mock
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scraper import Scraper
from browser_flow import SearchResult

with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True,chromium_sandbox=True)
    try:
        page=browser.new_page()
        page.set_content('''<div style="display:none"><button class="areaTextContainer--hidden">区域</button></div><span onclick="window.wrong=true">广东</span>
        <button class="areaTextContainer--test" onclick="document.querySelector('.areaWrap--test').style.display='block'">区域</button>
        <div class="areaWrap--test" style="display:none"><div style="height:120px;overflow:auto">
        <div style="height:500px"></div><button onclick="document.querySelector('#cities').innerHTML='<button onclick=&quot;window.chosen=true&quot;>深圳</button>'">广东</button></div>
        <div id="cities"></div></div><button onclick="window.submitted=window.chosen">查看99件宝贝</button>
        <div style="display:none" class="areaWrap--hidden"><span>广东</span><span>深圳</span></div>
        <div class="search-filter-distance-page-container--test"><button disabled>prev</button><button onclick="window.next=true">next</button></div>''')
        obj=Scraper()
        obj._page=page
        obj._check=Mock()
        obj._phase_callback=None
        obj._results=[]
        response=SearchResult({'keyword':'test','extraFilterValue':json.dumps({'divisionList':[{'city':'深圳'}]})},{})
        def confirm(*args):
            assert page.evaluate('window.submitted === true && window.wrong !== true')
            return response
        obj._response=confirm
        assert obj._apply_city('test','深圳',SearchResult({'keyword':'test'},{})) is response
        assert obj._next()
        assert page.evaluate('window.next === true')
        page.set_content('''<style>.search-select-container--test .options {display:none}
        .search-select-container--test:hover .options {display:block}</style>
        <span onclick="window.wrong=true">最新</span>
        <div class="search-select-container--hidden" style="display:none">新发布<span>最新</span></div>
        <div class="search-select-container--test"><span>新发布</span>
        <div class="options"><span onclick="window.sorted=(window.sorted||0)+1">最新</span><span>1天内</span></div></div>''')
        obj._wait=Mock()
        response.request.update(sortField='create',sortValue='desc',pageNumber=1)
        def sorted_response(*args):
            assert page.evaluate('window.sorted === 1 && window.wrong !== true')
            return response
        obj._response=sorted_response
        assert obj._apply_latest('test','深圳',SearchResult({'keyword':'test'},{})) is response
        assert obj._apply_latest('test','深圳',response) is response
        assert page.evaluate('window.sorted === 1')  # 重复点击会取消排序，必须避免。
        print('BROWSER_CONTROLS_PASS: city, pagination, scoped latest dropdown and no toggle')
    finally:
        browser.close()
