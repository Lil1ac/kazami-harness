"""三份固定公开发行包源码上的十八个独立功能合同与行为断言。"""

from fixtures import Task


CLICK = (
    Task("short-help-sentence", "locate", "utils", "Question punctuation in short help",
         "The help synopsis keeps a later sentence when its first sentence is a question or exclamation.",
         "Treat a word ending in ?, ! or . as a complete sentence terminator in _make_default_short_help. "
         "The current word and total width rules, ellipsis, and first-paragraph handling stay intact.",
         (('        if word[-1] == ".":  # sentence end, truncate without "..."',
           '        if word[-1] in ".?!":  # sentence end, truncate without "..."'),),
         """
         f = m._make_default_short_help
         assert f('Ready? Continue later') == 'Ready?'
         assert f('Great! Extra notes') == 'Great!'
         assert f('Welcome. More later') == 'Welcome.'
         assert f('No final punctuation\\n\\nExtra paragraph') == 'No final punctuation'
         assert f('alpha betaword more', max_length=10) == 'alpha...'
         """),
    Task("table-min-columns", "locate", "formatting", "Sparse table width preview",
         "Section headers need width slots even if all supplied rows leave out their trailing columns.",
         "Add keyword-only min_columns=0 to measure_table. Seed absent widths with zero up to that minimum, "
         "preserve wider rows and ANSI display width behavior, and reject negative min_columns.",
         (("def measure_table(rows: cabc.Iterable[tuple[str, str]]) -> tuple[int, ...]:",
           "def measure_table(rows: cabc.Iterable[tuple[str, str]], *, min_columns: int = 0) -> tuple[int, ...]:"),
          ("    widths: dict[int, int] = {}\n\n    for row in rows:",
           "    if min_columns < 0:\n        raise ValueError('min_columns must be nonnegative')\n"
           "    widths: dict[int, int] = dict.fromkeys(range(min_columns), 0)\n\n    for row in rows:")),
         """
         f = m.measure_table
         assert f([], min_columns=2) == (0, 0)
         assert f([('ab','X'),('X','abcd')], min_columns=3) == (2,4,0)
         assert f([('a','bbb')], min_columns=1) == (1,3)
         assert f([]) == ()
         try:
             f([], min_columns=-1)
         except ValueError:
             pass
         else:
             raise AssertionError('negative column minimum accepted')
         """),
    Task("custom-display-slug", "knowledge", "utils", "Custom label separator",
         "The command summary needs a derived lower-case label with caller-selected separator.",
         "Add keyword-only separator='-' to _posixify. Join whitespace-split segments with the exact "
         "string separator, then lower-case as before. An empty separator is valid and punctuation is retained.",
         (("def _posixify(name: str) -> str:\n    return \"-\".join(name.split()).lower()",
           "def _posixify(name: str, *, separator: str = '-') -> str:\n    return separator.join(name.split()).lower()"),),
         """
         f=m._posixify
         assert f('  Blue\\t CAFE ') == 'blue-cafe'
         assert f(' A.B   D ', separator='_') == 'a.b_d'
         assert f('A  B', separator='') == 'ab'
         assert f('  ', separator='--') == ''
         """),
    Task("crop-row-preview", "knowledge", "formatting", "Fixed width row preview",
         "A consumer wants an opt-in preview that shows only the first requested columns of a long row.",
         "Add keyword-only trim=False to iter_rows. Trim true truncates overly long rows to col_count "
         "and pads short rows as usual. Zero columns yields empty tuples in trim mode. Default does not trim.",
         (("    rows: cabc.Iterable[tuple[str, str]], col_count: int\n) -> cabc.Iterator[tuple[str, ...]]:",
           "    rows: cabc.Iterable[tuple[str, str]], col_count: int, *, trim: bool = False\n) -> cabc.Iterator[tuple[str, ...]]:"),
          ('        yield row + ("",) * (col_count - len(row))',
           '        if trim:\n            row = row[:col_count]\n        yield row + ("",) * (col_count - len(row))')),
         """
         f=m.iter_rows
         assert list(f([('a','b','c'),('a',)], 2, trim=True)) == [('a','b'),('a','')]
         assert list(f([('a','b','c')], 2)) == [('a','b','c')]
         assert list(f([('a',)], 0, trim=True)) == [()]
         assert list(f([], 4, trim=True)) == []
         """),
    Task("display-filename-parent", "safety", "utils", "Current short filename captions",
         "Short file captions are ambiguous when several directories have the same basename.",
         "Add keyword-only keep_parent=False to format_filename. When both shorten and keep_parent are true, "
         "display the final parent directory and leaf joined with the platform separator. Existing shorten "
         "still displays the leaf, and keep_parent has no effect on the full-path mode. Preserve text, bytes, "
         "and PathLike formatting.",
         (("    shorten: bool = False,\n) -> str:\n    \"\"\"Format a filename",
           "    shorten: bool = False,\n    *,\n    keep_parent: bool = False,\n) -> str:\n    \"\"\"Format a filename"),
          ("    if shorten:\n        filename = os.path.basename(filename)\n    else:\n        filename = os.fspath(filename)",
           "    filename = os.fspath(filename)\n    if shorten and keep_parent:\n"
           "        parent, leaf = os.path.split(filename)\n        filename = os.path.join(os.path.basename(parent), leaf)\n"
           "    elif shorten:\n        filename = os.path.basename(filename)")),
         """
         f=m.format_filename
         target=Path('docs', 'app', 'guide.md')
         assert f(target, shorten=True, keep_parent=True) == 'app/guide.md'
         assert f(b'docs/app/guide.md', shorten=True, keep_parent=True) == 'app/guide.md'
         assert f(target, shorten=True) == 'guide.md'
         assert f('guide.md', shorten=True, keep_parent=True) == 'guide.md'
         assert f(target, keep_parent=True) == str(target)
         """,
         ("withdrawn", "A draft caption policy showed only the leaf even when a parent was requested.",
          """
          try:
              shown=m.format_filename('docs/app/guide.md', shorten=True, keep_parent=True)
          except TypeError:
              shown=m.format_filename('docs/app/guide.md', shorten=True)
          OLD = shown == 'guide.md'
          """)),
    Task("display-byte-representation", "safety", "utils", "Current undecodable byte display",
         "A help preview needs enough fidelity to distinguish different undecodable byte values.",
         "When filesystem decoding of bytes fails, make_str uses UTF-8 with backslashreplace. Preserve the "
         "first attempt with sys.getfilesystemencoding and ordinary str() conversions.",
         (('            return value.decode("utf-8", "replace")', '            return value.decode("utf-8", "backslashreplace")'),),
         """
         from unittest.mock import patch
         with patch.object(m.sys, 'getfilesystemencoding', return_value='ascii'):
             assert m.make_str(bytes([255, 254])) == r'\\xff\\xfe'
             assert m.make_str('café'.encode()) == 'café'
         with patch.object(m.sys, 'getfilesystemencoding', return_value='latin-1'):
             assert m.make_str(bytes([0xe9])) == 'é'
         assert m.make_str(123) == '123'
         """,
         ("foreign", "The companion terminal skin collapsed every undecodable byte to U+FFFD.",
          "from unittest.mock import patch\nwith patch.object(m.sys, 'getfilesystemencoding', return_value='ascii'):\n"
          "    OLD = m.make_str(bytes([255])) == chr(0xfffd)")),
)


