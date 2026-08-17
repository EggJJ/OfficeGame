#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
谁是卧底 - 单文件，零依赖
=======================

部署:
    python3 undercover.py [端口]       # 默认 8001

访问:
    浏览器打开 http://本机IP:8001

特性:
    - 4-12 人局，1 或 2 卧底（5 人双卧底更刺激）
    - 房间制，6 位房间号
    - 私下限时查看词语，忘记可再看 → 轮流描述 → 投票放逐
    - 自动违规检测：描述含词中任一字直接出局
    - 自动判定胜负

ponytail: 全部状态在内存，单进程；内网几人够用。
   要水平扩展或持久化时再加 Redis/DB。
"""

import json
import random
import secrets
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
MIN_PLAYERS, MAX_PLAYERS = 4, 12
WORD_REVEAL_SECONDS = 8

ROOMS = {}
LOCK = threading.Lock()

# 词库：(平民词, 卧底词)，开局随机互换
WORD_PAIRS = [
    ('烤冷面', '煎饼果子'), ('螺蛳粉', '臭豆腐'), ('柠檬茶', '青柠水'),
    ('奶盖茶', '芝士茶'), ('热狗', '汉堡'), ('酸辣粉', '重庆小面'),
    ('甄嬛传', '如懿传'), ('长相思', '星汉灿烂'), ('跨年晚会', '春节联欢晚会'),
    ('脱口秀', '相声'), ('密室逃脱', '剧本杀'),
    ('柴犬', '柯基'), ('榴莲', '菠萝蜜'), ('冰箱', '空调'),
    ('防晒霜', '隔离霜'), ('充电宝', '电池'), ('签字笔', '中性笔'),
    ('遮瑕膏', '粉底液'), ('蓝牙耳机', '有线耳机'),
    ('甲方', '老板'), ('摸鱼', '划水'), ('退堂鼓', '摆烂'),
    ('直拍', '饭拍'), ('回归', '回归秀'), ('打歌服', '舞台装'),
    ('应援棒', '手幅'), ('小卡', '拍立得'),
]


# ============================================================
# 数据模型
# ============================================================
class Player:
    def __init__(self, name, token):
        self.name = name
        self.token = token
        self.role = None              # 'civilian' / 'undercover'
        self.word = None
        self.alive = True
        self.left = False             # 中途离场
        self.ready = False
        self.viewed = False           # 是否已查看自己的词
        self.word_visible_until = 0.0 # 词语临时展示截止时间


class Room:
    def __init__(self):
        self.id = self._gen_id()
        self.players = {}             # token -> Player（保持加入顺序）
        self.host = None
        self.uc_count = 1             # 房主可改
        self.phase = 'waiting'        # waiting/reveal/describe/vote/ended
        self.round = 0
        self.civ_word = None
        self.uc_word = None
        self.descriptions = {}        # round -> [{'name','text'}]
        self.votes = {}               # round -> {voter_token: target_token}
        self.speaker_order = []
        self.speaker_idx = 0
        self.public_log = []
        self.next_mid = 1
        self.winner = None
        self.created_at = time.time()

    def _gen_id(self):
        for _ in range(200):
            rid = ''.join(random.choices('0123456789', k=6))
            if rid not in ROOMS:
                return rid
        raise RuntimeError("无法生成唯一房间号")

    def add_msg(self, text):
        mid = self.next_mid
        self.next_mid += 1
        self.public_log.append({'id': mid, 'text': text})

    def alive_players(self):
        return [p for p in self.players.values() if p.alive]

    def current_speaker(self):
        if self.phase != 'describe':
            return None
        if self.speaker_idx >= len(self.speaker_order):
            return None
        return self.speaker_order[self.speaker_idx]


# ============================================================
# 游戏流程
# ============================================================
def start_game(room):
    n = len(room.players)
    civ, uc = random.choice(WORD_PAIRS)
    if random.random() < 0.5:
        civ, uc = uc, civ
    room.civ_word, room.uc_word = civ, uc
    roles = ['undercover'] * room.uc_count + ['civilian'] * (n - room.uc_count)
    random.shuffle(roles)
    for p, r in zip(room.players.values(), roles):
        p.role = r
        p.word = uc if r == 'undercover' else civ
        p.viewed = False
        p.word_visible_until = 0.0
        p.alive = True
        p.ready = False
    room.round = 1
    room.phase = 'describe'          # 不再有统一看词阶段：各自看完即可描述
    room.descriptions = {}
    room.votes = {}
    room.speaker_order = [p.token for p in room.players.values() if p.alive]
    random.shuffle(room.speaker_order)   # 发言顺序随机，固定顺序对先手不利
    room.speaker_idx = 0
    room.winner = None
    first = room.players[room.speaker_order[0]]
    room.add_msg(f'游戏开始！共 {n} 人，其中卧底 {room.uc_count} 人。'
                 f'请先【查看我的词】，从【{first.name}】开始描述（不能直接说出词中的任一字）。')


def view_word(room, player):
    # 只标记“看过”，不阻塞流程。词语限时显示，忘记可再看。
    player.viewed = True
    player.word_visible_until = time.time() + WORD_REVEAL_SECONDS


def hide_word(player):
    player.word_visible_until = 0.0


def _post_describe(room):
    """描述完一位后：若全员描述完毕则进投票，否则点名下一位。"""
    if room.speaker_idx >= len(room.speaker_order):
        if check_winner(room):
            return
        room.phase = 'vote'
        room.votes[room.round] = {}
        room.add_msg(f'第 {room.round} 轮描述结束。请讨论后投票放逐你认为是卧底的玩家。')
        return
    nxt = room.players[room.speaker_order[room.speaker_idx]]
    room.add_msg(f'轮到【{nxt.name}】描述。')


def describe(room, token, text):
    if room.phase != 'describe' or token != room.current_speaker():
        return False, '还没轮到你描述'
    p = room.players[token]
    if not p.viewed:
        return False, '请先查看自己的词'
    text = (text or '').strip()
    if not text:
        return False, '描述不能为空'
    if len(text) > 80:
        return False, '描述太长（限 80 字）'
    # 违规检测：描述包含词中任一字 → 直接出局
    leaked = sorted(set(c for c in p.word if c in text))
    if leaked:
        p.alive = False
        room.add_msg(f'【{p.name}】的描述包含词中的字，违规出局！')
        room.speaker_order = [t for t in room.speaker_order if t != token]
        # 不递增 idx：下一位已经在当前位置
        if check_winner(room):
            return True, '违规出局，游戏结束'
        _post_describe(room)
        return True, '违规出局'
    room.descriptions.setdefault(room.round, []).append({'name': p.name, 'text': text})
    room.add_msg(f'【{p.name}】：{text}')
    room.speaker_idx += 1
    _post_describe(room)
    return True, 'ok'


def skip_describe(room, token):
    if room.phase != 'describe' or token != room.current_speaker():
        return False, '还没轮到你描述'
    p = room.players[token]
    room.add_msg(f'【{p.name}】选择跳过本轮描述。')
    room.speaker_idx += 1
    _post_describe(room)
    return True, 'ok'


def vote(room, voter_token, target_token):
    if room.phase != 'vote':
        return False, '不在投票阶段'
    voter = room.players[voter_token]
    if not voter.alive:
        return False, '你已出局'
    if voter_token in room.votes[room.round]:
        return False, '你已经投过票了'
    if voter_token == target_token:
        return False, '不能投自己'
    target = room.players.get(target_token)
    if not target or not target.alive:
        return False, '无效投票对象'
    room.votes[room.round][voter_token] = target_token
    alive = room.alive_players()
    if all(p.token in room.votes[room.round] for p in alive):
        tally_votes(room)
    return True, 'ok'


def tally_votes(room):
    votes = room.votes[room.round]
    summary = '，'.join(f'{room.players[v].name}→{room.players[t].name}' for v, t in votes.items())
    room.add_msg(f'投票明细：{summary}')
    counter = Counter(votes.values())
    max_c = max(counter.values())
    cands = [t for t, c in counter.items() if c == max_c]
    # ponytail: 平票随机选一个，省去重投流程
    eliminated = random.choice(cands)
    p = room.players[eliminated]
    p.alive = False
    role_cn = '卧底' if p.role == 'undercover' else '平民'
    room.add_msg(f'【{p.name}】以 {max_c} 票被放逐。身份：{role_cn}。')
    if check_winner(room):
        return
    room.round += 1
    room.phase = 'describe'
    room.speaker_order = [p.token for p in room.alive_players()]
    random.shuffle(room.speaker_order)   # 每轮重新随机发言顺序
    room.speaker_idx = 0
    first = room.players[room.speaker_order[0]]
    room.add_msg(f'第 {room.round} 轮开始。从【{first.name}】描述。')


def check_winner(room):
    alive = room.alive_players()
    uc = [p for p in alive if p.role == 'undercover']
    civ = [p for p in alive if p.role == 'civilian']
    if not uc:
        room.winner = 'civilian'
        room.phase = 'ended'
        room.add_msg(f'游戏结束！平民获胜。平民词：{room.civ_word}，卧底词：{room.uc_word}。')
        return True
    if len(uc) >= len(civ):
        room.winner = 'undercover'
        room.phase = 'ended'
        room.add_msg(f'游戏结束！卧底获胜。平民词：{room.civ_word}，卧底词：{room.uc_word}。')
        return True
    return False


def reset_game(room):
    for p in room.players.values():
        if p.left:          # 离场者不复活，房主可在大厅踢掉
            continue
        p.role = None
        p.word = None
        p.alive = True
        p.ready = False
        p.viewed = False
        p.word_visible_until = 0.0
    room.phase = 'waiting'
    room.round = 0
    room.civ_word = None
    room.uc_word = None
    room.descriptions = {}
    room.votes = {}
    room.speaker_order = []
    room.speaker_idx = 0
    room.winner = None
    room.add_msg('房主开启了新一局。')


def leave_room(room, p):
    """退出房间：大厅直接移除；游戏中离场视为出局。房主空缺自动转让。"""
    was_host = room.host == p.token
    if room.phase == 'waiting':
        del room.players[p.token]
        room.add_msg(f'{p.name} 离开了房间。')
    else:
        p.alive = False
        p.left = True
        room.add_msg(f'{p.name} 中途离场，视为出局。')
        if room.phase == 'describe':
            cur = room.current_speaker()
            room.speaker_order = [t for t in room.speaker_order if t != p.token]
            if cur == p.token:
                # 正轮到他：不递增 idx，下一位已在当前位置（同违规出局）
                if not check_winner(room):
                    _post_describe(room)
            else:
                if cur and cur in room.speaker_order:
                    room.speaker_idx = room.speaker_order.index(cur)
                check_winner(room)
        else:
            check_winner(room)
    # 房主转让
    if was_host:
        rest = [x for x in room.players.values() if not x.left]
        if rest:
            room.host = rest[0].token
            room.add_msg(f'{room.players[room.host].name} 成为新房主。')
    # 全员离场 → 删房间
    if not any(not x.left for x in room.players.values()):
        ROOMS.pop(room.id, None)
        return {'ok': True, 'closed': True}
    return {'ok': True}


# ============================================================
# Action / State
# ============================================================
def handle_action(room, p, body):
    t = body.get('type')
    if t == 'toggle_ready':
        if room.phase != 'waiting':
            return {'err': '游戏已开始'}
        p.ready = not p.ready
        return {'ok': True}
    if t == 'set_uc':
        if room.host != p.token or room.phase != 'waiting':
            return {'err': '只有房主在等待阶段可设置'}
        n = len(room.players)
        room.uc_count = 2 if (int(body.get('count', 1)) == 2 and n >= 5) else 1
        room.add_msg(f'房主设置卧底数：{room.uc_count}')
        return {'ok': True, 'uc_count': room.uc_count}
    if t == 'start_game':
        if room.host != p.token:
            return {'err': '只有房主能开始'}
        if room.phase != 'waiting':
            return {'err': '游戏已开始'}
        n = len(room.players)
        if n < MIN_PLAYERS:
            return {'err': f'至少需要 {MIN_PLAYERS} 人'}
        if room.uc_count == 2 and n < 5:
            room.uc_count = 1
        start_game(room)
        return {'ok': True}
    if t == 'view_word':
        if room.phase in ('waiting', 'ended'):
            return {'err': '当前不能查看词语'}
        view_word(room, p)
        # ponytail: 不返回 role —— 谁是卧底的核心是玩家不知道自己身份，
        # 只能靠听描述推断。出局/结束时才亮。
        return {'ok': True, 'word': p.word, 'visible_for': WORD_REVEAL_SECONDS}
    if t == 'hide_word':
        if room.phase in ('waiting', 'ended'):
            return {'err': '当前没有可隐藏的词语'}
        hide_word(p)
        return {'ok': True}
    if t == 'describe':
        ok, msg = describe(room, p.token, body.get('text', ''))
        return {'ok': ok, 'msg': msg}
    if t == 'skip_describe':
        ok, msg = skip_describe(room, p.token)
        return {'ok': ok, 'msg': msg}
    if t == 'vote':
        ok, msg = vote(room, p.token, body.get('target', ''))
        return {'ok': ok, 'msg': msg}
    if t == 'reset':
        if room.host != p.token:
            return {'err': '只有房主能重置'}
        reset_game(room)
        return {'ok': True}
    if t == 'leave':
        return leave_room(room, p)
    if t == 'kick':
        if room.host != p.token:
            return {'err': '只有房主能踢人'}
        if room.phase != 'waiting':
            return {'err': '只能在大厅踢人'}
        tgt = room.players.get(body.get('target', ''))
        if not tgt or tgt.token == p.token:
            return {'err': '无效目标'}
        del room.players[tgt.token]
        room.add_msg(f'{tgt.name} 被房主移出房间。')
        return {'ok': True}
    return {'err': 'unknown action'}


def find_player(token):
    # ponytail: O(rooms) 找 token，房间数小够用；要多机/规模时再加 token->room 索引
    for r in ROOMS.values():
        if token in r.players:
            return r, r.players[token]
    return None, None


def state_of(room, p, since):
    if not p:
        return {'error': 'invalid token'}
    pending = [m for m in room.public_log if m['id'] > since]
    votes_now = room.votes.get(room.round, {})
    my_vote = votes_now.get(p.token)
    return {
        'roomId': room.id,
        'phase': room.phase,
        'maxPlayers': MAX_PLAYERS,
        'round': room.round,
        'myName': p.name,
        'myToken': p.token,
        # 谁是卧底规则：自己不知道身份，只有结束才亮（放逐消息会公布出局者身份）
        'myRole': p.role if room.phase == 'ended' else None,
        # 只在玩家主动查看后的短窗口内下发，避免词语一直留在页面/轮询响应里
        'myWord': (p.word
                   if room.phase in ('describe', 'vote')
                   and p.word_visible_until > time.time()
                   else None),
        'wordRevealSeconds': WORD_REVEAL_SECONDS,
        'viewed': p.viewed,
        'alive': p.alive,
        'isHost': room.host == p.token,
        'uc_count': room.uc_count,
        'winner': room.winner,
        'civ_word': room.civ_word if room.phase == 'ended' else None,
        'uc_word': room.uc_word if room.phase == 'ended' else None,
        'players': [{'name': x.name, 'token': x.token, 'alive': x.alive,
                     'left': x.left,
                     'role': x.role if room.phase == 'ended' else None,
                     'ready': x.ready, 'viewed': x.viewed,
                     'is_me': x.token == p.token,
                     'is_host': room.host == x.token}
                    for x in room.players.values()],
        'speaker_token': room.current_speaker(),
        'descriptions': room.descriptions.get(room.round, []),
        'all_descriptions': [{'round': r, 'items': items}
                             for r, items in sorted(room.descriptions.items())],
        'voted': my_vote is not None,
        # 投票阶段公示"谁还没投"（只公开进度，不公开投给了谁）
        'pending_voters': ([x.name for x in room.alive_players() if x.token not in votes_now]
                           if room.phase == 'vote' else []),
        'vote_targets': ([{'name': x.name, 'token': x.token}
                          for x in room.alive_players() if x.token != p.token]
                         if (room.phase == 'vote' and p.alive) else []),
        'log': pending,
        'log_next': room.next_mid,
        'alive_count': len(room.alive_players()),
    }


# ============================================================
# HTTP
# ============================================================
class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive：浏览器复用连接，不用每次轮询都重新建连
    # （也少过几次系统代理，减少偶发 502）
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ct):
        try:
            with open(name, 'rb') as f:
                body = f.read()
        except FileNotFoundError:
            return self._j(404, {'err': 'no html'})
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html', '/undercover.html'):
            return self._file('undercover.html', 'text/html; charset=utf-8')
        if u.path == '/api/state':
            q = parse_qs(u.query)
            token = q.get('token', [''])[0]
            since = int(q.get('since', ['0'])[0])
            with LOCK:
                room, p = find_player(token)
                if not room:
                    return self._j(200, {'error': 'invalid token'})
                return self._j(200, state_of(room, p, since))
        return self._j(404, {'err': 'not found'})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return self._j(400, {'err': 'bad json'})
        if u.path == '/api/create':
            name = '1 号'   # 不起名，按加入顺序自动编号
            with LOCK:
                room = Room()
                token = secrets.token_hex(8)
                room.players[token] = Player(name, token)
                room.host = token
                ROOMS[room.id] = room
                room.add_msg(f'{name} 创建了房间。')
                return self._j(200, {'roomId': room.id, 'token': token})
        if u.path == '/api/join':
            rid = (body.get('roomId') or '').strip()
            with LOCK:
                room = ROOMS.get(rid)
                if not room:
                    return self._j(200, {'err': '房间不存在'})
                if room.phase != 'waiting':
                    return self._j(200, {'err': '游戏已开始'})
                if len(room.players) >= MAX_PLAYERS:
                    return self._j(200, {'err': '房间已满'})
                name = f'{len(room.players) + 1} 号'   # 按加入顺序自动编号
                token = secrets.token_hex(8)
                room.players[token] = Player(name, token)
                room.add_msg(f'{name} 加入了房间。')
                return self._j(200, {'token': token, 'roomId': rid})
        if u.path == '/api/action':
            with LOCK:
                room, p = find_player(body.get('token', ''))
                if not room:
                    return self._j(200, {'err': 'invalid token'})
                return self._j(200, handle_action(room, p, body))
        return self._j(404, {'err': 'not found'})


def _lan_ips():
    """列出本机可用地址，方便手机加入。"""
    ips = set()
    try:
        import socket
        # 连一下外部地址触发路由表，拿默认网卡的本地 IP（不真正发包）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith('127.'):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def main():
    print(f'谁是卧底 已启动，端口 {PORT}')
    print(f'  本机访问:   http://127.0.0.1:{PORT}/')
    for ip in _lan_ips():
        print(f'  其他人访问: http://{ip}:{PORT}/')
    print('  提示: 若浏览器打开报 502，是系统代理劫持了内网地址。')
    print('        把该地址加入代理软件直连(bypass)列表，或临时关系统代理即可。')
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
