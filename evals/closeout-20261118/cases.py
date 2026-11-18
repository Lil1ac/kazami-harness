"""六个公开仓库的 36 个合成回归。每组依次覆盖两个局部、两个调用链、两个多处修复。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Regression:
    """描述一个独立故障。替换方向从上游正确代码指向供模型修复的起点。"""
    key: str  # 仓库内唯一题号。
    module: str  # 导入候选模块使用的包内路径。
    problem: str  # 用户可见的症状及兼容约束。
    mutations: tuple  # 零模型预检逐项确认每个上游原文匹配唯一。
    checks: str  # 隐藏测试正文，m 指向实际工作区中的候选模块。


R = Regression
CASES = {
"settings": [
R("empty-values","sources.utils","Blank strings disappear even when ignore_empty is off. Keep the flag meaningful and keep nonempty settings.",(("if not (ignore_empty and v == '')","if v != ''"),),"""f=m.parse_env_vars
assert f({'X':'','Y':'k'},True,False)=={'X':'','Y':'k'}
assert f({'X':'','Y':'k'},True,True)=={'Y':'k'}"""),
R("builtin-factories","sources.utils","Builtin functions disappear from settings callable detection. Plain functions and ordinary objects must retain their classification.",(("return isinstance(obj, (FunctionType, BuiltinFunctionType))","return isinstance(obj, FunctionType)"),),"""assert m._is_function(len)
assert m._is_function(lambda:1) and not m._is_function('x')"""),
R("env-null-sentinel","sources.utils","An explicit textual null in the environment loses its sentinel identity before source decoding. Leave an ordinary value and actual None alone.",(("else EnvNoneType(value)","else value"),),"""v=m.parse_env_vars({'A':'NULL','B':'ok','C':None},True,False,'NULL')
assert isinstance(v['A'],m.EnvNoneType) and v['B']=='ok' and v['C'] is None"""),
R("annotated-unions","sources.utils","Validation sees an Annotated union as one type, losing nested member types. Non-union annotations must remain single members.",(("return annotation.__origin__","return annotation"),),"""from typing import Annotated,Union
assert set(m._get_union_members(Annotated[Union[int,str],'config']))=={int,str}
assert m._get_union_members(float)==(float,)"""),
R("enum-roundtrip","sources.utils","Both directions of a settings enum conversion stop at the first unrelated union member. Restore round trips for a later enum and leave misses unresolved.",(("return enum_name\n","return enum_name\n        return None\n"),("return enum_val\n","return enum_val\n        return None\n")), """from enum import Enum
from typing import Union
class E(str,Enum): RED='r'
t=Union[int,E]
assert m._annotation_enum_val_to_name(t,'r')=='RED'
assert m._annotation_enum_name_to_val(t,'RED') is E.RED
assert m._annotation_enum_name_to_val(t,'missing') is None"""),
R("numeric-literal-unwrapping","sources.utils","Numeric enum literal hints disappear through Annotated and through unions. A plain integer literal must not be classified as an enum.",(("return _literal_has_numeric_enum(inner)","return False"),("return any(_literal_has_numeric_enum(arg) for arg in get_args(annotation))","return False")), """from typing import Literal,Annotated,Union
from enum import IntEnum
class E(IntEnum): ONE=1
hint=Literal[E.ONE]
assert m._literal_has_numeric_enum(Annotated[hint,'label'])
assert m._literal_has_numeric_enum(Union[str,hint])
assert not m._literal_has_numeric_enum(Literal[3])""")
],
"marsh":[
R("byte-text","utils","UTF-8 byte values appear as Python bytes literals in text fields. Preserve Unicode strings and normal scalar conversion.",(('val = val.decode("utf-8")','val = str(val)'),),"""assert m.ensure_text_type('雪'.encode())=='雪'
assert m.ensure_text_type('OK')=='OK' and m.ensure_text_type(2)=='2'"""),
R("fifo-pop","orderedset","Removing the first entry with pop(last=False) removes the latest instead. Preserve insertion order and the default LIFO behavior.",(("if last else self.end[2][0]","if last else self.end[1][0]"),),"""s=m.OrderedSet([1,2,3]);assert s.pop(last=False)==1 and list(s)==[2,3]
assert s.pop()==3 and list(s)==[2]"""),
R("index-zero-errors","error_store","List validation loses the index on the first record's errors. Keep named fields and unindexed errors unchanged.",(("if index is not None:","if index:"),),"""s=m.ErrorStore();s.store_error(['bad'],'name',0);s.store_error(['x'],'name',1)
assert s.errors=={0:{'name':['bad']},1:{'name':['x']}}
q=m.ErrorStore();q.store_error(['x'],'name');assert q.errors=={'name':['x']}"""),
R("mapping-collection","utils","Field deserialization treats mapping keys as a sequence of collection elements. Keep generator and tuple inputs iterable and exclude text.",(("return is_iterable_but_not_string(obj) and not isinstance(obj, Mapping)","return is_iterable_but_not_string(obj)"),),"""from collections import UserDict
assert not m.is_collection(UserDict({'a':2})) and not m.is_collection({})
assert m.is_collection(iter([2])) and m.is_collection((1,)) and not m.is_collection('ab')"""),
R("nested-errors-detach","error_store","After storing nested field errors, edits to the caller's lists or dictionaries alter the stored errors. Detach both kinds of nested containers.",(("return [copy_containers(val) for val in errors]","return list(errors)"),("return {key: copy_containers(val) for key, val in errors.items()}","return dict(errors)")), """a=[{'z':['x']}];b={'nested':{'field':['a']}};s=m.ErrorStore();s.store_error(a,'one');s.store_error(b,'two')
a[0]['z'].append('bad');b['nested']['field'].append('bad')
assert s.errors=={'one':[{'z':['x']}],'two':{'nested':{'field':['a']}}}
assert m.copy_containers(2)==2"""),
R("ordered-link-removal","orderedset","Removing a middle element leaves stale forward and reverse iteration links. Preserve length, membership and repeated discard.",(("prev[2] = next","prev[2] = self.end"),("next[1] = prev","next[1] = self.end")), """s=m.OrderedSet([1,2,3,4]);s.discard(2)
assert list(s)==[1,3,4] and list(reversed(s))==[4,3,1]
s.discard(99);assert len(s)==3 and 2 not in s""")
],
"jinja":[
R("trim-char-set","filters","The trim filter ignores its explicit character set. Default whitespace trimming must still work.",(("return soft_str(value).strip(chars)","return soft_str(value).strip()"),),"""assert m.do_trim('..x...', '.')=='x'
assert m.do_trim(' x ')=='x' and m.do_trim(' x ','.')==' x '"""),
R("integer-predicate","tests","Templates classify booleans as integers. Integers including zero must match and floats must not.",(("return isinstance(value, int) and value is not True and value is not False","return isinstance(value, int)"),),"""assert not m.test_integer(True) and not m.test_integer(False)
assert m.test_integer(0) and m.test_integer(-2) and not m.test_integer(2.0)"""),
R("numeric-path","filters","Template map(attribute=...) no longer resolves numeric list indexes within dotted paths. Preserve ordinary dictionary key lookup.",(('int(x) if x.isdigit() else x','x'),),"""from jinja2 import Environment
env=Environment();f=m.make_attrgetter(env,'a.1.b');assert f({'a':[None,{'b':3}]})==3
assert m.make_attrgetter(env,'a')({'a':'ok'})=='ok'"""),
R("cycler-current","utils","The template cycler skips the first item when next() is called. current reports the upcoming item and reset restarts the cycle.",(("rv = self.current\n        self.pos = (self.pos + 1) % len(self.items)\n        return rv","self.pos = (self.pos + 1) % len(self.items)\n        return self.current"),),"""c=m.Cycler('a','b');assert (c.current,c.next(),c.current,c.next(),c.next())==('a','a','b','b','a')
c.reset();assert c.current=='a'"""),
R("int-filter-fallbacks","filters","The int filter ignores an explicit string base and discards valid decimal strings such as 2.7. Preserve caller-selected defaults for invalid numbers.",(("return int(value, base)","return int(value)"),("return int(float(value))","return default")), """assert m.do_int('ff',base=16)==255
assert m.do_int('2.7')==2 and m.do_int('bad',13)==13
assert m.do_int(3.9)==3"""),
R("filesize-units","filters","File size labels select the wrong binary radix and pluralize a single byte. Keep decimal and large binary labels compatible.",(("base = 1024 if binary else 1000","base = 1000"),('return "1 Byte"','return "1 Bytes"')), """assert m.do_filesizeformat(1)=='1 Byte' and m.do_filesizeformat(2)=='2 Bytes'
assert m.do_filesizeformat(1000)=='1.0 kB'
assert m.do_filesizeformat(1000,True)=='1000 Bytes' and m.do_filesizeformat(1048576,True)=='1.0 MiB'""")
],
"click":[
R("bool-whitespace","types","CLI boolean conversion rejects padded textual values. Preserve Python booleans and rejection of unknown strings.",(("bool_states.get(value.strip().lower())","bool_states.get(value.lower())"),),"""assert m.BoolParamType.str_to_bool('  YeS  ') is True and m.BoolParamType.str_to_bool(' off ') is False
assert m.BoolParamType.str_to_bool(True) is True and m.BoolParamType.str_to_bool('other') is None"""),
R("empty-paragraph","formatting","A fresh help formatter unexpectedly starts with a blank line after write_paragraph. Still insert a paragraph break after existing content.",(('if self.buffer:\n            self.write("\\n")','if True:\n            self.write("\\n")'),),"""f=m.HelpFormatter();f.write_paragraph();assert f.getvalue()==''
f.write('A');f.write_paragraph();assert f.getvalue()=='A\\n'"""),
R("option-prefix","parser","Normalizing a CLI token strips the option prefix before matching. Preserve the spelling of each prefix and normalize only its name.",(('return f"{prefix}{ctx.token_normalize_func(opt)}"','return ctx.token_normalize_func(opt)'),),"""from types import SimpleNamespace
c=SimpleNamespace(token_normalize_func=str.lower)
assert m._normalize_opt('--NAME',c)=='--name' and m._normalize_opt('/X',c)=='/x'
assert m._normalize_opt('--NAME',None)=='--NAME'"""),
R("path-result-bytes","types","CLI path conversion returns text to callers that requested bytes. Preserve custom PathLike types and already-correct values.",(("return os.fsencode(value)","return os.fsdecode(value)"),),"""from pathlib import Path
assert m.Path(path_type=bytes).coerce_path_result('abc')==b'abc'
assert m.Path(path_type=str).coerce_path_result(b'abc')=='abc'
assert m.Path(path_type=Path).coerce_path_result('abc')==Path('abc')"""),
R("help-option-order","formatting","Help aliases are sorted longest-prefix-first and slash flags are not signalled. Restore short-first ordering and slash reporting separately.",(('if prefix == "/":','if prefix == "":'),('rv.sort(key=lambda x: x[0])','rv.sort(key=lambda x: -x[0])')), """assert m.join_options(['--blue','-b'])==('-b, --blue',False)
assert m.join_options(['/B','--blue'])==('/B, --blue',True)
assert m.join_options([])==('',False)"""),
R("indent-roundtrip","formatting","Nested help sections ignore their configured indentation increment when entering and leaving a level. Both operations must use the configured width.",(("self.current_indent += self.indent_increment","self.current_indent += 1"),("self.current_indent -= self.indent_increment","self.current_indent -= 1")), """f=m.HelpFormatter(indent_increment=4);f.indent();f.indent();assert f.current_indent==8
f.dedent();assert f.current_indent==4
f.write_heading('X');f.dedent();assert f.getvalue()=='    X:\\n' and f.current_indent==0""")
],
"packaging":[
R("epoch-prefix","version","Base version output drops nonzero epochs from package release labels. Local and prerelease parts should still be omitted.",(('return f"{self.epoch}!{release_segment}" if self.epoch else release_segment','return release_segment'),),"""assert m.Version('2!1.0rc2+cpu').base_version=='2!1.0'
assert m.Version('1.0rc1').base_version=='1.0'"""),
R("platform-spaces","tags","Platform tag normalization leaves spaces inside wheel tag components. Preserve normalization of dots and hyphens as well.",(('.replace("-", "_").replace(" ", "_")','.replace("-", "_")'),),"""assert m._normalize_string('Mac OS-X.1')=='Mac_OS_X_1'
assert m._normalize_string('manylinux')=='manylinux'"""),
R("implicit-post","version","A legacy dash-number release loses its post number when parsed by Version. Keep prerelease spelling and regular finals intact.",(('return "post", int(number)','return "post", 0'),),"""assert m.Version('1.2-7').post==7 and str(m.Version('1.2-7'))=='1.2.post7'
assert m.Version('1.2c2').pre==('rc',2) and m.Version('1.2').post is None"""),
R("tag-minor-backfill","tags","Pure-Python compatibility tags omit the oldest minor, py30. Preserve tag priority and do not invent future minor tags.",(("range(py_version[1] - 1, -1, -1)","range(py_version[1] - 1, 0, -1)"),),"""names=[str(t) for t in m.pure_python_tags((3,2))]
assert names==['py32-none-any','py3-none-any','py31-none-any','py30-none-any']
assert [str(t) for t in m.pure_python_tags((3,))]==['py3-none-any']"""),
R("release-components","version","Version.minor and Version.micro read the previous tuple position. Both accessors must return zero for a missing component.",(("self.release[1] if len(self.release) >= 2","self.release[0] if len(self.release) >= 2"),("self.release[2] if len(self.release) >= 3","self.release[1] if len(self.release) >= 3")), """assert (m.Version('7.2.5').major,m.Version('7.2.5').minor,m.Version('7.2.5').micro)==(7,2,5)
assert m.Version('7').minor==m.Version('7.2').micro==0"""),
R("wheel-tag-case","tags","Equivalent ABI and platform spellings compare unequal in wheel tag sets. Normalize both while retaining interpreter case folding.",(("\n        self._abi = abi.lower()","\n        self._abi = abi"),("\n        self._platform = platform.lower()","\n        self._platform = platform")), """a=m.Tag('CP312','ABI3','LINUX');assert a.abi=='abi3' and a.platform=='linux'
assert {a,m.Tag('cp312','abi3','linux')}=={a} and a.interpreter=='cp312'""")
],
"pluggy":[
R("excinfo-type","_result","Plugin result excinfo exposes the wrong exception type. Preserve the original exception object and the no-error case.",(("return (type(exc), exc, self._traceback)","return (BaseException, exc, self._traceback)"),),"""e=ValueError('bad');r=m.Result(None,e);assert r.excinfo[:2]==(ValueError,e)
assert m.Result(4,None).excinfo is None"""),
R("trace-empty-token","_tracing","Trace value formatting treats printable tokens containing spaces as plain. Preserve unquoted single tokens and quoted multiword values.",((' and " " not in text',''),),"""assert m._render_value('a b')==repr('a b')
assert m._render_value('ok')=='ok' and not m._is_plain_token('')"""),
R("blocked-lookup","_manager","A blocked plugin name is reported as an installed plugin by has_plugin. Keep ordinary registrations and unknown names distinct.",(('return self.get_plugin(name) is not None','return name in self._name2plugin'),),"""p=m.PluginManager('demo');x=object();p.register(x,name='ok');p.set_blocked('bad')
assert p.has_plugin('ok') and not p.has_plugin('bad') and not p.has_plugin('missing')
assert p.is_blocked('bad')"""),
R("trace-child-path","_tracing","A nested tracer loses its parent's tags, so a registered processor never sees the child path. Preserve root tracing behavior.",(("self.__class__(self.root, self.tags + (name,))","self.__class__(self.root, (name,))"),),"""root=m.TagTracer();got=[]; root.setprocessor('p:c',lambda tags,args:got.append((tags,args)))
root.get('p').get('c')('hello');assert got==[(('p','c'),('hello',))]
assert root.get('p').tags==('p',)"""),
R("force-result-transition","_result","Forcing a result after failure leaves the exception live, and subsequently forcing an exception keeps a stale result value. Restore both transition invariants.",(("self._result = result\n        self._exception = None","self._result = result\n        self._exception = self._exception"),("self._result = None\n        self._exception = exception","self._result = self._result\n        self._exception = exception")), """e=ValueError('bad');r=m.Result(None,e);r.force_result(4);assert r.get_result()==4 and r.excinfo is None
r.force_exception(e);assert r._result is None and r.exception is e
try: r.get_result()
except ValueError as caught: assert caught is e
else: raise AssertionError('exception suppressed')"""),
R("unblock-state","_manager","Unblocking returns success without releasing the name, and treats unregistered names as blocked. Repair both state transitions without disturbing active plugins.",(("if self._name2plugin.get(name, -1) is None:","if self._name2plugin.get(name) is None:"),("del self._name2plugin[name]\n            return True","self._name2plugin.pop(name, None) if False else None\n            return True")), """p=m.PluginManager('demo');assert not p.unblock('missing')
p.set_blocked('x');assert p.unblock('x') and not p.is_blocked('x')
o=object();p.register(o,name='x');assert not p.unblock('x') and p.get_plugin('x') is o""")
]}
