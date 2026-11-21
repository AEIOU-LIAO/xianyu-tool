"""沙箱Edge + 全假网络复现旧cookie误判、登录窗口等待及成功保存。"""
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scraper

URL = 'https://login-fixture.invalid/'

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel='msedge', headless=True, chromium_sandbox=True)
    try:
        for mode in ('stale', 'prompt_remains', 'success', 'reuse'):
            context = browser.new_context()
            try:
                context.add_cookies([{'name':name,'value':'old','url':URL}
                                     for name in ('unb','cookie2')])
                if mode in ('stale', 'reuse'):
                    script = ''
                else:
                    script = "setTimeout(()=>{document.cookie='unb=fresh; path=/';document.cookie='cookie2=fresh; path=/';" + (
                        "document.querySelector('button').remove();" if mode == 'success' else '') + '},700)'
                html = '<meta charset="utf-8">' + ('<div>已登录账户</div>' if mode == 'reuse'
                       else '<button onclick="' + script + '">登录</button>')
                context.route('**/*', lambda route: route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html))
                page = context.new_page()
                worker = scraper.Scraper()
                worker.LOGIN_TIMEOUT = 5 if mode in ('success','reuse') else 1.5
                worker._save_session = Mock()
                with patch.object(scraper, 'HOME_URL', URL):
                    ok = worker._interactive_login(context, page)
                assert not page.is_closed(), 'login helper prematurely closed window'
                if mode in ('success','reuse'):
                    assert ok
                    worker._save_session.assert_called_once_with(context)
                    assert all(c['value'] == ('old' if mode == 'reuse' else 'fresh') for c in context.cookies())
                else:
                    assert not ok, 'old cookies or visible login prompt counted as success'
                    worker._save_session.assert_not_called()
                if mode == 'stale':
                    assert worker._has_login_cookie(context)  # 保留旧会话，不能当作退出登录处理。
            finally:
                context.close()
        print('LOGIN_BROWSER_PASS: valid login reused; stale prompt waits; fresh login saves; cookies preserved')
    finally:
        browser.close()
