# -*- coding: utf-8 -*-
"""
中文自然语言时间解析层（纯函数，无框架依赖，可直接单测）。

把「明天下午3点交周报」「每天18:30喂猫」这类文本解析成结构化提醒：
  - 单次提醒（相对时间 / 日期+时刻 / 仅时刻）
  - 循环提醒（每天 / 工作日 / 每周X / 每月X号 + 时刻）

设计约束：
  - 时间表达必须从文本开头开始匹配（引导词由本模块剥掉），
    时间片段结束后的剩余部分就是提醒内容 —— 天然支持无空格连写。
  - 输入文本建议先经过 NFKC 归一（hoshino 的 norm_text 已做），本模块对
    全角数字/冒号也做了兜底。
  - 循环提醒一律给出「下一次触发时间」，由 rmd.py 映射为 APScheduler cron。
"""

import re
import calendar
from datetime import datetime, timedelta, date

# ---------------------------------------------------------------------------
# 中文数字
# ---------------------------------------------------------------------------

_CN_DIGIT = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
             '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}

# 出现在日期/时间表达里的数字字符（阿拉伯 + 常用中文）
_NUM_RUN = r'[0-9零一二两三四五六七八九十]{1,3}'

_WEEKDAY = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}
_WEEKDAY_CLS = '一二三四五六日天'


def cn_to_int(s):
    """中文/阿拉伯数字字符串 -> int；非法返回 None。支持 0~99。"""
    if s is None or s == '':
        return None
    if s.isdigit():
        return int(s)
    total, num, seen_ten = 0, 0, False
    for ch in s:
        if ch in _CN_DIGIT:
            n = _CN_DIGIT[ch]
            if seen_ten:
                total += n          # 十X
            else:
                num = n
        elif ch == '十':
            seen_ten = True
            total += (num if num else 1) * 10
            num = 0
        else:
            return None
    return total + num


# ---------------------------------------------------------------------------
# 时刻（几点几分）
# ---------------------------------------------------------------------------

_RE_TOD = re.compile(
    r'^(凌晨|清晨|早上|上午|中午|午后|下午|傍晚|晚上|夜里|半夜)?\s*'
    r'(' + _NUM_RUN + r')\s*'
    r'(?:[点时]\s*(整|半|一刻|三刻|(' + _NUM_RUN + r')\s*分?)?'
    r'|[:：]\s*([0-9]{1,2})\s*分?)'
)


def _parse_tod(s):
    """从字符串开头解析时刻。返回 (hour, minute, consumed_len) 或 None。"""
    m = _RE_TOD.match(s)
    if not m:
        return None
    period, hour_raw, tod_min, min_raw, colon_min = m.groups()
    hour = cn_to_int(hour_raw)
    if hour is None or not 0 <= hour <= 23:
        return None
    if colon_min is not None:
        minute = int(colon_min)
    elif tod_min == '半':
        minute = 30
    elif tod_min == '一刻':
        minute = 15
    elif tod_min == '三刻':
        minute = 45
    elif min_raw is not None:
        minute = cn_to_int(min_raw)
    else:
        minute = 0
    if minute is None or not 0 <= minute <= 59:
        return None

    seg = period or ''
    if seg in ('凌晨', '半夜', '清晨', '早上', '上午'):
        if hour == 12:
            hour = 0
    elif seg == '中午':
        if hour in (1, 2):
            hour += 12
    elif seg in ('下午', '午后', '傍晚'):
        if 1 <= hour <= 11:
            hour += 12
    elif seg in ('晚上', '夜里'):
        if hour == 12:
            hour = 0
        elif 1 <= hour <= 11:
            hour += 12
    return hour, minute, m.end()


# ---------------------------------------------------------------------------
# 引导词
# ---------------------------------------------------------------------------

_RE_LEAD = re.compile(
    r'^(?:记得|麻烦你?|请你?|帮忙|帮我|给我|'
    r'[、,，。.:：\s])*'
    r'(?:提醒(?:一下|下|我|我一下)?[、,，。.:：\s]*)?'
)

_RE_CONTENT_LEAD = re.compile(
    r'^(?:记得|麻烦|帮我)*提醒(?:一下|下|我|我一下)?[、,，。.:：\s]*'
)


def _strip_lead(s):
    """剥掉开头的人称/客套词和「提醒我」引导语，返回 (剩余文本, 是否出现过引导)。"""
    rest = _RE_LEAD.sub('', s, count=1)
    return rest, rest != s


# ---------------------------------------------------------------------------
# 各模板
# ---------------------------------------------------------------------------

_RE_REL = re.compile(r'^(半|' + _NUM_RUN + r')个?(分钟|秒钟|秒|小时|天|周|个月|月|[sS])\s*(?:之|以)?后')

