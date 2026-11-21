# -*- coding: utf-8 -*-
"""核心逻辑单元测试（不联网、不触碰真实数据）。

运行：
    python -m unittest discover -s backend/tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import seller_filter as sf          # noqa: E402
import scraper as sc                # noqa: E402
import storage                      # noqa: E402


def _item(title, **kw):
    base = {'title': title, 'dist': '宝安区', 'days_ago': 0, 'price': 100}
    base.update(kw)
    return base


class TestSimilarity(unittest.TestCase):
    def test_near_identical_titles_are_same(self):
        a = sf._normalize_title('联想拯救者Y7000 2019款笔记本，i5-9300H，16G DDR4内存')
        b = sf._normalize_title('联想拯救者Y7000 2019款笔记本 i5-9300H 16G DDR4 内存 GTX')
        self.assertTrue(sf._similar(a, b))

    def test_different_products_are_not_same(self):
        a = sf._normalize_title('戴尔Inspiron 7472笔记本 i5-8250U 8G')
        b = sf._normalize_title('联想ThinkPad E470笔记本 i5-7200U 8G')
        self.assertFalse(sf._similar(a, b))

    def test_empty_titles_are_not_same(self):
        self.assertFalse(sf._similar('', 'abc'))


class TestEvaluate(unittest.TestCase):
    def test_batch_seller_is_reseller(self):
        items = [_item('联想拯救者Y7000 2019款笔记本 i5-9300H 16G') for _ in range(3)]
        typ, score, signals = sf.evaluate('华硕电脑批发回收诚信', items)
        self.assertEqual(typ, 'reseller')
        self.assertGreaterEqual(score, 70)
        self.assertTrue(any('同款商品' in s for s in signals))

    def test_single_item_personal_is_not_reseller(self):
        typ, score, _ = sf.evaluate('我爱吃西瓜', [_item('山地自行车 九成新')])
        self.assertEqual(typ, 'personal')
        self.assertLess(score, 45)

    def test_two_same_category_auto_reseller(self):
        """用户规则：一天挂两台电脑（同类目 2 件）也算贩子，不考虑误判。"""
        items = [_item('联想小新Pro13 笔记本电脑 i5'), _item('戴尔Inspiron 7472 笔记本电脑 i7')]
        typ, score, signals = sf.evaluate('小明', items)
        self.assertEqual(typ, 'reseller')
        self.assertEqual(score, 100)
        self.assertTrue(any('同类商品' in s for s in signals))

    def test_two_different_categories_are_not_reseller(self):
        items = [_item('捷安特公路车 九成新'), _item('考研英语资料 全套')]
        typ, _, _ = sf.evaluate('小明', items)
        self.assertEqual(typ, 'personal')

    def test_three_items_any_category_is_reseller(self):
        """挂售 ≥3 件（任意品类）直接判贩子。"""
        items = [_item('捷安特公路车 九成新'), _item('考研英语资料 全套'), _item('宜家书架 白色')]
        typ, score, signals = sf.evaluate('小明', items)
        self.assertEqual(typ, 'reseller')
        self.assertTrue(any('多件挂售' in s for s in signals))

    def test_three_same_items_auto_reseller(self):
        """硬规则：同款 ≥3 件直接判贩子，与昵称/话术无关。"""
        items = [_item('宜家书架 白色 九成新') for _ in range(3)]
        typ, score, signals = sf.evaluate('小明', items)
        self.assertEqual(typ, 'reseller')
        self.assertEqual(score, 100)
        self.assertTrue(any('直接判定' in s for s in signals))

    def test_two_same_items_auto_reseller(self):
        """用户要求：2 件同款也直接判贩子。"""
        items = [_item('宜家书架 白色 九成新'), _item('宜家书架 白色 九成新')]
        typ, score, signals = sf.evaluate('小明', items)
        self.assertEqual(typ, 'reseller')
        self.assertEqual(score, 100)
        self.assertTrue(any('直接判定' in s for s in signals))

    def test_two_same_plus_shop_nickname_is_reseller(self):
        # 2 件同款 50 + 昵称商家词 20 = 70 → 判贩子
        items = [_item('宜家书架 白色 九成新'), _item('宜家书架 白色 九成新')]
        typ, score, _ = sf.evaluate('数码批发', items)
        self.assertEqual(typ, 'reseller')


class TestClassify(unittest.TestCase):
    def test_unknown_sellers_are_not_grouped(self):
        items = [dict(_item('不同商品%d' % i), item_id=str(i), seller='未知卖家') for i in range(8)]
        out = sf.classify_items(items)
        self.assertTrue(all(i['seller_type'] == 'personal' for i in out))

    def test_manual_mark_overrides_algorithm(self):
        items = [dict(_item('自行车'), item_id='1', seller='某人', seller_id='sid-A')]
        marks = {'reseller': {'sid:sid-A': {'name': '某人'}}, 'personal': {}}
        out = sf.classify_items(items, marks)
        self.assertEqual(out[0]['seller_type'], 'reseller')
        self.assertEqual(out[0]['seller_score'], 100)

    def test_nickname_mark_applies_to_seller_id(self):
        """早期按昵称标记的，在拿到 seller_id 后仍然生效。"""
        items = [dict(_item('自行车'), item_id='9', seller='老王', seller_id='sid-C')]
        marks = {'reseller': {'name:老王': {'name': '老王'}}, 'personal': {}}
        out = sf.classify_items(items, marks)
        self.assertEqual(out[0]['seller_type'], 'reseller')

    def test_manual_personal_clears_score(self):
        items = [dict(_item('批发 现货 多台'), item_id='2', seller='数码批发仓', seller_id='sid-B')]
        marks = {'reseller': {}, 'personal': {'sid:sid-B': {'name': '数码批发仓'}}}
        out = sf.classify_items(items, marks)
        self.assertEqual(out[0]['seller_type'], 'personal')
        self.assertEqual(out[0]['seller_score'], 0)


class TestPrune(unittest.TestCase):
    """回归：曾因新数据缺 created_at 导致整个信息池被清空。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_items = storage.ITEMS_FILE
        storage.ITEMS_FILE = os.path.join(self.tmp, 'items.json')

    def tearDown(self):
        storage.ITEMS_FILE = self.orig_items
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data):
        with open(storage.ITEMS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

    def _read(self):
        with open(storage.ITEMS_FILE, encoding='utf-8') as f:
            return json.load(f)

    def test_missing_created_at_is_kept(self):
        self._write([{'title': 'a'}, {'title': 'b'}])
        removed = storage.prune_items(keep_days=30)
        self.assertEqual(removed, 0)
        data = self._read()
        self.assertEqual(len(data), 2)
        self.assertTrue(all(d.get('created_at') for d in data))

    def test_old_items_are_removed(self):
        now = time.time()
        self._write([
            {'title': 'old', 'created_at': now - 40 * 86400},
            {'title': 'new', 'created_at': now},
        ])
        removed = storage.prune_items(keep_days=30)
        self.assertEqual(removed, 1)
        titles = [d['title'] for d in self._read()]
        self.assertEqual(titles, ['new'])

    def test_max_items_cap_keeps_newest(self):
        now = time.time()
        data = [{'title': 't%d' % i, 'created_at': now - i} for i in range(10)]
        self._write(data)
        removed = storage.prune_items(keep_days=30, max_items=3)
        self.assertEqual(removed, 7)
        self.assertEqual(len(self._read()), 3)


class TestDedupKey(unittest.TestCase):
    """同标题不同 item_id 的商品不能被当成同一件（否则同款统计和评分会失真）。"""

    def test_different_item_ids_are_different(self):
        a = sc._item_key({'item_id': '111', 'title': '同一标题', 'seller': '老王'})
        b = sc._item_key({'item_id': '222', 'title': '同一标题', 'seller': '老王'})
        self.assertNotEqual(a, b)

    def test_same_item_id_is_same(self):
        a = sc._item_key({'item_id': '111', 'title': 'A'})
        b = sc._item_key({'item_id': '111', 'title': 'B'})
        self.assertEqual(a, b)

    def test_fallback_when_missing_id(self):
        a = sc._item_key({'title': 'A', 'seller': '老王'})
        b = sc._item_key({'title': 'A', 'seller': '老王'})
        self.assertEqual(a, b)


class TestSearchParams(unittest.TestCase):
    """按闲鱼前端 JS 的写法构造请求：最新排序 + 服务端 publishDays 时间筛选。"""

    def test_filtered_mode_uses_newest_sort_and_server_day_filter(self):
        o = sc.Scraper.build_search_obj('笔记本', 1, '深圳', 1, 'filtered')
        self.assertTrue(o['fromFilter'])
        self.assertEqual(o['sortField'], 'create')
        self.assertEqual(o['sortValue'], 'desc')
        self.assertEqual(o['propValueStr'], {'searchFilter': 'publishDays:1;'})
        self.assertIn('深圳', o['extraFilterValue'])

    def test_unsupported_days_only_applies_sort(self):
        o = sc.Scraper.build_search_obj('x', 1, None, 15, 'filtered')
        self.assertEqual(o['propValueStr'], {})
        self.assertEqual(o['sortField'], 'create')

    def test_plain_mode_keeps_legacy_params(self):
        o = sc.Scraper.build_search_obj('x', 1, None, 1, 'plain')
        self.assertFalse(o['fromFilter'])
        self.assertEqual(o['sortField'], '')
        self.assertEqual(o['propValueStr'], {})


class TestAge(unittest.TestCase):
    """跨天后不应再显示“今天”。"""

    def test_published_at_wins(self):
        now = time.time()
        it = {'days_ago': 0, 'published_at': now - 3 * 86400}
        self.assertEqual(sf.age_days(it, now), 3)

    def test_fallback_to_days_ago(self):
        self.assertEqual(sf.age_days({'days_ago': 5}), 5)

    def test_unknown_stays_unknown(self):
        self.assertIsNone(sf.age_days({}))


class TestTransactions(unittest.TestCase):
    """回归：读改写不在同一把锁内会丢更新。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_fav = storage.FAVORITES_FILE
        self.orig_items = storage.ITEMS_FILE
        storage.FAVORITES_FILE = os.path.join(self.tmp, 'favorites.json')
        storage.ITEMS_FILE = os.path.join(self.tmp, 'items.json')

    def tearDown(self):
        storage.FAVORITES_FILE = self.orig_fav
        storage.ITEMS_FILE = self.orig_items
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_favorites_no_lost_update(self):
        def add(i):
            storage.update_favorites(
                lambda favs: favs.__setitem__('item%d' % i, {'item_id': 'item%d' % i}))

        threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(storage.load_favorites()), 20)

    def test_concurrent_items_no_lost_update(self):
        def add(i):
            storage.update_items(lambda items: items.append({'item_id': str(i)}))

        threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(storage.get_items()), 20)

    def test_corrupt_file_is_backed_up_not_overwritten(self):
        path = storage.ITEMS_FILE
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"broken":')          # 截断的 JSON
        self.assertEqual(storage.get_items(), [])
        self.assertFalse(os.path.exists(path), '损坏文件应被改名备份')
        backups = [n for n in os.listdir(self.tmp) if '.corrupt-' in n]
        self.assertEqual(len(backups), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
