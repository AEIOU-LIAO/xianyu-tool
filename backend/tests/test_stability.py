"""稳定性回归：全部运行数据均指向临时目录，不联网、不启动浏览器。"""
import builtins
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage
import scraper
import item_time
import app
from browser_flow import Challenge, SearchResult, BrowserCollection, LoginRequired


class IsolatedStorage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name in ('ITEMS_FILE', 'MARKS_FILE', 'FAVORITES_FILE', 'SCHEDULES_FILE',
                     'SETTINGS_FILE', 'STATE_FILE', 'JOURNAL_FILE'):
            p = patch.object(storage, name, os.path.join(self.temp.name, name + '.json'))
            p.start()
            self.addCleanup(p.stop)


class RecoveryTests(IsolatedStorage):
    def test_backup_failure_preserves_original_and_blocks_update(self):
        path = Path(storage.ITEMS_FILE)
        path.write_text('{broken', encoding='utf-8')
        real_open = builtins.open
        def fail_backup(name, mode='r', *args, **kwargs):
            if mode == 'xb':
                raise PermissionError('fake backup denied')
            return real_open(name, mode, *args, **kwargs)
        with patch('builtins.open', side_effect=fail_backup):
            with self.assertRaises(RuntimeError):
                storage.update_items(lambda xs: xs.append({'item_id': 'new'}))
        self.assertEqual(path.read_text(), '{broken')

    def test_same_second_backups_are_unique(self):
        path = Path(storage.ITEMS_FILE)
        with patch.object(storage.time, 'time', return_value=1000):
            for value in ('{broken-one', '{broken-two'):
                path.write_text(value)
                self.assertEqual(storage.get_items(), [])
        self.assertEqual({p.read_text() for p in path.parent.glob('*.corrupt-*')},
                         {'{broken-one', '{broken-two'})

    def test_recovery_reader_cannot_move_new_writer_file(self):
        path = Path(storage.ITEMS_FILE)
        path.write_text('{broken')
        entered, resume = threading.Event(), threading.Event()
        real_backup = storage._backup_corrupt
        errors = []
        def delayed_backup(p):
            entered.set()
            if not resume.wait(3):
                raise RuntimeError('test synchronization timeout')
            return real_backup(p)
        def reader():
            try:
                storage.get_items()
            except Exception as exc:
                errors.append(exc)
        def writer():
            try:
                storage.update_items(lambda xs: xs.append({'item_id': 'new'}))
            except Exception as exc:
                errors.append(exc)
        with patch.object(storage, '_backup_corrupt', side_effect=delayed_backup):
            a = threading.Thread(target=reader)
            a.start()
            self.assertTrue(entered.wait(3))
            b = threading.Thread(target=writer)
            b.start()
            resume.set()
            a.join(3)
            b.join(3)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(storage.get_items(), [{'item_id': 'new'}])

    def test_pair_commit_recovers_after_second_write_failure(self):
        storage.save_items([{'item_id': '1'}])
        real_save = storage._save
        def fail_items(path, value):
            if path == storage.ITEMS_FILE:
                raise OSError('fake disk failure')
            return real_save(path, value)
        with patch.object(storage, '_save', side_effect=fail_items):
            with self.assertRaises(OSError):
                storage.update_marks_and_items(
                    lambda ms: ms['personal'].update({'sid:a': {}}),
                    lambda xs, ms: xs[0].update(seller_type='personal'))
        self.assertTrue(os.path.exists(storage.JOURNAL_FILE))
        self.assertIn('sid:a', storage.load_marks()['personal'])
        self.assertEqual(storage.get_items()[0]['seller_type'], 'personal')
        self.assertFalse(os.path.exists(storage.JOURNAL_FILE))

    def test_settings_updates_preserve_other_fields(self):
        storage.update_settings(lambda s: s.update(keep_days=10))
        storage.update_settings(lambda s: s.update(max_items=999))
        self.assertEqual(storage.load_settings(), {'keep_days': 10, 'max_items': 999})