_RE_RECUR_WEEK = re.compile(
    r'^每(?:个)?(?:周|星期|礼拜)\s*([' + _WEEKDAY_CLS + r']'
    r'(?:[、,，\s]*(?:周|星期|礼拜)?[' + _WEEKDAY_CLS + r'])*)'
)
_RE_RECUR_MONTH = re.compile(r'^每(?:个)?月\s*(' + _NUM_RUN + r')\s*[号日]')

_RE_DATE_RELDAY = re.compile(r'^(今天|明天|后天|大后天)')
_RE_DATE_WEEK = re.compile(r'^(这|本|下)?(?:周|星期|礼拜)([' + _WEEKDAY_CLS + r'])')
_RE_DATE_MONTHDAY = re.compile(r'^(' + _NUM_RUN + r')\s*月\s*(' + _NUM_RUN + r')\s*[号日]')
_RE_DATE_DAY = re.compile(r'^(' + _NUM_RUN + r')\s*[号日]')
_RE_DATE_FULL = re.compile(r'^(\d{4})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*[号日]?')

ERR_NO_TIME = '没认出时间。示例：提醒我 明天下午3点 交周报 / 每天18:30 喂猫'
ERR_NO_CONTENT = '提醒内容呢？示例：提醒我 明天下午3点 交周报'
ERR_RECUR_NO_TIME = '循环提醒需要具体时间。示例：每天 18:30 喂猫 / 每周五 20:00 公会战'


# ---------------------------------------------------------------------------
# 日历工具
# ---------------------------------------------------------------------------

def _next_weekday_base(today, target):
    """本/下周界内的周 target（无「下」前缀）：从今天起算最近一次。"""
    days = (target - today.weekday()) % 7
    return today + timedelta(days=days)


def _next_week_monday(today):
    """下个自然周的周一。"""
    delta = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


def _add_month(d, n=1):
    month_index = d.year * 12 + (d.month - 1) + n
    y, m = divmod(month_index, 12)
    m += 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _combine(d, hour, minute):
    return datetime(d.year, d.month, d.day, hour, minute)


def _next_occurrence(kind, dow, day, hour, minute, now):
    """循环提醒的下一次触发时间（预览用）。"""
    if kind == 'daily':
        cand = _combine(now.date(), hour, minute)
        if cand <= now:
            cand += timedelta(days=1)
        return cand
    if kind == 'workday':
        cand = now.date()
        while cand.weekday() >= 5 or _combine(cand, hour, minute) <= now:
            cand += timedelta(days=1)
        return _combine(cand, hour, minute)
    if kind == 'weekly':
        cands = []
        for t in dow:
            d = _next_weekday_base(now.date(), t)
            dt = _combine(d, hour, minute)
            if dt <= now:
                dt += timedelta(days=7)
            cands.append(dt)
        return min(cands)
    if kind == 'monthly':
        d = now.date().replace(day=1)
        for _ in range(1200):
            if day <= calendar.monthrange(d.year, d.month)[1]:
                cand = _combine(date(d.year, d.month, day), hour, minute)
                if cand > now:
                    return cand
            d = _add_month(d)
        return now
    return now


def _finish_once(d, hour, minute, now, date_src):
    """组装单次提醒；已过去的时间按日期来源决定滚动或报错。"""
    dt = _combine(d, hour, minute)
    if dt > now:
        return dt, d, None

    # 已过去：显式带年份 → 报错；X月X日 → 明年；周X → +7 天；N号 → 下月；今天/仅时刻 → 明天
    if date_src == 'full':
        return None, d, '这个时间已经过去了……'
    if date_src == 'monthday':
        try:
            dt = _combine(d.replace(year=d.year + 1), hour, minute)
        except ValueError:
            ny = d.year + 1
            last = calendar.monthrange(ny, d.month)[1]
            dt = _combine(date(ny, d.month, last), hour, minute)
        if dt <= now:
            return None, d, '这个时间已经过去了……'
        return dt, dt.date(), None
    if date_src == 'week':
        dt = _combine(d + timedelta(days=7), hour, minute)
    elif date_src == 'day':
        dt = _combine(_add_month(d), hour, minute)
    else:
        dt = _combine(d + timedelta(days=1), hour, minute)
    if dt <= now:
        return None, d, '这个时间已经过去了……'
    return dt, dt.date(), None


# ---------------------------------------------------------------------------
# 模板尝试（全部锚定开头）
# ---------------------------------------------------------------------------

