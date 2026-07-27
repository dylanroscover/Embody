"""
Test suite: MCP tool wrapper <-> main-thread handler signature conformance.

THE GAP THIS CLOSES: no test in the suite invoked a registered
`@self.mcp.tool()` wrapper. Every MCP test calls the HANDLER directly
(e.g. `self.envoy._remove_externalization_tag(op_path=...)`), so the layer
that builds the advertised schema and the `_execute_in_td` params dict was
never exercised. A wrapper could advertise and forward a parameter its
handler could not accept, and nothing failed.

That shipped: from v6.0.154 to 6.0.158 the `remove_externalization_tag`
tool was DEAD. Its wrapper put `delete_file` in the params dict
unconditionally, dispatch is `handler(**params)`, and the handler took
only `op_path` -- so every call from every client returned
`{'error': "... unexpected keyword argument 'delete_file'"}`. Two releases
went out that way with 2,300+ tests green.

Analysis is static (ast over EnvoyExt.py) rather than live, so it covers
every tool without invoking any of them -- invoking them would mutate the
project.

Three directions of drift, all real failure modes:
  A. forwarded key the handler does not accept -> TypeError, tool is dead
  B. handler required param never forwarded    -> TypeError, tool is dead
  C. advertised param never forwarded AND never used in the wrapper body
     -> SILENT no-op (the caller sets a flag and nothing happens), which
     is the worse failure because nothing surfaces
"""

import ast
from pathlib import Path

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


# _execute_operation strips these before `handler(**params)`; they belong
# to the multi-session gate, not to any handler.
STRIPPED_BEFORE_DISPATCH = {'override'}


_ANALYSIS = {}   # parsed once per session, not per test