class BrowserCleanupTests(unittest.TestCase):
    def test_close_always_stops_playwright(self):
        obj = scraper.Scraper()
        browser, pw = Mock(), Mock()
        browser.close.side_effect = RuntimeError('fake close failure')
        obj._browser, obj._playwright = browser, pw
        obj.close()
        pw.stop.assert_called_once()
        self.assertIsNone(obj._playwright)
        obj.close()

    def test_launch_failure_stops_driver(self):
        obj=scraper.Scraper()
        factory=Mock()
        factory.start.return_value.chromium.launch_persistent_context.side_effect=RuntimeError('fake launch error')
        with patch.object(scraper,'sync_playwright',return_value=factory):
            with self.assertRaises(RuntimeError):
                obj._launch()
        factory.start.return_value.stop.assert_called_once()
        launch=factory.start.return_value.chromium.launch_persistent_context
        self.assertTrue(launch.call_args.kwargs['chromium_sandbox'])
        self.assertEqual(launch.call_count,1)  # 不允许失败后无沙箱降级重试。

    def test_anonymous_cookie_does_not_count_as_login(self):
        obj=scraper.Scraper()
        ctx=Mock()
        ctx.cookies.return_value=[{'name':name, 'domain':'.goofish.com'} for name in ('uc1','us')]
        self.assertFalse(obj._has_login_cookie(ctx))
        ctx.cookies.return_value=[{'name':name, 'domain':'.goofish.com'} for name in ('unb','cookie2')]
        self.assertTrue(obj._has_login_cookie(ctx))


class TimeTests(unittest.TestCase):
    def test_session_restore_missing_only_and_excludes_expired_or_external_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            cookies=[{'name':'unb','domain':'.goofish.com','path':'/','value':'older','expires':-1},
                     {'name':'cookie2','domain':'.goofish.com','path':'/','value':'saved','expires':-1},
                     {'name':'old','domain':'.goofish.com','path':'/','value':'expired','expires':1},
                     {'name':'other','domain':'.example.com','path':'/','value':'ignore','expires':-1}]
            Path(directory,'browser-session.json').write_text(json.dumps({'cookies':cookies}))
            context=Mock()
            context.cookies.return_value=[dict(cookies[0],value='newer')]
            with patch.object(scraper,'DATA_DIR',directory):
                scraper.Scraper()._restore_session_cookies(context)
            context.add_cookies.assert_called_once_with([cookies[1]])

    def test_old_milliseconds_migrate(self):
        now = 1794400000
        item = {'time_str': str((now - 3 * 86400) * 1000), 'days_ago': 0}
        item_time.refresh_age(item, now)
        self.assertEqual(item['published_at'], now - 3 * 86400)
        self.assertEqual(item['days_ago'], 3)

    def test_exact_24_hour_boundary(self):
        now = 1794400000
        self.assertTrue(item_time.in_window({'published_at': now - 86399}, 1, now))
        self.assertFalse(item_time.in_window({'published_at': now - 86400}, 1, now))

    def test_unknown_does_not_stay_fresh(self):
        item = item_time.refresh_age({'days_ago': 0})
        self.assertIsNone(item['days_ago'])
        self.assertFalse(item_time.in_window(item, 1))


