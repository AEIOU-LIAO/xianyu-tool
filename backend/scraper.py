# -*- coding: utf-8 -*-
"""
闲鱼（goofish.com）抓取器 —— 基于 Playwright 驱动真实浏览器。

为什么用浏览器而不是直接调接口：
  - 闲鱼搜索页的接口有 mtop 签名 + 登录态 + 风控，直接请求极易被拦；
  - 用真实浏览器（本机已装 Edge / Chrome）走正常页面，抗风控更稳；
  - 首次扫码登录一次，之后复用保存的会话（storage_state）。

抓取方式：优先「拦截页面返回的搜索 JSON」，其次「从 DOM 提取」。
闲鱼 DOM 的 class 名是混淆的、经常变，所以接口拦截更稳；这里两者都做，
并在找不到数据时保存截图方便排查。

注意：抓取闲鱼属于灰色地带，请控制频率、仅作个人使用。
"""
import hashlib
import json
import os
import re
import time
import urllib.parse
import logging
from item_time import refresh_age, in_window
from browser_flow import BrowserCollection

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('XIANYU_DATA_DIR') or os.path.join(BASE_DIR, 'data')
SESSION_FILE = os.path.join(DATA_DIR, 'session.json')
PROFILE_DIR = os.path.join(DATA_DIR, 'browser-profile')
DEBUG_DIR = os.path.join(DATA_DIR, 'debug')

def _item_key(it):
    """商品去重键：优先 item_id；没有才退回标题 + 卖家。

    不能只用「标题 + 昵称」：同卖家发两个同标题但不同 item_id 的商品会被误合并。
    """
    iid = it.get('item_id')
    if iid:
        return ('id', iid)
    return ('ts', (it.get('title'), it.get('seller') or it.get('seller_id')))


SEARCH_URL = 'https://www.goofish.com/search?q={kw}'
HOME_URL = 'https://www.goofish.com/'

# 登录成功后才会出现的 cookie（匿名会话里没有），用于判断是否已登录
LOGIN_COOKIES = ('unb', 'cookie2', 'sgcookie')

# mtop 搜索接口相关（用于直接翻页请求）
APPKEY = '34839810'
MTOP_API = 'mtop.taobao.idlemtopsearch.pc.search'
MTOP_BASE = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/'

# 城市 -> 省份（区域筛选需要省+市；省份缺失时接口也能只按市过滤，但带上更稳）
CITY_PROVINCE = {
    '深圳': '广东', '广州': '广东', '东莞': '广东', '佛山': '广东', '珠海': '广东',
    '中山': '广东', '惠州': '广东', '汕头': '广东', '江门': '广东',
    '北京': '北京', '上海': '上海', '天津': '天津', '重庆': '重庆',
    '杭州': '浙江', '宁波': '浙江', '温州': '浙江',
    '南京': '江苏', '苏州': '江苏', '无锡': '江苏',
    '成都': '四川', '武汉': '湖北', '长沙': '湖南', '西安': '陕西',
    '郑州': '河南', '青岛': '山东', '济南': '山东', '厦门': '福建', '福州': '福建',
    '合肥': '安徽', '昆明': '云南', '南宁': '广西', '沈阳': '辽宁', '大连': '辽宁',
}


def _parse_days(time_str):
    """把「刚刚 / x分钟前 / x小时前 / x天前 / 日期 / 时间戳」转成距今天数。"""
    s = str(time_str or '').strip()
    if not s:
        return None
    # 纯数字：可能是 unix 时间戳（秒 10 位 / 毫秒 13 位）
    if s.isdigit():
        import datetime
        try:
            ts = int(s)
            if ts > 10**12:  # 毫秒
                ts = ts / 1000
            dt = datetime.datetime.fromtimestamp(ts)
            return max(0, (datetime.date.today() - dt.date()).days)
        except (ValueError, OSError, OverflowError):
            return None
    if '刚刚' in s or '分钟前' in s or '小时前' in s or '今天' in s:
        return 0
    m = re.search(r'(\d+)\s*天前', s)
    if m:
        return int(m.group(1))
    # 绝对日期，如 2024-05-01 或 05-01
    m = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m:
        import datetime
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return max(0, (datetime.date.today() - d).days)
        except ValueError:
            return None
    return None


