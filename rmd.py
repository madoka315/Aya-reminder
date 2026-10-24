# -*- coding: utf-8 -*-
"""
定时提醒（重构版）：自然语言设置，支持无空格连写。

用法示例：
  @bot 提醒我 明天下午3点 交周报        单次（全员可用）
  @bot 半小时后 开会                    相对时间单次
  @bot 每天18:30 喂猫                   循环
  @bot 每周五 20:00 公会战
  @bot 工作日 8:00 起床
  @bot 每月1号 9:00 发工资
  解析后回复「确认」生效，「取消」放弃。
  我的提醒 / 本群提醒(管理) / 取消提醒 编号 / 提醒帮助

与旧版（cron 表达式）不兼容，数据文件为 rmd_data.json（原子写入，
损坏时自动隔离并从空数据启动，不会阻塞 bot 启动）。
"""

import os
import re
import json
import time
import asyncio
import unicodedata
from datetime import datetime

import hoshino
import nonebot
from hoshino import Service, priv

from .time_parser import parse_reminder, describe, weekday_cn, WEEK_CN

sv = hoshino.Service('Aya_reminder', help_='''
用一句话设提醒，时间可以和内容连着写：
@bot 提醒我 明天下午3点 交周报
@bot 半小时后 开会
@bot 每天18:30 喂猫
@bot 每周五 20:00 公会战
@bot 工作日 8:00 起床
@bot 每月1号 9:00 发工资
识别后回复「确认」生效，「取消」放弃。
我的提醒 / 本群提醒(管理) / 取消提醒 编号 / 提醒帮助
私聊也可以直接说「提醒我...」。
'''.strip())

GROUP_LIMIT = 20    # 每群最多条数
PERSONAL_LIMIT = 5  # 每人在每个场景（群/私聊）最多条数
CONFIRM_TTL = 180   # 预览确认有效期（秒）
MISFIRE_GRACE = 600 # 重启期间错过的单次提醒，10 分钟内补发

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE, 'rmd_data.json')

DATA = None      # {'version':2,'next_id':int,'reminders':[...]}
PENDING = {}     # (scope, chat_id, user_id) -> {'rem':..., 'expires':float}
_lck = asyncio.Lock()

bot = nonebot.get_bot()

# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

def _empty():
    return {'version': 2, 'next_id': 1, 'reminders': []}


def _load():
    if not os.path.exists(DATA_PATH):
        return _empty()
    try:
        with open(DATA_PATH, encoding='utf8') as f:
            d = json.load(f)
        if not isinstance(d.get('reminders'), list) or not isinstance(d.get('next_id'), int):
            raise ValueError('bad schema')
        return d
    except Exception:
        bak = DATA_PATH + '.corrupt-' + datetime.now().strftime('%Y%m%d%H%M%S')
        try:
            os.replace(DATA_PATH, bak)
            sv.logger.error('rmd_data.json 损坏，已隔离到 %s，从空数据启动' % bak)
        except Exception:
            sv.logger.exception('rmd_data.json 损坏且隔离失败，从空数据启动')
        return _empty()


def _save():
    tmp = DATA_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(DATA, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)


def _ensure():
    global DATA
    if DATA is None:
        DATA = _load()
    return DATA


# ---------------------------------------------------------------------------
# 调度与投递
# ---------------------------------------------------------------------------

def _schedule(rem):
    jid = 'rmd_%d' % rem['id']
    if rem['kind'] == 'once':
        at = datetime.strptime(rem['at'], '%Y-%m-%d %H:%M')
        nonebot.scheduler.add_job(_run_once, 'date', run_date=at,
                                  args=[dict(rem)], id=jid,
                                  replace_existing=True, misfire_grace_time=MISFIRE_GRACE)
    else:
        kw = {'hour': rem['hour'], 'minute': rem['minute']}
        if rem['kind'] == 'workday':
            kw['day_of_week'] = 'mon-fri'
        elif rem['kind'] == 'weekly':
            kw['day_of_week'] = ','.join(str(d) for d in rem['dow'])
        elif rem['kind'] == 'monthly':
            kw['day'] = rem['day']
        nonebot.scheduler.add_job(_run_recur, 'cron', args=[dict(rem)], id=jid,
                                  replace_existing=True, misfire_grace_time=300, **kw)


