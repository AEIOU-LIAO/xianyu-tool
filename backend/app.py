# -*- coding: utf-8 -*-
"""
后端服务：提供网页 + 接口。
  GET  /               -> 前端页面 index.html
  GET  /api/status     -> 运行状态 / 是否登录 / 信息池条数
  GET  /api/items      -> 信息池商品（支持 cat/price/dist/only_personal 筛选）
  GET  /api/sellers    -> 卖家聚合（含二道贩子识别结果）
  POST /api/scrape     -> 触发一次抓取（后台线程执行）
  POST /api/login      -> 打开浏览器扫码登录（后台线程执行）

启动：python backend/app.py   （浏览器访问 http://localhost:8000）
"""
import json
import mimetypes
import os
import threading
import time
import uuid
import math
import logging
from logging.handlers import RotatingFileHandler
from werkzeug.exceptions import BadRequest
from item_time import refresh_age

from flask import Flask, jsonify, request, send_from_directory

import seller_filter
import storage
from scraper import Scraper

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows 上 .webp 常未被识别，手动注册，否则壁纸会以 octet-stream 返回
mimetypes.add_type('image/webp', '.webp')

app = Flask(__name__, static_folder=None)

# 抓取器：headless=False 更稳（能看到扫码窗口），channel 用本机已装的 Edge
scraper = Scraper(headless=False, channel='msedge')

state = {
    'running': False,
    'paused': False,
    'logging_in': False,
    'last_run': None,
    'last_added': 0,
    'last_updated': 0,
    'last_error': None,
    'progress': {},
    'started_at': None,
    'phase': 'idle',
    'outcome': None,
    'cooldown_until': 0,
}

MAX_SCRAPE_SECONDS = 20 * 60   # 单次采集超过 20 分钟自动中止，避免线程卡死
state_lock = threading.Lock()
# 采集线程的控制开关（暂停/取消）
control = {'paused': False, 'cancel': False}


def _cooldown_until():
    value = storage.get_state().get('cooldown_until', 0)
    value = value if isinstance(value, (int, float)) and math.isfinite(value) else 0
    return max(value, state.get('cooldown_until', 0))


def _snapshot():
    cooldown = _cooldown_until()
    with state_lock:
        snapshot = dict(state)
        snapshot['progress'] = dict(state['progress'])
    snapshot['phase'] = snapshot['phase'] if snapshot['running'] or snapshot['logging_in'] else ('cooldown' if cooldown > time.time() else 'idle')
    snapshot.update(logged_in=scraper.is_logged_in(), total=len(storage.get_items()), cooldown_until=cooldown)
    return snapshot



# ---------- 页面 ----------
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/static/<path:filename>')
def static_files(filename):
    """壁纸等静态资源（static/skins/...）。"""
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)


