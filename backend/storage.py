# -*- coding: utf-8 -*-
"""本地信息池存储（JSON 文件）。

并发约定：
- 所有「读 → 改 → 写」必须走 `update_items / update_favorites / update_schedules /
  update_marks`，它们在同一把 RLock 内完成整个事务，避免并发丢更新。
- 临时文件使用唯一名字，防止两个写入共用 `xxx.tmp` 互相踩踏。
- 文件损坏时先备份为 `*.corrupt-<时间戳>` 再返回默认值，绝不让后续保存覆盖原内容；
  权限等其他读取错误直接抛出，避免静默丢数据。
"""
import json
import os
import threading
import time
import uuid
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('XIANYU_DATA_DIR') or os.path.join(BASE_DIR, 'data')
ITEMS_FILE = os.path.join(DATA_DIR, 'items.json')
STATE_FILE = os.path.join(DATA_DIR, 'state.json')
MARKS_FILE = os.path.join(DATA_DIR, 'blacklist.json')
SCHEDULES_FILE = os.path.join(DATA_DIR, 'schedules.json')
FAVORITES_FILE = os.path.join(DATA_DIR, 'favorites.json')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
JOURNAL_FILE = os.path.join(DATA_DIR, 'marks_items.pending.json')

# 进程内事务锁（可重入，便于嵌套调用）
_WRITE_LOCK = threading.RLock()


def _backup_corrupt(path):
    """把损坏的文件改名保留，返回备份路径。"""
    backup = '%s.corrupt-%s' % (path, uuid.uuid4().hex)
    try:
        with open(path, 'rb') as source, open(backup, 'xb') as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.remove(path)
        print('[storage] 损坏数据已保留备份', flush=True)
    except OSError as e:
        raise RuntimeError('损坏数据备份失败，已停止写入') from e
    return backup


def _load(path, default):
    with _WRITE_LOCK:
        if path in (ITEMS_FILE, MARKS_FILE, FAVORITES_FILE):
            _recover_pair()
        return _read(path, default)


def _read(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            value = json.load(f)
            if not isinstance(value, type(default)):
                raise ValueError('数据结构错误')
            return value
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError):
        _backup_corrupt(path)
        return default
    except OSError as e:
        # 权限等错误不能被当成「空数据」，否则后续保存会覆盖原件
        raise RuntimeError('读取 %s 失败：%s' % (path, e))


def _save(path, data):
    with _WRITE_LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 唯一临时文件名，避免并发写入共用同一个 tmp
        tmp = '%s.%d.%d.tmp' % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def _update(path, default, fn):
    """在写锁内完成「读 → 改 → 写」。fn(数据) 就地修改，返回值作为结果透传。"""
    with _WRITE_LOCK:
        data = _load(path, default)
        result = fn(data)
        _save(path, data)
        return result


# ---------------- 商品 ----------------
def get_items():
    return _load(ITEMS_FILE, [])


def update_items(fn):
    """事务式更新信息池。fn(items) 就地修改 items，返回值透传。"""
    return _update(ITEMS_FILE, [], fn)


def save_items(items):
    _save(ITEMS_FILE, items)
    return len(items)


def prune_items(keep_days=30, max_items=20000):
    """清理过期商品：删除超过保留天数的，并限制总量。返回删除条数。

    安全约定：没有 created_at 的老数据一律视为「刚刚入库」并补时间戳，
    绝不因为缺字段而删除（曾因此误清空过整个信息池）。
    整个清理在事务锁内完成，避免与采集写入交错。
    """
    now = time.time()
    cutoff = now - keep_days * 86400

    def _prune(items):
        if not items:
            return 0
        kept = []
        for i in items:
            ts = i.get('created_at')
            if not ts:
                i['created_at'] = now
                ts = now
            if ts >= cutoff:
                kept.append(i)
        if len(kept) > max_items:
            kept.sort(key=lambda x: x.get('created_at') or 0, reverse=True)
            kept = kept[:max_items]
        removed = len(items) - len(kept)
        items[:] = kept
        return removed

    return update_items(_prune)


# ---------------- 人工标记 ----------------
def _normalize_marks(data):
    if not isinstance(data, dict):
        data = {}
    data.setdefault('reseller', {})
    data.setdefault('personal', {})
    return data


def load_marks():
    """人工标记：{'reseller': {key: {...}}, 'personal': {key: {...}}}"""
    return _normalize_marks(_load(MARKS_FILE, {}))


