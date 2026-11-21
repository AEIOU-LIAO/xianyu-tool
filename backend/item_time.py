"""所有发布窗口均按滚动秒数计算；旧毫秒时间戳可以原位迁移。"""
import datetime
import math
import time


def published_at(item):
    for value in (item.get('published_at'), item.get('time_str')):
        if value in (None, '') or isinstance(value, bool):
            continue
        try:
            stamp = float(value)
            if stamp > 1e11:
                stamp /= 1000
            if math.isfinite(stamp) and stamp >= 1e8:
                return stamp
        except (ValueError, TypeError, OverflowError):
            try:
                return datetime.datetime.fromisoformat(str(value)).timestamp()
            except (ValueError, TypeError, OSError, OverflowError):
                pass
    return None


def refresh_age(item, now=None):
    now = time.time() if now is None else now
    stamp = published_at(item)
    item['published_at'] = stamp
    item['days_ago'] = max(0, int((now - stamp) // 86400)) if stamp is not None else None
    return item


def in_window(item, days, now=None):
    stamp = published_at(item)
    now = time.time() if now is None else now
    return stamp is not None and 0 <= now - stamp < days * 86400