def _try_relative(s, now):
    """N分钟后 / 半小时后 / 3天后"""
    m = _RE_REL.match(s)
    if not m:
        return None
    val_raw, unit = m.group(1), m.group(2)
    if val_raw == '半':
        if unit != '小时':
            return None
        when = now + timedelta(minutes=30)
    else:
        v = cn_to_int(val_raw)
        if not v or v <= 0:
            return None
        if unit in ('秒', '秒钟', 's', 'S'):
            if v > 86400:
                return None
            when = now + timedelta(seconds=v)
        else:
            if v > 366:
                return None
            if unit == '小时':
                minutes = v * 60
            elif unit == '天':
                minutes = v * 60 * 24
            elif unit == '周':
                minutes = v * 60 * 24 * 7
            elif unit in ('个月', '月'):
                minutes = v * 60 * 24 * 30  # 按月近似 30 天
            else:
                minutes = v
            when = now + timedelta(minutes=minutes)
    return {'kind': 'once', 'date': when.date(),
            'hour': when.hour, 'minute': when.minute, 'dow': None, 'day': None,
            'when': when,
            'end': m.end(), 'matched': m.group(0), 'date_src': 'rel'}


def _try_recur(s, now):
    """每天 / 工作日 / 每周X / 每月N号 + 时刻"""
    if s.startswith(('每周', '每星期', '每礼拜')) and not _RE_RECUR_WEEK.match(s):
        return {'ok': False, 'error': '每周几呢？示例：每周五 20:00 公会战'}
    if s.startswith(('每月', '每个月')) and not _RE_RECUR_MONTH.match(s):
        return {'ok': False, 'error': '每月几号呢？示例：每月1号 9:00 发工资'}

    kind, dow, day, rest = None, None, None, None
    if s.startswith(('每天', '每日')):
        kind, rest = 'daily', s[2:]
    elif s.startswith('工作日'):
        kind, rest = 'workday', s[3:]
    else:
        m = _RE_RECUR_WEEK.match(s)
        if m:
            chars = re.findall(r'[' + _WEEKDAY_CLS + r']', m.group(1))
            dow = sorted({_WEEKDAY[c] for c in chars})
            kind, rest = 'weekly', s[m.end():]
        else:
            m = _RE_RECUR_MONTH.match(s)
            if m:
                day = cn_to_int(m.group(1))
                if day is None or not 1 <= day <= 31:
                    return {'ok': False, 'error': '每月几号没看懂，示例：每月1号 9:00 发工资'}
                kind, rest = 'monthly', s[m.end():]
            else:
                return None

    rest_l = rest.lstrip(' \t、,，。.:：')
    rest_l = _RE_CONTENT_LEAD.sub('', rest_l, count=1)  # 允许「每天提醒我18:30」
    tod = _parse_tod(rest_l)
    if not tod:
        return {'ok': False, 'error': ERR_RECUR_NO_TIME}
    hour, minute, tod_end = tod
    end = len(s) - len(rest_l) + tod_end
    when = _next_occurrence(kind, dow, day, hour, minute, now)
    return {'kind': kind, 'dow': dow, 'day': day, 'hour': hour, 'minute': minute,
            'date': None, 'when': when, 'end': end, 'matched': s[:end],
            'date_src': None}


