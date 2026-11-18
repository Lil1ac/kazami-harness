"""独立检查业务输出；参数为被验收副本目录，失败退出非零。"""
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("subject", Path(sys.argv[1]) / "subject.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
assert subject.solve(*[[{'expires_at': None}, {'expires_at': 2}, {'expires_at': 3}], 2]) == [{'expires_at': None}, {'expires_at': 3}]
assert subject.solve(*[[], 1]) == []
