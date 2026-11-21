# -*- coding: utf-8 -*-
"""
二道贩子识别 —— 基于多信号加权打分（总分 100）。

硬规则：
  · 同一卖家出现 ≥2 件同款商品 → 直接判定为二道贩子（100 分），不看其它信号。

信号与权重：
  1. 同款商品（标题相似聚类）            直接判定（≥2 件）
  2. 昵称含商家词                        20
  3. 标题批量话术（量大/批发/现货…）      15
  4. 池内在售条数                         15
  5. 好评率异常（评价数≥200 且好评率<80%） 10
  6. 覆盖区数（≥3 个区）                   5
  7. 上新密度（1 天内 ≥3 条）              5

判定：>=70 二道贩子 / 45-69 待判定 / <45 个人卖家。
人工标记（data/blacklist.json）优先级最高，覆盖算法结果。
"""
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from item_time import published_at, in_window


def age_days(item, now=None):
    """商品距今天数。

    优先用 published_at 动态计算（跨天后仍然准确）；没有才退回入库时算好的 days_ago。
    """
    pub = published_at(item)
    if pub is not None:
        return max(0, int(((time.time() if now is None else now) - pub) // 86400))
    # 兼容纯规则调用；入库/查询先 refresh_age，把不可恢复的旧年龄置为未知。
    return item.get('days_ago')

SIM_THRESHOLD = 0.8        # 同款判定：Jaccard 相似度阈值（标题几乎一致）
THRESHOLD_RESELLER = 70
THRESHOLD_PENDING = 45
AUTO_RESELLER_SAME_ITEMS = 2   # 同一卖家同款/同类商品达到该件数 → 直接判为二道贩子
AUTO_RESELLER_TOTAL_ITEMS = 3  # 同一卖家挂售总件数达到该数量 → 直接判为二道贩子

# 昵称里的「商家味」关键词
NICKNAME_RESELLER = [
    '回收', '批发', '数码', '科技', '通讯', '二手店', '闲置仓', '工作室',
    '掌柜', '旗舰', '商贸', '电子', '手机店', '名品', '奢品', '专柜',
    '严选', '好物仓', '商店', '商城', '渠道', '供应商', '总仓', '优品',
]

# 标题里的「批量/营销」话术
TITLE_RESELLER = [
    '量大', '批发', '现货', '多台', '大量', '可发', '全新未拆', '价格可谈',
    '支持验机', '走量', '一件代发', '长期收', '回收', '求购', '秒发',
    '货源', '渠道', '正品保证', '假一赔', '同款', '打包', '仅剩',
    '可小刀', '不议价', '支持置换', '欢迎来撩', '全场', '清货',
]

# 品类推断（搜索结果通常不带品类，按标题关键词猜）
CATEGORY_RULES = [
    ('手机数码', ['手机', 'iphone', '华为手机', '小米', '红米', 'oppo', 'vivo', '三星', '平板', 'ipad', '耳机', '相机', '单反', '微单', 'switch', '手环', '手表']),
    ('家用电器', ['冰箱', '洗衣机', '空调', '电视', '吸尘器', '扫地', '空气炸锅', '电饭煲', '微波炉', '净化器', '热水器', '油烟机', '风扇']),
    ('家具家居', ['床', '沙发', '餐桌', '椅子', '衣柜', '书架', '柜子', '桌子', '床垫', '茶几', '梳妆']),
    ('服装鞋包', ['衣服', '鞋', '包', '外套', '羽绒服', '裤子', '裙子', '耐克', '阿迪', 'aj', 'nike', 'adidas', '围巾', '帽子']),
    ('图书文娱', ['书', '绘本', '考研', '教材', '乐高', '玩具', '游戏', 'kindle', '漫画', '手办']),
    ('母婴玩具', ['婴儿', '宝宝', '儿童', '推车', '安全座椅', '奶瓶', '餐椅', '待产', '童装']),
    ('运动户外', ['自行车', '捷安特', '哑铃', '跑步机', '健身', '瑜伽', '帐篷', '露营', '球拍', '鱼竿', '滑板', '电动车']),
    ('电脑办公', ['笔记本', '电脑', 'macbook', 'matebook', 'thinkpad', 'ideapad', '拯救者',
                  '小新', '暗影精灵', '机械革命', '雷神', '神舟', '键盘', '显示器', '鼠标',
                  '主机', '内存', '固态', '显卡', '打印机', '办公']),
]


def infer_category(title):
    t = (title or '').lower()
    for cat, words in CATEGORY_RULES:
        for w in words:
            if w in t:
                return cat
    return '其他'


# ---------------- 同款检测 ----------------
def _normalize_title(t):
    """去掉标点/表情/空白，便于比较。"""
    s = (t or '').lower()
    s = re.sub(r'[^\w一-鿿]+', '', s)
    return s[:60]


def _bigrams(s):
    if len(s) < 2:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


# 通用规格词：出现在不同商品的标题里也不能算「同款」
GENERIC_SPEC_RE = re.compile(r'^(g?ddr\d+|win\d+|i\d|r\d|[a-z]?\d+[gt]b?|\d+gb|\d+寸)$')

# 产品系列词：最长公共片段里必须出现其中之一，才认为是同一型号
SERIES_WORDS = (
    'macbook', 'matebook', 'magicbook', 'thinkpad', 'ideapad', 'legion',
    'inspiron', 'vostro', 'pavilion', 'surface', 'iphone', 'ipad', 'galaxy',
    'redmi', 'xiaomi', 'rog', 'predator', 'aspire', '拯救者', '小新', '天选',
    '暗影精灵', '游匣', '幻16', '爱玛', '台铃', '雅迪', '小牛', '九号', '绿源',
    '捷安特', '喜德盛', '美利达', '凤凰', '永久',
)


def _model_tokens(s):
    """提取型号词（必须同时含字母和数字，如 322tb / v11015isk / n95c）。

    会剔除内存/显存/系统版本这类通用规格词（8g / 512g / gddr4 / win10），
    否则「16G+512G」会让两台完全不同的笔记本看起来像同款。
    """
    toks = re.findall(r'[a-z]+[0-9][a-z0-9\-]*', s)
    return {t for t in toks if len(t) >= 4 and not GENERIC_SPEC_RE.match(t)}


def _lcs_match(a, b):
    """返回两条标题的最长公共片段（文本, 长度）。"""
    try:
        m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
        return a[m.a:m.a + m.size], m.size
    except Exception:
        return '', 0


def _lcs_len(a, b):
    return _lcs_match(a, b)[1]


def _similar(a, b):
    """判断两条标题是否是「同款」。

    四个通道任一命中即算同款（长标题里细小差异会拉低 Jaccard，需要补充判定）：
      1. 归一化后前 16 个字符完全相同（同型号不同描述）
      2. Jaccard 相似度 >= 0.6
      3. 重叠系数 >= 0.8（短标题包含关系）
      4. 共享型号词（如 322tb / v11015isk）
      5. 最长公共片段 >= 16 字且其中包含产品系列词（如「华为折叠电脑matebookfold非凡大师」）

    注意：只比通用规格词（处理器 / 内存 / win10）不算同款——不同型号的笔记本
    这些词几乎一样，会误伤正常卖家。
    """
    if not a or not b:
        return False
    if len(a) >= 16 and len(b) >= 16 and a[:16] == b[:16]:
        return True
    A, B = _bigrams(a), _bigrams(b)
    if A and B:
        inter = len(A & B)
        if inter:
            if inter / len(A | B) >= SIM_THRESHOLD:
                return True
            if inter / min(len(A), len(B)) >= 0.9:
                return True
    if _model_tokens(a) & _model_tokens(b):
        return True
    text, size = _lcs_match(a, b)
    if size >= 16 and any(w in text for w in SERIES_WORDS):
        return True
    return False


def _max_same_cluster(titles):
    """把该卖家的标题做相似聚类，返回最大同款簇的件数。"""
    clusters = []
    for t in titles:
        n = _normalize_title(t)
        if not n:
            continue
        placed = False
        for c in clusters:
            if _similar(n, c[0]):
                c.append(n)
                placed = True
                break
        if not placed:
            clusters.append([n])
    return max((len(c) for c in clusters), default=1)


# ---------------- 卖家身份 ----------------
def seller_key(item):
    """卖家的稳定标识：优先 seller_id，其次昵称。"""
    sid = (item.get('seller_id') or '').strip()
    if sid:
        return 'sid:' + sid
    name = (item.get('seller') or '').strip()
    if name and name != '未知卖家':
        return 'name:' + name
    return None


def evaluate(name, items):
    """对单个卖家打分。items 为该卖家名下商品（dict 列表）。"""
    score = 0
    signals = []
    titles = [i.get('title', '') for i in items]

    # 1. 同款商品（最强信号）
    same = _max_same_cluster(titles)

    # 硬规则 1：同一卖家出现 ≥2 件同款商品 → 直接判定
    if same >= AUTO_RESELLER_SAME_ITEMS:
        return 'reseller', 100, [
            '同款商品 ×%d（≥%d 件，直接判定）' % (same, AUTO_RESELLER_SAME_ITEMS)
        ]

    # 硬规则 2：同一卖家出现 ≥2 件同类商品（如一天挂两台电脑）→ 直接判定
    # 正常个人不会同时出两件同类物品，因此不考虑误判风险。
    cats = {}
    for t in titles:
        c = infer_category(t)
        if c and c != '其他':
            cats[c] = cats.get(c, 0) + 1
    if cats:
        cat, cnt = max(cats.items(), key=lambda kv: kv[1])
        if cnt >= AUTO_RESELLER_SAME_ITEMS:
            return 'reseller', 100, [
                '同类商品 ×%d（%s，直接判定）' % (cnt, cat)
            ]

    # 硬规则 3：同一卖家挂售 ≥3 件（任意品类）→ 直接判定
    # 正常个人清理闲置通常只挂 1–2 件，多件同时在售按贩子处理。
    if len(items) >= AUTO_RESELLER_TOTAL_ITEMS:
        return 'reseller', 100, [
            '多件挂售 ×%d（≥%d 件，直接判定）' % (len(items), AUTO_RESELLER_TOTAL_ITEMS)
        ]

    # 2. 昵称商家词（命中多个营销词时加重）
    hits = [w for w in NICKNAME_RESELLER if w in (name or '')]
    if hits:
        score += 28 if len(hits) >= 2 else 20
        signals.append('昵称含「%s」' % '、'.join(hits[:2]))

    # 3. 标题批量话术
    hit = sum(1 for t in titles if any(w in (t or '') for w in TITLE_RESELLER))
    if hit >= 3:
        score += 15
        signals.append('%d 条商品含批量话术' % hit)
    elif hit >= 1:
        score += 8
        signals.append('含营销话术')

    # 4. 池内在售条数
    n = len(items)
    if n >= 10:
        score += 15
        signals.append('在售 %d 条' % n)
    elif n >= 5:
        score += 10
        signals.append('在售 %d 条' % n)
    elif n >= 3:
        score += 5

    # 5. 好评率异常
    review = max((i.get('review_count') or 0) for i in items) if items else 0
    rates = [i.get('positive_rate') for i in items if i.get('positive_rate') is not None]
    rate = min(rates) if rates else None
    if review >= 200 and rate is not None and rate < 80:
        score += 10
        signals.append('好评率 %d%%（%d 条评价）' % (rate, review))

    # 6. 覆盖区数
    dists = {i.get('dist') for i in items if i.get('dist')}
    if len(dists) >= 3:
        score += 5
        signals.append('覆盖 %d 个区' % len(dists))

    # 7. 上新密度（用发布时间动态计算，避免采集快照过期）
    now = time.time()
    fresh = 0
    for i in items:
        d = age_days(i, now)
        if (published_at(i) is not None and in_window(i, 1, now)) or (published_at(i) is None and d is not None and d <= 1):
            fresh += 1
    if fresh >= 3:
        score += 5
        signals.append('1 天内上新 %d 条' % fresh)

    score = min(score, 100)
    if score >= THRESHOLD_RESELLER:
        return 'reseller', score, signals
    if score >= THRESHOLD_PENDING:
        return 'pending', score, signals
    return 'personal', score, signals


def classify_items(items, marks=None):
    """按卖家分组打分，返回带 seller_type / seller_score / seller_signals / seller_key 的商品列表。

    marks: {'reseller': {key: {...}}, 'personal': {key: {...}}} —— 人工标记优先。
    """
    marks = marks or {}
    mark_reseller = marks.get('reseller') or {}
    mark_personal = marks.get('personal') or {}

    groups = defaultdict(list)
    keys = {}
    for it in items:
        key = seller_key(it)
        if key is None:
            # 未知卖家：单独成组，避免被误判为「大量同款」
            key = 'anon:%s' % (it.get('item_id') or it.get('id') or it.get('title', ''))
            name = ''
        else:
            name = (it.get('seller') or '').strip()
        groups[key].append(it)
        keys[key] = name

    result = []
    for key, its in groups.items():
        name = keys.get(key, '')
        typ, score, signals = evaluate(name, its)

        # 人工标记优先；早期只按昵称标记的，也沿用到拿到 seller_id 的商品
        name_key = ('name:' + name) if name else None
        if key in mark_reseller or (name_key and name_key in mark_reseller):
            typ, score = 'reseller', 100
            signals = ['人工标记为二道贩子'] + signals
        elif key in mark_personal or (name_key and name_key in mark_personal):
            typ, score = 'personal', 0
            signals = ['人工标记为个人卖家']

        for i in its:
            i = dict(i)
            i['seller_type'] = typ
            i['seller_score'] = score
            i['seller_signals'] = signals
            i['seller_key'] = key
            if not i.get('cat'):
                i['cat'] = infer_category(i.get('title', ''))
            result.append(i)
    return result
