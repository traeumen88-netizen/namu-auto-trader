"""[HARNESS VALIDATION RUNNER]
(scripts/run_harness_validation.py)

Executes all 8 harness reliability and integrity checks and prints the official gate block:
==================================================
HARNESS VALIDATION
==================================================

Real Function Invocation       PASS / FAIL
Mock Bypass Audit              PASS / FAIL
Mutation Detection             PASS / FAIL
Input Integrity                PASS / FAIL
Call Sequence Parity           PASS / FAIL
Output Integrity               PASS / FAIL
Exception/Fallback Safety      PASS / FAIL
Behavior Coverage              PASS / FAIL

OVERALL HARNESS VALIDITY       PASS / FAIL
==================================================
"""

import os
import sys
import unittest

# Path setup
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_harness_validation import TestHarnessValidation


def run_harness_validation() -> int:
    suite = unittest.TestSuite()
    suite.addTest(TestHarnessValidation("test_real_function_invocation"))
    suite.addTest(TestHarnessValidation("test_mock_bypass_audit"))
    suite.addTest(TestHarnessValidation("test_mutation_detection"))
    suite.addTest(TestHarnessValidation("test_input_integrity"))
    suite.addTest(TestHarnessValidation("test_call_sequence_parity"))
    suite.addTest(TestHarnessValidation("test_output_integrity_validation"))
    suite.addTest(TestHarnessValidation("test_exception_fallback_safety"))
    suite.addTest(TestHarnessValidation("test_behavior_coverage"))

    results = {}
    test_instance = TestHarnessValidation()
    test_instance.setUp()

    checks = [
        ("Real Function Invocation", "test_real_function_invocation"),
        ("Mock Bypass Audit", "test_mock_bypass_audit"),
        ("Mutation Detection", "test_mutation_detection"),
        ("Input Integrity", "test_input_integrity"),
        ("Call Sequence Parity", "test_call_sequence_parity"),
        ("Output Integrity", "test_output_integrity_validation"),
        ("Exception/Fallback Safety", "test_exception_fallback_safety"),
        ("Behavior Coverage", "test_behavior_coverage")
    ]

    for label, method_name in checks:
        try:
            method = getattr(test_instance, method_name)
            method()
            results[label] = "PASS"
        except Exception as e:
            print(f"[-] Check {label} ({method_name}) failed: {e}")
            results[label] = "FAIL"

    overall_pass = all(v == "PASS" for v in results.values())
    overall_status = "PASS" if overall_pass else "FAIL"

    print("\n==================================================")
    print("HARNESS VALIDATION")
    print("==================================================")
    print("")
    for label, status in results.items():
        print(f"{label:<30} {status}")
    print("")
    print(f"OVERALL HARNESS VALIDITY       {overall_status}")
    print("==================================================\n")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(run_harness_validation())
