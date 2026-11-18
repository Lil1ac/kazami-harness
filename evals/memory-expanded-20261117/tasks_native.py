"""三份完整发行源码上的十八个独立功能合同与行为断言。"""

from fixtures import Task


SETTINGS = (
    Task("arg-count-cap", "locate", "sources.providers.cli", "CLI preview membership count",
         "The help preview cannot cap the number of known argument values before rendering.",
         "Add keyword-only limit=None to _count_option_values. A nonnegative limit caps known finite counts. "
         "Unknown counts remain None even with limit=0. Reject a negative limit with ValueError.",
         (
             ("def _count_option_values(action: Action | None) -> int | None:",
              "def _count_option_values(action: Action | None, *, limit: int | None = None) -> int | None:"),
             ("    if action is None:\n        return 0\n    if action.nargs is None or action.nargs == '?':\n"
              "        return 1\n    if isinstance(action.nargs, int):\n        return action.nargs\n    return None",
              "    if limit is not None and limit < 0:\n        raise ValueError('limit must be nonnegative')\n"
              "    if action is None:\n        count = 0\n    elif action.nargs is None or action.nargs == '?':\n"
              "        count = 1\n    elif isinstance(action.nargs, int):\n        count = action.nargs\n"
              "    else:\n        return None\n    return count if limit is None else min(count, limit)"),
         ),
         """
         from types import SimpleNamespace
         f = m._count_option_values
         assert f(SimpleNamespace(nargs=4), limit=2) == 2
         assert f(SimpleNamespace(nargs='?'), limit=5) == 1
         assert f(SimpleNamespace(nargs='*'), limit=0) is None
         assert f(None) == 0 and f(SimpleNamespace(nargs=7)) == 7
         try:
             f(None, limit=-1)
         except ValueError:
             pass
         else:
             raise AssertionError('negative limit was accepted')
         """),
    Task("path-label-preference", "locate", "utils", "Path label for diagnostic reports",
         "The help preview needs to label a symbolic link as a link instead of showing the destination kind.",
         "Add keyword-only prefer_symlink=False to path_type_label. When true, classify a symbolic link as symlink "
         "before filesystem destination checks, including dangling links. Legacy calls preserve destination labels "
         "and raise the existing missing-path assertion for missing ordinary entries.",
         (("def path_type_label(p: Path) -> str:", "def path_type_label(p: Path, *, prefer_symlink: bool = False) -> str:"),
          ("    assert p.exists(), 'path does not exist'", "    if prefer_symlink and p.is_symlink():\n        return 'symlink'\n    assert p.exists(), 'path does not exist'")),
         """
         from tempfile import TemporaryDirectory
         with TemporaryDirectory() as folder:
             root = Path(folder)
             plain = root / 'value'
             plain.write_text('ready')
             link = root / 'link'
             link.symlink_to(plain)
             dangling = root / 'future'
             dangling.symlink_to(root / 'missing')
             assert m.path_type_label(link, prefer_symlink=True) == 'symlink'
             assert m.path_type_label(dangling, prefer_symlink=True) == 'symlink'
             assert m.path_type_label(plain, prefer_symlink=True) == 'file'
             assert m.path_type_label(link) == 'file'
         """),
    Task("fallback-skip-blank", "knowledge", "sources.providers.nested_secrets", "Blank override selection",
         "A user has an empty textual override and would like to opt into trying the next candidate.",
         "Add keyword-only skip_blank=False to first_not_none. When true skip empty or whitespace-only str values "
         "as well as None. Retain false and zero as values and return nonempty strings untrimmed.",
         (("def first_not_none(*objs: Any) -> Any:\n    return next(filter(lambda o: o is not None, objs), None)",
           "def first_not_none(*objs: Any, skip_blank: bool = False) -> Any:\n"
           "    for obj in objs:\n        if obj is None or (skip_blank and isinstance(obj, str) and not obj.strip()):\n"
           "            continue\n        return obj\n    return None"),),
         """
         f = m.first_not_none
         assert f(None, '  ', None, ' value ', skip_blank=True) == ' value '
         assert f('  ', False, 'next', skip_blank=True) is False
         assert f('', 0, skip_blank=True) == 0
         assert f(None, '\\t', skip_blank=True) is None
         assert f(None, '', 'next') == ''
         """),
    Task("description-collapse", "knowledge", "sources.providers.cli", "Compact CLI descriptions",
         "A one-line help display needs an opt-in view of the model's effective description.",
         "The keyword-only compact flag defaults to False on _get_model_description. When true, collapse "
         "runs of whitespace in the selected description into a single space and trim it. Preserve preference "
         "for schema-extra description, a docstring fallback, and the distinction between no text and empty text.",
         (("def _get_model_description(model_cls: type[Any]) -> str | None:",
           "def _get_model_description(model_cls: type[Any], *, compact: bool = False) -> str | None:"),
          ("    config: Any = {}\n    if is_model_class(model_cls):",
           "    def prepare(value: str) -> str:\n        return ' '.join(value.split()) if compact else value\n"
           "    config: Any = {}\n    if is_model_class(model_cls):"),
          ("            return desc\n    elif callable(json_schema_extra):",
           "            return prepare(desc)\n    elif callable(json_schema_extra):"),
          ("                return desc\n        except Exception:",
           "                return prepare(desc)\n        except Exception:"),
          ("        return dedent(model_cls.__doc__)\n    return None",
           "        return prepare(dedent(model_cls.__doc__))\n    return None")),
         """
         from pydantic import BaseModel, ConfigDict
         class Extra(BaseModel):
             'Fallback.'
             model_config = ConfigDict(json_schema_extra={'description': '  Alpha\\n  Beta  '})
         class Manual:
             'Manual   preview'
         class NoText:
             pass
         f = m._get_model_description
         assert f(Extra, compact=True) == 'Alpha Beta'
         assert f(Extra) == '  Alpha\\n  Beta  '
         assert f(Manual, compact=True) == 'Manual preview'
         assert f(NoText, compact=True) is None
         """),
    Task("debug-switch-token", "safety", "utils", "Current settings debug switch",
         "Support staff report the published on-value differs from older handover notes.",
         "PYDANTIC_SETTINGS_DEBUG enables the current profile for enabled plus the existing 1, true, yes and on "
         "tokens, stripping spaces and ignoring case. The trial token debug is not an on-value.",
         (("in ('1', 'true', 'yes', 'on')", "in ('1', 'true', 'yes', 'on', 'enabled')"),),
         """
         import os
         for token, expected in [(' enabled ', True), ('EnAbLeD', True), ('on', True), ('true', True),
                                 ('debug', False), ('off', False), ('', False)]:
             os.environ['PYDANTIC_SETTINGS_DEBUG'] = token
             assert m._settings_debug_enabled() is expected, token
         """,
         ("expired", "Debug was the accepted additional token, enabled was off in the trial profile.",
          """
          import os
          os.environ['PYDANTIC_SETTINGS_DEBUG'] = 'debug'
          OLD = m._settings_debug_enabled()
          """)),
    Task("settings-name-precedence", "safety", "sources.utils", "Current settings name fallback",
         "A migrated settings class disables alias validation but carries a legacy populate-by-name false value.",
         "The current priority is explicit validate_by_name, then the validate_by_alias=False implication, "
         "then legacy populate_by_name. Default with none of these is false. Preserve explicit true or false.",
         (("    populate_by_name = config.get('populate_by_name')\n    if populate_by_name is not None:\n"
           "        return populate_by_name\n    return config.get('validate_by_alias') is False",
           "    if config.get('validate_by_alias') is False:\n        return True\n"
           "    return bool(config.get('populate_by_name'))"),),
         """
         f = m._validate_by_name_enabled
         assert f({'validate_by_alias': False, 'populate_by_name': False}) is True
         assert f({'validate_by_name': False, 'validate_by_alias': False, 'populate_by_name': True}) is False
         assert f({'validate_by_name': True, 'validate_by_alias': True}) is True
         assert f({'populate_by_name': True}) is True and f({}) is False
         """,
         ("changed", "The legacy populate_by_name value takes precedence over the alias-disabled implication.",
          "OLD = m._validate_by_name_enabled({'validate_by_alias': False, 'populate_by_name': False}) is False")),
)


