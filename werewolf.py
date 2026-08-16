#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内网狼人杀游戏 - 单文件，零依赖
================================

部署:
    python3 werewolf.py [端口]       # 默认端口 8000

访问:
    浏览器打开 http://本机IP:8000

特性:
    - 支持 4-12 人局（狼/预/女/猎/民）
    - 房间制，6位房间号
    - 纯文字交互，操作简单
    - 自动判定胜负
    - 无需任何 pip 依赖，仅用 Python 标准库

 ponytail: 全部状态在内存，单进程；内网几十人够用。
    要水平扩展或持久化时再加 Redis/DB。
"""

import json
import os
import random
import secrets
import socket
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

# ============================================================
# 全局状态
# ============================================================
ROOMS = {}                       # room_id -> Room
LOCK = threading.Lock()          # 保护 ROOMS 及所有 Room 内部状态

# ponytail: 屠神胜利条件——所有这些角色都死狼才赢；村民不算神
GOD_ROLES = ('seer', 'witch', 'hunter')
FREE_CHAT_SECONDS = 120   # 白天自由讨论时长

# ============================================================
# 角色配置
# ============================================================
ROLE_CONFIGS = {
    4:  ['wolf', 'seer', 'villager', 'villager'],
    5:  ['wolf', 'wolf', 'seer', 'witch', 'villager'],
    6:  ['wolf', 'wolf', 'seer', 'witch', 'villager', 'villager'],
    7:  ['wolf', 'wolf', 'seer', 'witch', 'villager', 'villager', 'villager'],
    8:  ['wolf', 'wolf', 'wolf', 'seer', 'witch', 'villager', 'villager', 'villager'],
    9:  ['wolf', 'wolf', 'wolf', 'seer', 'witch', 'hunter', 'villager', 'villager', 'villager'],
    10: ['wolf', 'wolf', 'wolf', 'seer', 'witch', 'hunter', 'villager', 'villager', 'villager', 'villager'],
    11: ['wolf', 'wolf', 'wolf', 'seer', 'witch', 'hunter', 'villager', 'villager', 'villager', 'villager', 'villager'],
    12: ['wolf', 'wolf', 'wolf', 'wolf', 'seer', 'witch', 'hunter', 'villager', 'villager', 'villager', 'villager', 'villager'],
}
MIN_PLAYERS, MAX_PLAYERS = 4, 12

ROLE_NAMES = {
    'wolf': '狼人', 'seer': '预言家', 'witch': '女巫',
    'hunter': '猎人', 'villager': '村民',
}
ROLE_DESCS = {
    'wolf':     '你是【狼人】。每晚与同伴一起选择一名玩家击杀。白天伪装成好人混淆视听。',
    'seer':     '你是【预言家】。每晚查验一名玩家是否为狼人。',
    'witch':    '你是【女巫】。有一瓶解药（救今晚被杀的人）和一瓶毒药（毒杀一人），各只能用一次。',
    'hunter':   '你是【猎人】。死亡时（被毒除外）可以开枪带走一名玩家。',
    'villager': '你是【村民】。没有特殊能力，白天通过讨论和投票找出狼人。',
}

# ============================================================
# 数据模型
# ============================================================
class Player:
    def __init__(self, name, token):
        self.name = name
        self.token = token
        self.role = None
        self.alive = True
        self.left = False
        self.ready = False    # 等待阶段的准备状态
        self.spectator = False  # 游戏开始后加入的旁观者


class Room:
    def __init__(self):
        self.id = self._gen_id()
        self.players = {}              # token -> Player（保持加入顺序）
        self.host = None               # host token
        self.phase = 'waiting'         # waiting/night_wolf/night_seer/night_witch/day/hunter/vote/ended
        self.day = 0
        # 夜晚临时状态
        self.wolf_picks = {}           # wolf_token -> target_token
        self.wolf_killed = None        # 本夜被狼杀 token
        self.witch_save_used = False
        self.witch_poison_used = False
        self.deaths = []               # [(token, cause)] 白天公布的死亡
        # 日志
        self.public_log = []           # [{'id','type','text'}]
        self.next_mid = 1
        self.private_log = {}          # token -> [str]（已读即清）
        self.chat_log = []             # [{'id','name','text','alive'}]
        self.next_chat_id = 1
        self.wolf_chat_log = []        # [{'id','name','text'}] 仅狼可见
        self.next_wolf_chat_id = 1
        self.next_seat = 1             # 座位号计数，自动命名 1号 2号...
        # 投票
        self.votes = {}                # voter_token -> target_token|None
        # 白天轮流发言
        self.day_stage = None          # day 阶段子状态: 'speak'/'free'
        self.speech_queue = []         # 待发言 token（已随机洗牌）
        self.current_speaker = None    # 当前发言 token
        self.free_deadline = None      # 自由讨论截止 epoch（秒）
        # 其他
        self.winner = None             # 'wolf' / 'good'
        self.hunter_token = None       # 等待开枪的猎人 token
        self.created_at = time.time()

    def _gen_id(self):
        for _ in range(200):
            rid = ''.join(random.choices('0123456789', k=6))
            if rid not in ROOMS:
                return rid
        raise RuntimeError("无法生成唯一房间号")

    def add_msg(self, mtype, text):
        mid = self.next_mid
        self.next_mid += 1
        self.public_log.append({'id': mid, 'type': mtype, 'text': text})

    def add_private(self, token, text):
        self.private_log.setdefault(token, []).append(text)

    def add_chat(self, name, text, alive):
        cid = self.next_chat_id
        self.next_chat_id += 1
        self.chat_log.append({'id': cid, 'name': name, 'text': text, 'alive': alive})

    def add_wolf_chat(self, name, text):
        wid = self.next_wolf_chat_id
        self.next_wolf_chat_id += 1
        self.wolf_chat_log.append({'id': wid, 'name': name, 'text': text})

    def alive_players(self):
        return [p for p in self.players.values() if p.alive]

    def alive_by_role(self, role):
        return [p for p in self.players.values() if p.alive and p.role == role]


# ============================================================
# 游戏流程
# ============================================================
def start_game(room):
    n = len(room.players)
    roles = list(ROLE_CONFIGS[n])
    random.shuffle(roles)
    for p, r in zip(room.players.values(), roles):
        p.role = r
    room.day = 1
    room.add_msg('system', f'游戏开始！共 {n} 人。')
    # 给每个玩家发一条私密消息，宣告身份
    for p in room.players.values():
        room.add_private(p.token, f'你的身份：{ROLE_NAMES[p.role]}。{ROLE_DESCS[p.role]}')
        if p.role == 'wolf':
            teammates = [pl.name for pl in room.players.values()
                         if pl.role == 'wolf' and pl.token != p.token]
            if teammates:
                room.add_private(p.token, '你的狼人同伴：' + '、'.join(teammates))
    enter_night_wolf(room)


def enter_night_wolf(room):
    room.phase = 'night_wolf'
    room.wolf_picks = {}
    room.wolf_killed = None
    room.deaths = []
    room.add_msg('system', f'【第 {room.day} 夜】天黑请闭眼。狼人请睁眼，选择今晚击杀的目标。')


def wolf_pick(room, wolf_token, target_token):
    if wolf_token in room.wolf_picks:
        return
    room.wolf_picks[wolf_token] = target_token
    wolves = room.alive_by_role('wolf')
    if wolves and all(w.token in room.wolf_picks for w in wolves):
        counter = Counter(room.wolf_picks.values())
        top = counter.most_common()
        max_c = top[0][1]
        cands = [t for t, c in top if c == max_c]
        room.wolf_killed = random.choice(cands)
        enter_night_seer(room)


def enter_night_seer(room):
    if not room.alive_by_role('seer'):
        return enter_night_witch(room)
    room.phase = 'night_seer'
    room.add_msg('system', '狼人闭眼。预言家请睁眼，选择查验目标。')


def seer_check(room, seer_token, target_token):
    tgt = room.players.get(target_token)
    if not tgt or not tgt.alive or target_token == seer_token:
        return
    is_wolf = tgt.role == 'wolf'
    room.add_private(seer_token,
                     f'【第 {room.day} 夜】查验结果：{tgt.name} 是 {"狼人" if is_wolf else "好人"}。')
    enter_night_witch(room)


def enter_night_witch(room):
    witch = next(iter(room.alive_by_role('witch')), None)
    if not witch:
        return resolve_night(room, saved=False, poisoned=None)
    room.phase = 'night_witch'
    if room.wolf_killed:
        room.add_private(witch.token,
                         f'【第 {room.day} 夜】今晚 {room.players[room.wolf_killed].name} 被狼人袭击。'
                         f'你可以使用解药（{"剩余" if not room.witch_save_used else "已用完"}）。')
    else:
        room.add_private(witch.token, f'【第 {room.day} 夜】今晚无人被狼人袭击。')
    room.add_msg('system', '预言家闭眼。女巫请睁眼。')


def witch_act(room, witch_token, save, poison_target):
    saved = False
    if save and not room.witch_save_used and room.wolf_killed:
        room.witch_save_used = True
        saved = True
    poisoned = None
    if poison_target and not room.witch_poison_used:
        tgt = room.players.get(poison_target)
        if tgt and tgt.alive:
            room.witch_poison_used = True
            poisoned = poison_target
    resolve_night(room, saved, poisoned)


def resolve_night(room, saved, poisoned):
    deaths = []
    if room.wolf_killed and not saved:
        deaths.append((room.wolf_killed, 'wolf'))
    if poisoned:
        deaths.append((poisoned, 'witch'))
    # 应用死亡
    for t, _ in deaths:
        room.players[t].alive = False
    room.deaths = deaths
    enter_day(room)


def enter_day(room):
    room.phase = 'day'
    if not room.deaths:
        room.add_msg('death', f'【第 {room.day} 天】天亮了。昨晚是平安夜。')
    else:
        names = '、'.join(room.players[t].name for t, _ in room.deaths)
        room.add_msg('death', f'【第 {room.day} 天】天亮了。昨晚 {names} 死亡。')
    # 触发猎人开枪（被毒不能开枪）
    for t, cause in room.deaths:
        p = room.players[t]
        if p.role == 'hunter' and cause != 'witch':
            room.phase = 'hunter'
            room.hunter_token = t
            room.add_private(t, '你是猎人，你死了！请选择一名玩家带走，或跳过。')
            return
    if check_winner(room):
        return
    start_day_discussion(room)


def start_day_discussion(room):
    """白天开始：随机顺序轮流发言，全说完进 2 分钟自由讨论。"""
    alive = [p.token for p in room.alive_players()]
    random.shuffle(alive)
    room.speech_queue = alive
    room.current_speaker = room.speech_queue.pop(0) if alive else None
    if room.current_speaker:
        room.day_stage = 'speak'
        room.add_msg('system', f'白天讨论开始，{room.players[room.current_speaker].name} 先发言，'
                               '说完点「发言结束」，随后随机轮到下一位。')
    else:
        # ponytail: 没有存活玩家（正常会被 check_winner 截住），兜底直接进自由讨论
        room.day_stage = 'free'
        room.free_deadline = time.time() + FREE_CHAT_SECONDS


def next_speaker_or_free(room, prev_name):
    """end_speech / 发言人退出后：切下一个或进自由讨论。"""
    if room.speech_queue:
        nxt = room.speech_queue.pop(0)
        room.current_speaker = nxt
        room.add_msg('system', f'{prev_name} 发言结束，轮到 {room.players[nxt].name}。')
    else:
        room.current_speaker = None
        room.day_stage = 'free'
        room.free_deadline = time.time() + FREE_CHAT_SECONDS
        room.add_msg('system', '轮流发言结束，进入自由讨论（2 分钟），之后任意存活玩家可发起投票。')


def check_winner(room):
    # ponytail: 屠神规则——所有狼死则好人赢；所有神职死则狼赢
    if not room.alive_by_role('wolf'):
        room.winner = 'good'
        room.phase = 'ended'
        room.add_msg('end', '游戏结束！好人阵营胜利！')
        return True
    alive = room.alive_players()
    if not any(p.role in GOD_ROLES for p in alive):
        room.winner = 'wolf'
        room.phase = 'ended'
        room.add_msg('end', '游戏结束！狼人阵营胜利！所有神职已被消灭。')
        return True
    # 屠城兜底：好人只剩 1 个且仍有狼，夜里必被刀，直接判狼胜
    goods = [p for p in alive if p.role != 'wolf']
    if len(goods) <= 1:
        room.winner = 'wolf'
        room.phase = 'ended'
        room.add_msg('end', '游戏结束！狼人阵营胜利！狼人已达成人数优势。')
        return True
    return False


def hunter_shoot(room, hunter_token, target_token):
    hunter = room.players[hunter_token]
    if target_token:
        tgt = room.players.get(target_token)
        if tgt and tgt.alive and tgt.token != hunter_token:
            tgt.alive = False
            room.add_msg('death', f'猎人 {hunter.name} 开枪带走了 {tgt.name}！')
    else:
        room.add_msg('system', f'猎人 {hunter.name} 选择不开枪。')
    room.hunter_token = None
    if check_winner(room):
        return
    # 开枪后回到白天讨论（hunter 阶段只会从 day 或 vote 进入）
    room.phase = 'day'
    start_day_discussion(room)


def tick_free_deadline(room):
    """自由讨论倒计时到点自动进投票。
    ponytail: 无后台线程，挂在 state 轮询里触发（每 1.5s 全员 poll，延迟可忽略）。
    """
    if (room.phase == 'day' and room.day_stage == 'free'
            and room.free_deadline and time.time() > room.free_deadline):
        room.free_deadline = None
        room.add_msg('system', '自由讨论时间到，自动进入投票。')
        start_vote(room)


def start_vote(room):
    room.phase = 'vote'
    room.votes = {}
    room.add_msg('system', '投票阶段开始！请选择你认为是狼人的玩家。')


def cast_vote(room, voter, target):
    room.votes[voter] = target
    if all(p.token in room.votes for p in room.alive_players()):
        resolve_vote(room)


def resolve_vote(room):
    if not room.votes:
        room.add_msg('system', '投票无效，进入下一夜。')
        return enter_next_night(room)
    # ponytail: 一行一票 + 得票统计，前端 pre-wrap 渲染；不再挤一行难读
    lines = [
        f'{room.players[v].name} → {room.players[t].name if t else "弃票"}'
        for v, t in room.votes.items()
    ]
    tally = Counter(t for t in room.votes.values() if t)
    tally_str = '，'.join(f'{room.players[t].name} {c}票' for t, c in tally.most_common())
    room.add_msg('vote', '投票详情：\n' + '\n'.join(lines) + f'\n— 得票：{tally_str}' if tally_str
                 else '投票详情：\n' + '\n'.join(lines))
    valid = [t for t in room.votes.values() if t]
    if not valid:
        room.add_msg('system', '全员弃票，无人出局。')
        return enter_next_night(room)
    counter = Counter(valid)
    top = counter.most_common()
    max_c = top[0][1]
    cands = [t for t, c in top if c == max_c]
    voted_out = random.choice(cands)
    p = room.players[voted_out]
    p.alive = False
    room.add_msg('vote', f'{p.name} 被投票出局。')
    if p.role == 'hunter':
        room.phase = 'hunter'
        room.hunter_token = voted_out
        room.add_private(voted_out, '你是猎人，你被投票出局！可以开枪带走一人，或跳过。')
        return
    if check_winner(room):
        return
    enter_next_night(room)


def enter_next_night(room):
    room.day += 1
    room.phase = 'night_wolf'
    room.wolf_picks = {}
    room.wolf_killed = None
    room.deaths = []
    room.add_msg('system', f'【第 {room.day} 夜】天黑请闭眼。')


def reset_room(room):
    """房主强制重置：回到等待状态（用来脱困，例如玩家挂机）"""
    for p in room.players.values():
        p.role = None
        p.alive = True
        p.ready = False
        p.spectator = False
    room.phase = 'waiting'
    room.day = 0
    room.wolf_picks = {}
    room.wolf_killed = None
    room.witch_save_used = False
    room.witch_poison_used = False
    room.deaths = []
    room.private_log = {}
    room.votes = {}
    room.winner = None
    room.hunter_token = None
    # 清白天发言状态
    room.day_stage = None
    room.speech_queue = []
    room.current_speaker = None
    room.free_deadline = None
    # ponytail: 重置时清空上一局日志和聊天，避免新局看到旧局记录
    room.public_log = []
    room.chat_log = []
    room.wolf_chat_log = []
    room.add_msg('system', '房主已重置游戏，重新等待开始。')


def leave_room(room, token, reason='left'):
    """玩家中途退出。
    等待阶段：直接从房间移除；空房间回收。
    游戏中：标记 left + 视为死亡，转移房主，推进可能卡住的阶段，重判胜负。
    reason: 'left'（自愿）/ 'kicked'（被房主踢）— 仅影响公告措辞。
    """
    p = room.players.get(token)
    if not p or p.left:
        return
    p.left = True
    p.alive = False
    # 清掉该玩家在当前阶段的临时状态
    room.wolf_picks.pop(token, None)
    room.votes.pop(token, None)
    # 猎人退出
    if room.hunter_token == token:
        room.hunter_token = None
        if room.phase == 'hunter':
            room.phase = 'day'
    # 房主转移（跳过旁观者）
    if room.host == token:
        new_host = next((t for t, pl in room.players.items()
                         if t != token and not pl.left and not pl.spectator), None)
        room.host = new_host
        if new_host:
            room.add_msg('system', f'房主已转移给 {room.players[new_host].name}。')
    # 等待阶段：直接删除；房间空了回收
    if room.phase == 'waiting':
        del room.players[token]
        if not room.players:
            ROOMS.pop(room.id, None)
        return
    # 游戏中：公告 + 推进 + 胜负
    msg = f'{p.name} 被房主踢出。' if reason == 'kicked' else f'{p.name} 已离开游戏。'
    room.add_msg('system', msg)
    _advance_current_phase(room, token, p.name)


def _advance_current_phase(room, token, name):
    """有玩家离开后，检查当前阶段是否需要推进或结算。"""
    if room.phase == 'night_wolf':
        wolves = room.alive_by_role('wolf')
        if not wolves:
            return check_winner(room)
        if all(w.token in room.wolf_picks for w in wolves):
            counter = Counter(room.wolf_picks.values())
            top = counter.most_common()
            max_c = top[0][1]
            cands = [t for t, c in top if c == max_c]
            room.wolf_killed = random.choice(cands)
            enter_night_seer(room)
    elif room.phase == 'night_seer':
        if not room.alive_by_role('seer'):
            enter_night_witch(room)
    elif room.phase == 'night_witch':
        if not room.alive_by_role('witch'):
            resolve_night(room, saved=False, poisoned=None)
    elif room.phase == 'vote':
        alive = room.alive_players()
        if alive and all(pl.token in room.votes for pl in alive):
            resolve_vote(room)
    elif room.phase == 'day':
        # 发言人退出：从队列剔除；若正在发言则直接切下一个
        if token in room.speech_queue:
            room.speech_queue.remove(token)
        if room.current_speaker == token:
            next_speaker_or_free(room, f'{name}（已退出）')
    check_winner(room)


# ============================================================
# 状态序列化
# ============================================================
def get_state(room, token):
    p = room.players.get(token)
    # 玩家列表（已退出/被踢的不展示）
    players = []
    for pl in room.players.values():
        if pl.left:
            continue
        show_role = (pl.token == token) or room.phase == 'ended'
        players.append({
            'name': pl.name,
            'token': pl.token,
            'alive': pl.alive,
            'left': pl.left,
            'ready': pl.ready,
            'spectator': pl.spectator,
            'isYou': pl.token == token,
            'role': pl.role if show_role else None,
        })
    # 狼队友提示
    teammates = None
    if p.role == 'wolf':
        teammates = [pl.name for pl in room.players.values()
                     if pl.role == 'wolf' and pl.token != token]
        if not teammates:
            teammates = None
    return {
        'roomId': room.id,
        'phase': room.phase,
        'day': room.day,
        'isHost': room.host == token,
        'players': players,
        'myRole': p.role,
        'myRoleName': ROLE_NAMES.get(p.role),
        'myRoleDesc': ROLE_DESCS.get(p.role),
        'myAlive': p.alive,
        'mySpectator': p.spectator,
        # 白天发言轮转状态
        'dayStage': room.day_stage,
        'currentSpeaker': (room.players[room.current_speaker].name
                           if room.current_speaker else None),
        'isCurrentSpeaker': token == room.current_speaker,
        'freeDeadline': room.free_deadline,
        'teammates': teammates,
        'winner': room.winner,
        'actions': get_available_actions(room, token),
        # 私密信息快照后清空
        'private': room.private_log.pop(token, []),
    }


def get_available_actions(room, token):
    p = room.players.get(token)
    if not p:
        return []
    # 等待阶段：非房主准备/取消准备；房主在所有人都准备好后才能开始
    if room.phase == 'waiting':
        is_host = (room.host == token)
        others = [pl for t, pl in room.players.items() if t != token]
        all_ready = bool(others) and all(pl.ready for pl in others)
        if is_host:
            if all_ready and len(room.players) in ROLE_CONFIGS:
                return [{'type': 'start_game'}]
            return []
        return [{'type': 'toggle_ready', 'ready': p.ready}]
    # 已死亡（且不是正在开枪的猎人），观战
    if not p.alive and room.phase != 'hunter':
        return []
    if room.phase == 'night_wolf' and p.role == 'wolf' and p.alive:
        if token in room.wolf_picks:
            return []  # 已选择
        # ponytail: 允许自杀（目标列表包含自己），不杀走 can_skip；仍排除其他狼同伴
        tgts = [{'token': pl.token, 'name': pl.name}
                for pl in room.alive_players() if not (pl.role == 'wolf' and pl.token != token)]
        return [{'type': 'wolf_pick', 'can_skip': True, 'targets': tgts}]
    if room.phase == 'night_seer' and p.role == 'seer' and p.alive:
        tgts = [{'token': pl.token, 'name': pl.name}
                for pl in room.alive_players() if pl.token != token]
        return [{'type': 'seer_check', 'targets': tgts}]
    if room.phase == 'night_witch' and p.role == 'witch' and p.alive:
        act = {'type': 'witch_act'}
        act['can_save'] = (not room.witch_save_used) and (room.wolf_killed is not None)
        if room.wolf_killed:
            act['save_target'] = {
                'token': room.wolf_killed,
                'name': room.players[room.wolf_killed].name,
            }
        else:
            act['save_target'] = None
        act['witch_save_used'] = room.witch_save_used
        act['can_poison'] = not room.witch_poison_used
        act['witch_poison_used'] = room.witch_poison_used
        act['poison_targets'] = [{'token': pl.token, 'name': pl.name}
                                  for pl in room.alive_players() if pl.token != token]
        return [act]
    if room.phase == 'hunter' and token == room.hunter_token:
        tgts = [{'token': pl.token, 'name': pl.name} for pl in room.alive_players()]
        return [{'type': 'hunter_shoot', 'targets': tgts}]
    if room.phase == 'day' and p.alive and not p.spectator:
        # 白天两阶段：轮流发言（仅发言人拿到结束按钮）/ 自由讨论（可发起投票）
        if room.day_stage == 'speak':
            if token == room.current_speaker:
                return [{'type': 'end_speech'}]
            return []
        if room.day_stage == 'free':
            return [{'type': 'start_vote'}]
        return []
    if room.phase == 'vote' and p.alive:
        if token in room.votes:
            return []  # 已投票
        tgts = [{'token': pl.token, 'name': pl.name}
                for pl in room.alive_players() if pl.token != token]
        return [{'type': 'cast_vote', 'targets': tgts}]
    return []


# ============================================================
# HTTP Handler
# ============================================================
def find_room_by_token(token):
    for r in ROOMS.values():
        if token in r.players:
            return r
    return None


# index.html 路径：优先当前工作目录，其次脚本所在目录
_INDEX_CANDIDATES = [
    os.path.join(os.getcwd(), 'index.html'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'),
]
_INDEX_CACHE = {'mtime': 0, 'data': None}  # ponytail: 简单 mtime 缓存，改了 html 立即生效


def load_index_html():
    """读取 index.html，带 mtime 缓存（改文件自动重载，方便调试）。"""
    path = next((p for p in _INDEX_CANDIDATES if os.path.exists(p)), None)
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
        if _INDEX_CACHE['mtime'] != mtime or _INDEX_CACHE['data'] is None:
            with open(path, 'rb') as f:
                _INDEX_CACHE['data'] = f.read()
            _INDEX_CACHE['mtime'] = mtime
        return _INDEX_CACHE['data']
    except OSError:
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默

    # --- 工具方法 ---
    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode('utf-8'))

    # --- 路由 ---
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/':
            return self._serve_html()
        if url.path == '/api/state':
            return self._handle_state(url.query)
        if url.path == '/api/rooms':
            with LOCK:
                rooms = [r.id for r in ROOMS.values() if r.phase == 'waiting']
            return self._json(200, {'rooms': rooms})
        return self._json(404, {'error': 'not_found'})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            data = self._read_json()
        except Exception:
            return self._json(400, {'error': '请求格式错误'})
        if url.path == '/api/create':
            return self._handle_create(data)
        if url.path == '/api/join':
            return self._handle_join(data)
        if url.path == '/api/action':
            return self._handle_action(data)
        return self._json(404, {'error': 'not_found'})

    # --- 页面 ---
    def _serve_html(self):
        body = load_index_html()
        if body is None:
            self._json(500, {'error': 'index.html 未找到'})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    # --- API ---
    def _handle_create(self, data):
        # ponytail: 玩家不命名，服务端按进房顺序自动编号（1号 2号...）
        with LOCK:
            room = Room()
            name = f'{room.next_seat}号'
            room.next_seat += 1
            token = secrets.token_hex(16)
            room.players[token] = Player(name, token)
            room.host = token
            ROOMS[room.id] = room
        return self._json(200, {'token': token, 'roomId': room.id, 'name': name})

    def _handle_join(self, data):
        room_id = (data.get('roomId') or '').strip()
        if not room_id:
            return self._json(400, {'error': '请填写房间号'})
        with LOCK:
            room = ROOMS.get(room_id)
            if not room:
                return self._json(404, {'error': '房间不存在'})
            if len(room.players) >= MAX_PLAYERS:
                return self._json(400, {'error': '房间已满'})
            # 座位号单调递增，名字天然唯一，无需重名检查
            name = f'{room.next_seat}号'
            room.next_seat += 1
            token = secrets.token_hex(16)
            p = Player(name, token)
            # ponytail: 游戏开始后加入的人是旁观者——能看公共区，评论区带(旁观)后缀
            if room.phase != 'waiting':
                p.spectator = True
                p.alive = False
                room.add_msg('system', f'{name} 以旁观者身份加入。')
            room.players[token] = p
        return self._json(200, {'token': token, 'roomId': room.id, 'name': name})

    def _handle_state(self, query):
        qs = parse_qs(query)
        token = qs.get('token', [''])[0]
        with LOCK:
            room = find_room_by_token(token)
            if not room:
                return self._json(404, {'error': 'room_not_found'})
            # ponytail: 被踢/已离开的玩家下次轮询拿到 kicked 错误，前端自动登出
            p = room.players.get(token)
            if p and p.left:
                return self._json(403, {'error': 'kicked'})
            tick_free_deadline(room)  # 自由讨论到点自动进投票（要在取 state 前）
            state = get_state(room, token)
            # 增量日志
            since = int(qs.get('since', ['0'])[0])
            chat_since = int(qs.get('chatSince', ['0'])[0])
            wolf_chat_since = int(qs.get('wolfChatSince', ['0'])[0])
            state['log'] = [m for m in room.public_log if m['id'] > since]
            state['chat'] = [c for c in room.chat_log if c['id'] > chat_since]
            # 狼聊仅下发给狼人（不论死活，死人只读）
            p = room.players.get(token)
            state['wolf_chat'] = ([m for m in room.wolf_chat_log if m['id'] > wolf_chat_since]
                                  if p and p.role == 'wolf' else [])
        return self._json(200, state)

    def _handle_action(self, data):
        token = data.get('token')
        act = data.get('type')
        with LOCK:
            room = find_room_by_token(token)
            if not room:
                return self._json(404, {'error': '房间不存在或已失效'})
            p = room.players[token]
            try:
                err = self._dispatch_action(room, p, token, act, data)
                if err:
                    return self._json(400, {'error': err})
            except GameError as e:
                return self._json(400, {'error': str(e)})
            except Exception as e:
                return self._json(500, {'error': '服务器错误: ' + str(e)})
        return self._json(200, {'ok': True})

    def _dispatch_action(self, room, p, token, act, data):
        if act == 'chat':
            if room.phase not in ('day', 'vote'):
                return '当前阶段不能发言'
            # 白天发言按阶段收紧：轮流发言只有当前发言人能说；自由讨论限 2 分钟
            if room.phase == 'day':
                if room.day_stage == 'speak':
                    if p.spectator or token != room.current_speaker:
                        return '现在轮到别人发言'
                elif room.day_stage == 'free':
                    if room.free_deadline and time.time() > room.free_deadline:
                        return '自由讨论时间已结束'
            if not p.alive and not p.spectator:
                return '已出局，无法发言'
            text = (data.get('text') or '').strip()[:200]
            if text:
                name = f'{p.name}(旁观)' if p.spectator else p.name
                # 旁观者始终按"活"显示（不带删除线）
                room.add_chat(name, text, True if p.spectator else p.alive)
            return None
        if act == 'end_speech':
            # ponytail: 轮流发言结束按钮，只有当前发言人能点
            if room.phase != 'day' or room.day_stage != 'speak' or token != room.current_speaker:
                return '当前无法操作'
            next_speaker_or_free(room, p.name)
            return None
        if act == 'wolf_chat':
            # ponytail: 狼人私聊频道，任何阶段都可发，仅活狼能发言（死狼只读）
            if p.role != 'wolf':
                return '只有狼人能进狼人频道'
            if not p.alive:
                return '已出局，无法发言'
            text = (data.get('text') or '').strip()[:200]
            if text:
                room.add_wolf_chat(p.name, text)
            return None
        if act == 'reset':
            if room.host != token:
                return '只有房主能重置'
            reset_room(room)
            return None
        if act == 'leave':
            leave_room(room, token)
            return None
        if act == 'kick':
            # ponytail: 房主踢人，复用 leave_room 的全套清理（房主转移、阶段推进、胜负）
            if room.host != token:
                return '只有房主能踢人'
            tgt = data.get('target')
            if tgt == token:
                return '不能踢自己'
            tp = room.players.get(tgt)
            if not tp or tp.left:
                return '目标不在房间'
            name = tp.name
            leave_room(room, tgt, reason='kicked')
            # 等待阶段 leave_room 不广播（直接删玩家），这里补一条让其他人看到
            if room.phase == 'waiting':
                room.add_msg('system', f'{name} 被房主踢出。')
            return None
        if act == 'toggle_ready':
            p.ready = not p.ready
            return None
        if act == 'start_game':
            if room.host != token:
                return '只有房主能开始'
            if len(room.players) not in ROLE_CONFIGS:
                return f'人数需为 {MIN_PLAYERS}-{MAX_PLAYERS}，当前 {len(room.players)} 人'
            others = [pl for t, pl in room.players.items() if t != token]
            if not others or not all(pl.ready for pl in others):
                return '还有玩家未准备'
            start_game(room)
            return None
        if act == 'wolf_pick':
            if room.phase != 'night_wolf' or p.role != 'wolf' or not p.alive:
                return '当前无法操作'
            if token in room.wolf_picks:
                return '你已经选择过了'
            tgt = data.get('target')
            # ponytail: 狼可自杀（target=self）或空刀（target=None）；仍禁止刀同伴
            if tgt is None:
                wolf_pick(room, token, None)
                return None
            tp = room.players.get(tgt)
            if not tp or not tp.alive:
                return '目标无效'
            if tp.role == 'wolf' and tgt != token:
                return '不能刀狼人同伴'
            wolf_pick(room, token, tgt)
            return None
        if act == 'seer_check':
            if room.phase != 'night_seer' or p.role != 'seer' or not p.alive:
                return '当前无法操作'
            tgt = data.get('target')
            tp = room.players.get(tgt)
            if not tp or not tp.alive or tgt == token:
                return '目标无效'
            seer_check(room, token, tgt)
            return None
        if act == 'witch_act':
            if room.phase != 'night_witch' or p.role != 'witch' or not p.alive:
                return '当前无法操作'
            save = bool(data.get('save'))
            poison = data.get('poison')
            witch_act(room, token, save, poison)
            return None
        if act == 'hunter_shoot':
            if room.phase != 'hunter' or token != room.hunter_token:
                return '当前无法操作'
            hunter_shoot(room, token, data.get('target'))
            return None
        if act == 'start_vote':
            # ponytail: 投票只在自由讨论阶段开放，轮流发言时不能抢跑
            if room.phase != 'day' or room.day_stage != 'free' or not p.alive or p.spectator:
                return '当前无法操作'
            start_vote(room)
            return None
        if act == 'cast_vote':
            if room.phase != 'vote' or not p.alive:
                return '当前无法投票'
            if token in room.votes:
                return '你已经投过票了'
            cast_vote(room, token, data.get('target'))
            return None
        return '未知操作'


class GameError(Exception):
    pass


# ============================================================
# 启动
# ============================================================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def cleanup_idle_rooms(interval=300):
    """后台清理 2 小时空闲的房间"""
    while True:
        time.sleep(interval)
        now = time.time()
        with LOCK:
            stale = [rid for rid, r in ROOMS.items()
                     if now - r.created_at > 7200 and r.phase == 'waiting']
            for rid in stale:
                ROOMS.pop(rid, None)


def main():
    threading.Thread(target=cleanup_idle_rooms, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    ip = get_local_ip()
    print('=' * 50)
    print('  狼人杀服务器已启动')
    print(f'  本机访问:   http://127.0.0.1:{PORT}')
    print(f'  内网访问:   http://{ip}:{PORT}')
    print(f'  玩家数量:   {MIN_PLAYERS}-{MAX_PLAYERS} 人')
    print('  按 Ctrl+C 停止')
    print('=' * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')





if __name__ == '__main__':
    main()
