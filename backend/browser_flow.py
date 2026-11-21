"""通过页面自身搜索/筛选/翻页获取数据；不重放签名或设备指纹。"""
import json
import logging
import time
import urllib.parse
import re
import random
from itertools import count
from dataclasses import dataclass

from item_time import in_window, refresh_age


def is_platform_host(host):
    return any(host == domain or host.endswith('.' + domain)
               for domain in ('goofish.com', 'taobao.com', 'xianyu.com'))


def platform_https_url(url):
    if not isinstance(url, str) or '\\' in url or any(ord(c) <= 32 for c in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme == 'https' and parts.username is None and parts.password is None
                and parts.port in (None, 443) and is_platform_host(parts.hostname or '')):
            return parts
    except ValueError:
        pass
    return None


def is_search_response_url(url):
    parts = platform_https_url(url)
    return bool(parts and parts.hostname == 'h5api.m.goofish.com'
                and parts.path.lower() == '/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/')


class Challenge(Exception):
    def __init__(self, url=None, rate_limited=False):
        self.url = url
        self.rate_limited = rate_limited


class Stopped(Exception):
    def __init__(self, outcome):
        self.outcome = outcome


class LoginRequired(Exception):
    pass


@dataclass
class SearchResult:
    request: dict
    body: dict
    kind: str = 'success'
    verify_url: str = None