MARSH = (
    Task("text-bom-preview", "locate", "utils", "Preview import text decoding",
         "A user-facing preview displays a byte order mark at the beginning of UTF-8 text.",
         "Add keyword-only strip_bom=False to ensure_text_type. An opt-in decodes bytes as utf-8-sig, canonical "
         "UTF-8 for default calls. Existing str inputs are unchanged even when they begin with U+FEFF.",
         (("def ensure_text_type(val: str | bytes) -> str:", "def ensure_text_type(val: str | bytes, *, strip_bom: bool = False) -> str:"),
          ('        val = val.decode("utf-8")', '        val = val.decode("utf-8-sig" if strip_bom else "utf-8")')),
         """
         raw = bytes.fromhex('efbbbf') + 'café'.encode()
         assert m.ensure_text_type(raw, strip_bom=True) == 'café'
         assert m.ensure_text_type(raw) == chr(0xfeff) + 'café'
         assert m.ensure_text_type(chr(0xfeff) + 'A', strip_bom=True) == chr(0xfeff) + 'A'
         assert m.ensure_text_type(b'empty', strip_bom=True) == 'empty'
         """),
    Task("pluck-missing-default", "locate", "utils", "Sparse import column extraction",
         "A pluck preview crashes if some rows lack an optional column.",
         "Add keyword-only default=missing to pluck. No supplied default keeps KeyError on an absent column. "
         "An explicit default is substituted only for absent keys, retaining false values and None.",
         (("def pluck(dictlist: list[dict[str, typing.Any]], key: str):",
           "def pluck(dictlist: list[dict[str, typing.Any]], key: str, *, default=missing):"),
          ("    return [d[key] for d in dictlist]",
           "    if default is missing:\n        return [d[key] for d in dictlist]\n    return [d.get(key, default) for d in dictlist]")),
         """
         assert m.pluck([{'x': False}, {}, {'x': None}], 'x', default='none') == [False, 'none', None]
         assert m.pluck([{}], 'x', default=None) == [None]
         assert m.pluck([{'x': 0}], 'x') == [0]
         try:
             m.pluck([{}], 'x')
         except KeyError:
             pass
         else:
             raise AssertionError('default historically raised')
         """),
    Task("literal-field-key", "knowledge", "utils", "Literal dotted field access",
         "A flat import source has columns containing literal dots and needs an opt-in access mode.",
         "Add keyword-only literal=False to get_value. True bypasses only dotted traversal and uses the "
         "existing one-key lookup and fallback. Other callers still traverse dotted paths.",
         (("def get_value(obj, key: int | str, default=missing):", "def get_value(obj, key: int | str, default=missing, *, literal=False):"),
          ('    if not isinstance(key, int) and "." in key:', '    if not literal and not isinstance(key, int) and "." in key:')),
         """
         data = {'contact.email': 'flat', 'contact': {'email': 'nested'}}
         assert m.get_value(data, 'contact.email', literal=True) == 'flat'
         assert m.get_value(data, 'contact.email') == 'nested'
         assert m.get_value(data, 'contact.name', 'NA', literal=True) == 'NA'
         assert m.get_value(['zero'], 0, literal=True) == 'zero'
         """),
    Task("duration-floor", "knowledge", "utils", "Display-only nonnegative duration",
         "The UI wants to opt into a zero floor for imported negative durations without changing signed accounting.",
         "Add keyword-only nonnegative=False to timedelta_to_microseconds. True clamps signed microseconds at zero. "
         "Use integer arithmetic and retain exact signed behavior by default.",
         (("def timedelta_to_microseconds(value: dt.timedelta) -> int:",
           "def timedelta_to_microseconds(value: dt.timedelta, *, nonnegative: bool = False) -> int:"),
          ("    return (value.days * (24 * 3600) + value.seconds) * 1000000 + value.microseconds",
           "    count = (value.days * (24 * 3600) + value.seconds) * 1000000 + value.microseconds\n"
           "    return max(count, 0) if nonnegative else count")),
         """
         from datetime import timedelta
         f = m.timedelta_to_microseconds
         assert f(timedelta(microseconds=-7), nonnegative=True) == 0
         assert f(timedelta(microseconds=-7)) == -7
         assert f(timedelta(days=23, microseconds=3), nonnegative=True) == 23 * 86400000000 + 3
         assert f(timedelta()) == 0
         """),
    Task("historical-ms-dates", "safety", "utils", "Current historical timestamp import",
         "Date previews imported from a historical dataset should display pre-epoch dates from millisecond input.",
         "Accept finite negative numeric values in from_timestamp_ms, returning the same naive UTC format as "
         "positive values. Keep the bool rejection and existing positive ms conversion. Keep from_timestamp "
         "strict on negative seconds. Handle platform conversion failures with ValueError.",
         (("    return from_timestamp(value / 1000)", "    if value >= 0:\n        return from_timestamp(value / 1000)\n"
           "    try:\n        return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).replace(tzinfo=None)\n"
           "    except (OverflowError, OSError) as exc:\n        raise ValueError('Error converting value to datetime') from exc"),),
         """
         from datetime import datetime
         assert m.from_timestamp_ms(-1250) == datetime(1969, 12, 31, 23, 59, 58, 750000)
         assert m.from_timestamp_ms('1500') == datetime(1970, 1, 1, 0, 0, 1, 500000)
         for f, raw in ((m.from_timestamp_ms, True), (m.from_timestamp, -1)):
             try:
                 f(raw)
             except ValueError:
                 pass
             else:
                 raise AssertionError('must stay strict on this entry')
         """,
         ("withdrawn", "Historically the millisecond and second entry points both rejected earlier-than-epoch dates.",
          """
          try:
              m.from_timestamp_ms(-1250)
          except ValueError:
              OLD = True
          else:
              OLD = False
          """)),
    Task("select-label-fallback", "safety", "validate", "Current select field fallback labels",
         "A selection preview presents options without names when the supplied list of labels is too short.",
         "OneOf.options uses str(choice) for an omitted label only. Keep explicit empty labels, truncate extra "
         "labels, and keep valuegetter behavior independent of the label.",
         (("        padded_labels = labels[: len(choices)] + (\"\",) * max(\n            0, len(choices) - len(labels)\n        )",
           "        padded_labels = labels[: len(choices)] + tuple(str(choice) for choice in choices[len(labels):])"),),
         """
         assert list(m.OneOf([2, 4], ['']).options(lambda x: x * 2)) == [(4, ''), (8, '4')]
         assert list(m.OneOf(['a'], ['A','unused']).options()) == [('a', 'A')]
         assert list(m.OneOf([1]).options()) == [('1', '1')]
         """,
         ("foreign", "The companion UI fills missing labels with the empty string, preserving a blank option.",
          "OLD = list(m.OneOf([1]).options()) == [('1', '')]")),
)


