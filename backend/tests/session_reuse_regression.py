"""生产沙箱启动路径验证会话快照保存、跨进程重开恢复、免扫码；全假网络。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scraper

URL = 'https://www.goofish.com/'

with tempfile.TemporaryDirectory(prefix='xianyu-session-test-') as directory:
    profile = Path(directory) / 'profile'
    profile.mkdir()
    (profile / '.session-imported').touch()
    with patch.object(scraper, 'PROFILE_DIR', str(profile)), \
         patch.object(scraper, 'DATA_DIR', directory), \
         patch.object(scraper, 'SESSION_FILE', str(Path(directory) / 'session.json')):
        first = scraper.Scraper(headless=True)
        try:
            first._launch()
            first._context.add_cookies([{'name':n,'value':'test-account','url':URL}
                                       for n in ('unb','cookie2')])
            first._save_session(first._context)
            # 仅在临时测试配置模拟浏览器退出丢失session cookie。
            first._context.clear_cookies()
        finally:
            first.close()
        for _ in range(2):
            worker = scraper.Scraper(headless=True)
            try:
                worker._launch()
                context = worker._context
                assert worker._has_login_cookie(context), 'saved session not restored'
                assert all(c['value']=='test-account' for c in context.cookies())
                context.route('**/*',lambda r:r.fulfill(content_type='text/html; charset=utf-8',
                              body='<meta charset="utf-8"><div>我的账户已登录</div>'))
                page = context.pages[0]
                worker._click_login = Mock()
                assert worker._interactive_login(context,page)
                worker._click_login.assert_not_called()
                assert worker._has_login_cookie(context)
            finally:
                worker.close()
print('SESSION_REUSE_PASS: saved snapshot restored after missing cookies, two browser restarts, no login clicks')