class BrowserCollection:
    PAGE_INTERVAL = 5
    RESPONSE_TIMEOUT = 30
    VERIFY_TIMEOUT = 300
    TASK_TIMEOUT = 1200
    BATCH_PAGES = 20

    def _phase(self, value):
        self.phase = value
        logging.info('collection phase=%s', value)
        if self._phase_callback:
            self._phase_callback(value)

    def _check(self, allow_pause=True):
        if time.monotonic() >= self._deadline:
            raise Stopped('timeout')
        if self._cancel and self._cancel():
            raise Stopped('cancelled')
        if allow_pause:
            while self._pause and self._pause():
                self._phase('paused')
                self._check(False)
                self._page.wait_for_timeout(200)
            if getattr(self, 'phase', None) == 'paused':
                self._phase('collecting')

    def _wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check()
            self._page.wait_for_timeout(200)

    @staticmethod
    def _request_data(request):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.url).query)
        fields = urllib.parse.parse_qs(request.post_data or '')
        value = (fields.get('data') or query.get('data') or ['{}'])[0]
        return json.loads(value)

    def _observe(self, response):
        # 只解析搜索主接口；激活接口、验证资源均不得当成商品响应。
        if not is_search_response_url(response.url):
            return
        try:
            request = self._request_data(response.request)
            if not request.get('keyword'):
                return
            if response.status == 429:
                result = SearchResult(request, {}, 'rate_limited')
            elif response.status >= 500:
                result = SearchResult(request, {}, 'network')
            else:
                body = response.json()
                ret = '|'.join(body.get('ret') or [])
                data = body.get('data') or {}
                if 'RGV587' in ret or 'USER_VALIDATE' in ret:
                    result = SearchResult(request, body, 'challenge', data.get('url'))
                elif any(code in ret for code in ('LIMIT', 'FLOW_CONTROL', 'FREQUENCY')):
                    result = SearchResult(request, body, 'rate_limited')
                elif 'SESSION' in ret or 'LOGIN' in ret:
                    result = SearchResult(request, body, 'login_expired')
                elif ret.startswith('SUCCESS') and isinstance(data.get('resultList'), list):
                    result = SearchResult(request, body)
                else:
                    result = SearchResult(request, body, 'api_error')
            self._results.append(result)
            logging.info('search phase=%s page=%s result=%s elapsed=%.1fs', getattr(self, 'phase', 'opening'),
                         request.get('pageNumber'), result.kind,
                         max(0, time.monotonic() - getattr(self, '_started_monotonic', time.monotonic())))
        except Exception as exc:
            # 不记录响应正文、链接和异常字符串，避免令牌进入日志。
            logging.warning('search response parse: %s', type(exc).__name__)

    @staticmethod
    def _city_matches(request, city):
        if not city:
            return True
        try:
            extra = request.get('extraFilterValue') or '{}'
            extra = json.loads(extra) if isinstance(extra, str) else extra
            return any(str(d.get('city', '')).removesuffix('市') == city.removesuffix('市')
                       for d in extra.get('divisionList', []))
        except (ValueError, TypeError, AttributeError):
            return False

    def _take_result(self, keyword, city=None, after=0):
        for result in self._results[after:]:
            if result.request.get('keyword') != keyword:
                if getattr(self, 'phase', None) == 'collecting':
                    raise RuntimeError('翻页搜索关键词已偏离任务范围，已停止并保留已采结果')
                continue
            # 即使筛选尚未应用，也必须立即停止验证/限流循环。
            if result.kind in ('challenge', 'rate_limited'):
                raise Challenge(result.verify_url, result.kind == 'rate_limited')
            if not self._city_matches(result.request, city):
                if getattr(self, 'phase', None) == 'collecting':
                    raise RuntimeError('翻页城市已偏离任务范围，已停止并保留已采结果')
                continue
            if result.kind == 'login_expired':
                raise LoginRequired()
            if result.kind == 'network':
                raise TimeoutError('页面搜索暂时不可用')
            if result.kind != 'success':
                raise RuntimeError('搜索接口未成功，已停止；未把错误响应当作空结果')
            return result
        return None

    def _response(self, keyword, city=None, after=0):
        end = time.monotonic() + self.RESPONSE_TIMEOUT
        while time.monotonic() < end:
            self._check()
            result = self._take_result(keyword, city, after)
            if result:
                return result
            if self._on_login_page(self._page) or self._login_prompt_visible(self._page):
                raise LoginRequired()
            self._page.wait_for_timeout(200)
        raise TimeoutError('等待页面搜索结果超时')

    def _perform(self, action, keyword, city=None):
        # 网络问题只重试一次；验证、登录失效、业务错误绝不在此刷新重试。
        for attempt in range(2):
            self._check()
            after = len(self._results)
            try:
                action()
                return self._response(keyword, city, after)
            except Exception as exc:
                if not isinstance(exc, TimeoutError) and type(exc).__name__ != 'TimeoutError':
                    raise
                if attempt:
                    raise RuntimeError('页面网络超时，重试一次后停止') from None
                self._wait(10)

    def _click_text(self, text):
        self._check()
        locator = self._page.get_by_text(text, exact=True)
        for index in range(locator.count()):
            self._check()
            option = locator.nth(index)
            if option.is_visible():
                option.click(timeout=10000)
                return True
        return False

    def _apply_city(self, keyword, city, current):
        if not city or self._city_matches(current.request, city):
            return current
        self._phase('applying_filters')
        # 当前页面是两列省/市弹层；必须限定弹层，不能点到商品的同名地区。
        after = len(self._results)
        trigger = self._page.locator('[class*="areaTextContainer--"]:visible').first
        if not trigger.count():
            raise RuntimeError('页面区域控件不可用，已停止，未接收全国结果')
        panel = self._page.locator('[class*="areaWrap--"]:visible').first
        if not panel.count():
            trigger.click(timeout=10000)
        panel = self._page.locator('[class*="areaWrap--"]:visible').first
        panel.wait_for(state='visible', timeout=10000)
        province = self.CITY_PROVINCE.get(city, city)
        option = panel.get_by_text(province, exact=True).first
        if not option.count():
            option = panel.get_by_text(province + '省', exact=True).first
        option.scroll_into_view_if_needed(timeout=10000)
        option.hover(timeout=10000)
        option.click(timeout=10000)
        self._check()
        if province != city:
            target = panel.get_by_text(re.compile('^' + re.escape(city) + '市?$')).first
            target.wait_for(state='visible', timeout=10000)
            target.click(timeout=10000)
        else:
            # 直辖市可能同时显示在省列和市列；选最右侧的同名项。
            targets = panel.get_by_text(re.compile('^' + re.escape(city) + '市?$'))
            if targets.count() > 1:
                targets.last.click(timeout=10000)
        # 当前页面用“查看…件宝贝”提交选择，而不是“确定”。
        submit = self._page.get_by_text(re.compile('^查看.*件宝贝$')).filter(visible=True).first
        submit.click(timeout=10000)
        result = self._response(keyword, city, after)
        self._phase('collecting')
        return result

    def _next(self):
        candidates = (
            self._page.locator('[class*="search-filter-distance-page-container--"]:visible').first.locator('button').last,
            self._page.get_by_role('button', name='下一页', exact=True),
            self._page.locator('[title="下一页"], [aria-label="下一页"], .ant-pagination-next'),
            self._page.get_by_text('下一页', exact=True),
        )
        for locator in candidates:
            self._check()
            for index in range(locator.count()):
                button = locator.nth(index)
                if not button.is_visible():
                    continue
                disabled = button.get_attribute('aria-disabled') == 'true' or 'disabled' in (button.get_attribute('class') or '')
                if disabled or not button.is_enabled():
                    return False
                button.click(timeout=10000)
                return True
        return False

    @staticmethod
    def _latest_matches(request):
        return request.get('sortField') == 'create' and request.get('sortValue') == 'desc'

    def _page_interval(self, processed_pages):
        # 仅降低请求频率，不模拟用户行为或尝试解除平台验证。
        if processed_pages % self.BATCH_PAGES == 0:
            self._phase('resting')
            self._wait(random.uniform(10, 13))
            self._phase('collecting')
        else:
            self._wait(random.uniform(self.PAGE_INTERVAL, self.PAGE_INTERVAL + 2))

    @staticmethod
    def _old_page_boundary(rows, parsed, days, now, streak, previous_last):
        """仅连续完整、时间已知且顺序一致的过期页才构成提前停止依据。"""
        if not days or not rows or len(rows) != len(parsed):
            return 0, None
        stamps = [item.get('published_at') for item in parsed]
        cutoff = now - days * 86400
        if any(stamp is None or stamp > cutoff for stamp in stamps):
            return 0, None
        if any(a < b for a, b in zip(stamps, stamps[1:])):
            return 0, None
        streak = streak + 1 if previous_last is not None and stamps[0] <= previous_last else 1
        return streak, stamps[-1]

    def _apply_latest(self, keyword, city, current):
        if self._latest_matches(current.request):
            return current
        self._phase('applying_filters')
        self._wait(self.PAGE_INTERVAL)
        # 网站“新发布”下的“最新”才是发布时间排序；只操作该菜单，避免商品同名文本。
        menu = self._page.locator('[class*="search-select-container--"]:visible').filter(
            has=self._page.get_by_text('新发布', exact=True)).first
        if not menu.count():
            raise RuntimeError('页面最新发布排序控件不可用，已停止，未接收综合排序结果')
        menu.hover(timeout=10000)
        option = menu.get_by_text('最新', exact=True)
        option.wait_for(state='visible', timeout=10000)
        self._check()
        after = len(self._results)
        option.click(timeout=10000)
        # 此控件重复点击会取消排序，不能在超时后盲目重试点击。
        result = self._response(keyword, city, after)
        if not self._latest_matches(result.request) or int(result.request.get('pageNumber') or 0) != 1:
            raise RuntimeError('最新发布排序未生效，已停止，未接收错误排序结果')
        return result

    def _manual_verification(self, challenge, keyword, verified_count):
        if challenge.rate_limited or verified_count:
            self.cooldown_required = True
            self.last_error = '再次触发验证或平台限流，已停止并冷却30分钟'
            return False
        self._phase('waiting_verification')
        self.cooldown_required = True
        start = len(self._results)
        # 优先保留页面自己的验证框。仅在没有框时打开平台给出的验证页。
        has_frame = any('punish' in f.url or 'x5sec' in f.url for f in self._page.frames)
        url = challenge.url or ''
        if not has_frame and platform_https_url(url):
            self._page.goto(url, wait_until='domcontentloaded', timeout=30000)
        end = time.monotonic() + self.VERIFY_TIMEOUT
        while time.monotonic() < end:
            self._check(False)
            # 网站自行返回有效搜索结果，或用户明确点击现有“验证完成，继续”。
            if any(r.kind == 'success' and r.request.get('keyword') == keyword
                   for r in self._results[start:]):
                self._verified_result = next(r for r in self._results[start:]
                                             if r.kind == 'success' and r.request.get('keyword') == keyword)
                self.cooldown_required = False
                return True
            if self._resume and self._resume():
                self.cooldown_required = False
                return True
            self._page.wait_for_timeout(200)
        self.cooldown_required = True
        self.last_error = '人工验证等待超时，已停止并冷却30分钟'
        return False

    def collect(self, keyword, days=0, city=None, should_pause=None, should_cancel=None,
                on_progress=None, on_phase=None, should_resume=None, max_pages=None):
        self._phase_callback, self._cancel, self._pause = on_phase, should_cancel, should_pause
        self._resume = should_resume
        self._started_monotonic = time.monotonic()
        self._deadline = self._started_monotonic + self.TASK_TIMEOUT
        self._results = []
        self._verified_result = None
        self.last_error = self.partial_error = None
        self.cooldown_required = False
        self.outcome = 'success'
        seen, verified_count, processed_pages, login_count = {}, 0, 0, 0
        # 正常任务由网页实际下一页和总时限终止；max_pages 仅供有限验收。
        limit = max_pages
        try:
            self._phase('opening')
            self._check(False)
            self._launch()
            context = self._new_context()
            self._page = context.pages[0] if context.pages else context.new_page()
            self._page.on('response', self._observe)
            while True:
                try:
                    self._phase('checking_verification' if verified_count else 'opening')
                    url = 'https://www.goofish.com/search?q=' + urllib.parse.quote(keyword, safe='')
                    if self._verified_result:
                        current, self._verified_result = self._verified_result, None
                    else:
                        current = self._perform(lambda: self._page.goto(url, wait_until='domcontentloaded', timeout=30000), keyword)
                    current = self._apply_city(keyword, city, current)
                    current = self._apply_latest(keyword, city, current)
                    self._phase('collecting')
                    old_streak, previous_old_last = 0, None
                    for number in count(1):
                        self._check()
                        if (current.request.get('keyword') != keyword
                                or not self._latest_matches(current.request)
                                or not self._city_matches(current.request, city)):
                            raise RuntimeError('翻页后关键词、排序或城市条件丢失，已停止并保留已采结果')
                        parsed_count = 0
                        parsed, now = [], time.time()
                        rows = current.body['data']['resultList']
                        for row in rows:
                            item = self._parse_result_item(row)
                            if item:
                                parsed_count += 1
                                refresh_age(item, now)
                                parsed.append(item)
                                if days and not in_window(item, days, now):
                                    continue
                                if city:
                                    item['city'] = city
                                key = ('id', item['item_id']) if item.get('item_id') else ('ts', item['title'], item['seller'])
                                seen[key] = item
                        if current.body['data']['resultList'] and not parsed_count:
                            raise RuntimeError('页面有搜索结果但商品格式无法解析，已停止以避免误报空结果')
                        if on_progress:
                            on_progress(number, limit or 0, len(seen))
                        processed_pages += 1
                        has_next = self._body_has_next(json.dumps(current.body))
                        if has_next is False:
                            break
                        if not rows:
                            self.partial_error = '页面已无商品结果，已结束，可能不完整'
                            break
                        old_streak, previous_old_last = self._old_page_boundary(
                            rows, parsed, days, now, old_streak, previous_old_last)
                        if old_streak >= 2:
                            self.partial_error = '连续2页商品均超出所选时间范围，已结束；后续结果未取尽，可能不完整'
                            break
                        if number == limit:
                            self.partial_error = '已达页数上限，可能不完整'
                            break
                        self._page_interval(processed_pages)
                        clicked = [False]
                        def next_page():
                            clicked[0] = self._next()
                            if not clicked[0]:
                                raise StopIteration()
                        previous = int(current.request.get('pageNumber') or number)
                        try:
                            current = self._perform(next_page, keyword, city)
                        except StopIteration:
                            if has_next is not False:
                                self.partial_error = '页面未提供可用下一页，可能不完整'
                            break
                        if int(current.request.get('pageNumber') or 0) != previous + 1:
                            raise RuntimeError('页面页码与预期不符，已停止以避免重复或漏页')
                    break
                except LoginRequired:
                    if login_count:
                        raise RuntimeError('重新登录后查询仍未通过登录检查，已停止并保留已采结果')
                    login_count += 1
                    self._phase('waiting_login')
                    if not self._interactive_login(context, self._page, lambda: self._check(False)):
                        raise RuntimeError('等待登录超时或登录窗口已关闭，已保存已采结果')
                    # 重新发起原条件网页查询；须收到有效响应才算续采成功。
                    self._verified_result = None
                except Challenge as challenge:
                    if not self._manual_verification(challenge, keyword, verified_count):
                        self.outcome = 'partial' if seen else 'failed'
                        break
                    verified_count += 1
                    # 原配置、原上下文重进查询；已采 ID 保留，重复页只更新不新增。
            self._check(False)
        except Stopped as stopped:
            self.outcome = stopped.outcome
            self.last_error = '任务达到20分钟时限' if stopped.outcome == 'timeout' else None
        except Exception as exc:
            self.outcome = 'partial' if seen else 'failed'
            if getattr(self, '_page', None) and isinstance(exc, RuntimeError):
                try:
                    self._debug_screenshot(self._page, keyword)
                except Exception:
                    pass
            if isinstance(exc, RuntimeError):
                self.last_error = str(exc)
            else:
                self.last_error = '浏览器操作失败（%s）' % type(exc).__name__
        finally:
            if self._context:
                try:
                    self._save_session(self._context)
                except Exception as exc:
                    logging.warning('session save: %s', type(exc).__name__)
            self.close()
        if self.last_error or self.partial_error:
            self.partial_error = self.last_error or self.partial_error
            if self.outcome == 'success':
                self.outcome = 'partial'
        return list(seen.values())
