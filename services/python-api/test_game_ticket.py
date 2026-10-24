import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proxy


def test_per_user_budget():
    proxy.GAME_TICKET_ATTEMPTS.clear()
    assert all(proxy.allow_game_ticket('member-a', now=100) for _ in range(10))
    assert not proxy.allow_game_ticket('member-a', now=101)
    assert proxy.allow_game_ticket('member-b', now=101)
    assert proxy.allow_game_ticket('member-a', now=401)


def test_online_presence_expires():
    with proxy.ONLINE_VISITORS_LOCK:
        proxy.ONLINE_VISITORS.clear()
    assert proxy.record_online_visitor('a' * 36, now=100) == 1
    assert proxy.record_online_visitor('b' * 36, now=150) == 2
    assert proxy.record_online_visitor('b' * 36, now=191) == 1


def demo():
    test_per_user_budget()
    test_online_presence_expires()
    print('game ticket rate limit and online presence: PASS')


if __name__ == '__main__':
    demo()
