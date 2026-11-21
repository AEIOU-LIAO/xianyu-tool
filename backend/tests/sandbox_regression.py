"""使用生产启动路径和临时配置验证Edge沙箱；不联网、不读取账号。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scraper


with tempfile.TemporaryDirectory(prefix='xianyu-sandbox-test-') as directory:
    profile = Path(directory) / 'profile'
    profile.mkdir()
    (profile / '.session-imported').touch()
    worker = scraper.Scraper(headless=True, channel='msedge')
    try:
        with patch.object(scraper, 'PROFILE_DIR', str(profile)), \
             patch.object(scraper, 'DATA_DIR', directory), \
             patch.object(scraper, 'SESSION_FILE', str(Path(directory) / 'session.json')):
            worker._launch()
            page = worker._context.pages[0]
            page.goto('edge://version', wait_until='domcontentloaded')
            command_line = page.locator('#command_line').inner_text()
            assert '--no-sandbox' not in command_line, 'sandbox unexpectedly disabled'
            assert '--disable-setuid-sandbox' not in command_line
            page.goto('about:blank')
            page.set_content('<html><body>Sandbox renderer works</body></html>')
            assert page.locator('body').inner_text() == 'Sandbox renderer works'
            print('SANDBOX_PASS: production launch, no --no-sandbox, renderer works, no account data')
    finally:
        worker.close()