PACKAGING = (
    Task("case-preserved-label", "locate", "utils", "Case-preserving distribution display",
         "A maintenance view wants a readable normalized separator, while keeping capital letters on request.",
         "Add keyword-only preserve_case=False to canonicalize_name. Explicit opt-in keeps letters as input "
         "while replacing punctuation and repeated separators exactly as the default does. Validation remains identical.",
         (("def canonicalize_name(name: str, *, validate: bool = False) -> NormalizedName:",
           "def canonicalize_name(name: str, *, validate: bool = False, preserve_case: bool = False) -> NormalizedName:"),
          ('    value = name.lower().replace("_", "-").replace(".", "-")',
           '    value = (name if preserve_case else name.lower()).replace("_", "-").replace(".", "-")')),
         """
         f=m.canonicalize_name
         assert f('Blue..APP__tools', preserve_case=True) == 'Blue-APP-tools'
         assert f('Blue..APP__tools') == 'blue-app-tools'
         assert f('APP', validate=True, preserve_case=True) == 'APP'
         try:
             f('-APP', validate=True, preserve_case=True)
         except m.InvalidName:
             pass
         else:
             raise AssertionError('validation changed')
         """),
    Task("strict-display-version", "locate", "utils", "Strict version display preview",
         "An index preview wants malformed versions reported instead of silently echoed.",
         "Add keyword-only reject_invalid=False to canonicalize_version. True reraises InvalidVersion "
         "for invalid strings. Default keeps the original string. Valid strings and Version instances "
         "keep existing stripping and rendering behavior.",
         (("    version: Version | str, *, strip_trailing_zero: bool = True\n) -> str:",
           "    version: Version | str, *, strip_trailing_zero: bool = True, reject_invalid: bool = False\n) -> str:"),
          ("        except InvalidVersion:\n            return str(version)",
           "        except InvalidVersion:\n            if reject_invalid:\n                raise\n            return str(version)")),
         """
         f=m.canonicalize_version
         assert f('oops version') == 'oops version'
         assert f('1.2.0', reject_invalid=True) == '1.2'
         assert f(m.Version('1.2.0'), reject_invalid=True, strip_trailing_zero=False) == '1.2.0'
         try:
             f('oops version', reject_invalid=True)
         except m.InvalidVersion:
             pass
         else:
             raise AssertionError('invalid version returned')
         """),
    Task("name-preview-ignore-case", "knowledge", "utils", "Case-insensitive name preview",
         "A registry display optionally accepts case aliases when checking the normalized punctuation shape.",
         "Add keyword-only case_insensitive=False to is_normalized_name. Only allow uppercase ASCII letters "
         "as an alternative when enabled. The ASCII digit and single-hyphen grammar, and default lowercase "
         "requirement, are unchanged. Do not normalize Unicode or punctuation.",
         (("def is_normalized_name(name: str) -> bool:",
           "def is_normalized_name(name: str, *, case_insensitive: bool = False) -> bool:"),
          ("    return _normalized_regex.fullmatch(name) is not None",
           "    if case_insensitive and name.isascii():\n        name = name.lower()\n"
           "    return _normalized_regex.fullmatch(name) is not None")),
         """
         f=m.is_normalized_name
         assert f('ALPHA-2', case_insensitive=True) and not f('ALPHA-2')
         assert f('alpha-2') and f('a', case_insensitive=True)
         for raw in ['-alpha','foo--bar','foo_bar','foo.bar','K','İ','']:
             assert not f(raw, case_insensitive=True), raw
         """),
    Task("opt-in-xz-preview", "knowledge", "utils", "Local xz source artifact preview",
         "A local test catalog needs opt-in parsing of its .tar.xz source archive name.",
         "Add keyword-only accept_xz=False to parse_sdist_filename. True recognizes .tar.xz exactly with "
         "the same name and version validation. Always keep .zip and .tar.gz. Default rejects .tar.xz.",
         (("def parse_sdist_filename(filename: str) -> tuple[NormalizedName, Version]:",
           "def parse_sdist_filename(filename: str, *, accept_xz: bool = False) -> tuple[NormalizedName, Version]:"),
          ('    elif filename.endswith(".zip"):\n        file_stem = filename[: -len(".zip")]\n    else:',
           '    elif filename.endswith(".zip"):\n        file_stem = filename[: -len(".zip")]\n'
           '    elif accept_xz and filename.endswith(".tar.xz"):\n'
           '        file_stem = filename[: -len(".tar.xz")]\n    else:')),
         """
         f=m.parse_sdist_filename
         assert f('cool_tools-2.1.tar.xz', accept_xz=True) == ('cool-tools', m.Version('2.1'))
         assert f('demo-1.zip', accept_xz=True) == ('demo', m.Version('1'))
         assert f('demo-1.tar.gz') == ('demo', m.Version('1'))
         for raw, enabled in [('demo-1.tar.xz',False), ('demo-nope.tar.xz',True)]:
             try:
                 f(raw, accept_xz=enabled)
             except m.InvalidSdistFilename:
                 pass
             else:
                 raise AssertionError('unexpectedly parsed ' + raw)
         """),
    Task("wheel-extension-display", "safety", "utils", "Current wheel display extension",
         "Archived wheel filenames exported for display can have an uppercase extension.",
         "Accept any letter case in exactly the .whl extension of parse_wheel_filename. Keep parsing and "
         "validating the rest of the wheel name as before, both with and without build tags.",
         (('    if not filename.endswith(".whl"):', '    if not filename.lower().endswith(".whl"):'),
          ("extension must be '.whl'", "extension must be '.whl' (case-insensitive)")),
         """
         f=m.parse_wheel_filename
         assert f('cool_app-2.0-py3-none-any.WHL') == f('cool_app-2.0-py3-none-any.whl')
         assert f('cool_app-2.0-3build-py3-none-any.WhL')[2] == (3,'build')
         for raw in ('cool_app-2.0-py3-none-any.zip','cool_app-nope-py3-none-any.WHL'):
             try:
                 f(raw)
             except m.InvalidWheelFilename:
                 pass
             else:
                 raise AssertionError('incorrectly parsed ' + raw)
         """,
         ("expired", "Earlier tooling only accepted all-lowercase wheel extension names.",
          """
          try:
              m.parse_wheel_filename('test-1-py3-none-any.WHL')
          except m.InvalidWheelFilename:
              OLD = True
          else:
              OLD = False
          """)),
    Task("ordered-environment-preview", "safety", "markers", "Current environment display ordering",
         "A maintenance view displays marker environment entries in inconsistent source order.",
         "default_environment returns a fresh dictionary with its keys in lexicographic order each time. "
         "Keep every field and value and never let consumer mutations affect the next result.",
         (('    return cast("Environment", dict(_cached_default_environment()))',
           '    return cast("Environment", dict(sorted(_cached_default_environment().items())))'),),
         """
         from unittest.mock import patch
         with patch.object(m, '_cached_default_environment', return_value={'zeta':'x','alpha':'y','omega':'z'}):
             result=m.default_environment()
             assert list(result) == ['alpha','omega','zeta']
             assert result == {'zeta':'x','alpha':'y','omega':'z'}
             result['alpha']='changed'
             assert m.default_environment()['alpha'] == 'y'
         assert 'python_version' in m.default_environment()
         """,
         ("changed", "The earlier view promised to preserve whatever ordering the local runtime collector supplied.",
          """
          from unittest.mock import patch
          with patch.object(m, '_cached_default_environment', return_value={'zeta':'x','alpha':'y'}):
              OLD = list(m.default_environment()) == ['zeta','alpha']
          """)),
)


