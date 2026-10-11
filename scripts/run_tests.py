"""Run a tests/ directory without pytest: unittest test cases plus plain pytest-style `test_*` functions.

scripts/team.py uses it when pytest is not installed, so a venv synced without the dev group does not make every
trial merge look red. Exit code 0 only if at least one test ran and none failed.

    python scripts/run_tests.py [tests]
"""
import importlib.util
import inspect
import sys
import traceback
import unittest
from pathlib import Path


def main(folder: str = "tests") -> int:
    root = Path(folder).resolve()
    sys.path[:0] = [str(root.parent), str(root)]
    suite, plain = unittest.TestSuite(), []
    for path in sorted(root.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - an import error is a failed test file
            traceback.print_exc()
            return 1
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
        plain += [(path.stem, name, fn) for name, fn in vars(module).items()
                  if name.startswith("test_") and inspect.isfunction(fn) and not inspect.signature(fn).parameters]
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    failed = len(result.failures) + len(result.errors)
    for module, name, fn in plain:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {module}.{name}")
            traceback.print_exc()
    ran = result.testsRun + len(plain)
    print(f"{ran} tests, {failed} failed")
    return 0 if ran and not failed else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