JINJA = (
    Task("center-fillchar", "locate", "filters", "Printable centered report labels",
         "Report authors want dot fill while keeping familiar defaults for existing templates.",
         "Add a fillchar argument to do_center, defaulting to one space. Delegate character validation and "
         "width semantics to the text's center method and preserve Markup type.",
         (("def do_center(value: str, width: int = 80) -> str:", "def do_center(value: str, width: int = 80, fillchar: str = ' ') -> str:"),
          ("    return soft_str(value).center(width)", "    return soft_str(value).center(width, fillchar)")),
         """
         from markupsafe import Markup
         assert m.do_center('A', 4, '.') == '.A..'
         assert m.do_center('A', 3) == ' A '
         value = m.do_center(Markup('<em>'), 6, '.')
         assert isinstance(value, Markup) and value == '.<em>.'
         try:
             m.do_center('x', 3, '..')
         except TypeError:
             pass
         else:
             raise AssertionError('invalid filler accepted')
         """),
    Task("iterator-consume-limit", "locate", "utils", "Bounded preview consumption",
         "A preview pass wants to advance only some records of an iterable shared with the next renderer.",
         "Add keyword-only limit=None to consume. Omission uses the original complete consumption. A supplied "
         "nonnegative integer consumes at most that many items without reading the next one. Negative raises "
         "ValueError and zero consumes nothing. The return value stays None.",
         (("def consume(iterable: t.Iterable[t.Any]) -> None:",
           "def consume(iterable: t.Iterable[t.Any], *, limit: t.Optional[int] = None) -> None:"),
          ("    for _ in iterable:\n        pass\n\n\ndef clear_caches",
           "    if limit is not None:\n        from itertools import islice\n        iterable = islice(iterable, limit)\n"
           "    for _ in iterable:\n        pass\n\n\ndef clear_caches")),
         """
         rows = iter(range(5))
         assert m.consume(rows, limit=0) is None and next(rows) == 0
         assert m.consume(rows, limit=2) is None and next(rows) == 3
         assert m.consume(rows) is None and list(rows) == []
         try:
             m.consume(iter([1]), limit=-1)
         except ValueError:
             pass
         else:
             raise AssertionError('negative limit was accepted')
         """),
    Task("qualified-type-label", "knowledge", "utils", "Nested custom type labels",
         "The template inspector needs an option to distinguish classes with the same short name.",
         "Add keyword-only qualified=False to object_type_repr. Use __qualname__ for non-builtin custom "
         "types when enabled. Keep the normal module prefix, builtins, and singleton display unchanged.",
         (("def object_type_repr(obj: t.Any) -> str:", "def object_type_repr(obj: t.Any, *, qualified: bool = False) -> str:"),
          ('    return f"{cls.__module__}.{cls.__name__} object"',
           '    name = cls.__qualname__ if qualified else cls.__name__\n    return f"{cls.__module__}.{name} object"')),
         """
         class Job:
             class Item:
                 pass
         assert m.object_type_repr(Job.Item(), qualified=True).endswith('.Job.Item object')
         assert m.object_type_repr(Job.Item()).endswith('.Item object')
         assert '.Job.Item object' not in m.object_type_repr(Job.Item())
         assert m.object_type_repr(None, qualified=True) == 'None'
         assert m.object_type_repr(3, qualified=True) == 'int object'
         """),
    Task("empty-cache-copy", "knowledge", "utils", "Reuse an empty template cache",
         "A preview environment wants a factory-like copy of its LRU cache with the same budget.",
         "LRUCache.copy gains keyword-only empty=False. With empty=True preserve class and capacity and "
         "do not copy mapping or queue. Keep shallow populated-copy order for the default.",
         (('    def copy(self) -> "te.Self":', '    def copy(self, *, empty: bool = False) -> "te.Self":'),
          ("        rv._mapping.update(self._mapping)\n        rv._queue.extend(self._queue)",
           "        if not empty:\n            rv._mapping.update(self._mapping)\n            rv._queue.extend(self._queue)")),
         """
         cache = m.LRUCache(3)
         cache['first'] = ['payload']
         cache['second'] = 4
         fresh = cache.copy(empty=True)
         assert len(fresh) == 0 and fresh.capacity == 3 and type(fresh) is type(cache)
         assert cache.copy().items() == cache.items()
         assert cache.copy()['first'] is cache['first']
         fresh['third'] = 6
         assert 'third' not in cache
         """),
    Task("pretty-report-dict-order", "safety", "utils", "Current pretty report key order",
         "Pretty-printing a mapping for a preview changes the user's field ordering.",
         "pformat preserves mapping order by default for preview output. Add keyword-only sort_dicts=False, "
         "then pass it to the standard formatter. Explicit true still alphabetizes keys, and printing "
         "non-mapping values is unchanged.",
         (("def pformat(obj: t.Any) -> str:", "def pformat(obj: t.Any, *, sort_dicts: bool = False) -> str:"),
          ("    return pformat(obj)\n", "    return pformat(obj, sort_dicts=sort_dicts)\n")),
         """
         source = {'zeta': 1, 'alpha': 2}
         assert m.pformat(source) == "{'zeta': 1, 'alpha': 2}"
         assert m.pformat(source, sort_dicts=True) == "{'alpha': 2, 'zeta': 1}"
         assert m.pformat(['last', 'first']) == "['last', 'first']"
         assert m.pformat({'first': source}) == "{'first': {'zeta': 1, 'alpha': 2}}"
         """,
         ("expired", "Preview code always sorted mapping keys before displaying them.",
          "OLD = m.pformat({'zeta': 1, 'alpha': 2}) == \"{'alpha': 2, 'zeta': 1}\"")),
    Task("column-empty-placeholder", "safety", "filters", "Current empty column layout",
         "Empty report input still emits placeholder items in every otherwise empty column.",
         "When the source is empty, sync_do_slice yields exactly the requested number of empty lists even "
         "when fill_with is supplied. Nonempty lists retain normal balancing and padding.",
         (("        if fill_with is not None and slice_number >= slices_with_extra:",
           "        if length and fill_with is not None and slice_number >= slices_with_extra:"),),
         """
         assert list(m.sync_do_slice([], 3, '-')) == [[],[],[]]
         assert list(m.sync_do_slice([], 2)) == [[],[]]
         assert list(m.sync_do_slice([1,2,3], 2, '-')) == [[1,2],[3,'-']]
         assert list(m.sync_do_slice([1,2], 2)) == [[1],[2]]
         """,
         ("changed", "The previous dashboard inserted one placeholder in every empty column when fill_with was set.",
          "OLD = list(m.sync_do_slice([], 2, '-')) == [['-'],['-']]")),
)


TASKS = {"settings": SETTINGS, "marsh": MARSH, "jinja": JINJA}  # 按冻结上游标识分层。
