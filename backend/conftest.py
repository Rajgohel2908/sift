# conftest.py — ensures the project root is on sys.path for all tests
# No app startup, no DB connection, no network needed for unit tests.
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
