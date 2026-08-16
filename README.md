# OfficeGame

伪装成办公文档的内网小游戏合集：**狼人杀** + **谁是卧底**。纯 Python 标准库，零依赖，一个命令开玩。

## 两个游戏

| 游戏 | 人数 | 玩法 |
|------|------|------|
| 狼人杀 | 4-12 人 | 狼人 / 预言家 / 女巫 / 猎人 / 村民，夜晚行动 + 白天发言投票，屠神规则判胜 |
| 谁是卧底 | 4-5 人 | 每人一个词（卧底词不同），轮流描述猜身份，描述含词中字直接出局 |

两个游戏都是房间制（6 位房间号）、自动编号（1号 2号…）、纯文字交互、自动判定胜负。

## 快速开始

需要 Python 3.7+，无任何 pip 依赖。

```bash
# 方式一：合集（推荐）—— 一个进程跑 大厅 + 两个游戏
python3 gamehub/main.py
# 打开 http://本机IP:8080 进大厅选游戏
# 狼人杀 :8000  谁是卧底 :8001（端口被占自动向后找）

# 方式二：单游戏独立运行
python3 werewolf.py 8000     # 狼人杀
python3 undercover.py 8001   # 谁是卧底
```

同一 WiFi 下的手机 / 电脑直接访问启动时打印的内网地址即可加入。

## 公网联机（ngrok）

```bash
python3 gamehub/main.py single 8080   # 单端口模式（免费版隧道只映射一个端口）
ngrok http 8080                       # 需先 ngrok config add-authtoken <token>
```

把 ngrok 给的 `https://xxx.ngrok-free.app` 分享给朋友，从大厅进两个游戏。
免费版每次重启地址会变；玩家首次打开会点一次 "Visit Site" 提示页。

## 目录结构

```
werewolf.py / index.html        狼人杀（服务端 + 前端，单文件各一）
undercover.py / undercover.html 谁是卧底（同上）
gamehub/main.py                 合集启动器：单进程三服务，HTML 软链复用
gamehub/test_hub.py             合集冒烟测试
test_*.py                       各游戏逻辑自检（不走 HTTP，直接跑）
```

## 测试

```bash
python3 test_werewolf.py && python3 test_undercover.py
cd gamehub && python3 test_hub.py
```

## 说明

- 全部状态在内存，单进程；内网几十人够用，要扩展时再加持久化
- 游戏逻辑单文件零框架，`gamehub` 通过 import + 软链复用原始文件，不复制代码
