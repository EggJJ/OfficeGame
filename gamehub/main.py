#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
游戏合集（狼人杀 + 谁是卧底）—— 单进程启动器
================================================

部署:
    python3 main.py [大厅端口] [狼人杀端口] [卧底端口]
    默认 8080 / 8000 / 8001，端口被占自动向后找空闲端口。

    python3 main.py single [端口]        单端口模式，默认 8080。
    所有流量走一个端口（/werewolf/、/undercover/ 路径前缀区分），
    公网穿透（ngrok 等）必须用这个模式——免费版隧道只映射一个端口。

公网（ngrok）:
    python3 main.py single 8080
    ngrok http 8080
    把 ngrok 给的 https://xxx.ngrok-free.app 分享给朋友即可，
    大厅页链接为相对路径，穿透后不用改任何东西。

说明:
    - 一个 Python 进程同时跑三个 HTTP 服务，比两个独立进程省一份解释器内存
    - 游戏逻辑直接 import 上层目录的原始单文件，零复制、零改动
      （import 前清空 sys.argv，防止游戏模块把合集的参数误读成端口）
    - 两个 html 是软链：改原文件，合集立即生效
    - 端口占用自动 +1 重试，大厅页里的游戏链接按实际端口生成，不会指错
"""

import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)
os.chdir(HERE)  # undercover 按 cwd 读 undercover.html → 固定命中本目录软链

# 游戏模块 import 时会把 sys.argv[1] 当自己端口（如 'single' 会 int() 崩），
# 先取走本脚本参数并清空 argv，再 import
_ARGS = sys.argv[1:]
sys.argv = [sys.argv[0]]

import werewolf
import undercover

# ---- 对原文件打的运行时补丁（不动原文件）----
# 1) 狼人杀页面固定从本目录软链读
werewolf._INDEX_CANDIDATES = [os.path.join(HERE, 'index.html')]
# 2) 开 HTTP/1.1 keep-alive：它的每个响应都带 Content-Length，
#    1.5s 一次的轮询不用反复建 TCP 连接（卧底本来就开了）
werewolf.Handler.protocol_version = 'HTTP/1.1'


# ============================================================
# 大厅
# ============================================================
_HUB_SHELL = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>游戏合集</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f5f6f8; margin: 0; min-height: 100vh;
         display: flex; align-items: center; justify-content: center; }
  .wrap { padding: 32px 16px; width: 100%; max-width: 720px; box-sizing: border-box; }
  h1 { font-size: 22px; color: #1f2329; text-align: center; margin: 0 0 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
  a.card { display: block; background: #fff; border: 1px solid #e5e6eb; border-radius: 12px;
           padding: 24px; text-decoration: none; color: #1f2329;
           transition: box-shadow .15s, transform .15s; }
  a.card:hover { box-shadow: 0 4px 16px rgba(0,0,0,.08); transform: translateY(-2px); }
  a.card:active { transform: none; }
  .card h2 { margin: 0 0 8px; font-size: 18px; }
  .card p { margin: 0; color: #86909c; font-size: 14px; line-height: 1.6; }
  .go { margin-top: 16px; color: #3370ff; font-size: 14px; font-weight: 600; }
  .tip { text-align: center; color: #c9cdd4; font-size: 12px; margin-top: 24px; }
</style></head><body>
<div class="wrap">
  <h1>游戏合集</h1>
  <div class="grid" id="g"></div>
  <div class="tip">__TIP__</div>
</div>
<script>
__SCRIPT__
</script>
</body></html>
"""

# 多端口模式：链接指向同主机的游戏端口
_SCRIPT_PORTS = """
const G = [
  {t:'狼人杀',   d:'4-12 人 · 身份对抗 · 预言家 / 女巫 / 猎人', p:__W__},
  {t:'谁是卧底', d:'4-5 人 · 轮流描述词语 · 揪出词不同的卧底',   p:__U__},
];
document.getElementById('g').innerHTML = G.map(g =>
  '<a class="card" href="http://' + location.hostname + ':' + g.p + '/">' +
  '<h2>' + g.t + '</h2><p>' + g.d + '</p>' +
  '<div class="go">进入游戏 →</div></a>').join('');
"""