def update_marks(fn):
    """事务式更新人工标记。fn(marks) 就地修改，返回值透传。"""
    def _wrap(data):
        return fn(_normalize_marks(data))
    return _update(MARKS_FILE, {}, _wrap)


def save_marks(marks):
    _save(MARKS_FILE, marks)
    return marks


def update_marks_and_items(marks_fn, items_fn):
    """在同一把锁内更新「人工标记 + 商品打分」，保证两者使用一致快照。"""
    with _WRITE_LOCK:
        marks = _normalize_marks(_load(MARKS_FILE, {}))
        result = marks_fn(marks)
        items = _load(ITEMS_FILE, [])
        items_fn(items, marks)
        _save(JOURNAL_FILE, {'marks': marks, 'items': items})
        _recover_pair()
        return result


def _recover_pair():
    """在锁内重放商品、标记及可选收藏；失败留日志，相关读写先恢复。"""
    # 支持测试/导入工具独立指定 ITEMS_FILE，绝不恢复另一个数据目录的日志。
    journal = JOURNAL_FILE
    if os.path.dirname(ITEMS_FILE) != os.path.dirname(journal):
        journal = os.path.join(os.path.dirname(ITEMS_FILE), os.path.basename(journal))
    if not os.path.exists(journal):
        return
    with open(journal, 'r', encoding='utf-8') as f:
        pending = json.load(f)
    if not isinstance(pending.get('marks'), dict) or not isinstance(pending.get('items'), list):
        raise RuntimeError('标记事务日志损坏，停止读写以保护原始数据')
    if 'favorites' in pending and not isinstance(pending['favorites'], dict):
        raise RuntimeError('删除事务日志损坏，停止读写以保护原始数据')
    _save(MARKS_FILE, pending['marks'])
    _save(ITEMS_FILE, pending['items'])
    if 'favorites' in pending:
        _save(FAVORITES_FILE, pending['favorites'])
    os.remove(journal)


def delete_items_matching(predicate):
    """同一可恢复事务内删除商品和关联收藏，保留人工标记。"""
    with _WRITE_LOCK:
        items = get_items()
        favs = load_favorites()
        removed = [i for i in items if predicate(i)]
        ids = {str(i['item_id']) for i in removed if i.get('item_id') is not None}
        kept = [i for i in items if not predicate(i)]
        kept_favs = {k: v for k, v in favs.items()
                     if str(k) not in ids and not predicate(v)}
        result = {'deleted': len(removed), 'deleted_favorites': len(favs) - len(kept_favs),
                  'total': len(kept)}
        if removed or len(kept_favs) != len(favs):
            _save(JOURNAL_FILE, {'marks': load_marks(), 'items': kept, 'favorites': kept_favs})
            _recover_pair()
        return result


# ---------------- 收藏 ----------------
def load_favorites():
    """收藏夹：{item_id: 商品快照}。独立存储，不受信息池清理影响。"""
    data = _load(FAVORITES_FILE, {})
    return data if isinstance(data, dict) else {}


def update_favorites(fn):
    def _wrap(data):
        if not isinstance(data, dict):
            data = {}
        return fn(data)
    return _update(FAVORITES_FILE, {}, _wrap)


def save_favorites(favs):
    _save(FAVORITES_FILE, favs)
    return favs


# ---------------- 定时任务 ----------------
def load_schedules():
    data = _load(SCHEDULES_FILE, [])
    return data if isinstance(data, list) else []


def update_schedules(fn):
    def _wrap(data):
        if not isinstance(data, list):
            data = []
        return fn(data)
    return _update(SCHEDULES_FILE, [], _wrap)


def save_schedules(schedules):
    _save(SCHEDULES_FILE, schedules)
    return schedules


# ---------------- 设置 ----------------
def load_settings():
    data = _load(SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault('keep_days', 30)      # 数据保留天数
    data.setdefault('max_items', 20000)   # 信息池上限
    return data


def save_settings(settings):
    _save(SETTINGS_FILE, settings)
    return settings


def update_settings(fn):
    return _update(SETTINGS_FILE, {}, fn)


def update_state(fn):
    return _update(STATE_FILE, {}, fn)


# ---------------- 其他 ----------------
def get_state():
    return _load(STATE_FILE, {})


def save_state(state):
    _save(STATE_FILE, state)
