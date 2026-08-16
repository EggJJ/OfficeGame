"""谁是卧底 自检 —— 直接驱动游戏逻辑，不走 HTTP（避免环境代理/沙箱坑）。

用法:
    python3 test_undercover.py
"""
import random
import undercover as uc


def make_room(n, uc_count):
    room = uc.Room()
    for i in range(n):
        t = f'tok{i}'
        room.players[t] = uc.Player(f'P{i}', t)
    room.host = 'tok0'
    room.uc_count = uc_count if (uc_count == 2 and n >= 5) else 1
    return room


def run_one(n, uc_count):
    random.seed()  # 真随机，覆盖不同分配
    room = make_room(n, uc_count)
    uc.start_game(room)

    # 1. 身份与词语分配正确
    roles = [p.role for p in room.players.values()]
    uc_n = sum(1 for r in roles if r == 'undercover')
    assert uc_n == room.uc_count, f'{n}p/{room.uc_count}uc: 卧底数错 {uc_n}'

    civ_words = {p.word for p in room.players.values() if p.role == 'civilian'}
    uc_words = {p.word for p in room.players.values() if p.role == 'undercover'}
    assert len(civ_words) == 1, f'平民词不一致: {civ_words}'
    assert len(uc_words) == 1, f'卧底词不一致: {uc_words}'
    assert civ_words != uc_words, '平民词与卧底词相同'
    assert room.phase == 'describe', f'start 后应直接进 describe, 实际 {room.phase}'
    assert room.current_speaker() == list(room.players.keys())[0], '从 1 号开始'

    # 2. 没看词不能描述；看完即可描述，不等别人
    first_sp = room.current_speaker()
    ok, msg = uc.describe(room, first_sp, '一个东西')
    assert not ok and '查看' in msg, f'未看词应被拒: {msg}'
    uc.view_word(room, room.players[first_sp])
    ok, msg = uc.describe(room, first_sp, '一种常见的事物')
    assert ok, f'看完应能立刻描述: {msg}'

    # 3. 跑完整局：描述 → 投票 → 直到 ended
    safe_descs = ['一种常见的事物', '大家应该都知道', '生活中会接触',
                  '和某个场景相关', '不太好描述']
    # 这些句子的字与词库任一词都不相交（已人工核对）
    rounds = 0
    while room.phase != 'ended':
        if room.phase == 'describe':
            sp = room.current_speaker()
            if sp:
                uc.view_word(room, room.players[sp])   # 轮到谁谁看，不等别人
                uc.describe(room, sp, random.choice(safe_descs))
        elif room.phase == 'vote':
            for p in list(room.alive_players()):
                cands = [x.token for x in room.alive_players() if x.token != p.token]
                if cands and p.token not in room.votes[room.round]:
                    uc.vote(room, p.token, random.choice(cands))
        rounds += 1
        assert rounds < 200, '超时未结束'
    assert room.winner in ('civilian', 'undercover'), '未决出胜负'
    return room.winner, list(civ_words)[0], list(uc_words)[0]


def run_word_leak_case():
    """描述含词中字 → 该玩家直接出局。"""
    room = make_room(4, 1)
    uc.start_game(room)
    for t in room.players:
        uc.view_word(room, room.players[t])
    speaker = room.current_speaker()
    p = room.players[speaker]
    # 描述直接含词中第一个字
    leak_text = p.word[0] + '是个东西'
    uc.describe(room, speaker, leak_text)
    assert not p.alive, '违规玩家应已出局'
    print(f'  违规检测 OK：{p.name} 描述 "{leak_text}" 含 "{p.word[0]}" → 出局')


def run_reset_case():
    room = make_room(4, 1)
    uc.start_game(room)
    uc.reset_game(room)
    assert room.phase == 'waiting', '重置后应回 waiting'
    for p in room.players.values():
        assert p.alive and p.word is None and p.role is None
    print('  重置 OK')


def run_leave_kick_case():
    uc.ROOMS.clear()
    room = make_room(5, 1)
    uc.ROOMS[room.id] = room

    # 大厅踢人：房主踢非自己
    r = uc.handle_action(room, room.players['tok0'], {'type': 'kick', 'target': 'tok2'})
    assert r.get('ok') and 'tok2' not in room.players, f'kick 失败: {r}'

    # 非房主踢人被拒
    r = uc.handle_action(room, room.players['tok1'], {'type': 'kick', 'target': 'tok3'})
    assert r.get('err'), '非房主不应能踢人'

    # 大厅房主退出 → 房主转让给最早加入的剩余玩家
    r = uc.handle_action(room, room.players['tok0'], {'type': 'leave'})
    assert r.get('ok') and 'tok0' not in room.players
    assert room.host == 'tok1', f'房主应转让给 tok1，实际 {room.host}'

    # 游戏中正轮到描述的人离场 → 视为出局，流程不卡
    room.players['tok5'] = uc.Player('5 号', 'tok5')   # 补到 4 人
    uc.start_game(room)
    for x in room.players.values():
        uc.view_word(room, x)
    leaving = room.current_speaker()
    r = uc.handle_action(room, room.players[leaving], {'type': 'leave'})
    assert r.get('ok'), f'游戏中 leave 失败: {r}'
    assert not room.players[leaving].alive and room.players[leaving].left
    assert room.phase in ('describe', 'vote', 'ended')
    if room.phase == 'describe':
        assert room.current_speaker() != leaving, '轮到的离场者应被跳过'
    # 剩余玩家还能继续跑（至少能再描述一步/投票不报错）
    print('  踢人/退出/转让 OK')


def run_empty_room_case():
    uc.ROOMS.clear()
    room = make_room(4, 1)
    uc.ROOMS[room.id] = room
    for t in list(room.players):
        uc.handle_action(room, room.players[t], {'type': 'leave'})
    assert room.id not in uc.ROOMS, '全员离场应删除房间'
    print('  空房回收 OK')


def main():
    print('--- 4 人 1 卧底 ---')
    w, c, u = run_one(4, 1)
    print(f'  胜者={w}  平民词={c}  卧底词={u}')
    print('--- 5 人 1 卧底 ---')
    w, c, u = run_one(5, 1)
    print(f'  胜者={w}  平民词={c}  卧底词={u}')
    print('--- 5 人 2 卧底 ---')
    w, c, u = run_one(5, 2)
    print(f'  胜者={w}  平民词={c}  卧底词={u}')
    print('--- 违规出局 ---')
    run_word_leak_case()
    print('--- 重置 ---')
    run_reset_case()
    print('--- 踢人/退出 ---')
    run_leave_kick_case()
    run_empty_room_case()
    print('PASS')


if __name__ == '__main__':
    main()
