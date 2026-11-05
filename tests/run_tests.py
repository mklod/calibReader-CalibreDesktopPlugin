# Last modified: 2026-11-05--0414
#
# run_tests.py — runs every CalibReader Sync test and REFUSES a run that executed nothing.
#
#   python3 tests/run_tests.py              the in-file tests of sidecar_merge.py and
#                                           sync_core.py (+ the real-calibre tests, skipped)
#   tests/run_calibre.sh                    the same under calibre's own python
#                                           (calibre-debug), real-calibre tests included
#
# Why a runner: the unit tests live in each module's `if __name__ == '__main__':` block
# (so they ship with the plugin and `python3 sync_core.py` runs them). Under calibre
# 9.14, `calibre-debug -e sync_core.py` reports "Ran 0 tests" for that block: unittest.main
# looks for the tests in sys.modules['__main__'], which is calibre-debug's own module, not
# the file it executed. A "green" run of 0 tests is VOID. This runner executes each file
# with runpy (run_name='__main__'), catches the unittest.main call, loads the TestCase
# classes that block defined, and exits non-zero when nothing ran.
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

import importlib.util
import os
import runpy
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
IN_FILE = ('sidecar_merge.py', 'sync_core.py')
MODULES = ('test_real_calibre.py', 'test_kobo_position.py', 'test_kobo_import.py', 'test_kobo_push.py')


def in_file_suite(path):
    '''The TestCase classes defined in `path`'s __main__ block, as one suite.'''
    captured = {}
    real_main = unittest.main

    def grab(*args, **kwargs):
        captured.update(sys._getframe(1).f_globals)
    unittest.main = grab
    try:
        runpy.run_path(path, run_name='__main__')
    finally:
        unittest.main = real_main
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for value in captured.values():
        if isinstance(value, type) and issubclass(value, unittest.TestCase):
            suite.addTests(loader.loadTestsFromTestCase(value))
    return suite


def module_suite(path):
    name = 'cr_tests_' + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return unittest.TestLoader().loadTestsFromModule(module)


def run(sources, verbosity=1, stream=None):
    '''Run [(label, suite)] one after another. Returns (exit code, [(label, run, skipped,
    failed)]).'''
    stream = stream or sys.stderr
    rows, failed_any, void = [], False, not sources
    for label, suite in sources:
        result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
        bad = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
        executed = result.testsRun - len(result.skipped)
        rows.append((label, executed, len(result.skipped), bad))
        failed_any = failed_any or bad > 0
        void = void or executed == 0                # a source that ran nothing proves nothing
    if failed_any:
        return 1, rows
    return (2 if void else 0), rows


def main(argv=None):
    sys.path.insert(0, REPO)
    sources = [(name, in_file_suite(os.path.join(REPO, name))) for name in IN_FILE]
    sources += [(name, module_suite(os.path.join(TESTS, name))) for name in MODULES]
    code, rows = run(sources, verbosity=2 if '-v' in (argv or sys.argv[1:]) else 1)
    print('\n{:<24} {:>8} {:>8} {:>8}'.format('source', 'executed', 'skipped', 'failed'))
    for label, executed, skipped, bad in rows:
        print('{:<24} {:>8} {:>8} {:>8}'.format(label, executed, skipped, bad))
    print('python {} ({})'.format(sys.version.split()[0], sys.executable))
    try:
        import calibre  # noqa: F401
        under_calibre = True
    except ImportError:
        under_calibre = False
    # Under calibre the real-calibre tests must run (none skipped). A platform skip
    # elsewhere (the POSIX file-mode tests on Windows) does not void the run.
    if under_calibre and code == 0 and any(skipped for label, _, skipped, _ in rows
                                           if label == 'test_real_calibre.py'):
        code = 2
    total = sum(executed for _, executed, _, _ in rows)
    print('TOTAL executed {}: {}'.format(total, {0: 'PASS', 1: 'FAIL', 2: 'VOID (nothing, or not '
                                                 'everything, was executed)'}[code]))
    return code


if __name__ == '__main__':
    sys.exit(main())