class APITests(IsolatedStorage):
    def setUp(self):
        super().setUp()
        app.state.update(running=False, logging_in=False, paused=False, started_at=None,
                         phase='idle', outcome=None, cooldown_until=0, progress={})
        app.control.update(cancel=False, paused=False, resume_verification=False)
        self.client = app.app.test_client()

    def delete_tag(self, **data):
        return self.client.delete('/api/items', base_url='http://localhost:8000', json=data)

    def test_scrape_accepts_12_hours_without_truncation(self):
        with patch.object(app.scraper,'is_logged_in',return_value=True), \
             patch.object(app,'_start_scrape',return_value=(True,'started')) as start:
            response=self.client.post('/api/scrape',base_url='http://localhost:8000',
                                      json={'keyword':'test','city':'深圳','days':0.5})
        self.assertEqual(response.status_code,200)
        start.assert_called_once_with('test',0.5,'深圳')

    def test_schedule_persists_12_hour_range(self):
        response=self.client.post('/api/schedules',base_url='http://localhost:8000',
                                  json={'keyword':'test','city':'深圳','days':0.5,'interval':30})
        self.assertEqual(response.status_code,200)
        self.assertEqual(storage.load_schedules()[0]['days'],0.5)

    def test_scheduler_preserves_12_hours(self):
        storage.save_schedules([{'id':'12h','enabled':True,'keyword':'test','city':'深圳',
                                 'days':0.5,'interval':30,'last_run':0}])
        with patch.object(app.scraper,'is_logged_in',return_value=True), \
             patch.object(app,'_start_scrape',return_value=(True,'started')) as start, \
             patch.object(app.time,'sleep',side_effect=StopIteration):
            with self.assertRaises(StopIteration):app._scheduler_loop()
        start.assert_called_once_with('test',0.5,'深圳')

    def test_range_parser_only_adds_half_day_not_arbitrary_fractions(self):
        for window in (1/24,2/24,3/24,0.5):
            for v in (window,str(window)):
                self.assertEqual(app._parse_days(v),window)
        for v in (True,False,1.9,0.1,float('nan'),float('inf'),'abc',-1):
            self.assertIsNone(app._parse_days(v))
        self.assertEqual(app._parse_days(1),1)

    def test_hour_windows_reach_manual_and_schedule_apis(self):
        for hours in (1,2,3):
            window=hours/24
            with patch.object(app.scraper,'is_logged_in',return_value=True), \
                 patch.object(app,'_start_scrape',return_value=(True,'started')) as start:
                response=self.client.post('/api/scrape',base_url='http://localhost:8000',
                                          json={'keyword':'test','days':window})
            self.assertEqual(response.status_code,200)
            start.assert_called_once_with('test',window,'')
            response=self.client.post('/api/schedules',base_url='http://localhost:8000',
                                      json={'keyword':'test','days':window,'interval':30})
            self.assertEqual(response.status_code,200)
            self.assertEqual(storage.load_schedules()[-1]['days'],window)

    def test_delete_city_removes_pool_and_related_favorites_preserves_marks(self):
        storage.save_items([{'item_id':'1','city':'深圳'}, {'item_id':'2','city':'深圳'},
                            {'item_id':'3','city':'广州'}])
        storage.save_favorites({'1':{'city':'旧城市'}, 'old':{'city':'深圳'}, '3':{'city':'广州'}})
        marks = {'personal':{'seller':{}}, 'reseller':{}}
        storage.save_marks(marks)
        response = self.delete_tag(kind='city',value='深圳')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'ok':True,'deleted':2,'deleted_favorites':2,'total':1})
        self.assertEqual([i['item_id'] for i in storage.get_items()], ['3'])
        self.assertEqual(list(storage.load_favorites()), ['3'])
        self.assertEqual(storage.load_marks(), marks)

    def test_delete_keyword_across_cities_and_no_match_is_idempotent(self):
        storage.save_items([{'keyword':'笔记本','city':'深圳'}, {'keyword':'笔记本','city':'广州'},
                            {'keyword':'手机','city':'深圳'}])
        self.assertEqual(self.delete_tag(kind='keyword',value='笔记本').json['deleted'], 2)
        self.assertEqual(self.delete_tag(kind='keyword',value='笔记本').json['deleted'], 0)
        self.assertEqual(storage.get_items(), [{'keyword':'手机','city':'深圳'}])

    def test_delete_district_scoped_to_city_matches_filter_title_fallback(self):
        storage.save_items([{'city':'深圳','dist':'南山区'}, {'city':'深圳','title':'南山自提'},
                            {'city':'广州','dist':'南山区'}, {'city':'深圳','dist':'福田区'}])
        response = self.delete_tag(kind='dist',value='南山区',city='深圳')
        self.assertEqual(response.json['deleted'], 2)
        self.assertEqual(len(storage.get_items()), 2)

    def test_delete_invalid_scope_running_and_bad_origin_never_write(self):
        original = [{'city':'深圳'}]
        storage.save_items(original)
        for data in ({}, {'kind':'city','value':''}, {'kind':[],'value':'深圳'},
                     {'kind':'price','value':'100'}, {'kind':'city','value':True},
                     {'kind':'city','value':'深圳','city':'广州'}):
            self.assertEqual(self.delete_tag(**data).status_code,400)
        response = self.client.delete('/api/items',base_url='http://localhost:8000',
                                     headers={'Origin':'https://evil.example'},json={'kind':'city','value':'深圳'})
        self.assertEqual(response.status_code,403)
        app.state['running'] = True
        self.assertEqual(self.delete_tag(kind='city',value='深圳').status_code,409)
        self.assertEqual(storage.get_items(), original)

    def test_delete_partial_save_recovers_pool_and_favorites_together(self):
        storage.save_items([{'item_id':'1','city':'深圳'}])
        storage.save_favorites({'1':{'city':'深圳'}})
        save = storage._save
        def fail_favorites(path, data):
            if path == storage.FAVORITES_FILE:
                raise OSError('fake disk failure')
            return save(path,data)
        with patch.object(storage,'_save',side_effect=fail_favorites):
            self.assertEqual(self.delete_tag(kind='city',value='深圳').status_code,500)
        self.assertTrue(os.path.exists(storage.JOURNAL_FILE))
        # 任意相关文件的读取先恢复完整删除事务，不暴露两份不同步数据。
        self.assertEqual(storage.load_favorites(), {})
        self.assertEqual(storage.get_items(), [])
        self.assertFalse(os.path.exists(storage.JOURNAL_FILE))

    def test_unknown_host_get_and_post_rejected(self):
        for method in (self.client.get, self.client.post):
            self.assertEqual(method('/api/items', base_url='http://evil.example:8000').status_code, 403)

    def test_bad_origin_rejected_before_write(self):
        r = self.client.post('/api/settings', base_url='http://localhost:8000',
                             headers={'Origin': 'https://evil.example'}, json={'keep_days': 1})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(os.path.exists(storage.SETTINGS_FILE))

    def test_invalid_body_types_and_numbers_rejected(self):
        for data in ([1], {'keyword': 1}, {'keyword': 'test', 'days': 'abc'},
                     {'keyword': 'test', 'days': 1.9}, {'keyword': 'test', 'days': True}):
            response = self.client.post('/api/scrape', base_url='http://localhost:8000', json=data)
            self.assertEqual(response.status_code, 400, data)

    def test_cooldown_blocks_start_and_survives_memory_reset(self):
        until = time.time() + 1800
        storage.update_state(lambda s: s.update(cooldown_until=until))
        app.state['cooldown_until'] = 0
        with patch.object(app.threading, 'Thread') as thread:
            self.assertFalse(app._start_scrape('test', 1, '深圳')[0])
            thread.assert_not_called()
        with patch.object(app.scraper, 'is_logged_in', return_value=True):
            r = self.client.post('/api/scrape', base_url='http://localhost:8000', json={'keyword':'test','days':1})
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers['Retry-After']), 1700)

    def test_thread_start_failure_restores_idle(self):
        with patch.object(app.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('fake thread failure')
            self.assertFalse(app._start_scrape('test', 1, '')[0])
        self.assertFalse(app.state['running'])
        self.assertEqual(app.state['phase'], 'idle')

    def test_login_and_scrape_mutually_exclusive(self):
        app.state['logging_in'] = True
        self.assertFalse(app._start_scrape('test', 1, '')[0])
        app.state.update(logging_in=False, running=True)
        r = self.client.post('/api/login', base_url='http://localhost:8000')
        self.assertEqual(r.status_code, 400)

    def test_verification_resume_uses_existing_button(self):
        app.state.update(running=True, phase='waiting_verification')
        r = self.client.post('/api/pause', base_url='http://localhost:8000')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(app._resume_requested())
        self.assertFalse(app._resume_requested())

    def test_legacy_query_and_favorite_age(self):
        old = {'item_id':'1','title':'old','seller':'a','time_str':str(int((time.time()-3*86400)*1000)), 'days_ago':0}
        storage.save_items([old])
        storage.save_favorites({'1':dict(old, added_at=time.time())})
        self.assertEqual(app._query_items({'days_max':'0'})[0], [])
        r = self.client.get('/api/favorites', base_url='http://localhost:8000')
        self.assertEqual(r.json['favorites'][0]['days_ago'], 3)

    def test_live_classification_preserves_manual_mark(self):
        storage.save_items([{'item_id':str(i), 'seller_id':'a', 'title':'test', 'seller':'test'} for i in range(3)])
        storage.save_marks({'personal':{'sid:a':{}},'reseller':{}})
        self.assertTrue(all(i['seller_type']=='personal' for i in app._classified_items()))

    def test_failed_scrape_does_not_rewrite_existing_pool(self):
        storage.save_items([{'item_id':'old','title':'old','created_at':1}])
        before = Path(storage.ITEMS_FILE).read_bytes()
        worker = Mock(outcome='failed', cooldown_required=True, partial_error='verification timeout')
        worker.scrape.return_value = []
        with patch.object(app,'Scraper',return_value=worker):
            app._run_scrape('test',1,'深圳')
        self.assertEqual(Path(storage.ITEMS_FILE).read_bytes(),before)
        self.assertGreater(storage.get_state()['cooldown_until'],time.time())
        self.assertFalse(app.state['running'])
        self.assertEqual(app.state['outcome'],'failed')

    def test_status_fields_and_cooldown_persist_after_restart(self):
        storage.update_state(lambda s:s.update(cooldown_until=time.time()+100))
        with patch.object(app.scraper,'is_logged_in',return_value=True):
            response=self.client.get('/api/status',base_url='http://localhost:8000')
        self.assertEqual(response.json['phase'],'cooldown')
        self.assertIn('outcome',response.json)
        self.assertIn('progress',response.json)

    def test_favorite_add_remove_and_settings_keep_existing_data(self):
        storage.save_items([{'item_id':'1','title':'sample','price':100}])
        for action in ('add','remove'):
            r=self.client.post('/api/favorites',base_url='http://localhost:8000',
                               json={'item_id':'1','action':action})
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.json['count'],1 if action=='add' else 0)
        storage.update_settings(lambda s:s.update(max_items=500))
        r=self.client.post('/api/settings',base_url='http://localhost:8000',json={'keep_days':5})
        self.assertEqual(r.json['settings'],{'keep_days':5,'max_items':500})
        self.assertEqual(storage.get_items()[0]['item_id'],'1')

    def test_mark_seller_immediately_applies_to_queries(self):
        storage.save_items([{'item_id':'1','seller_id':'a','seller':'test','title':'sample'}])
        r=self.client.post('/api/mark_seller',base_url='http://localhost:8000',
                           json={'key':'sid:a','mark':'reseller'})
        self.assertEqual(r.status_code,200)
        rows=self.client.get('/api/items?seller_type=reseller',base_url='http://localhost:8000').json['items']
        self.assertEqual([r['item_id'] for r in rows],['1'])

    def test_schedule_add_toggle_delete(self):
        base='http://localhost:8000'
        self.assertEqual(self.client.post('/api/schedules',base_url=base,
            json={'keyword':'test','city':'深圳','days':1,'interval':15}).status_code,200)
        sid=storage.load_schedules()[0]['id']
        self.client.post('/api/schedules',base_url=base,json={'action':'toggle','id':sid})
        self.assertFalse(storage.load_schedules()[0]['enabled'])
        self.client.post('/api/schedules',base_url=base,json={'action':'delete','id':sid})
        self.assertEqual(storage.load_schedules(),[])

    def test_scheduler_does_not_restore_deleted_schedule(self):
        storage.save_schedules([{'id':'a','enabled':True,'keyword':'test','interval':15,'last_run':0}])
        def start(*args):
            storage.update_schedules(lambda xs:xs.clear())
            return True,'test'
        with patch.object(app.scraper,'is_logged_in',return_value=True), \
             patch.object(app,'_start_scrape',side_effect=start), \
             patch.object(app.time,'sleep',side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                app._scheduler_loop()
        self.assertEqual(storage.load_schedules(),[])

    def test_csv_uses_same_filter_and_escapes_formula(self):
        import csv,io
        storage.save_items([{'item_id':'1','title':'=1+1','price':10,'seller':'a'},
                            {'item_id':'2','title':'other','price':200,'seller':'b'}])
        r=self.client.get('/api/export.csv?price=0-20',base_url='http://localhost:8000')
        self.assertEqual(r.status_code,200)
        rows=list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1][0],"'=1+1")