def _unschedule(rid):
    try:
        nonebot.scheduler.remove_job('rmd_%d' % rid)
    except Exception:
        pass


def _remove_record(rid):
    global DATA
    _ensure()
    DATA['reminders'] = [r for r in DATA['reminders'] if r['id'] != rid]
    _save()
    _unschedule(rid)


async def _deliver(rem):
    """投递一条提醒，重试 3 次。"""
    t = '⏰ 定时提醒：' + rem['content']
    tries = (0, 5, 15)
    for i, delay in enumerate(tries):
        if delay:
            await asyncio.sleep(delay)
        try:
            if rem['scope'] == 'group':
                gid = int(rem['chat_id'])
                if not sv.check_enabled(gid):
                    sv.logger.info('rmd #%s 群%s已关闭本服务，跳过' % (rem['id'], gid))
                    return True
                await hoshino.get_bot().send_group_msg(
                    group_id=gid,
                    message='[CQ:at,qq=%s] %s' % (rem['user_id'], t))
            else:
                await hoshino.get_bot().send_private_msg(
                    user_id=int(rem['chat_id']), message=t)
            return True
        except Exception as e:
            sv.logger.warning('rmd #%s 第%d次投递失败：%r' % (rem['id'], i + 1, e))
    return False


async def _run_once(rem):
    try:
        await _deliver(rem)
    finally:
        _remove_record(rem['id'])


async def _run_recur(rem):
    await _deliver(rem)


# ---------------------------------------------------------------------------
# 确认流
# ---------------------------------------------------------------------------

def _fmt_rem(rem):
    """列表用的一行描述。"""
    if rem['kind'] == 'once':
        dt = datetime.strptime(rem['at'], '%Y-%m-%d %H:%M')
        year = '' if dt.year == datetime.now().year else '%d-' % dt.year
        t = '📅 %s%02d-%02d（%s）%02d:%02d' % (
            year, dt.month, dt.day, weekday_cn(dt.date()), dt.hour, dt.minute)
    elif rem['kind'] == 'daily':
        t = '🔁 每天 %02d:%02d' % (rem['hour'], rem['minute'])
    elif rem['kind'] == 'workday':
        t = '🔁 工作日 %02d:%02d' % (rem['hour'], rem['minute'])
    elif rem['kind'] == 'weekly':
        t = '🔁 每%s %02d:%02d' % ('、'.join('周' + WEEK_CN[d] for d in rem['dow']),
                                  rem['hour'], rem['minute'])
    else:
        t = '🔁 每月%d号 %02d:%02d' % (rem['day'], rem['hour'], rem['minute'])
    return '#%d %s · %s' % (rem['id'], t, rem['content'])


def _limits_ok(scope, chat_id, user_id, rem):
    _ensure()
    same_chat = [r for r in DATA['reminders'] if r['scope'] == scope and r['chat_id'] == chat_id]
    if scope == 'group' and len(same_chat) >= GROUP_LIMIT:
        return '本群最多 %d 条提醒，先删掉一些吧（取消提醒 编号）' % GROUP_LIMIT
    mine = [r for r in same_chat if r['user_id'] == user_id]
    if len(mine) >= PERSONAL_LIMIT:
        return '你在这里最多 %d 条提醒，先删掉一些吧（取消提醒 编号）' % PERSONAL_LIMIT
    return None


def _pending_jid(scope, chat_id, user_id):
    return 'rmd_pending_%s_%s_%s' % (scope, chat_id, user_id)


