try:
    from ._bootstrap import ensure_local_venv
except ImportError:
    from _bootstrap import ensure_local_venv

ensure_local_venv()

import unittest


def test_run_all_local_tests() -> None:
    """PyCharm 可以直接点这个函数左侧箭头，运行本项目全部本地测试。"""
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    test_run_all_local_tests()