def result(item_id='1', page=1, has_next=False, city='深圳'):
    row = {'data':{'item':{'main':{'exContent':{
        'title':'测试商品', 'userNickName':'测试卖家', 'area':'南山区', 'detailParams':{'itemId':item_id}},
        'clickParam':{'args':{'price':'100', 'seller_id':'a', 'publishTime':str(int(time.time()*1000))}}}}}}
    return SearchResult({'keyword':'test','pageNumber':page,
                         'sortField':'create','sortValue':'desc',
                         'extraFilterValue':json.dumps({'divisionList':[{'city':city}]})},
                        {'ret':['SUCCESS::调用成功'], 'data':{'resultList':[row], 'resultInfo':{'hasNextPage':has_next}}})


class FakeCollector(scraper.Scraper):
    def __init__(self, results):
        super().__init__()
        self.sequence = iter(results)
        self.calls = 0
        self.closed = False
    def _launch(self):
        self._context = Mock()
        self._page = Mock()
        self._page.frames = []
        self._context.pages = [self._page]
    def _perform(self, action, keyword, city=None):
        self._check()
        self.calls += 1
        value = next(self.sequence)
        if isinstance(value, Exception):
            raise value
        return value
    def _apply_city(self, keyword, city, current):
        if not self._city_matches(current.request, city):
            raise RuntimeError('目标城市未应用')
        return current
    def _wait(self, seconds):
        self._check()
    def _save_session(self, context):
        pass
    def close(self):
        self.closed = True
        self._context = None