def _try_datetime(s, now):
    """[日期] + 时刻，或仅时刻"""
    d, date_src, end = None, None, 0

    m = _RE_DATE_FULL.match(s)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return {'ok': False, 'error': '日期不存在，检查一下？'}
        date_src, end = 'full', m.end()
    else:
        m = _RE_DATE_MONTHDAY.match(s)
        if m:
            mo, dy = cn_to_int(m.group(1)), cn_to_int(m.group(2))
            try:
                d = date(now.year, mo, dy)
            except ValueError:
                return {'ok': False, 'error': '日期不存在，检查一下？'}
            date_src, end = 'monthday', m.end()
        else:
            m = _RE_DATE_WEEK.match(s)
            if m:
                target = _WEEKDAY[m.group(2)]
                if m.group(1) == '下':
                    d = _next_week_monday(now.date()) + timedelta(days=target)
                else:
                    d = _next_weekday_base(now.date(), target)
                date_src, end = 'week', m.end()
            else:
                m = _RE_DATE_RELDAY.match(s)
                if m:
                    offset = {'今天': 0, '明天': 1, '后天': 2, '大后天': 3}[m.group(1)]
                    d = now.date() + timedelta(days=offset)
                    date_src, end = 'relday', m.end()
                else:
                    m = _RE_DATE_DAY.match(s)
                    if m:
                        dy = cn_to_int(m.group(1))
                        try:
                            d = date(now.year, now.month, dy)
                        except ValueError:
                            return {'ok': False, 'error': '日期不存在，检查一下？'}
                        date_src, end = 'day', m.end()

    if d is not None:
        rest_l = s[end:].lstrip(' \t、,，。.:：')
        rest_l = _RE_CONTENT_LEAD.sub('', rest_l, count=1)  # 允许「明天提醒我下午3点」
        tod = _parse_tod(rest_l)
        if not tod:
            return {'ok': False, 'error': '需要具体时间。示例：明天 下午3点 开会'}
        hour, minute, tod_end = tod
        end = len(s) - len(rest_l) + tod_end
        when, d2, err = _finish_once(d, hour, minute, now, date_src)
        if err:
            return {'ok': False, 'error': err}
        return {'kind': 'once', 'date': d2, 'hour': hour, 'minute': minute,
                'dow': None, 'day': None,
                'when': when, 'end': end, 'matched': s[:end], 'date_src': date_src}

    # 无日期，仅时刻
    tod = _parse_tod(s)
    if not tod:
        return None
    hour, minute, end = tod
    when, d2, err = _finish_once(now.date(), hour, minute, now, 'today')
    if err:
        return {'ok': False, 'error': err}
    return {'kind': 'once', 'date': d2, 'hour': hour, 'minute': minute,
            'dow': None, 'day': None,
            'when': when, 'end': end, 'matched': s[:end], 'date_src': 'today'}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def parse_reminder(text, now=None, nicknames=()):
    """
    解析提醒文本。成功返回：
      { ok: True, kind: once|daily|workday|weekly|monthly,
        when: datetime（单次=触发时间，循环=下次触发）,
        hour, minute, dow, day, date, matched, content }
    失败返回 { ok: False, error: str, had_trigger: bool, stage: 'time'|'specific' }。
    had_trigger 表示文本以「提醒我」类引导词开头；
    stage='time' 表示完全没认出时间（大概率不是在设提醒），'specific' 表示
    认出了意图但表达有缺漏 —— 调用方据此决定是否回复错误提示。
    nicknames: 调用方传入 bot 的昵称列表（建议直接给 config.NICKNAME），
    用于剥掉消息开头的昵称；不传则不做昵称剥离。
    """
    now = now or datetime.now()
    s = text.strip()
    if not s:
        return {'ok': False, 'error': ERR_NO_TIME, 'had_trigger': False, 'stage': 'time'}

    if isinstance(nicknames, str):
        nicknames = (nicknames,)
    for nick in sorted(nicknames, key=len, reverse=True):
        if s.startswith(nick):
            s = s[len(nick):].lstrip(' \t、,，。.:：')
            break

    s, had_trigger = _strip_lead(s)
    if not s:
        return {'ok': False, 'error': ERR_NO_TIME, 'had_trigger': had_trigger, 'stage': 'time'}

    res = _try_relative(s, now) or _try_recur(s, now) or _try_datetime(s, now)
    if res is None:
        return {'ok': False, 'error': ERR_NO_TIME, 'had_trigger': had_trigger, 'stage': 'time'}
    if not res.get('ok', True):
        return {'ok': False, 'error': res['error'], 'had_trigger': had_trigger, 'stage': 'specific'}

    content = s[res['end']:].strip(' \t、,，。.:：;；!！?？\n\r')
    content = _RE_CONTENT_LEAD.sub('', content).strip()
    if not content:
        return {'ok': False, 'error': ERR_NO_CONTENT, 'had_trigger': had_trigger, 'stage': 'specific'}
    if len(content) > 128:
        content = content[:128]

    return {'ok': True, 'kind': res['kind'], 'when': res['when'],
            'hour': res['hour'], 'minute': res['minute'],
            'dow': res['dow'], 'day': res['day'], 'date': res['date'],
            'matched': res['matched'], 'content': content}


# ---------------------------------------------------------------------------
# 展示辅助（rmd.py 共用）
# ---------------------------------------------------------------------------

WEEK_CN = '一二三四五六日'


def weekday_cn(d):
    return '周' + WEEK_CN[d.weekday()]


def describe(res):
    """人类可读的提醒时间描述（不含内容）。"""
    k = res['kind']
    hm = '%02d:%02d' % (res['hour'], res['minute'])
    if k == 'once':
        d = res['date']
        return '📅 %d-%02d-%02d（%s）%s · 单次' % (
            d.year, d.month, d.day, weekday_cn(d), hm)
    if k == 'daily':
        return '🔁 每天 %s' % hm
    if k == 'workday':
        return '🔁 工作日 %s' % hm
    if k == 'weekly':
        names = '、'.join('周' + WEEK_CN[i] for i in res['dow'])
        return '🔁 每%s %s' % (names, hm)
    if k == 'monthly':
        return '🔁 每月%d号 %s' % (res['day'], hm)
    return str(res)
