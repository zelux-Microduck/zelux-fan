#!/usr/bin/env python3
"""Run socket contract tests."""
import sys
import unittest
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

# Import the test module
from tests import test_socket_contract

# Create a test suite
loader = unittest.TestLoader()
suite = loader.loadTestsFromModule(test_socket_contract)

# Run the tests
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)

# Exit with appropriate code
sys.exit(0 if result.wasSuccessful() else 1)