class CollectionTests(unittest.TestCase):
    def test_expired_login_waits_in_same_context_then_resumes(self):
        obj=FakeCollector([result(has_next=True),LoginRequired(),result(has_next=True),result('2',2)])
        obj._interactive_login=Mock(return_value=True)
        phases=[]
        data=obj.scrape('test',1,'深圳',on_phase=phases.append)
        self.assertEqual({i['item_id'] for i in data},{'1','2'})
        self.assertIn('waiting_login',phases)
        obj._interactive_login.assert_called_once()
        self.assertEqual(obj.outcome,'success')

    def test_repeated_login_expiry_does_not_loop(self):
        obj=FakeCollector([LoginRequired(),LoginRequired()])
        obj._interactive_login=Mock(return_value=True)
        self.assertEqual(obj.scrape('test',1,'深圳'),[])
        obj._interactive_login.assert_called_once()
        self.assertEqual(obj.calls,2)
        self.assertEqual(obj.outcome,'failed')

    def test_login_timeout_preserves_partial_results(self):
        obj=FakeCollector([result(has_next=True),LoginRequired()])
        obj._interactive_login=Mock(return_value=False)
        self.assertEqual(len(obj.scrape('test',1,'深圳')),1)
        self.assertEqual(obj.outcome,'partial')
        self.assertIn('等待登录',obj.last_error)

    def test_cancel_during_login_preserves_results_and_releases(self):
        from browser_flow import Stopped
        obj=FakeCollector([result(has_next=True),LoginRequired()])
        obj._interactive_login=Mock(side_effect=Stopped('cancelled'))
        self.assertEqual(len(obj.scrape('test',1,'深圳')),1)
        self.assertEqual(obj.outcome,'cancelled')
        self.assertTrue(obj.closed)

    def test_12_hour_collection_window_and_early_exit(self):
        now=time.time()
        obj=FakeCollector([self.old_result(1,now-43100),self.old_result(2,now-44000),
                           self.old_result(3,now-45000)])
        self.assertEqual(len(obj.scrape('test',0.5,'深圳')),1)
        self.assertEqual(obj.calls,3)
        self.assertIn('连续2页',obj.partial_error)
        self.assertTrue(item_time.in_window({'published_at':now-43199},0.5,now))
        self.assertFalse(item_time.in_window({'published_at':now-43200},0.5,now))
        self.assertTrue(item_time.in_window({'published_at':now-43200},1,now))

    def test_1_2_3_hour_exact_boundaries_and_collection(self):
        now=time.time()
        for hours in (1,2,3):
            window=hours/24
            seconds=hours*3600
            self.assertTrue(item_time.in_window({'published_at':now-seconds+1},window,now))
            self.assertFalse(item_time.in_window({'published_at':now-seconds},window,now))
            obj=FakeCollector([self.old_result(1,now-seconds+100),
                               self.old_result(2,now-seconds-100),self.old_result(3,now-seconds-200)])
            self.assertEqual(len(obj.scrape('test',window,'深圳')),1)
            self.assertEqual(obj.calls,3)
            self.assertIn('连续2页',obj.partial_error)
    def test_recent_window_continues_beyond_old_page_caps(self):
        pages = [result(str(n), n, n < 45) for n in range(1, 46)]
        # 未知时间不能成为提前停止依据，也不能恢复为固定8页上限。
        for page in pages[:8]:
            page.body['data']['resultList'][0]['data']['item']['main']['clickParam']['args']['publishTime'] = ''
        obj = FakeCollector(pages)
        progress = []
        data = obj.scrape('test', 1, '深圳', on_progress=lambda *p: progress.append(p))
        self.assertEqual(obj.calls, 45)
        self.assertEqual(len(data), 37)
        self.assertEqual(progress[-1], (45, 0, 37))
        self.assertEqual(obj.outcome, 'success')

    def test_batch_rest_at_20_and_40_pages_keeps_minimum_intervals(self):
        obj = FakeCollector([result(str(n),n,n<41) for n in range(1,42)])
        waits, phases = [], []
        obj._wait = waits.append
        with patch('browser_flow.random.uniform', side_effect=lambda a,b:a):
            self.assertEqual(len(obj.scrape('test',1,'深圳',on_phase=phases.append)),41)
        self.assertEqual(len(waits),40)
        self.assertEqual([i+1 for i,v in enumerate(waits) if v==10],[20,40])
        self.assertTrue(all(v in (5,10) for v in waits))
        self.assertEqual(phases.count('resting'),2)

    def test_final_page_20_never_rests_or_requests_page_21(self):
        obj = FakeCollector([result(str(n),n,n<20) for n in range(1,21)])
        phases=[]
        obj.scrape('test',1,'深圳',on_phase=phases.append)
        self.assertEqual(obj.calls,20)
        self.assertNotIn('resting',phases)

    def test_batch_rest_cancel_or_timeout_keeps_all_20_pages(self):
        from browser_flow import Stopped
        for outcome in ('cancelled','timeout'):
            obj=FakeCollector([result(str(n),n,True) for n in range(1,21)])
            def wait(seconds):
                if seconds >= 10:
                    raise Stopped(outcome)
            obj._wait=wait
            self.assertEqual(len(obj.scrape('test',1,'深圳')),20)
            self.assertEqual(obj.calls,20)
            self.assertEqual(obj.outcome,outcome)
            self.assertTrue(obj.closed)

    def test_real_wait_observes_cancellation_during_batch_rest(self):
        from browser_flow import Stopped
        obj=scraper.Scraper()
        obj._deadline=time.monotonic()+100
        cancelled=[False]
        obj._cancel=lambda:cancelled[0]
        obj._pause=None
        obj._phase_callback=None
        obj._page=Mock()
        obj._page.wait_for_timeout.side_effect=lambda ms:cancelled.__setitem__(0,True)
        with self.assertRaises(Stopped) as caught:
            obj._page_interval(20)
        self.assertEqual(caught.exception.outcome,'cancelled')
        obj._page.wait_for_timeout.assert_called_once_with(200)

    @staticmethod
    def old_result(page, stamp):
        value=result(str(page),page,True)
        value.body['data']['resultList'][0]['data']['item']['main']['clickParam']['args']['publishTime']=str(int(stamp*1000))
        return value

    def test_two_complete_old_pages_stop_before_next_page(self):
        now=time.time()
        obj=FakeCollector([result(has_next=True),self.old_result(2,now-100000),
                           self.old_result(3,now-110000)])
        data=obj.scrape('test',1,'深圳')
        self.assertEqual(len(data),1)
        self.assertEqual(obj.calls,3)
        self.assertEqual(obj.outcome,'partial')
        self.assertIn('连续2页',obj.partial_error)

    def test_recent_page_breaks_old_page_streak(self):
        now=time.time()
        obj=FakeCollector([self.old_result(1,now-100000),result('2',2,True),
                           self.old_result(3,now-110000),result('4',4)])
        self.assertEqual(len(obj.scrape('test',1,'深圳')),2)
        self.assertEqual(obj.calls,4)
        self.assertEqual(obj.outcome,'success')

    def test_unknown_incomplete_or_unsorted_pages_are_not_time_boundaries(self):
        boundary=BrowserCollection._old_page_boundary
        now=time.time()
        old=now-100000
        self.assertEqual(boundary([{},{}],[{'published_at':old}],1,now,1,old),(0,None))
        self.assertEqual(boundary([{}],[{'published_at':None}],1,now,1,old),(0,None))
        self.assertEqual(boundary([{},{}],[{'published_at':old-1},{'published_at':old}],1,now,1,old),(0,None))
        # 跨页倒序不成立时重开计数，不把两页相加。
        self.assertEqual(boundary([{}],[{'published_at':old}],1,now,1,old-10),(1,old))
        self.assertEqual(boundary([{}],[{'published_at':old}],0,now,1,old),(0,None))
        self.assertEqual(boundary([{}],[{'published_at':now-86400}],1,now,0,None),(1,now-86400))

    def test_query_drift_stops_immediately_without_retry(self):
        for field,value in [('keyword','other'),('extraFilterValue','{}')]:
            obj=scraper.Scraper()
            obj.phase='collecting'
            response=result()
            response.request[field]=value
            obj._results=[response]
            with self.assertRaises(RuntimeError):
                obj._take_result('test','深圳')
        # 应用城市期间允许忽略尚未带上城市条件的过渡响应。
        obj.phase='applying_filters'
        self.assertIsNone(obj._take_result('test','深圳'))

    def test_page_keyword_drift_keeps_preceding_items(self):
        wrong=result('2',2)
        wrong.request['keyword']='other'
        obj=FakeCollector([result(has_next=True),wrong])
        self.assertEqual(len(obj.scrape('test',1,'深圳')),1)
        self.assertEqual(obj.calls,2)
        self.assertEqual(obj.outcome,'partial')

    def test_empty_page_with_has_next_stops_without_more_requests(self):
        empty=result(has_next=True)
        empty.body['data']['resultList']=[]
        obj=FakeCollector([empty])
        self.assertEqual(obj.scrape('test',1,'深圳'),[])
        self.assertEqual(obj.calls,1)
        self.assertEqual(obj.outcome,'partial')

    def test_sort_lost_on_next_page_stops_before_accepting_items(self):
        wrong = result('2', 2)
        wrong.request['sortField'] = ''
        obj = FakeCollector([result(has_next=True), wrong])
        data = obj.scrape('test', 1, '深圳')
        self.assertEqual([i['item_id'] for i in data], ['1'])
        self.assertEqual(obj.outcome, 'partial')
        self.assertIn('条件丢失', obj.partial_error)

    def test_sort_not_applied_fails_without_accepting_comprehensive_results(self):
        wrong = result()
        wrong.request['sortField'] = ''
        obj = FakeCollector([wrong])
        obj._response = Mock(return_value=wrong)
        self.assertEqual(obj.scrape('test', 1, '深圳'), [])
        self.assertEqual(obj.outcome, 'failed')
        self.assertIn('排序未生效', obj.last_error)
        self.assertEqual(obj.calls, 1)

    def test_no_cap_still_saves_partial_results_on_cancel_or_timeout(self):
        for timeout in (False, True):
            obj = FakeCollector([result(has_next=True)])
            def interrupt(seconds):
                if timeout:
                    obj._deadline = 0
                else:
                    obj._cancel = lambda: True
                obj._check()
            obj._wait = interrupt
            self.assertEqual(len(obj.scrape('test', 1, '深圳')), 1)
            self.assertEqual(obj.calls, 1)
            self.assertEqual(obj.outcome, 'timeout' if timeout else 'cancelled')

    def test_verification_then_resume_keeps_items_without_duplicates(self):
        obj = FakeCollector([result(has_next=True), Challenge(), result(has_next=True), result('2',2)])
        phases = []
        data = obj.scrape('test',1,'深圳',should_resume=lambda:True,on_phase=phases.append,max_pages=3)
        self.assertEqual({i['item_id'] for i in data}, {'1','2'})
        self.assertIn('waiting_verification', phases)
        self.assertFalse(obj.cooldown_required)
        self.assertEqual(obj.outcome, 'success')
        self.assertTrue(obj.closed)

    def test_second_challenge_stops_and_cools(self):
        obj = FakeCollector([result(has_next=True), Challenge(), result(has_next=True), Challenge()])
        data = obj.scrape('test',1,'深圳',should_resume=lambda:True,max_pages=3)
        self.assertEqual(len(data), 1)
        self.assertEqual(obj.calls, 4)
        self.assertTrue(obj.cooldown_required)
        self.assertEqual(obj.outcome, 'partial')

    def test_verification_timeout_does_not_retry(self):
        obj = FakeCollector([Challenge()])
        obj.VERIFY_TIMEOUT = 0
        self.assertEqual(obj.scrape('test',1,'深圳',max_pages=3), [])
        self.assertEqual(obj.calls, 1)
        self.assertTrue(obj.cooldown_required)

    def test_rate_limit_stops_without_waiting_for_verification(self):
        obj = FakeCollector([Challenge(rate_limited=True)])
        phases = []
        obj.scrape('test',1,'深圳',on_phase=phases.append,max_pages=3)
        self.assertNotIn('waiting_verification',phases)
        self.assertTrue(obj.cooldown_required)

    def test_cancel_and_deadline_do_not_request_more_pages(self):
        for timeout in (False, True):
            obj = FakeCollector([result()])
            if timeout:
                obj.TASK_TIMEOUT = 0
            obj.scrape('test',1,'深圳',should_cancel=lambda:not timeout,max_pages=3)
            self.assertEqual(obj.calls, 0)
            self.assertEqual(obj.outcome, 'timeout' if timeout else 'cancelled')
            self.assertTrue(obj.closed)

    def test_wrong_city_never_enters_pool(self):
        obj = FakeCollector([result(city='广州')])
        self.assertEqual(obj.scrape('test',1,'深圳',max_pages=3), [])
        self.assertEqual(obj.outcome, 'failed')

    def test_normal_empty_result_does_not_trigger_parameter_fallback(self):
        empty = result()
        empty.body['data']['resultList'] = []
        obj = FakeCollector([empty])
        self.assertEqual(obj.scrape('test',1,'深圳',max_pages=3), [])
        self.assertEqual(obj.calls, 1)
        self.assertEqual(obj.outcome, 'success')

    def test_unknown_has_next_at_cap_is_partial(self):
        page = result()
        page.body['data']['resultInfo'] = {}
        obj = FakeCollector([page])
        obj.scrape('test',1,'深圳',max_pages=1)
        self.assertEqual(obj.outcome, 'partial')
        self.assertIn('上限',obj.partial_error)

    def test_network_retry_once_and_never_retries_challenge(self):
        obj = scraper.Scraper()
        obj._check = Mock()
        obj._wait = Mock()
        obj._results = []
        obj._response = Mock(side_effect=[TimeoutError(), result()])
        action = Mock()
        obj._perform(action, 'test')
        self.assertEqual(action.call_count, 2)
        obj._wait.assert_called_once_with(10)
        obj._response = Mock(side_effect=Challenge())
        action.reset_mock()
        with self.assertRaises(Challenge):
            obj._perform(action,'test')
        self.assertEqual(action.call_count, 1)

    def test_automatic_valid_response_resumes_without_extra_navigation(self):
        obj = FakeCollector([Challenge()])
        def dispatch(ms):
            obj._results.append(result())
        original_launch=obj._launch
        def launch():
            original_launch()
            obj._page.wait_for_timeout.side_effect=dispatch
        obj._launch=launch
        data=obj.scrape('test',1,'深圳',max_pages=3)
        self.assertEqual(len(data),1)
        self.assertEqual(obj.calls,1)
        self.assertEqual(obj.outcome,'success')
        self.assertFalse(obj.cooldown_required)

    def test_signed_requests_are_not_used_by_collection(self):
        obj=FakeCollector([result()])
        obj.scrape('test',1,'深圳',max_pages=3)
        obj._page.request.post.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