@app.route('/api/skins')
def api_skins():
    path = os.path.join(BASE_DIR, 'static', 'skins', 'skins.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {'count': 0, 'roles': [], 'skins': []}
    return jsonify(data)


ALLOWED_HOSTS = {'localhost:8000', '127.0.0.1:8000'}
ALLOWED_ORIGINS = {'http://localhost:8000', 'http://127.0.0.1:8000'}


@app.before_request
def _guard_local():
    if request.host not in ALLOWED_HOSTS:
        return jsonify({'ok': False, 'msg': 'Host 不被允许'}), 403
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('Origin')
        if origin and origin not in ALLOWED_ORIGINS:
            return jsonify({'ok': False, 'msg': '请求来源不被允许'}), 403
        # 所有有请求体的写接口只接受对象，并预检字符串类型。
        if request.content_length:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({'ok': False, 'msg': '请求体必须为 JSON 对象'}), 400
            for field in ('keyword', 'city', 'key', 'mark', 'name', 'action', 'id'):
                if field in data and not isinstance(data[field], str):
                    return jsonify({'ok': False, 'msg': field + ' 必须为文本'}), 400
            if 'item_id' in data and (isinstance(data['item_id'], bool) or not isinstance(data['item_id'], (str, int))):
                return jsonify({'ok': False, 'msg': 'item_id 不合法'}), 400
            if len(data.get('keyword', '')) > 60 or len(data.get('city', '')) > 30:
                return jsonify({'ok': False, 'msg': '关键词或城市过长'}), 400
    return None


@app.errorhandler(BadRequest)
def _bad_request(error):
    return jsonify({'ok': False, 'msg': error.description}), 400


def _classified_items():
    # 同一锁中读取人工标记与商品，查询时重新计算时间敏感信号，不写全量文件。
    with storage._WRITE_LOCK:
        items = [refresh_age(dict(i)) for i in storage.get_items()]
        return seller_filter.classify_items(items, storage.load_marks())



def _parse_int(value, default=None, lo=None, hi=None):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        n = int(value)
    except (ValueError, TypeError, OverflowError):
        return default
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        return default
    return n


def _parse_days(value, default=None):
    # 保留 days 接口兼容性，仅接受新增1/2/3/12小时对应的小数。
    for window in (1 / 24, 2 / 24, 3 / 24, 0.5):
        if value == window or value == str(window):
            return window
    return _parse_int(value, default, 0, 365)



def _display_seller(it):
    """卖家展示名：昵称缺失时用 seller_id 尾部区分。"""
    name = (it.get('seller') or '').strip()
    if name and name != '未知卖家':
        return name
    sid = (it.get('seller_id') or '').strip()
    return ('卖家#' + sid[:6]) if sid else '未知卖家'


def _csv_safe(value):
    """导出 CSV 时防止表格软件把文本当公式执行。"""
    s = '' if value is None else str(value)
    if s[:1] in ('\t', '\r', '\n') or s.lstrip()[:1] in ('=', '+', '-', '@'):
        return "'" + s
    return s


# ---------- 接口 ----------
@app.route('/api/status')
def api_status():
    return jsonify(_snapshot())


def _query_items(args):
    """按查询参数筛选信息池，返回 (items, keywords, dists, cities)。"""
    cat = args.get('cat', '')
    keyword = args.get('keyword', '')
    price = args.get('price', '')
    dist = args.get('dist', '')
    city = args.get('city', '')
    seller_type = args.get('seller_type', '')
    only_personal = args.get('only_personal', '') in ('1', 'true', 'True')
    q = (args.get('q') or '').strip().lower()
    days_max = args.get('days_max', '')
    try:
        days_max = int(days_max) if days_max not in ('', None) else None
    except ValueError:
        raise BadRequest('days_max 参数不合法')
    if days_max is not None and not 0 <= days_max <= 3650:
        raise BadRequest('days_max 超出范围')

    items = _classified_items()
    # 收集全部搜索关键词 / 城市 / 区（未过滤），供前端筛选项使用
    keywords = []
    dists = []
    cities = []
    for it in items:
        kw = it.get('keyword', '')
        if kw and kw not in keywords:
            keywords.append(kw)
        d = it.get('dist', '')
        if d and d not in dists:
            dists.append(d)
        c = it.get('city', '')
        if c and c not in cities:
            cities.append(c)

    out = []
    now = time.time()
    for it in items:
        # 老数据没有 url 的，用 item_id 补齐详情页链接
        if not it.get('url') and it.get('item_id'):
            it['url'] = 'https://www.goofish.com/item?id=%s' % it['item_id']
        # 按发布时间动态计算「几天前」，避免跨天后仍显示“今天”
        pub = it.get('published_at')
        if pub:
            try:
                it['days_ago'] = max(0, int((now - float(pub)) // 86400))
            except (TypeError, ValueError):
                pass
        if cat and it.get('cat') != cat:
            continue
        if keyword and it.get('keyword') != keyword:
            continue
        if city and it.get('city') != city:
            continue
        if dist and (it.get('dist') or '') != dist:
            # 兜底：结构化字段没有区时，用标题里的区名匹配
            core = dist.replace('新区', '').replace('区', '')
            if core not in (it.get('title') or ''):
                continue
        if seller_type and it.get('seller_type') != seller_type:
            continue
        if only_personal and it.get('seller_type') != 'personal':
            continue
        if q:
            hay = ((it.get('title') or '') + ' ' + (it.get('seller') or '')).lower()
            if q not in hay:
                continue
        if days_max is not None:
            d = it.get('days_ago')
            if d is None or d > days_max:
                continue
        if price:
            a, _, b = price.partition('-')
            try:
                lo = float(a) if a else None
                hi = float(b) if b else None
            except ValueError:
                raise BadRequest('价格区间不合法')
            if any(v is not None and (not math.isfinite(v) or v < 0) for v in (lo, hi)) or (lo is not None and hi is not None and lo > hi):
                raise BadRequest('价格区间不合法')
            p = it.get('price')
            if p is None:
                continue
            if lo is not None and p < lo:
                continue
            if hi is not None and p > hi:
                continue
        out.append(it)
    # 排序
    sort = args.get('sort', 'new')
    if sort == 'price-asc':
        out.sort(key=lambda x: (x.get('price') is None, x.get('price') or 0))
    elif sort == 'price-desc':
        out.sort(key=lambda x: (x.get('price') is None, -(x.get('price') or 0)))
    elif sort == 'days':
        out.sort(key=lambda x: (x.get('days_ago') is None, x.get('days_ago') or 0))
    else:
        out.sort(key=lambda x: x.get('created_at', 0), reverse=True)
    return out, keywords, dists, cities


@app.route('/api/items')
def api_items():
    out, keywords, dists, cities = _query_items(request.args)
    return jsonify({'items': out, 'total': len(out), 'keywords': keywords,
                    'dists': dists, 'cities': cities})


@app.route('/api/items', methods=['DELETE'])
def api_items_delete():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BadRequest('请求体必须为 JSON 对象')
    kind, value, city = data.get('kind'), data.get('value'), data.get('city', '')
    if not isinstance(kind, str) or kind not in ('keyword', 'city', 'dist'):
        raise BadRequest('删除类型不合法')
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise BadRequest('删除标签不能为空或过长')
    if not isinstance(city, str) or len(city) > 30 or (city and kind != 'dist'):
        raise BadRequest('城市范围不合法')

    def matches(item):
        if city and item.get('city') != city:
            return False
        if kind == 'dist':
            # 与地区筛选的标题兜底保持一致。
            core = value.replace('新区', '').replace('区', '')
            return item.get('dist') == value or bool(core and core in (item.get('title') or ''))
        return item.get(kind) == value

    # 与采集启动共用状态锁，避免删除后被正在运行的任务重新写回。
    with state_lock:
        if state['running']:
            return jsonify({'ok': False, 'msg': '请先停止采集，等待保存完成后再删除'}), 409
        try:
            result = storage.delete_items_matching(matches)
        except (OSError, RuntimeError):
            logging.exception('item deletion commit failed')
            return jsonify({'ok': False, 'msg': '删除保存失败，请重试；恢复日志已保留'}), 500
    return jsonify(ok=True, **result)


@app.route('/api/export.csv')
def api_export():
    """导出当前筛选结果为 CSV（带 BOM，Excel 直接打开不乱码）。"""
    import csv
    import io
    out, *_ = _query_items(request.args)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['标题', '价格', '区', '城市', '发布(天前)', '卖家', '卖家类型',
                     '嫌疑评分', '判定信号', '关键词', '商品链接', '图片链接'])
    type_label = {'personal': '个人卖家', 'pending': '待判定', 'reseller': '二道贩子'}
    for it in out:
        writer.writerow([
            _csv_safe(it.get('title', '')),
            it.get('price', ''),
            _csv_safe(it.get('dist', '')),
            _csv_safe(it.get('city', '')),
            it.get('days_ago', ''),
            _csv_safe(_display_seller(it)),
            type_label.get(it.get('seller_type'), it.get('seller_type', '')),
            it.get('seller_score', ''),
            _csv_safe('、'.join(it.get('seller_signals') or [])),
            _csv_safe(it.get('keyword', '')),
            _csv_safe(it.get('url', '')),
            _csv_safe(it.get('pic_url', '')),
        ])

    data = '﻿' + buf.getvalue()   # BOM，Excel 兼容
    filename = time.strftime('xianyu_%Y%m%d_%H%M.csv')
    from flask import Response
    return Response(data, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % filename})


@app.route('/api/sellers')
def api_sellers():
    items = _classified_items()
    groups = {}
    for it in items:
        key = it.get('seller_key') or seller_filter.seller_key(it)
        if not key or key.startswith('anon:'):
            continue
        groups.setdefault(key, []).append(it)

    out = []
    for key, its in groups.items():
        first = its[0]
        dists = sorted({i.get('dist') for i in its if i.get('dist')})
        name = (first.get('seller') or '').strip()
        if not name or name == '未知卖家':
            # 闲鱼部分卡片不返回昵称，用卖家 ID 区分不同的人
            sid = first.get('seller_id') or ''
            name = ('卖家#' + sid[:6]) if sid else '未知卖家'
        out.append({
            'key': key,
            'name': name,
            'seller_id': first.get('seller_id', ''),
            'type': first.get('seller_type', 'personal'),
            'score': first.get('seller_score', 0),
            'signals': first.get('seller_signals', []),
            'count': len(its),
            'districts': dists,
            'review_count': max((i.get('review_count') or 0) for i in its),
            'positive_rate': next((i.get('positive_rate') for i in its
                                   if i.get('positive_rate') is not None), None),
            'sample_pic': next((i.get('pic_url') for i in its if i.get('pic_url')), ''),
            'item_ids': [i.get('item_id') for i in its if i.get('item_id')][:12],
        })
    out.sort(key=lambda s: -s['score'])
    return jsonify({'sellers': out, 'total': len(out)})


@app.route('/api/mark_seller', methods=['POST'])
def api_mark_seller():
    data = request.get_json(silent=True) or {}
    key = (data.get('key') or '').strip()
    mark = (data.get('mark') or '').strip()   # reseller / personal / clear
    name = (data.get('name') or '').strip()
    if not key:
        return jsonify({'ok': False, 'msg': '缺少卖家标识'}), 400
    if mark not in ('reseller', 'personal', 'clear'):
        return jsonify({'ok': False, 'msg': '未知标记类型'}), 400

    def _apply(marks):
        marks['reseller'].pop(key, None)
        marks['personal'].pop(key, None)
        if mark in ('reseller', 'personal'):
            marks[mark][key] = {'name': name, 'ts': time.time()}
        return True

    def _reclassify(items, marks):
        # 与标记在同一事务内重算，避免读到旧标记
        items[:] = seller_filter.classify_items(items, marks)

    storage.update_marks_and_items(_apply, _reclassify)

    msg = {'reseller': '已标为二道贩子', 'personal': '已标为个人卖家', 'clear': '已取消标记'}[mark]
    return jsonify({'ok': True, 'msg': msg})


def _on_progress(page, pages, collected):
    with state_lock:
        state['progress'] = {'page': page, 'pages': pages, 'collected': collected}


_SNAPSHOT_FIELDS = ('title', 'dist', 'city', 'seller', 'seller_type', 'pic_url',
                    'url', 'keyword', 'item_id', 'time_str', 'days_ago',
                    'price', 'seller_score', 'published_at')


def _clean_item_snapshot(src):
    """校验前端提交的收藏快照：只保留已知字段，且链接必须是闲鱼商品页。"""
    if not isinstance(src, dict):
        return {}
    out = {}
    for f in _SNAPSHOT_FIELDS:
        v = src.get(f)
        numeric = f in ('days_ago', 'price', 'seller_score', 'published_at')
        if numeric and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            out[f] = v
        elif not numeric and isinstance(v, str):
            out[f] = v[:1000]
    url = out.get('url') or ''
    if url and not url.startswith(('https://www.goofish.com/item?id=',
                                   'http://www.goofish.com/item?id=')):
        out.pop('url', None)
    return out


def _item_key(it):
    """商品去重键：优先 item_id，缺失才回退标题 + 卖家。"""
    iid = it.get('item_id')
    if iid:
        return ('id', iid)
    return ('ts', (it.get('title'), it.get('seller') or it.get('seller_id')))


def _run_scrape(keyword, days, city):
    """采集 + 合并去重 + 重新打分 + 清理过期数据（后台线程执行）。

    每个任务使用独立的 Scraper 实例，避免共享 Playwright 对象导致线程冲突。
    """
    worker = Scraper(headless=False, channel='msedge')
    try:
        raw = worker.scrape(keyword, days, city or None,
                            should_pause=lambda: control['paused'],
                            should_cancel=_cancel_requested,
                            on_progress=_on_progress, on_phase=_on_phase,
                            should_resume=_resume_requested)
        for it in raw:
            it['keyword'] = keyword
            if city:
                it['city'] = city

        def _merge(items):
            """在事务锁内完成：合并去重 + 按人工标记重新打分。"""
            index = {_item_key(i): i for i in items}
            added = 0
            updated = 0
            for it in raw:
                key = _item_key(it)
                old = index.get(key)
                if old is not None:
                    changed = False
                    for f in ('title', 'price', 'days_ago', 'time_str', 'dist',
                              'pic_url', 'url', 'keyword', 'city', 'seller',
                              'seller_id', 'review_count', 'positive_rate',
                              'published_at'):
                        if it.get(f) not in (None, ''):
                            if old.get(f) != it.get(f):
                                old[f] = it[f]
                                changed = True
                    if changed:
                        updated += 1
                else:
                    it.setdefault('created_at', time.time())   # 入库时间，供保留策略使用
                    index[key] = it
                    items.append(it)
                    added += 1

            marks = storage.load_marks()
            items[:] = seller_filter.classify_items([refresh_age(i) for i in items], marks)
            return added, updated

        added, updated = storage.update_items(_merge) if raw else (0, 0)

        # 清理过期 / 超量数据
        settings = storage.load_settings()
        removed = storage.prune_items(settings.get('keep_days', 30),
                                      settings.get('max_items', 20000)) if raw else 0
        if removed:
            print('[scrape] 清理过期商品 %d 条' % removed, flush=True)

        with state_lock:
            state['last_added'] = added
            state['last_updated'] = updated
            state['last_run'] = time.time()
            state['outcome'] = 'timeout' if state.get('outcome') == 'timeout' else worker.outcome
            if state['outcome'] == 'timeout':
                state['last_error'] = '采集达到20分钟时限，已停止并保存已采结果'
            # 翻页中途失败：保留已采数据，但把原因告诉用户
            if getattr(worker, 'partial_error', None):
                state['last_error'] = '%s%s' % (worker.partial_error, '（已保存已采到的商品）' if raw else '')
    except Exception as e:
        with state_lock:
            state['last_error'] = '采集提交失败（%s），请查看本机日志' % type(e).__name__
            state['outcome'] = 'failed'
            state['last_run'] = time.time()
    finally:
        # 先释放浏览器资源，再把状态置为空闲：
        # 否则新任务可能在清理旧实例期间启动，导致浏览器对象被覆盖
        try:
            worker.close()
        except Exception as exc:
            logging.warning('worker cleanup failed: %s', type(exc).__name__)
        finally:
            if worker.cooldown_required:
                until = time.time() + 1800
                state['cooldown_until'] = until
                try:
                    storage.update_state(lambda value: value.update(cooldown_until=until))
                except Exception as exc:
                    logging.error('cooldown persistence failed: %s', type(exc).__name__)
        with state_lock:
            state['running'] = False
            state['phase'] = 'cooldown' if state['cooldown_until'] > time.time() else 'idle'
            state['paused'] = False
            state['progress'] = {}
            state['started_at'] = None
            control['paused'] = False


def _start_scrape(keyword, days, city):
    with state_lock:
        if state['running'] or state['logging_in']:
            return False, '正在采集或登录，请稍候'
        cooldown = _cooldown_until()
        if cooldown > time.time():
            return False, '采集冷却中，剩余 %d 秒' % math.ceil(cooldown - time.time())
        state.update(running=True, paused=False, last_error=None, started_at=time.time(),
                     phase='opening', outcome=None, progress={'page': 0, 'pages': 0, 'collected': 0},
                     last_added=0, last_updated=0)
        control.update(paused=False, cancel=False, resume_verification=False)
    try:
        threading.Thread(target=_run_scrape, args=(keyword, days, city), daemon=True).start()
    except Exception:
        with state_lock:
            state.update(running=False, phase='idle', outcome='failed', started_at=None, progress={})
        return False, '无法启动采集线程，请稍后重试'
    return True, '开始采集：%s' % keyword


def _on_phase(phase):
    with state_lock:
        state['phase'] = phase


def _cancel_requested():
    with state_lock:
        if state['started_at'] and time.time() - state['started_at'] >= MAX_SCRAPE_SECONDS:
            control['cancel'] = True
            state['outcome'] = 'timeout'
        return control['cancel']


def _resume_requested():
    with state_lock:
        value = control.get('resume_verification', False)
        control['resume_verification'] = False
        return value



def _scheduler_loop():
    """后台调度：按各定时任务的间隔触发采集（同一时间只跑一个）。"""
    while True:
        try:
            now = time.time()
            # 看门狗：采集卡住太久就请求停止
            with state_lock:
                started = state.get('started_at')
                if state['running'] and started and now - started > MAX_SCRAPE_SECONDS:
                    control['cancel'] = True
                    print('[scheduler] 采集超过 %d 分钟，已请求停止' % (MAX_SCRAPE_SECONDS // 60),
                          flush=True)
            scheds = storage.load_schedules()
            started_id = None
            for s in scheds:
                if not s.get('enabled', True):
                    continue
                interval = _parse_int(s.get('interval'), 30, 5, 1440) * 60
                last = s.get('last_run') or 0
                if now - last < interval:
                    continue
                if not scraper.is_logged_in():
                    break
                ok, msg = _start_scrape((s.get('keyword') or '').strip(),
                                        _parse_days(s.get('days'), 1),
                                        (s.get('city') or '').strip())
                if ok:
                    started_id = s.get('id')
                    print('[scheduler] ' + msg, flush=True)
                    break

            if started_id:
                # 只更新这一个任务的 last_run，避免用旧快照覆盖用户刚做的删除/改动
                def _mark_last_run(items):
                    for s in items:
                        if s.get('id') == started_id:
                            s['last_run'] = now

                storage.update_schedules(_mark_last_run)
        except Exception as e:
            print('[scheduler] error:', e)
        time.sleep(15)


@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    data = request.get_json(silent=True) or {}
    keyword = (data.get('keyword') or '').strip()
    if len(keyword) > 60:
        return jsonify({'ok': False, 'msg': '关键词过长'}), 400
    days = _parse_days(data.get('days', 1))
    if days is None:
        return jsonify({'ok': False, 'msg': 'days 参数不合法'}), 400
    city = (data.get('city') or '').strip()
    if not keyword:
        return jsonify({'ok': False, 'msg': '请先填写商品名/关键词'}), 400
    if not scraper.is_logged_in():
        return jsonify({'ok': False, 'msg': '尚未登录闲鱼，请先点击「登录闲鱼」扫码'}), 400
    ok, msg = _start_scrape(keyword, days, city)
    status = 200 if ok else (429 if '冷却' in msg else 400)
    response = jsonify({'ok': ok, 'msg': msg})
    if status == 429:
        response.headers['Retry-After'] = str(max(1, math.ceil(_cooldown_until() - time.time())))
    return response, status


@app.route('/api/pause', methods=['POST'])
def api_pause():
    with state_lock:
        if not state['running']:
            return jsonify({'ok': False, 'msg': '当前没有采集任务'}), 400
        if state['phase'] == 'waiting_verification':
            control['resume_verification'] = True
            return jsonify({'ok': True, 'paused': False, 'msg': '正在检查验证结果并恢复采集'})
        state['paused'] = not state['paused']
        control['paused'] = state['paused']
        paused = state['paused']
    return jsonify({'ok': True, 'paused': paused, 'msg': '已暂停采集' if paused else '已继续采集'})



@app.route('/api/favorites', methods=['GET'])
def api_favorites():
    favs = storage.load_favorites()
    # 用信息池里的最新数据刷新价格/状态（商品被清理时保留快照）
    pool = {}
    for it in _classified_items():
        iid = it.get('item_id')
        if iid:
            pool[iid] = it
    out = []
    now = time.time()
    for iid, snap in favs.items():
        rec = dict(snap)
        it = pool.get(iid)
        if it:
            for f in ('title', 'price', 'days_ago', 'dist', 'city', 'seller',
                      'seller_type', 'pic_url', 'url', 'keyword', 'published_at'):
                if it.get(f) not in (None, ''):
                    rec[f] = it[f]
        # 收藏快照也会随日期老化，按发布时间重新计算
        pub = rec.get('published_at')
        if pub:
            try:
                rec['days_ago'] = max(0, int((now - float(pub)) // 86400))
            except (TypeError, ValueError):
                pass
        refresh_age(rec, now)
        out.append(rec)
    out.sort(key=lambda x: x.get('added_at', 0), reverse=True)
    return jsonify({'favorites': out, 'total': len(out)})


@app.route('/api/favorites', methods=['POST'])
def api_favorites_post():
    data = request.get_json(silent=True) or {}
    item_id = str(data.get('item_id') or '').strip()
    action = data.get('action') or 'add'
    if not item_id:
        return jsonify({'ok': False, 'msg': '缺少商品 ID'}), 400

    if action not in ('add', 'remove'):
        return jsonify({'ok': False, 'msg': '未知操作'}), 400

    def _mutate(favs):
        if action == 'add':
            src = next((i for i in storage.get_items() if i.get('item_id') == item_id), None)
            if src is None:
                src = _clean_item_snapshot(data.get('item'))
                if not src:
                    return None
            rec = dict(src)
            rec['item_id'] = item_id
            rec['added_at'] = time.time()
            favs[item_id] = rec
        else:
            favs.pop(item_id, None)
        return len(favs)

    count = storage.update_favorites(_mutate)
    if count is None:
        return jsonify({'ok': False, 'msg': '商品不存在，且未提供有效快照'}), 400
    msg = '已收藏' if action == 'add' else '已取消收藏'
    return jsonify({'ok': True, 'msg': msg, 'count': count})


@app.route('/api/schedules', methods=['GET'])
def api_schedules():
    scheds = storage.load_schedules()
    now = time.time()
    out = []
    for s in scheds:
        interval = max(1, int(s.get('interval') or 30)) * 60
        last = s.get('last_run') or 0
        item = dict(s)
        item['next_in'] = max(0, int(interval - (now - last))) if s.get('enabled', True) else None
        out.append(item)
    return jsonify({'schedules': out})


@app.route('/api/schedules', methods=['POST'])
def api_schedules_post():
    data = request.get_json(silent=True) or {}
    action = data.get('action') or 'add'
    if action not in ('add', 'toggle', 'delete'):
        return jsonify({'ok': False, 'msg': '未知操作'}), 400
    keyword = (data.get('keyword') or '').strip()
    if action == 'add' and not keyword:
        return jsonify({'ok': False, 'msg': '请填写商品名'}), 400
    sched_days = _parse_days(data.get('days', 1))
    sched_interval = _parse_int(data.get('interval', 30), None, 5, 1440)
    if action == 'add' and (sched_days is None or sched_interval is None):
        return jsonify({'ok': False, 'msg': 'days / interval 参数不合法'}), 400

    result = {'ok': True, 'msg': '已保存'}

    def _mutate(scheds):
        if action == 'add':
            if len(scheds) >= 30:
                result.update(ok=False, msg='定时任务最多 30 个')
                return
            scheds.append({
                'id': uuid.uuid4().hex[:8],
                'keyword': keyword,
                'city': (data.get('city') or '').strip(),
                'days': sched_days,
                'interval': sched_interval,
                'enabled': True,
                'last_run': 0,
                'created_at': time.time(),
            })
            result['msg'] = '已添加定时任务'
        elif action == 'toggle':
            result['msg'] = '已切换'
            for s in scheds:
                if s.get('id') == data.get('id'):
                    s['enabled'] = not s.get('enabled', True)
                    if s['enabled']:
                        s['last_run'] = 0   # 重新开启后尽快跑一次
                    result['msg'] = '已启用' if s['enabled'] else '已暂停'
                    break
        else:  # delete
            scheds[:] = [s for s in scheds if s.get('id') != data.get('id')]
            result['msg'] = '已删除'

    storage.update_schedules(_mutate)
    code = 200 if result['ok'] else 400
    return jsonify(result), code


@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'GET':
        return jsonify(storage.load_settings())
    data = request.get_json(silent=True) or {}
    changes = {}
    for field, lo, hi in (('keep_days', 1, 3650), ('max_items', 100, 1000000)):
        if field in data:
            value = _parse_int(data[field], None, lo, hi)
            if value is None:
                return jsonify({'ok': False, 'msg': field + ' 参数不合法'}), 400
            changes[field] = value
    def update(settings):
        settings.update(changes)
        settings.setdefault('keep_days', 30)
        settings.setdefault('max_items', 20000)
        return dict(settings)
    saved = storage.update_settings(update)
    return jsonify({'ok': True, 'settings': saved, 'msg': '已保存'})



@app.route('/api/stop', methods=['POST'])
def api_stop():
    with state_lock:
        if not state['running']:
            return jsonify({'ok': False, 'msg': '当前没有采集任务'}), 400
        control['cancel'] = True
        control['paused'] = False
        state['paused'] = False
    return jsonify({'ok': True, 'msg': '正在停止采集'})


@app.route('/api/login', methods=['POST'])
def api_login():
    with state_lock:
        if state['logging_in']:
            return jsonify({'ok': False, 'msg': '登录窗口已在打开中'}), 400
        if state['running']:
            return jsonify({'ok': False, 'msg': '正在采集中，请先停止再登录'}), 400
        state['logging_in'] = True
        state['phase'] = 'waiting_login'
        state['last_error'] = None

    def worker():
        # 登录与采集各自持有独立浏览器实例，避免共享对象被不同线程覆盖
        worker_scraper = Scraper(headless=False, channel='msedge')
        try:
            if not worker_scraper.login():
                raise RuntimeError('登录未完成，请重新打开登录窗口')
            with state_lock:
                state['last_error'] = None
        except Exception as e:
            with state_lock:
                state['last_error'] = '登录未完成（%s）' % type(e).__name__
        finally:
            try:
                worker_scraper.close()
            except Exception:
                pass
            with state_lock:
                state['logging_in'] = False
                state['phase'] = 'idle'

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        with state_lock:
            state['logging_in'] = False
        return jsonify({'ok': False, 'msg': '无法启动登录线程'}), 500
    return jsonify({'ok': True, 'msg': '正在检查已保存账号，只有登录失效时才需要扫码'})


def _ensure_classified():
    """启动时用当前识别规则重算一遍卖家标签，保证规则更新后立即生效。"""
    def _migrate(items):
        if not items:
            return 0
        items[:] = seller_filter.classify_items([refresh_age(i) for i in items], storage.load_marks())
        return len(items)

    n = storage.update_items(_migrate)
    if n:
        print('已按当前识别规则重算 %d 条商品' % n, flush=True)


if __name__ == '__main__':
    import webbrowser
    os.makedirs(storage.DATA_DIR, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(storage.DATA_DIR, 'stability.log'),
                                  maxBytes=1024 * 1024, backupCount=2, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format='%(asctime)s %(levelname)s %(message)s')
    _ensure_classified()
    settings = storage.load_settings()
    removed = storage.prune_items(settings.get('keep_days', 30), settings.get('max_items', 20000))
    if removed:
        print('启动清理：删除过期商品 %d 条' % removed)
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print('闲鱼同城助手后端启动：http://localhost:8000')
    # 启动后 1.5 秒自动用默认浏览器打开页面
    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:8000')).start()
    app.run(host='127.0.0.1', port=8000, debug=False, threaded=True)