class Scraper(BrowserCollection):
    CITY_PROVINCE = CITY_PROVINCE
    def __init__(self, headless=False, channel='msedge'):
        self.headless = headless
        self.channel = channel
        self._playwright = None
        self._browser = None
        self._context = None
        self.outcome = 'success'
        self.cooldown_required = False
        self.last_error = None        # 最近一次失败原因（风控/令牌等）
        self.partial_error = None     # 部分完成时的提示（翻页中断/达到页数上限）
        self.last_punish_url = None   # 风控验证页地址（用于引导人工过滑块）

    # ---------- 底层 ----------
    def _launch(self):
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                PROFILE_DIR, headless=self.headless, channel=self.channel, chromium_sandbox=True,
                viewport={'width': 1366, 'height': 900}, locale='zh-CN', timeout=30000)
            self._context.set_default_timeout(10000)
            self._context.set_default_navigation_timeout(30000)
            # 单次迁移旧会话，绝不使用用户平时浏览器的配置目录。
            marker = os.path.join(PROFILE_DIR, '.session-imported')
            if not os.path.exists(marker):
                if os.path.exists(SESSION_FILE) and not os.path.exists(os.path.join(DATA_DIR, 'browser-session.json')):
                    with open(SESSION_FILE, encoding='utf-8') as f:
                        old = json.load(f)
                    self._context.add_cookies(old.get('cookies', []))
                    for origin in old.get('origins', []):
                        if origin.get('origin') not in ('https://www.goofish.com', 'https://goofish.com'):
                            continue
                        seed = self._context.new_page()
                        try:
                            seed.goto(origin['origin'], wait_until='domcontentloaded')
                            seed.evaluate('(entries) => entries.forEach(e => localStorage.setItem(e.name,e.value))',
                                          origin.get('localStorage', []))
                        finally:
                            seed.close()
                with open(marker, 'w', encoding='utf-8') as f:
                    f.write('1')
            self._restore_session_cookies(self._context)
        except Exception:
            self.close()
            raise

    def _new_context(self):
        return self._context

    def _restore_session_cookies(self, context):
        """补回重启丢失的会话cookie，不覆盖配置目录中更新的值。"""
        path = os.path.join(DATA_DIR, 'browser-session.json')
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding='utf-8') as source:
                saved = json.load(source)
            existing = {(c['name'], c['domain'], c.get('path', '/')) for c in context.cookies()}
            missing = []
            for cookie in saved.get('cookies', []):
                host = cookie.get('domain', '').lstrip('.')
                if not any(host == domain or host.endswith('.' + domain)
                           for domain in ('goofish.com', 'taobao.com', 'xianyu.com')):
                    continue
                expires = cookie.get('expires', -1)
                if expires != -1 and expires <= time.time():
                    continue
                key = (cookie['name'], cookie['domain'], cookie.get('path', '/'))
                if key not in existing:
                    missing.append(cookie)
            if missing:
                context.add_cookies(missing)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            logging.warning('saved session unavailable; keeping existing browser session')

    def _save_session(self, context):
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        # 已有 session.json 保留原件，配置目录是新的会话来源。
        target = os.path.join(DATA_DIR, 'browser-session.json')
        temporary = target + '.tmp'
        context.storage_state(path=temporary)
        os.replace(temporary, target)

    def close(self):
        failures = []
        try:
            if self._context:
                self._context.close()
            elif self._browser:
                self._browser.close()
        except Exception as exc:
            failures.append(type(exc).__name__)
        finally:
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception as exc:
                failures.append(type(exc).__name__)
            finally:
                self._context = self._browser = self._playwright = None
        if failures:
            logging.warning('browser cleanup: %s', ','.join(failures))

    # ---------- 登录 ----------
    def is_logged_in(self):
        """会话文件里含「登录后才有的 cookie」才算已登录。"""
        session = os.path.join(DATA_DIR, 'browser-session.json')
        if not os.path.exists(session):
            session = SESSION_FILE
        if not os.path.exists(session):
            return False
        try:
            with open(session, 'r', encoding='utf-8') as f:
                data = json.load(f)
            names = {c.get('name') for c in data.get('cookies', [])
                     if c.get('expires', -1) == -1 or c.get('expires', 0) > time.time()}
            return 'unb' in names and bool(names & {'cookie2', 'sgcookie'})
        except (json.JSONDecodeError, OSError, TypeError, AttributeError, ValueError):
            return False

    def _has_login_cookie(self, context):
        try:
            names = {c.get('name') for c in context.cookies()
                     if c.get('expires', -1) == -1 or c.get('expires', 0) > time.time()}
            return 'unb' in names and bool(names & {'cookie2', 'sgcookie'})
        except Exception:
            return False

    def _on_login_page(self, page):
        url = page.url.lower()
        return any(k in url for k in ('login.taobao', 'passport', 'havanaone', 'login.htm', 'login.aliyun'))

    def _click_login(self, page):
        """尝试点击闲鱼首页的「登录」入口，失败也不致命（用户可手动点）。"""
        for loc in (page.get_by_text('登录', exact=True),
                    page.locator('a:has-text("登录")'),
                    page.locator('[class*="login"] a, [class*="login"] button')):
            try:
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    LOGIN_TIMEOUT = 300
    LOGIN_STABLE_SECONDS = 2

    def _login_prompt_visible(self, page):
        controls = page.get_by_text('登录', exact=True)
        frames = page.locator('iframe[src*="login"], iframe[src*="passport"]')
        return any(controls.nth(i).is_visible() for i in range(controls.count())) or any(
            frames.nth(i).is_visible() for i in range(frames.count()))

    def _interactive_login(self, context, page, check=None):
        # 先让网站检查现有会话；不能每次打开登录都主动退出已保存的账号。
        page.goto(HOME_URL, wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(1000)
        needs_login = (not self._has_login_cookie(context) or self._on_login_page(page)
                       or self._login_prompt_visible(page))
        if needs_login:
            self._click_login(page)
        logging.info('login: checking saved session, login_needed=%s', needs_login)
        deadline = time.monotonic() + self.LOGIN_TIMEOUT
        stable_since = None
        while time.monotonic() < deadline:
            if check:
                check()
            if page.is_closed():
                return False
            if (self._has_login_cookie(context) and not self._on_login_page(page)
                    and not self._login_prompt_visible(page)):
                if stable_since is None:
                    stable_since = time.monotonic()
                if time.monotonic() - stable_since >= self.LOGIN_STABLE_SECONDS:
                    self._save_session(context)
                    return True
            else:
                stable_since = None
            page.wait_for_timeout(200)
        return False

    def login(self):
        """优先复用会话；失效时等待登录，资源由调用方统一清理。"""
        self.headless = False
        self._launch()
        context = self._new_context()
        page = context.pages[0] if context.pages else context.new_page()
        return self._interactive_login(context, page)

    # ---------- 抓取 ----------
    def scrape(self, keyword, days=0, city=None, should_pause=None, should_cancel=None,
               on_progress=None, on_phase=None, should_resume=None, max_pages=None):
        return self.collect(keyword, days, city, should_pause, should_cancel,
                            on_progress, on_phase, should_resume, max_pages)

    @staticmethod
    def _wait_if_paused(should_pause, should_cancel):
        """若处于暂停状态则在此等待；被取消返回 False。"""
        while should_pause and should_pause():
            if should_cancel and should_cancel():
                return False
            time.sleep(0.4)
        if should_cancel and should_cancel():
            return False
        return True

    def _get_mtop_token(self, context):
        for c in context.cookies():
            if c.get('name') == '_m_h5_tk':
                return c.get('value', '').split('_')[0]
        return None

    def _capture_bxua(self, req, out):
        """捕获设备指纹 bx-ua。

        注意：早期版本会先捕获到空值就锁死（导致后续真实指纹抓不到，
        请求缺少 bx-ua 时极易被判机器人 → RGV587 风控）。现在只接受
        长度正常的值，并允许用更完整的值替换。
        """
        try:
            if MTOP_API in req.url and 'activate' not in req.url:
                m = urllib.parse.parse_qs(req.post_data or '')
                vals = m.get('bx-ua') or []
                if vals and len(vals[0]) > 100:
                    if not out or len(vals[0]) > len(out[0]):
                        out.clear()
                        out.append(vals[0])
        except Exception:
            pass

    @staticmethod
    def _build_extra_filter(city):
        """构造区域过滤参数：extraFilterValue（同城/省份筛选真正生效的地方）。"""
        if not city:
            return '{}'
        province = CITY_PROVINCE.get(city, '')
        division = {'city': city}
        if province:
            division['province'] = province
        obj = {
            'divisionList': [division],
            'excludeMultiPlacesSellers': '0',
            'extraDivision': '',
        }
        return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))

    # 闲鱼前端支持的「按发布时间筛选」天数（见 p_search-index.js 的 publishDays 分支）
    SERVER_DAY_FILTERS = (1, 3, 7, 14)

    @classmethod
    def build_search_obj(cls, keyword, page_number, city=None, days=None, mode='filtered'):
        """构造 mtop 搜索请求体。

        mode='filtered' 时按闲鱼前端的方式启用：
          - 最新排序：fromFilter=true, sortField='create', sortValue='desc'
          - 时间筛选：propValueStr.searchFilter = 'publishDays:N;'（仅 1/3/7/14 天）
        mode='plain' 为旧行为（综合排序），作为参数不被支持时的回退。
        """
        obj = {
            'pageNumber': page_number,
            'keyword': keyword,
            'fromFilter': False,
            'rowsPerPage': 30,
            'sortValue': '',
            'sortField': '',
            'customDistance': '',
            'gps': '',
            'propValueStr': {},
            'customGps': '',
            'searchReqFromPage': 'pcSearch',
            'extraFilterValue': cls._build_extra_filter(city),
            'userPositionJson': '{}',
        }
        if mode == 'filtered':
            obj['fromFilter'] = True
            obj['sortField'] = 'create'
            obj['sortValue'] = 'desc'
            if days in cls.SERVER_DAY_FILTERS:
                obj['propValueStr'] = {'searchFilter': 'publishDays:%d;' % days}
        return obj


    @staticmethod
    def _sleep_interruptible(seconds, should_cancel):
        """可被「停止」打断的等待；被打断返回 False。"""
        end = time.time() + seconds
        while time.time() < end:
            if should_cancel and should_cancel():
                return False
            time.sleep(0.3)
        return True


    def _body_has_next(self, body):
        try:
            data = json.loads(body)
            ri = data.get('data', {}).get('resultInfo')
            if isinstance(ri, dict) and 'hasNextPage' in ri:
                return bool(ri.get('hasNextPage'))
        except Exception:
            pass
        return None

    def _do_search(self, page, keyword):
        """在首页搜索框输入关键词并回车；失败则直接跳搜索 URL。"""
        try:
            inp = page.locator('input').first
            inp.click()
            inp.fill(keyword)
            page.keyboard.press('Enter')
        except Exception as e:
            print('[scrape] 搜索框输入失败，改直接跳转:', e)
            page.goto(SEARCH_URL.format(kw=keyword), wait_until='domcontentloaded', timeout=60000)

    def _capture_search(self, response, captured):
        try:
            u = response.url
            if 'idlemtopsearch.pc.search' in u and 'activate' not in u:
                ct = response.headers.get('content-type', '')
                if 'json' in ct or 'text' in ct:
                    body = response.text()
                    if len(body) > 200:
                        captured.append(body)
        except Exception:
            pass

    def _parse_search_body(self, body):
        try:
            data = json.loads(body)
        except Exception:
            return []
        result_list = data.get('data', {}).get('resultList') or []
        items = []
        for r in result_list:
            it = self._parse_result_item(r)
            if it:
                items.append(it)
        return items

    def _parse_result_item(self, r):
        try:
            main = r.get('data', {}).get('item', {}).get('main', {})
            ex = main.get('exContent', {})
            dp = ex.get('detailParams', {})
            args = main.get('clickParam', {}).get('args', {})
            title = ex.get('title') or dp.get('title')
            seller = ex.get('userNickName') or dp.get('userNick')
            price = args.get('price') or args.get('displayPrice') or dp.get('soldPrice')
            if not title or price is None:
                return None
            # 发布时间：clickParam.args.publishTime（毫秒时间戳）
            publish_time = args.get('publishTime')
            published_at = None
            try:
                if publish_time:
                    ts = float(publish_time)
                    published_at = ts / 1000.0 if ts > 1e11 else ts
            except (TypeError, ValueError):
                published_at = None
            # 地域：exContent.area（有城市过滤时为区名，如「罗湖区」）
            area = ex.get('area') or ''
            item_id = str(dp.get('itemId') or args.get('item_id') or '')
            # 商品图片（http 换 https 避免混合内容拦截）
            pic = ex.get('picUrl') or ''
            if pic.startswith('http://'):
                pic = 'https://' + pic[len('http://'):]
            review_count, positive_rate = self._parse_shop_label(ex)
            return {
                'title': str(title).strip()[:80],
                'price': self._to_price(price),
                'seller': str(seller or '未知卖家').strip(),
                'seller_id': str(args.get('seller_id') or ''),
                'dist': str(area).strip(),
                'time_str': str(publish_time or ''),
                'days_ago': _parse_days(publish_time),
                'published_at': published_at,
                'url': ('https://www.goofish.com/item?id=%s' % item_id) if item_id else '',
                'pic_url': pic,
                'item_id': item_id,
                'review_count': review_count,
                'positive_rate': positive_rate,
            }
        except Exception:
            return None

    @staticmethod
    def _parse_shop_label(ex):
        """从 userFishShopLabel 里提取「N条评价」和「好评率X%」。"""
        count = None
        rate = None
        try:
            tags = (ex.get('userFishShopLabel') or {}).get('tagList') or []
            for t in tags:
                content = ((t or {}).get('data') or {}).get('content') or ''
                m = re.match(r'^\s*(\d+)\s*条评价\s*$', content)
                if m:
                    count = int(m.group(1))
                    continue
                m = re.search(r'好评率\s*(\d+(?:\.\d+)?)\s*%', content)
                if m:
                    rate = float(m.group(1))
        except Exception:
            pass
        return count, rate

    def _find_field(self, obj, needles, depth=0):
        """递归在对象里找第一个 key 含 needles 中任一词的值。"""
        if depth > 6 or obj is None:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = k.lower()
                if any(n in kl for n in needles):
                    return v
                r = self._find_field(v, needles, depth + 1)
                if r is not None and r != '':
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = self._find_field(v, needles, depth + 1)
                if r is not None and r != '':
                    return r
        return None

    def _to_price(self, v):
        try:
            return round(float(str(v).replace(',', '').replace('¥', '').strip()), 2)
        except (ValueError, TypeError):
            return None

    def _search_has_next(self, captured):
        """返回 True/False/None（None=还没拿到结果接口）。"""
        for body in captured:
            try:
                data = json.loads(body)
            except Exception:
                continue
            ri = data.get('data', {}).get('resultInfo')
            if isinstance(ri, dict) and 'hasNextPage' in ri:
                return bool(ri.get('hasNextPage'))
        return None

    def _debug_screenshot(self, page, keyword):
        os.makedirs(DEBUG_DIR, exist_ok=True)
        name = re.sub(r'[^\w一-鿿]', '_', keyword)[:20] or 'kw'
        path = os.path.join(DEBUG_DIR, '%s_%d.png' % (name, int(time.time())))
        try:
            page.screenshot(path=path, full_page=False)
        except Exception:
            pass
