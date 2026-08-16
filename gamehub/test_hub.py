#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合集冒烟测试：三个服务全起一遍，大厅页 + 两个游戏的建房/轮询各走一轮。

    python3 test_hub.py
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import main as hub
import werewolf
import undercover


def http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read()


def serve(handler):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run():
    wolf = serve(werewolf.Handler)
    uc = serve(undercover.Handler)
    hub.HubHandler.page = hub.hub_page(wolf.server_address[1], uc.server_address[1])
    h = serve(hub.HubHandler)
    url = lambda s, path='': f'http://127.0.0.1:{s.server_address[1]}{path}'

    # 大厅页：包含两个游戏入口和实际端口
    page = http('GET', url(h, '/')).decode()
    assert '狼人杀' in page and '谁是卧底' in page
    assert str(wolf.server_address[1]) in page and str(uc.server_address[1]) in page

    # 狼人杀：建房 → 拿状态
    tok = json.loads(http('POST', url(wolf, '/api/create'), {}))['token']
    st = json.loads(http('GET', url(wolf, f'/api/state?token={tok}&since=0&chatSince=0&wolfChatSince=0')))
    assert st['phase'] == 'waiting' and st['isHost']

    # 谁是卧底：建房 → 拿状态
    tok = json.loads(http('POST', url(uc, '/api/create'), {}))['token']
    st = json.loads(http('GET', url(uc, f'/api/state?token={tok}&since=0&chatSince=0')))
    assert st['phase'] == 'waiting' and st['isHost']

    # 两个游戏 html 都能出（软链生效）
    assert b'<html' in http('GET', url(wolf, '/')).lower()
    assert b'<html' in http('GET', url(uc, '/')).lower()

    # ---- 单端口模式（ngrok 穿透用）----
    hub.ProxyHandler.upstream = {'/werewolf': wolf.server_address[1],
                                 '/undercover': uc.server_address[1]}
    hub.ProxyHandler.page = hub.hub_page_single()
    sp = serve(hub.ProxyHandler)

    page = http('GET', url(sp, '/')).decode()
    assert '/werewolf/' in page and '/undercover/' in page  # 相对路径链接
    # HTML 被重写：API 前缀生效 + 注入了 ngrok 请求头补丁
    w_html = http('GET', url(sp, '/werewolf/'))
    assert b'/werewolf/api/' in w_html and b'ngrok-skip-browser-warning' in w_html
    # 前缀下的完整流程：建房 → 状态
    tok = json.loads(http('POST', url(sp, '/werewolf/api/create'), {}))['token']
    st = json.loads(http('GET', url(sp, f'/werewolf/api/state?token={tok}&since=0&chatSince=0&wolfChatSince=0')))
    assert st['phase'] == 'waiting'
    tok = json.loads(http('POST', url(sp, '/undercover/api/create'), {}))['token']
    st = json.loads(http('GET', url(sp, f'/undercover/api/state?token={tok}&since=0&chatSince=0')))
    assert st['phase'] == 'waiting'
    assert b'/undercover/api/' in http('GET', url(sp, '/undercover/'))

    print('test_hub: 全部通过')


if __name__ == '__main__':
    run()