class TestEnvoyToolSchema(EmbodyTestCase):

    def setUp(self):
        # setUp, not setUpClass -- this runner instantiates test cases
        # without calling class-level fixtures.
        cls = type(self)
        if not _ANALYSIS:
            src = Path(project.folder) / 'embody' / 'Embody' / 'EnvoyExt.py'
            tree = ast.parse(src.read_text(encoding='utf-8'))
            _ANALYSIS.update({
                'source_path': src,
                'tools': cls._collectTools(tree),
                'handler_map': cls._collectHandlerMap(tree),
                'methods': cls._collectMethods(tree),
            })
        self.source_path = _ANALYSIS['source_path']
        self.tools = _ANALYSIS['tools']
        self.handler_map = _ANALYSIS['handler_map']
        self.methods = _ANALYSIS['methods']

    # --- static collection ------------------------------------------------

    @staticmethod
    def _isMcpTool(node):
        for dec in getattr(node, 'decorator_list', []):
            f = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(f, ast.Attribute) and f.attr == 'tool':
                return True
        return False

    @staticmethod
    def _paramNames(fn):
        a = fn.args
        names = [p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)]
        return [n for n in names if n != 'self']

    @staticmethod
    def _requiredParams(fn):
        a = fn.args
        pos = a.posonlyargs + a.args
        n_def = len(a.defaults)
        req = [p.arg for p in (pos[:len(pos) - n_def] if n_def else pos)]
        req += [p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults)
                if d is None]
        return [n for n in req if n != 'self']

    @classmethod
    def _collectTools(cls, tree):
        tools = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and cls._isMcpTool(node)):
                continue
            opname, keys, literal = None, set(), True
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if not (isinstance(f, ast.Attribute)
                        and f.attr == '_execute_in_td'):
                    continue
                if sub.args and isinstance(sub.args[0], ast.Constant):
                    opname = sub.args[0].value
                if len(sub.args) > 1:
                    d = sub.args[1]
                    if isinstance(d, ast.Dict):
                        for k in d.keys:
                            if isinstance(k, ast.Constant):
                                keys.add(k.value)
                            else:
                                literal = False
                    else:
                        literal = False
                break
            # Names referenced anywhere in the wrapper body -- an advertised
            # param consumed locally (capture_top's `inline`) is legitimate.
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            tools[node.name] = {
                'advertised': cls._paramNames(node),
                'operation': opname,
                'forwarded': keys,
                'literal': literal,
                'body_names': used,
                'line': node.lineno,
            }
        return tools

    @staticmethod
    def _collectHandlerMap(tree):
        handler_map = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == '_execute_operation'):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Dict):
                    continue
                for k, v in zip(sub.keys, sub.values):
                    if (isinstance(k, ast.Constant)
                            and isinstance(v, ast.Attribute)
                            and isinstance(v.value, ast.Name)
                            and v.value.id == 'self'):
                        handler_map[k.value] = v.attr
        return handler_map

    @classmethod
    def _collectMethods(cls, tree):
        methods = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith('_'):
                methods[node.name] = {
                    'accepted': cls._paramNames(node),
                    'required': cls._requiredParams(node),
                    'kwargs': node.args.kwarg is not None,
                    'line': node.lineno,
                }
        return methods

    def _dispatched(self):
        """(tool_name, tool, handler_name, handler) for every tool that
        routes through _execute_in_td."""
        for name, t in sorted(self.tools.items()):
            if not t['operation']:
                continue          # bridge/local tool, dispatched positionally
            mname = self.handler_map.get(t['operation'])
            if not mname or mname not in self.methods:
                continue
            yield name, t, mname, self.methods[mname]

    # --- sanity -----------------------------------------------------------

    def test_source_parsed_and_tools_found(self):
        """Guard the guard: a broken collector must not pass vacuously."""
        self.assertTrue(self.source_path.is_file())
        self.assertGreater(len(self.tools), 40,
                           f'expected the full tool surface, got {len(self.tools)}')
        self.assertGreater(len(self.handler_map), 40)
        self.assertIn('remove_externalization_tag', self.tools)

    def test_every_params_dict_is_a_plain_literal(self):
        """If a wrapper stops building its params dict as an inline
        literal (a conditional key, a **spread), 'forwarded' silently
        becomes incomplete and directions A and C degrade to vacuous
        passes -- reintroducing the blind spot this suite exists to close.
        Fail loudly instead, so the collector gets taught the new shape."""
        bad = [f"{name} (L{t['line']})"
               for name, t in sorted(self.tools.items())
               if t['operation'] and not t['literal']]
        self.assertEqual(
            [], bad,
            'params dict is not a plain literal, so forwarded-key analysis '
            'is incomplete for: ' + '; '.join(bad))

    def test_each_tool_dispatches_from_exactly_one_call_site(self):
        """The collector reads the FIRST _execute_in_td call in a wrapper.
        A wrapper with two dispatch paths would be analysed on only one of
        them, so assert that shape does not exist."""
        multi = []
        for node in ast.walk(self.tree_for_multicheck()):
            if not (isinstance(node, ast.FunctionDef)
                    and type(self)._isMcpTool(node)):
                continue
            calls = [s for s in ast.walk(node)
                     if isinstance(s, ast.Call)
                     and isinstance(s.func, ast.Attribute)
                     and s.func.attr == '_execute_in_td']
            if len(calls) > 1:
                multi.append(f'{node.name} (L{node.lineno}): {len(calls)}')
        self.assertEqual(
            [], multi,
            'wrapper has more than one _execute_in_td call site: '
            + '; '.join(multi))

    def tree_for_multicheck(self):
        return ast.parse(self.source_path.read_text(encoding='utf-8'))

    # --- direction A ------------------------------------------------------

    def test_every_forwarded_param_is_accepted_by_its_handler(self):
        """REGRESSION (v6.0.154-6.0.157 dead tool)."""
        bad = []
        for name, t, mname, h in self._dispatched():
            if h['kwargs']:
                continue
            extra = sorted((t['forwarded'] - STRIPPED_BEFORE_DISPATCH)
                           - set(h['accepted']))
            if extra:
                bad.append(
                    f"{name} (L{t['line']}) forwards {extra} but "
                    f"{mname} (L{h['line']}) accepts {h['accepted']}")
        self.assertEqual(
            [], bad,
            'tool forwards a parameter its handler cannot accept -- '
            'dispatch is handler(**params), so EVERY call to these tools '
            'fails with a TypeError: ' + '; '.join(bad))

    # --- direction B ------------------------------------------------------

    def test_every_required_handler_param_is_forwarded(self):
        bad = []
        for name, t, mname, h in self._dispatched():
            missing = sorted(set(h['required'])
                             - (t['forwarded'] - STRIPPED_BEFORE_DISPATCH))
            if missing:
                bad.append(
                    f"{name} (L{t['line']}) never forwards required "
                    f"{missing} of {mname} (L{h['line']})")
        self.assertEqual(
            [], bad,
            'handler requires a parameter the tool never sends: '
            + '; '.join(bad))

    # --- direction C ------------------------------------------------------

    def test_every_advertised_param_is_forwarded_or_used_locally(self):
        """An advertised parameter that is neither forwarded nor read in the
        wrapper body is a SILENT no-op -- the agent sets it, nothing
        happens, and no error is raised."""
        bad = []
        for name, t, mname, h in self._dispatched():
            for p in sorted(set(t['advertised']) - t['forwarded']):
                if p in t['body_names']:
                    continue      # consumed in the wrapper itself
                bad.append(f"{name} (L{t['line']}) advertises '{p}'")
        self.assertEqual(
            [], bad,
            'advertised parameter is silently ignored: ' + '; '.join(bad))

    # --- dispatch integrity ----------------------------------------------

    def test_no_two_tools_dispatch_the_same_operation(self):
        """A collision would let one tool's contract silently shadow
        another's in any name-keyed analysis."""
        seen = {}
        for name, t in self.tools.items():
            if t['operation']:
                seen.setdefault(t['operation'], []).append(name)
        dupes = {k: sorted(v) for k, v in seen.items() if len(v) > 1}
        self.assertEqual({}, dupes, f'duplicate operation dispatch: {dupes}')

    def test_every_dispatched_operation_has_a_handler(self):
        bad = [f"{name} -> '{t['operation']}'"
               for name, t in sorted(self.tools.items())
               if t['operation'] and t['operation'] not in self.handler_map]
        self.assertEqual(
            [], bad,
            'tool dispatches an operation with no handler entry in '
            '_execute_operation: ' + '; '.join(bad))
