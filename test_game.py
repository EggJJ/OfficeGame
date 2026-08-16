"""自动跑一局狼人杀，验证流程 + 预言家查验显示。"""
import urllib.request, json, time, random, sys

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6
BASE = 'http://127.0.0.1:8000'

def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))

def get(url):
    return json.load(urllib.request.urlopen(url))

def state_of(tok, since, csince):
    return get(f'{BASE}/api/state?token={tok}&since={since}&chatSince={csince}')

def do_act(tok, body):
    body.setdefault('token', tok)
    return post(f'{BASE}/api/action', body)

# 建房 + N 人加入
r = post(f'{BASE}/api/create', {'name': 'P0'})
rid = r['roomId']
tokens = [r['token']]
for i in range(1, N):
    tokens.append(post(f'{BASE}/api/join', {'name': f'P{i}', 'roomId': rid})['token'])
print(f'房间 {rid}，{N} 人就位')

# 开局前：所有非房主准备，房主再开始
for t in tokens[1:]:
    do_act(t, {'type': 'toggle_ready'})
print('ready:', do_act(tokens[0], {'type': 'start_game'}))

since = [0] * N
csince = [0] * N
roles = {}

def handle(i, tok, s):
    """处理一个玩家的可用动作，返回是否做了操作"""
    acted = False
    me = f'P{i}'
    for a in (s.get('actions') or []):
        t = a['type']
        if t == 'start_game':
            continue
        if t == 'wolf_pick':
            tgt = random.choice(a['targets'])
            r = do_act(tok, {'type': 'wolf_pick', 'target': tgt['token']})
            print(f'  {me}[狼] 刀 {tgt["name"]}: {r}')
            acted = True
        elif t == 'seer_check':
            tgt = random.choice(a['targets'])
            r = do_act(tok, {'type': 'seer_check', 'target': tgt['token']})
            print(f'  {me}[预言家] 查 {tgt["name"]}: {r}')
            acted = True
        elif t == 'witch_act':
            save = False
            poison = None
            if a.get('save_target') and not a.get('witch_save_used'):
                save = True  # 必救，测一下
            if not a.get('witch_poison_used') and a.get('poison_targets'):
                if random.random() < 0.4:
                    poison = random.choice(a['poison_targets'])['token']
            r = do_act(tok, {'type': 'witch_act', 'save': save, 'poison': poison})
            print(f'  {me}[女巫] 救={save} 毒={poison}: {r}')
            acted = True
        elif t == 'end_speech':
            r = do_act(tok, {'type': 'end_speech'})
            print(f'  {me} 结束发言: {r}')
            acted = True
        elif t == 'start_vote':
            r = do_act(tok, {'type': 'start_vote'})
            print(f'  {me} 发起投票: {r}')
            acted = True
        elif t == 'cast_vote':
            tgt = random.choice(a['targets']) if a.get('targets') else None
            r = do_act(tok, {'type': 'cast_vote', 'target': tgt['token'] if tgt else None})
            print(f'  {me} 投票 {tgt["name"] if tgt else "弃"}: {r}')
            acted = True
        elif t == 'hunter_shoot':
            tgt = random.choice(a['targets']) if a.get('targets') and random.random() < 0.5 else None
            r = do_act(tok, {'type': 'hunter_shoot', 'target': tgt['token'] if tgt else None})
            print(f'  {me}[猎人] 开枪 {tgt["name"] if tgt else "不开"}: {r}')
            acted = True
    return acted

last_phase = None
winner = None
for it in range(300):
    cur_phase = None
    for i, tok in enumerate(tokens):
        s = state_of(tok, since[i], csince[i])
        cur_phase = s.get('phase')
        # 记录角色
        if s.get('myRole') and tok not in roles:
            roles[tok] = s['myRole']
            print(f'  P{i} 的身份: {s["myRoleName"]}')
        # 累积日志
        for m in (s.get('log') or []):
            print(f'    [日志#{m["id"]}] {m["text"]}')
            since[i] = max(since[i], m['id'])
        for m in (s.get('chat') or []):
            csince[i] = max(csince[i], m['id'])
        # 预言家私密信息（验证累积）
        if i == next((k for k, v in roles.items() if v == 'seer'), None):
            if s.get('private'):
                for p in s['private']:
                    print(f'    [预言家私密] {p}')
        handle(i, tok, s)
        if s.get('phase') == 'ended':
            winner = s.get('winner')
            break
    if cur_phase != last_phase:
        print(f'== 阶段: {cur_phase} ==')
        last_phase = cur_phase
    if winner:
        break
    time.sleep(0.4)

print(f'\n游戏结束，胜方: {winner}')
# 终局所有人身份
s = state_of(tokens[0], 0, 0)
print('终局身份:')
for p in s['players']:
    print(f'  {p["name"]}: {p.get("role")} 存活={p.get("alive")}')
