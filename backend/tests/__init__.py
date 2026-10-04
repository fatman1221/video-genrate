"""测试包。

存在 `__init__.py` 让 pytest 把 `backend/` 插到 sys.path，
这样 `import app.*` 与 `from tests.conftest import ...` 都能解析。
"""
