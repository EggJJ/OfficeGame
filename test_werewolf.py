"""狼人杀自检 —— 直接驱动游戏逻辑，不走 HTTP（同 test_undercover.py 风格）。

用法:
    python3 test_werewolf.py
"""
import werewolf as ww


def make_room(n):
    ww.ROOMS.clear()
    room = ww.Room()
    for i in range(n):
        t = f'tok{i}'
        room.players[t] = ww.Player(f'{i+1}号', t)
    room.host = 'tok0'
    ww.ROOMS[room.id] = room
    return room


def run_start_case():
    """开局：角色配置正确，直接进狼人夜。"""
    room = make_room(6)
    for t in list(room.players)[1:]:
        room.players[t].ready = True
    ww.start_game(room)
    roles = [p.role for p in room.players.values()]
    assert room.phase == 'night_wolf', f'start 后应进 night_wolf, 实际 {room.phase}'
    assert roles.count('wolf') == 2 and roles.count('seer') == 1 \
        and roles.count('witch') == 1, f'6 人局角色配错: {roles}'
    # 每人都有私密身份消息
    assert all(room.private_log[t] for t in room.players), '有人没收到身份'


def run_day_leave_case():
    """回归：白天阶段有人退出不崩溃，发言队列正确推进。
    （原 _advance_current_phase 引用了作用域外的 token/p，白天退出直接 NameError，
    当前发言人退出后整局卡死。）"""
    room = make_room(6)
    # 手动分配角色（走 start_game 是随机的，两次退出可能碰巧清光狼导致提前结束）
    for p, r in zip(room.players.values(),
                    ['wolf', 'wolf', 'seer', 'witch', 'villager', 'villager']):
        p.role = r
    room.phase = 'day'
    room.day_stage = 'speak'
    room.speech_queue = ['tok1', 'tok2']
    room.current_speaker = 'tok0'

    # 非当前发言人退出：从队列剔除，不切换发言人
    ww.leave_room(room, 'tok2')
    assert 'tok2' not in room.speech_queue
    assert room.current_speaker == 'tok0'

    # 当前发言人退出：切到下一位
    ww.leave_room(room, 'tok0')
    assert room.current_speaker == 'tok1', \
        f'发言人退出应切到 tok1, 实际 {room.current_speaker}'
    assert room.phase == 'day'
    print('  白天退出/发言推进 OK')


def run_waiting_leave_case():
    """等待阶段退出：直接移除；空房回收；房主转让。"""
    room = make_room(4)
    ww.leave_room(room, 'tok0')
    assert 'tok0' not in room.players and room.host == 'tok1'
    for t in list(room.players):
        ww.leave_room(room, t)
    assert room.id not in ww.ROOMS, '空房应回收'
    print('  等待退出/转让/空房回收 OK')


def main():
    run_start_case()
    print('  开局配置 OK')
    run_day_leave_case()
    run_waiting_leave_case()
    print('PASS')


if __name__ == '__main__':
    main()
