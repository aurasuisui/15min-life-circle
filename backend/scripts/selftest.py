"""无第三方依赖的自测运行器。

用一份极小的 pytest shim（approx / raises）跑同一套 tests/test_*.py，
这样在没装 pytest 的环境下也能验证算法正确性。

用法： python scripts/selftest.py
"""
from __future__ import annotations

import importlib.util
import math
import sys
import traceback
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


# ---------------- pytest shim ----------------
class _Approx:
    def __init__(self, expected, rel=None, abs=None):
        self.expected, self.rel, self.abs = expected, rel, abs

    def __eq__(self, other):
        if self.rel is not None:
            return math.isclose(other, self.expected, rel_tol=self.rel)
        if self.abs is not None:
            return math.isclose(other, self.expected, abs_tol=self.abs)
        return math.isclose(other, self.expected, rel_tol=1e-9)

    def __repr__(self):
        return f"approx({self.expected}, rel={self.rel}, abs={self.abs})"


class _Raises:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            raise AssertionError(f"DID NOT RAISE {self.exc.__name__}")
        return issubclass(et, self.exc)


def _install_shim() -> None:
    m = types.ModuleType("pytest")
    m.approx = lambda expected, rel=None, abs=None: _Approx(expected, rel, abs)
    m.raises = lambda exc: _Raises(exc)
    m.fixture = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))

    class _Mark:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda f: f)

    m.mark = _Mark()
    sys.modules["pytest"] = m


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    _install_shim()
    test_files = sorted((BACKEND / "tests").glob("test_*.py"))
    if not test_files:
        print("no test files found")
        return 1

    passed, failed = 0, 0
    for tf in test_files:
        try:
            mod = _load(tf)
        except Exception:
            print(f"[LOAD FAIL] {tf.name}")
            traceback.print_exc()
            failed += 1
            continue

        names = [n for n in dir(mod) if n.startswith("test_")]
        print(f"\n== {tf.name} ({len(names)} tests) ==")
        for name in names:
            try:
                getattr(mod, name)()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed += 1

    print(f"\n{'=' * 46}")
    print(f"  passed={passed}  failed={failed}")
    print("=" * 46)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
