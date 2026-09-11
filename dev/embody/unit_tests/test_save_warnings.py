"""Off-TD contract tests for the save-time report (issue #109).

Two pure decisions, pinned on both CI legs:

- EnvoyExt._save_warnings: which log entries a finished save_project job
  record lists. A save reinitializes extensions, so _logs on the next call
  is not a reliable channel; the record is. The helper must hold only the
  save's own WARNING/ERROR lines, across a buffer the reinit replaced, and
  its cap must never evict the earliest (the pre-save content report).
  _save_log_ring picks the deque it reads.
- EmbodyExt._storageLossConsequence: the level and text for storage a
  .tdxn does not hold. WARNING only when this save or the next open
  destroys it -- a deliberate Export-mode choice must not WARN every save.

No TouchDesigner import: both are pure by contract.
"""

from __future__ import annotations

import importlib.util
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_EMBODY = Path(__file__).resolve().parents[1] / "Embody"


def _load(name: str, relpath: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _EMBODY / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_ENVOY = _load("save_warnings_envoy", "EnvoyExt.py")
_EMBODY_EXT = _load("save_warnings_embodyext", "EmbodyExt.py").EmbodyExt

save_warnings = _ENVOY._save_warnings
consequence = _EMBODY_EXT._storageLossConsequence


def _entry(i: int, level: str, message: str) -> dict:
    return {'id': i, 'level': level, 'message': message}


# --- EnvoyExt._save_warnings ---------------------------------------------

def test_only_entries_after_the_mark_count() -> None:
    buf = deque([_entry(1, 'WARNING', 'before the save'),
                 _entry(2, 'INFO', 'mark')], maxlen=200)
    mark = buf[-1]['id']
    buf.append(_entry(3, 'WARNING', 'storage not in the .tdxn'))
    buf.append(_entry(4, 'SUCCESS', 'exported'))
    buf.append(_entry(5, 'ERROR', 'write failed'))
    assert save_warnings(buf, mark, buf) == [
        'write failed', 'storage not in the .tdxn']


def test_a_replaced_buffer_contributes_every_entry() -> None:
    """A reinit mid-save swaps the deque and restarts ids at 1: the old
    buffer keeps the pre-reinit lines, the new one holds only save lines."""
    old = deque([_entry(40, 'INFO', 'mark')], maxlen=200)
    mark = 40
    old.append(_entry(41, 'WARNING', 'pre-reinit warning'))
    new = deque([_entry(1, 'WARNING', 'post-reinit warning'),
                 _entry(2, 'DEBUG', 'noise')], maxlen=200)
    assert save_warnings(old, mark, new) == [
        'pre-reinit warning', 'post-reinit warning']


def test_same_buffer_is_not_counted_twice() -> None:
    buf = deque([_entry(1, 'INFO', 'mark')], maxlen=200)
    buf.append(_entry(2, 'WARNING', 'once'))
    assert save_warnings(buf, 1, buf) == ['once']


def test_cap_keeps_the_earliest_and_counts_the_rest() -> None:
    """The pre-save content report is logged before Phase 1's per-op lines
    (one per unanswered palette clone): keeping the newest evicted it."""
    buf = deque([_entry(0, 'INFO', 'mark')], maxlen=200)
    buf.append(_entry(1, 'WARNING', 'TDXN storage not in the .tdxn'))
    for i in range(2, 13):
        buf.append(_entry(i, 'WARNING', 'palette w%d' % i))
    out = save_warnings(buf, 0, buf)
    assert out[0] == 'TDXN storage not in the .tdxn'
    assert out[1:8] == ['palette w%d' % i for i in range(2, 9)]
    assert len(out) == 9
    assert out[8].startswith('... (+4 more')
    assert out[8].isascii()
    assert save_warnings(buf, 0, buf, cap=2)[:2] == [
        'TDXN storage not in the .tdxn', 'palette w2']


def test_errors_first_then_warnings_in_log_order() -> None:
    buf = deque([_entry(0, 'INFO', 'mark')], maxlen=200)
    buf.append(_entry(1, 'WARNING', 'w1'))
    buf.append(_entry(2, 'ERROR', 'e1'))
    buf.append(_entry(3, 'WARNING', 'w2'))
    buf.append(_entry(4, 'ERROR', 'e2'))
    assert save_warnings(buf, 0, buf) == ['e1', 'e2', 'w1', 'w2']


def test_repeats_collapse() -> None:
    buf = deque([_entry(0, 'INFO', 'mark')], maxlen=200)
    for i in range(1, 20):
        buf.append(_entry(i, 'WARNING', 'same line'))
    buf.append(_entry(20, 'WARNING', 'other line'))
    assert save_warnings(buf, 0, buf) == ['same line', 'other line']


def test_exactly_cap_has_no_marker() -> None:
    buf = deque([_entry(0, 'INFO', 'mark')], maxlen=200)
    for i in range(1, 9):
        buf.append(_entry(i, 'WARNING', 'w%d' % i))
    assert save_warnings(buf, 0, buf) == ['w%d' % i for i in range(1, 9)]


def test_no_baseline_attributes_nothing() -> None:
    after = deque([_entry(1, 'WARNING', 'unknown provenance')])
    assert save_warnings(None, 0, after) == []


def test_empty_before_buffer_is_a_valid_baseline() -> None:
    """A fresh deque (no entries yet) marks 0: everything later counts."""
    buf = deque(maxlen=200)
    buf.append(_entry(1, 'WARNING', 'first'))
    assert save_warnings(buf, 0, buf) == ['first']


# --- EnvoyExt._save_log_ring ----------------------------------------------

log_ring = _ENVOY._save_log_ring


def test_ring_prefers_the_warning_ring() -> None:
    notable, full = deque(maxlen=100), deque(maxlen=200)
    ext = SimpleNamespace(_notable_log_buffer=notable, _log_buffer=full)
    assert log_ring(ext) is notable


def test_an_empty_warning_ring_is_still_the_ring() -> None:
    """An empty deque is falsy: a truthiness fallback would read the full
    ring, whose DEBUG churn is what the warning ring exists to avoid."""
    notable, full = deque(maxlen=100), deque([_entry(1, 'DEBUG', 'x')])
    ext = SimpleNamespace(_notable_log_buffer=notable, _log_buffer=full)
    assert log_ring(ext) is notable


def test_ring_falls_back_to_the_full_ring_then_none() -> None:
    full = deque(maxlen=200)
    assert log_ring(SimpleNamespace(_log_buffer=full)) is full
    assert log_ring(SimpleNamespace()) is None


# --- EmbodyExt._storageLossConsequence ------------------------------------

def test_export_mode_is_info() -> None:
    level, text = consequence('export', True, True, False)
    assert level == 'INFO'
    assert 'saved .toe keep it' in text


def test_roundtrip_strip_is_warning() -> None:
    level, text = consequence('full', True, True, False)
    assert level == 'WARNING'
    assert 'gone from the live session after this save' in text


def test_roundtrip_create_on_start_without_strip_is_warning() -> None:
    level, text = consequence('full', False, True, False)
    assert level == 'WARNING'
    assert 'on the next open' in text


def test_roundtrip_without_strip_or_rebuild_is_info() -> None:
    level, _ = consequence('full', False, False, False)
    assert level == 'INFO'


def test_root_keys_are_info_in_every_mode() -> None:
    """Strip and clear_first destroy children only: root keys ride the
    shell in the .toe whatever the mode."""
    for mode in ('export', 'full', 'off'):
        for strip in (True, False):
            for create in (True, False):
                level, text = consequence(mode, strip, create, True)
                assert level == 'INFO', (mode, strip, create)
                assert 'rides the COMP shell' in text


def test_consequence_text_is_ascii() -> None:
    for mode in ('export', 'full'):
        for strip in (True, False):
            for create in (True, False):
                for comp_only in (True, False):
                    _, text = consequence(mode, strip, create, comp_only)
                    assert text.isascii(), text


def test_summarize_paths_is_ascii_and_capped() -> None:
    summarize = _EMBODY_EXT._summarizePaths
    assert summarize(['a', 'b']) == 'a, b'
    out = summarize(['p%d' % i for i in range(8)])
    assert out == 'p0, p1, p2, p3, p4, ... (+3 more)'
    assert out.isascii()