# 单端口模式：相对路径链接，穿透后同域名直达
_SCRIPT_PATHS = """
const G = [
  {t:'狼人杀',   d:'4-12 人 · 身份对抗 · 预言家 / 女巫 / 猎人', h:'/werewolf/'},
  {t:'谁是卧底', d:'4-5 人 · 轮流描述词语 · 揪出词不同的卧底',   h:'/undercover/'},
];
document.getElementById('g').innerHTML = G.map(g =>
  '<a class="card" href="' + g.h + '">' +
  '<h2>' + g.t + '</h2><p>' + g.d + '</p>' +
  '<div class="go">进入游戏 →</div></a>').join('');
"""


class HubHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    page = b''  # 启动时按实际端口生成后注入

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = self.page
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass


def hub_page(w_port, u_port):
    return (_HUB_SHELL
            .replace('__TIP__', '内网联机 · 手机电脑同 WiFi 即可加入')
            .replace('__SCRIPT__', _SCRIPT_PORTS.replace('__W__', str(w_port)).replace('__U__', str(u_port)))
            .encode('utf-8'))


def hub_page_single():
    return (_HUB_SHELL
            .replace('__TIP__', '把本页链接分享给朋友 · 一起开黑')
            .replace('__SCRIPT__', _SCRIPT_PATHS)
            .encode('utf-8'))


# ============================================================
# 单端口模式：路径前缀代理
# ============================================================
# ngrok 免费版会拦截不带该请求头的 fetch（返回防爬虫页），
# 页面又不能改，只能在代理层给所有游戏的 HTML 注入这段补丁。本地直连时无害。
_FETCH_PATCH = (b"<script>(function(f){window.fetch=function(u,o){o=o||{};"
                b"o.headers=Object.assign({},o.headers||{},"
                b"{'ngrok-skip-browser-warning':'1'});return f(u,o)}})(window.fetch)</script>")


class ProxyHandler(BaseHTTPRequestHandler):
    """单端口模式：/ → 大厅，/werewolf/*、/undercover/* → 本机两个游戏。
    HTML 响应重写 API 路径（/api/ → /前缀/api/）再下发。"""
    protocol_version = 'HTTP/1.1'
    page = b''
    upstream = {}   # '/prefix' -> 本机端口

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        u = urlparse(self.path)
        if u.path in ('', '/'):
            return self._reply(200, self.page, 'text/html; charset=utf-8')
        for prefix, port in self.upstream.items():
            if u.path == prefix or u.path.startswith(prefix + '/'):
                sub = u.path[len(prefix):] or '/'
                target = sub + (('?' + u.query) if u.query else '')
                return self._forward(port, prefix, target)
        self._reply(404, b'not found', 'text/plain')

    def _forward(self, port, prefix, target):
        length = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(f'http://127.0.0.1:{port}{target}',
                                     data=body, method=self.command)
        if body is not None:
            req.add_header('Content-Type', self.headers.get('Content-Type', 'application/json'))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code = r.status
                ctype = r.headers.get('Content-Type', 'text/plain')
                data = r.read()
        except urllib.error.HTTPError as e:
            code = e.code
            ctype = e.headers.get('Content-Type', 'text/plain')
            data = e.read()
        except Exception:
            return self._reply(502, b'upstream error', 'text/plain')
        if 'text/html' in (ctype or ''):
            data = data.replace(b'/api/', prefix.encode() + b'/api/')
            data = data.replace(b'<head>', b'<head>' + _FETCH_PATCH, 1)
        self._reply(code, data, ctype)

    def _reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass


# ============================================================
# 公共后台
# ============================================================
def cleanup_idle_rooms(interval=300, max_idle=7200):
    """两个游戏共用：回收 2 小时空闲的等待中房间，以及全员已离开的房间
    （狼人杀结束局全员走光后房间会一直留在内存，卧底原本连等待房都不回收）。"""
    while True:
        time.sleep(interval)
        now = time.time()
        for mod in (werewolf, undercover):
            with mod.LOCK:
                stale = [rid for rid, r in mod.ROOMS.items()
                         if (r.phase == 'waiting' and now - r.created_at > max_idle)
                         or all(p.left for p in r.players.values())]
                for rid in stale:
                    mod.ROOMS.pop(rid, None)


def bind_server(handler_cls, port, tries=20, host='0.0.0.0'):
    """绑端口，被占用则向后逐个尝试（port=0 表示系统随机分配）。"""
    for p in range(port, port + tries):
        try:
            return ThreadingHTTPServer((host, p), handler_cls), p
        except OSError:
            continue
    sys.exit(f'端口 {port}~{port + tries - 1} 都被占用，请指定其他端口')


# ============================================================
# 启动
# ============================================================
def run_single(port):
    """单端口模式：游戏只绑本机回环（不直接对外），统一由代理端口进出。
    公网穿透（ngrok 免费版一条隧道一个端口）用这个模式。"""
    wolf_srv, _ = bind_server(werewolf.Handler, 0, host='127.0.0.1')
    uc_srv, _ = bind_server(undercover.Handler, 0, host='127.0.0.1')
    ProxyHandler.upstream = {'/werewolf': wolf_srv.server_address[1],
                             '/undercover': uc_srv.server_address[1]}
    ProxyHandler.page = hub_page_single()
    hub_srv, hub_p = bind_server(ProxyHandler, port)

    threading.Thread(target=wolf_srv.serve_forever, daemon=True).start()
    threading.Thread(target=uc_srv.serve_forever, daemon=True).start()
    threading.Thread(target=cleanup_idle_rooms, daemon=True).start()

    ip = werewolf.get_local_ip()
    print('=' * 50)
    print('  游戏合集已启动（单端口模式）')
    print(f'  入口:   http://127.0.0.1:{hub_p}   (内网 http://{ip}:{hub_p})')
    print('  路径:   /            大厅')
    print('          /werewolf/   狼人杀')
    print('          /undercover/ 谁是卧底')
    print(f'  公网:   ngrok http {hub_p}   然后分享 ngrok 给的 https 地址')
    print('  按 Ctrl+C 停止')
    print('=' * 50)
    try:
        hub_srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')


def main():
    if _ARGS and _ARGS[0] == 'single':
        return run_single(int(_ARGS[1]) if len(_ARGS) > 1 else 8080)
    hub_p = int(_ARGS[0]) if _ARGS else 8080
    wolf_p = int(_ARGS[1]) if len(_ARGS) > 1 else 8000
    uc_p = int(_ARGS[2]) if len(_ARGS) > 2 else 8001

    # 先绑游戏（拿到实际端口），再生成大厅页
    wolf_srv, wolf_p = bind_server(werewolf.Handler, wolf_p)
    uc_srv, uc_p = bind_server(undercover.Handler, uc_p)
    HubHandler.page = hub_page(wolf_p, uc_p)
    hub_srv, hub_p = bind_server(HubHandler, hub_p)

    threading.Thread(target=wolf_srv.serve_forever, daemon=True).start()
    threading.Thread(target=uc_srv.serve_forever, daemon=True).start()
    threading.Thread(target=cleanup_idle_rooms, daemon=True).start()

    ip = werewolf.get_local_ip()
    print('=' * 50)
    print('  游戏合集已启动（单进程）')
    print(f'  大厅入口:   http://127.0.0.1:{hub_p}')
    print(f'              http://{ip}:{hub_p}')
    print(f'  狼人杀:     端口 {wolf_p}')
    print(f'  谁是卧底:   端口 {uc_p}')
    print(f'  内存: 一个进程跑全部（原为两个独立进程）')
    print('  按 Ctrl+C 停止')
    print('=' * 50)
    try:
        hub_srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')


if __name__ == '__main__':
    main()
