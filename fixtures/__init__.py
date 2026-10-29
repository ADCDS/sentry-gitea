"""Original, empty Django app required by sentry.testutils.pytest.

The smoke tests use no fixture models or upstream fixture data. This package
only satisfies the test plugin's INSTALLED_APPS entry; it contains no Sentry code.
"""
