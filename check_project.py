"""Run this project's full check entry from the repository root.

Executes the unittest suite, then the ordinary-reel slot baseline and the
joint slot-reuse business checks, propagating any failure as a nonzero exit.
"""
import sys
import unittest


def run_joint_checks():
    import check_slot_reuse
    import check_slot_reuse_baseline

    ok = True
    for name, module in (('slot-reuse-baseline', check_slot_reuse_baseline),
                         ('slot-reuse-joint', check_slot_reuse)):
        try:
            module.main()
        except Exception as error:
            print(f'FAIL {name}: {error}', file=sys.stderr)
            ok = False
    return ok


def main():
    suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
    if suite.countTestCases():
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    else:
        print('No tests discovered in tests/', file=sys.stderr)
        result = unittest.TestResult()
    for status, failed_cases in (
        ('FAIL', (test for test, _ in result.failures)),
        ('ERROR', (test for test, _ in result.errors)),
        ('UNEXPECTED_SUCCESS', result.unexpectedSuccesses),
    ):
        for test in failed_cases:
            print(f'{status} {test.id()}')
    successful = result.testsRun > 0 and result.wasSuccessful()
    print(f'TEST SUMMARY total={result.testsRun} failures={len(result.failures)} '
          f'errors={len(result.errors)} skipped={len(result.skipped)} '
          f'expected_failures={len(result.expectedFailures)} '
          f'unexpected_successes={len(result.unexpectedSuccesses)} '
          f'result={"PASS" if successful else "FAIL"}')
    joint = run_joint_checks()
    print(f'JOINT CHECK SUMMARY result={"PASS" if joint else "FAIL"}')
    return 0 if successful and joint else 1


if __name__ == '__main__':
    raise SystemExit(main())