PLUGGY = (
    Task("trace-writer-restore", "locate", "_tracing", "Restore temporary trace writer",
         "A plugin test context needs to restore the trace writer exactly as it was before collecting a preview.",
         "TagTracer.setwriter returns the previous writer, or None if there was none, after installing its "
         "argument. Setting None still disables output. No changes to processors or trace formatting.",
         (("    def setwriter(self, writer: _Writer | None) -> None:\n        self._writer = writer",
           "    def setwriter(self, writer: _Writer | None) -> _Writer | None:\n"
           "        previous = self._writer\n        self._writer = writer\n        return previous"),),
         """
         trace=m.TagTracer()
         first, second = [], []
         write_first, write_second = first.append, second.append
         assert trace.setwriter(write_first) is None
         assert trace.setwriter(write_second) is write_first
         trace.get('stage')('working')
         assert second == ['working [stage]\\n'] and first == []
         assert trace.setwriter(None) is write_second
         trace.get('stage')('silent')
         assert len(second) == 1
         """),
    Task("qualify-hook-signature", "locate", "_manager", "Qualified hook signature display",
         "Nested handlers with the same short name should be distinguishable in a signature preview.",
         "Add keyword-only qualified=False to _formatdef. True uses __qualname__ when available. "
         "Keep the existing signature formatting and short name by default, and fall back to __name__ "
         "for callables that do not expose __qualname__.",
         (("def _formatdef(func: Callable[..., object]) -> str:",
           "def _formatdef(func: Callable[..., object], *, qualified: bool = False) -> str:"),
          ('    return f"{func.__name__}{inspect.signature(func)}"',
           '    name = getattr(func, "__qualname__", func.__name__) if qualified else func.__name__\n'
           '    return f"{name}{inspect.signature(func)}"')),
         """
         class Plugin:
             def entry(self, value=2):
                 pass
         assert m._formatdef(Plugin.entry, qualified=True) == 'Plugin.entry(self, value=2)'
         assert m._formatdef(Plugin.entry) == 'entry(self, value=2)'
         assert m._formatdef(len, qualified=True) == 'len(obj, /)'
         """),
    Task("split-trace-path", "knowledge", "_tracing", "Nested trace path segments",
         "Nested tracing wants a shorthand for the two levels represented by a colon-separated path.",
         "Add keyword-only split=False to TagTracerSub.get. With true, append colon-separated components; "
         "preserve empty components. With false append a single literal name. Preserve root and subclass.",
         (("    def get(self, name: str) -> TagTracerSub:\n        return self.__class__(self.root, self.tags + (name,))",
           "    def get(self, name: str, *, split: bool = False) -> TagTracerSub:\n"
           "        suffix = tuple(name.split(':')) if split else (name,)\n"
           "        return self.__class__(self.root, self.tags + suffix)"),),
         """
         parent=m.TagTracer().get('hook')
         assert parent.get('one:two').tags == ('hook','one:two')
         assert parent.get('one:two', split=True).tags == ('hook','one','two')
         assert parent.get(':x', split=True).tags == ('hook','','x')
         assert parent.get('detail', split=True).root is parent.root
         """),
    Task("active-plugin-list", "knowledge", "_manager", "Active registry listing",
         "An administration display needs an opt-in list limited to actually registered plugin objects.",
         "Add keyword-only active_only=False to list_name_plugin. True hides None blocked-name placeholders. "
         "Keep registration order, active false-valued plugin objects and original listing behavior by default.",
         (("    def list_name_plugin(self) -> list[tuple[str, _Plugin]]:",
           "    def list_name_plugin(self, *, active_only: bool = False) -> list[tuple[str, _Plugin]]:"),
          ("        return list(self._name2plugin.items())",
           "        items = self._name2plugin.items()\n"
           "        return [(name, obj) for name, obj in items if obj is not None] if active_only else list(items)")),
         """
         class FalsePlugin:
             def __bool__(self):
                 return False
         a,b = FalsePlugin(), object()
         manager=m.PluginManager('preview')
         manager.register(a,'first')
         manager.set_blocked('paused')
         manager.register(b,'second')
         assert manager.list_name_plugin(active_only=True) == [('first',a),('second',b)]
         assert manager.list_name_plugin() == [('first',a),('paused',None),('second',b)]
         """),
    Task("sorted-trace-fields", "safety", "_tracing", "Current trace field order",
         "Snapshot tests need the same trace field ordering regardless of a caller's dict insertion order.",
         "TagTracer prints extra fields in ascending str(key) order. Preserve content line, rendering, "
         "existing indentation and multiline field handling. Ordering is stable on equal key strings.",
         (("        for name, value in extra.items():", "        for name, value in sorted(extra.items(), key=lambda pair: str(pair[0])):"),),
         """
         writes=[]
         tracer=m.TagTracer()
         tracer.indent=1
         tracer.setwriter(writes.append)
         tracer.get('hook')('sample', {'last':3, 'first':'hello', 2:'num'})
         lines=writes[0].splitlines()
         assert lines[0] == '  sample [hook]'
         assert [line.split(':',1)[0].strip() for line in lines[1:]] == ['2','first','last']
         tracer.get('hook')('more')
         assert writes[1] == '  more [hook]\\n'
         """,
         ("withdrawn", "Snapshot collectors used to render in the original registration order.",
          """
          s=m.TagTracer()._format_message(('hook',), ('x', {'z':1,'a':2}))
          OLD = s.index('z:') < s.index('a:')
          """)),
    Task("class-qualified-plugin-name", "safety", "_manager", "Current plug-in display fallback",
         "Instances of a plugin class produce opaque numeric titles in introspection.",
         "get_canonical_name returns a supplied nonempty __name__ first. Otherwise return the instance "
         "class's module plus qualified class name, with a dot between them. Explicitly registered alias "
         "lookup keeps existing behavior.",
         (('        return name or str(id(plugin))',
           '        if name:\n            return name\n'
           '        kind = type(plugin)\n        return f"{kind.__module__}.{kind.__qualname__}"'),),
         """
         from types import ModuleType
         class Outer:
             class Runner:
                 pass
         manager=m.PluginManager('preview')
         plugin=Outer.Runner()
         assert manager.get_canonical_name(plugin).endswith('.Outer.Runner')
         assert manager.get_canonical_name(ModuleType('name')) == 'name'
         manager.register(plugin,'external')
         assert manager.get_name(plugin) == 'external'
         """,
         ("foreign", "The other registry used a decimal identity string for unnamed instances.",
          "plugin=object()\nOLD = m.PluginManager('preview').get_canonical_name(plugin) == str(id(plugin))")),
)


TASKS = {"click": CLICK, "packaging": PACKAGING, "pluggy": PLUGGY}  # 按已冻结的发行包标识分层。
