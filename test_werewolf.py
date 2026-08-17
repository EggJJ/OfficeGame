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
    """开局：4~12 全部人数档，角色配置正确，直接进狼人夜。"""
    from collections import Counter
    for n in range(4, 13):
        room = make_room(n)
        ww.start_game(room)
        roles = [p.role for p in room.players.values()]
        assert room.phase == 'night_wolf', f'{n} 人 start 后应进 night_wolf, 实际 {room.phase}'
        assert Counter(roles) == Counter(ww.ROLE_CONFIGS[n]), \
            f'{n} 人局角色配错: {sorted(roles)}'
        assert all(room.private_log[t] for t in room.players), f'{n} 人局有人没收到身份'
    print('  4~12 人默认配置 OK')


def run_custom_roles_case():
    """房主自定义角色配置：生效、开局播报、边界被拒。"""
    room = make_room(7)
    room.role_counts = {'wolf': 3, 'seer': 1, 'witch': 1, 'hunter': 0}
    ww.start_game(room)
    roles = [p.role for p in room.players.values()]
    assert roles.count('wolf') == 3 and roles.count('villager') == 2, \
        f'自定义 7 人 3 狼配置错误: {roles}'
    assert any('本局配置' in m['text'] for m in room.public_log), '开局应播报配置'

    # 边界：狼 0 / 狼过多 / 神职全删 / 神职 >1，都应被拒
    for bad, why in [
        ({'wolf': 0, 'seer': 1, 'witch': 1, 'hunter': 0}, '狼为 0'),
        ({'wolf': 6, 'seer': 1, 'witch': 1, 'hunter': 0}, '狼太多'),
        ({'wolf': 2, 'seer': 0, 'witch': 0, 'hunter': 0}, '无神职'),
        ({'wolf': 2, 'seer': 2, 'witch': 1, 'hunter': 0}, '双预言家'),
    ]:
        c = {**bad}
        err = ww.validate_role_counts(c, 7)
        assert err, f'{why} 应被拒绝'
    # 恢复默认：清空自定义后回到人数默认表
    room.role_counts = None
    c = ww.effective_role_counts(room)
    assert c == {'wolf': 2, 'seer': 1, 'witch': 1, 'hunter': 0}, f'恢复默认错误: {c}'
    print('  自定义角色配置/边界/恢复默认 OK')


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
    run_custom_roles_case()
    run_day_leave_case()
    run_waiting_leave_case()
    print('PASS')


if __name__ == '__main__':
    main()
