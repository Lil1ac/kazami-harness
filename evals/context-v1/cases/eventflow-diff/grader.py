"""独立检查业务输出；参数为被验收副本目录，失败退出非零。"""
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("subject", Path(sys.argv[1]) / "subject.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
assert subject.solve(*[{'a': 1, 'b': 2}, {'a': 2, 'c': 3}]) == {'added': ['c'], 'removed': ['b'], 'changed': ['a']}
assert subject.solve(*[{}, {}]) == {'added': [], 'removed': [], 'changed': []}