def _drop_pending_job(scope, chat_id, user_id):
    try:
        nonebot.scheduler.remove_job(_pending_jid(scope, chat_id, user_id))
    except Exception:
        pass


def _make_pending(scope, chat_id, user_id, parsed):
    expires = time.time() + CONFIRM_TTL
    PENDING[(scope, chat_id, user_id)] = {
        'rem': {
            'scope': scope, 'chat_id': chat_id, 'user_id': user_id,
            'content': parsed['content'], 'kind': parsed['kind'],
            'hour': parsed['hour'], 'minute': parsed['minute'],
            'dow': parsed['dow'], 'day': parsed['day'],
            'at': parsed['when'].strftime('%Y-%m-%d %H:%M') if parsed['kind'] == 'once' else None,
        },
        'expires': expires,
    }
    # 到期未确认 → @用户提示未设置；确认/取消/新预览覆盖时任务自行落空
    nonebot.scheduler.add_job(
        _pending_expired, 'date',
        run_date=datetime.fromtimestamp(expires),
        args=[scope, chat_id, user_id, expires],
        id=_pending_jid(scope, chat_id, user_id),
        replace_existing=True)
    lines = ['已识别，回复「确认」生效（%d 分钟内有效）：' % (CONFIRM_TTL // 60),
             describe(parsed), '📝 ' + parsed['content']]
    if parsed['kind'] != 'once':
        lines.append('⏭ 下次触发：%s' % parsed['when'].strftime('%Y-%m-%d %H:%M'))
    return '\n'.join(lines)


async def _pending_expired(scope, chat_id, user_id, expires):
    p = PENDING.get((scope, chat_id, user_id))
    if not p or p['expires'] != expires:
        return  # 已确认/取消，或被新的预览覆盖
    del PENDING[(scope, chat_id, user_id)]
    text = '由于您未确认，这次提醒尚未设置哦～需要的话再叫我一次吧'
    try:
        if scope == 'group':
            await hoshino.get_bot().send_group_msg(
                group_id=int(chat_id),
                message='[CQ:at,qq=%s] %s' % (user_id, text))
        else:
            await hoshino.get_bot().send_private_msg(user_id=int(chat_id), message=text)
    except Exception as e:
        sv.logger.warning('rmd 未确认提示发送失败：%r' % e)


async def _do_confirm(scope, chat_id, user_id):
    key = (scope, chat_id, user_id)
    p = PENDING.get(key)
    if not p:
        return None
    del PENDING[key]
    _drop_pending_job(scope, chat_id, user_id)
    if time.time() > p['expires']:
        return '这条预览已过期，重新说一次吧～'
    rem = p['rem']
    _ensure()
    err = _limits_ok(scope, chat_id, user_id, rem)
    if err:
        return err
    async with _lck:
        rem['id'] = DATA['next_id']
        DATA['next_id'] += 1
        DATA['reminders'].append(rem)
        _save()
    _schedule(rem)
    return '✅ 已设置 %s\n📝 %s\n回复「我的提醒」可随时查看详情' % (
        _fmt_rem(rem), '到点我会在本群@你' if scope == 'group' else '到点我会私聊你')


def _normtext(s):
    s = unicodedata.normalize('NFKC', str(s))
    s = re.sub(r'\[CQ:[^\]]*\]', '', s)
    return s.strip()


def _parse_reply_hint(res, to_me):
    """解析失败时该不该回复：@过机器人、或错误已具体到可指导时才回复。"""
    if to_me or res.get('stage') == 'specific':
        return res['error']
    return None


# ---------------------------------------------------------------------------
# 群聊入口
# ---------------------------------------------------------------------------

# 时间表达开头的消息（需 @机器人/昵称，避免误触发）
_RX_TIME_START = re.compile(
    r'^(?:每天|每日|工作日|每周|每星期|每礼拜|每月|今天|明天|后天|大后天'
    r'|下?[周星期礼拜][一二三四五六日天]'
    r'|[0-9零一二两三四五六七八九十半])')

# 明确的「提醒」引导词开头（无需 @）；排除「提醒帮助」，避免遮蔽帮助指令
_RE_REMIND_LEAD = re.compile(r'^提醒(?!帮助)')


@sv.on_rex(_RE_REMIND_LEAD, only_to_me=False)
async def rmd_lead(bot, ev):
    res = parse_reminder(ev.norm_text, nicknames=hoshino.config.NICKNAME)
    if res.get('ok'):
        hint = _make_pending('group', str(ev.group_id), str(ev.user_id), res)
        if hint:
            await bot.send(ev, hint)
        return
    msg = _parse_reply_hint(res, bool(ev['to_me']))
    if msg:
        await bot.send(ev, msg)


@sv.on_rex(_RX_TIME_START, only_to_me=True)
async def rmd_timestart(bot, ev):
    res = parse_reminder(ev.norm_text, nicknames=hoshino.config.NICKNAME)
    if res.get('ok'):
        hint = _make_pending('group', str(ev.group_id), str(ev.user_id), res)
        if hint:
            await bot.send(ev, hint)
        return
    if res.get('stage') == 'specific':
        await bot.send(ev, res['error'])


@sv.on_rex(r'^(?:确认|确定)[!！。~～\s]*$')
async def rmd_confirm(bot, ev):
    msg = await _do_confirm('group', str(ev.group_id), str(ev.user_id))
    if msg:
        await bot.send(ev, msg)


@sv.on_rex(r'^取消[!！。~～\s]*$')
async def rmd_cancel_pending(bot, ev):
    key = ('group', str(ev.group_id), str(ev.user_id))
    if key in PENDING:
        del PENDING[key]
        _drop_pending_job(*key)
        await bot.send(ev, '好，不设了～')


@sv.on_rex(r'^(?:取消|删除)提醒\s*#?(\d+)$')
async def rmd_delete(bot, ev):
    _ensure()
    rid = int(ev['match'].group(1))
    is_admin = priv.check_priv(ev, priv.ADMIN)
    target = next((r for r in DATA['reminders'] if r['id'] == rid), None)
    if not target or target['scope'] != 'group' or target['chat_id'] != str(ev.group_id):
        await bot.send(ev, '没有这个编号，回复「我的提醒」看看～')
        return
    if target['user_id'] != str(ev.user_id) and not is_admin:
        await bot.send(ev, '只能删除自己的提醒（管理员可以删任何人的）')
        return
    async with _lck:
        _remove_record(rid)
    await bot.send(ev, '已删除 #%d' % rid)


@sv.on_fullmatch('我的提醒')
async def rmd_mine(bot, ev):
    _ensure()
    uid = str(ev.user_id)
    mine = [r for r in DATA['reminders']
            if r['scope'] == 'group' and r['chat_id'] == str(ev.group_id) and r['user_id'] == uid]
    if not mine:
        await bot.send(ev, '你在这里还没有提醒。示例：@bot 提醒我 明天下午3点 交周报')
        return
    await bot.send(ev, '你在本群的提醒：\n' + '\n'.join(_fmt_rem(r) for r in mine)
                   + '\n回复「取消提醒 编号」删除提醒')


@sv.on_fullmatch('本群提醒')
async def rmd_all_group(bot, ev):
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.send(ev, '查看全群提醒需要管理员权限，回复「我的提醒」看自己的～')
        return
    _ensure()
    rows = [r for r in DATA['reminders']
            if r['scope'] == 'group' and r['chat_id'] == str(ev.group_id)]
    if not rows:
        await bot.send(ev, '本群还没有任何提醒')
        return
    await bot.send(ev, '本群提醒（共 %d 条）：\n' % len(rows) + '\n'.join(_fmt_rem(r) for r in rows)
                   + '\n回复「取消提醒 编号」删除（管理员可删任何人的）')


@sv.on_rex(r'^(?:定时提醒|提醒)帮助$')
async def rmd_help(bot, ev):
    await bot.send(ev, sv.help)


# ---------------------------------------------------------------------------
# 私聊入口（Service 触发器只处理群聊，私聊走裸事件）
# ---------------------------------------------------------------------------

@bot.on_message('private')
async def rmd_private(ctx):
    text = _normtext(ctx['message'])
    uid = str(ctx['user_id'])
    key = ('private', uid, uid)

    if text in ('提醒帮助', '定时提醒帮助'):
        await bot.send_private_msg(user_id=int(uid), message=sv.help)
        return
    if text == '我的提醒':
        _ensure()
        mine = [r for r in DATA['reminders'] if r['scope'] == 'private' and r['chat_id'] == uid]
        if mine:
            await bot.send_private_msg(user_id=int(uid),
                                       message='你的提醒：\n' + '\n'.join(_fmt_rem(r) for r in mine)
                                       + '\n回复「取消提醒 编号」删除提醒')
        else:
            await bot.send_private_msg(user_id=int(uid),
                                       message='你还没有提醒。示例：提醒我 明天下午3点 交周报')
        return

    m = re.match(r'^(?:取消|删除)提醒\s*#?(\d+)$', text)
    if m:
        rid = int(m.group(1))
        _ensure()
        target = next((r for r in DATA['reminders']
                       if r['id'] == rid and r['scope'] == 'private' and r['chat_id'] == uid), None)
        if not target:
            await bot.send_private_msg(user_id=int(uid), message='没有这个编号，回复「我的提醒」看看～')
            return
        async with _lck:
            _remove_record(rid)
        await bot.send_private_msg(user_id=int(uid), message='已删除 #%d' % rid)
        return

    if re.match(r'^(?:确认|确定)[!！。~～\s]*$', text):
        msg = await _do_confirm('private', uid, uid)
        if msg:
            await bot.send_private_msg(user_id=int(uid), message=msg)
        return
    if re.match(r'^取消[!！。~～\s]*$', text):
        if key in PENDING:
            del PENDING[key]
            _drop_pending_job('private', uid, uid)
            await bot.send_private_msg(user_id=int(uid), message='好，不设了～')
        return

    res = parse_reminder(text, nicknames=hoshino.config.NICKNAME)
    if res.get('ok'):
        msg = _make_pending('private', uid, uid, res)
    elif res.get('had_trigger') or res.get('stage') == 'specific':
        msg = res['error']
    else:
        return
    await bot.send_private_msg(user_id=int(uid), message=msg)


# ---------------------------------------------------------------------------
# 启动重建
# ---------------------------------------------------------------------------

@nonebot.on_startup
async def rmd_startup():
    global DATA
    try:
        DATA = _load()
    except Exception:
        sv.logger.exception('定时提醒数据加载失败，从空数据启动')
        DATA = _empty()
    now = datetime.now()
    for rem in list(DATA['reminders']):
        try:
            if rem['kind'] == 'once':
                at = datetime.strptime(rem['at'], '%Y-%m-%d %H:%M')
                if at <= now:
                    if (now - at).total_seconds() <= MISFIRE_GRACE:
                        asyncio.ensure_future(_run_once(rem))
                        sv.logger.info('rmd #%s 补发重启期间错过的提醒' % rem['id'])
                    else:
                        _remove_record(rem['id'])
                        sv.logger.info('rmd #%s 已过期（%s），清除' % (rem['id'], rem['at']))
                else:
                    _schedule(rem)
            else:
                _schedule(rem)
        except Exception:
            sv.logger.exception('rmd #%s 重建失败，已跳过' % rem.get('id'))
    n = len(DATA['reminders'])
    sv.logger.info('定时提醒加载完成，共 %d 条' % n)
