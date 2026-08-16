"""单独验证预言家查验后能收到私密结果。"""
import urllib.request, json, time

BASE = 'http://127.0.0.1:8000'
def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))
def get(url):
    return json.load(urllib.request.urlopen(url))

# 4 人最小局：1 狼 1 预言家 2 村民
r = post(f'{BASE}/api/create', {'name': 'A'})
rid = r['roomId']
toks = [r['token']]
for n in 'BCD':
    toks.append(post(f'{BASE}/api/join', {'name': n, 'roomId': rid})['token'])
post(f'{BASE}/api/action', {'token': toks[0], 'type': 'start_game'})

# 找预言家和狼人
seer_idx = wolf_idx = None
for i, t in enumerate(toks):
    s = get(f'{BASE}/api/state?token={t}&since=0&chatSince=0')
    if s.get('myRole') == 'seer':
        seer_idx = i
    elif s.get('myRole') == 'wolf':
        wolf_idx = i
print(f'预言家=P{seer_idx} 狼人=P{wolf_idx}')

# 让狼人先刀，推进到预言家阶段
for _ in range(10):
    s = get(f'{BASE}/api/state?token={toks[wolf_idx]}&since=0&chatSince=0')
    for a in (s.get('actions') or []):
        if a['type'] == 'wolf_pick':
            tgt = a['targets'][0]
            post(f'{BASE}/api/action', {'token': toks[wolf_idx], 'type': 'wolf_pick', 'target': tgt['token']})
            print(f'狼人刀了 {tgt["name"]}')
            break
    else:
        time.sleep(0.3)
        continue
    break

# 等到预言家能查验
for _ in range(20):
    s = get(f'{BASE}/api/state?token={toks[seer_idx]}&since=0&chatSince=0')
    acts = s.get('actions') or []
    if any(a['type'] == 'seer_check' for a in acts):
        tgt = acts[0]['targets'][0]
        r = post(f'{BASE}/api/action', {'token': toks[seer_idx], 'type': 'seer_check', 'target': tgt['token']})
        print(f'查验 {tgt["name"]}: {r}')
        time.sleep(0.5)
        s2 = get(f'{BASE}/api/state?token={toks[seer_idx]}&since=0&chatSince=0')
        print(f'私密消息（第1次 poll）: {s2.get("private")}')
        time.sleep(0.5)
        s3 = get(f'{BASE}/api/state?token={toks[seer_idx]}&since=0&chatSince=0')
        print(f'私密消息（第2次 poll，验证 pop 后是否清空）: {s3.get("private")}')
        break
    time.sleep(0.3)
