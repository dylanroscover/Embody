"""The shipped guidance surface, checked as files (pytest, both CI legs).

Four hand-maintained surfaces name the skills and rules a user project gets:
EmbodyExt's template maps, the skill folders under .claude/, the routing
tables (skill-prerequisites.md, the CLAUDE.md and AGENTS.md templates, the
docs page), and the release skill's sync table. test_template_sync.py checks
content parity inside TouchDesigner; this suite checks the STRUCTURE with no
TD at all: frontmatter that every host validates, every shipped skill routed
in every table, reference files mapped both ways, client-neutral wording, and
dev-only rules that say so. Modelled on the validator TDMCPSkills ships
(audit 2026-09-25).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EMBODY_EXT = REPO / 'dev' / 'embody' / 'Embody' / 'EmbodyExt.py'
TEMPLATES = REPO / 'dev' / 'embody' / 'Embody' / 'templates'
SKILLS = REPO / '.claude' / 'skills'
RULES = REPO / '.claude' / 'rules'

ALLOWED_FRONTMATTER = {'name', 'description', 'disable-model-invocation'}
DESCRIPTION_MAX = 1024          # the agentskills.io ceiling every host applies
USER_INVOKED = {'brief', 'collab'}   # routed by their slash command, not a prerequisite row
# Shipped rules and skills reach Codex, Cursor, Gemini, OpenCode, ... unchanged,
# so they may not assume the reader is Claude. File names stay allowed.
CLIENT_WORD = re.compile(r'\bClaude\b(?! Docs)')
CLIENT_ALLOWED = ('CLAUDE.md', '.claude/')


def _class_dict(name):
    """A dict literal assigned as a class attribute in EmbodyExt (no TD import)."""
    tree = ast.parse(EMBODY_EXT.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in EmbodyExt.py')


RULE_MAP = _class_dict('_TEMPLATE_MAP_RULES')
SKILL_MAP = _class_dict('_TEMPLATE_MAP_SKILLS')
REF_MAP = _class_dict('_TEMPLATE_MAP_SKILL_REFS')
SHIPPED_SKILLS = sorted(SKILL_MAP.values())


def _frontmatter(text):
    lines = text.split('\n')
    assert lines[0] == '---', 'frontmatter must open on line 1'
    fields = {}
    for i, line in enumerate(lines[1:], start=2):
        if line == '---':
            return fields
        assert ':' in line and not line.startswith((' ', '\t')), f'line {i}: {line!r}'
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip().strip('"\'')
    raise AssertionError('frontmatter never closed')


def _read(path):
    return path.read_text(encoding='utf-8')


# --- frontmatter ----------------------------------------------------------------

def test_every_skill_has_portable_frontmatter():
    for skill_dir in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
        fields = _frontmatter(_read(skill_dir / 'SKILL.md'))
        assert fields.get('name') == skill_dir.name, skill_dir.name
        assert fields.get('description'), f'{skill_dir.name}: empty description'
        assert len(fields['description']) <= DESCRIPTION_MAX, skill_dir.name
        assert set(fields) <= ALLOWED_FRONTMATTER, f'{skill_dir.name}: {set(fields) - ALLOWED_FRONTMATTER}'


def test_skill_names_are_portable_slugs():
    for slug in SHIPPED_SKILLS:
        assert re.fullmatch(r'[a-z0-9]+(-[a-z0-9]+)*', slug), slug
        assert (SKILLS / slug / 'SKILL.md').is_file(), slug


# --- routing: every shipped skill is named in every table ---------------------------

def _mentions(text, slug):
    return re.search(r'(?<![\w-])/?%s(?![\w-])' % re.escape(slug), text) is not None


def test_prerequisites_table_routes_every_shipped_skill():
    table = _read(RULES / 'skill-prerequisites.md')
    for slug in SHIPPED_SKILLS:
        assert _mentions(table, slug), f'skill-prerequisites.md does not route {slug}'


def test_claude_md_template_routes_every_shipped_skill():
    text = _read(TEMPLATES / 'text_claude.md')
    for slug in SHIPPED_SKILLS:
        assert _mentions(text, slug), f'text_claude.md does not mention {slug}'


def test_agents_md_template_lists_every_shipped_skill():
    text = _read(TEMPLATES / 'text_agents_md.md')
    for slug in SHIPPED_SKILLS:
        assert _mentions(text, slug), f'text_agents_md.md does not list {slug}'


def test_docs_skill_table_lists_every_shipped_skill():
    text = _read(REPO / 'docs' / 'envoy' / 'claude-code.md')
    for slug in SHIPPED_SKILLS:
        assert _mentions(text, slug), f'docs/envoy/claude-code.md does not list {slug}'


def test_prerequisite_rows_point_at_real_skills():
    table = _read(RULES / 'skill-prerequisites.md')
    referenced = set(re.findall(r'`/([a-z0-9-]+)`', table))
    existing = {p.name for p in SKILLS.iterdir() if p.is_dir()}
    assert referenced <= existing, referenced - existing


# --- reference files: mapped both ways ----------------------------------------------

def test_reference_files_and_map_agree():
    on_disk = set()
    for slug in SHIPPED_SKILLS:
        refs = SKILLS / slug / 'references'
        if refs.is_dir():
            for f in sorted(refs.glob('*.md')):
                on_disk.add((slug, f'references/{f.name}'))
    mapped = set(REF_MAP.values())
    assert on_disk == mapped, {'unmapped': on_disk - mapped, 'missing': mapped - on_disk}


def test_reference_dat_names_follow_the_double_underscore_convention():
    for dat_name, (slug, relpath) in REF_MAP.items():
        stem = Path(relpath).stem.replace('-', '_')
        assert dat_name == f"text_skill_{slug.replace('-', '_')}__{stem}", dat_name


def test_skills_point_readers_at_their_references():
    for slug, relpath in REF_MAP.values():
        assert relpath in _read(SKILLS / slug / 'SKILL.md'), f'{slug} never mentions {relpath}'


# --- shipped templates are client-neutral ---------------------------------------------

def _shipped_templates():
    names = list(RULE_MAP) + list(SKILL_MAP) + list(REF_MAP)
    return [TEMPLATES / f'{n}.md' for n in names]


def test_shipped_templates_exist():
    for path in _shipped_templates():
        assert path.is_file(), path.name


def test_shipped_rules_and_skills_do_not_assume_the_reader_is_claude():
    offenders = []
    for path in _shipped_templates():
        for lineno, line in enumerate(_read(path).split('\n'), start=1):
            scrubbed = line
            for token in CLIENT_ALLOWED:
                scrubbed = scrubbed.replace(token, '')
            if CLIENT_WORD.search(scrubbed):
                offenders.append(f'{path.name}:{lineno}: {line.strip()[:80]}')
    assert not offenders, '\n'.join(offenders)


def test_shipped_templates_use_unqualified_tool_names():
    offenders = [p.name for p in _shipped_templates() if 'mcp__envoy__' in _read(p)]
    assert not offenders, offenders


# --- dev-only rules say so ---------------------------------------------------------------

def test_unshipped_rules_declare_themselves_dev_only():
    shipped = {f'{slug}.md' for slug in RULE_MAP.values()}
    for path in sorted(RULES.glob('*.md')):
        if path.name in shipped:
            continue
        text = _read(path)
        scoped = text.startswith('---') and '\npaths:' in text.split('---', 2)[1]
        assert scoped or 'Embody dev only' in text, f'{path.name} is unshipped but does not say so'


# --- the release skill's sync table mirrors the maps --------------------------------------

def test_release_sync_table_matches_the_maps():
    text = _read(SKILLS / 'release' / 'SKILL.md')
    start = text.index('## 4. Verify Template Sync')
    end = text.find('\n## ', start + 1)
    section = text[start:end if end != -1 else None]
    rows = set(re.findall(r'^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|', section, re.M))
    rows.discard(('.claude/ file', 'Template file'))
    expected = {(f'rules/{slug}.md', f'templates/{dat}.md') for dat, slug in RULE_MAP.items()}
    expected |= {(f'skills/{slug}/SKILL.md', f'templates/{dat}.md') for dat, slug in SKILL_MAP.items()}
    expected |= {(f'skills/{slug}/{rel}', f'templates/{dat}.md') for dat, (slug, rel) in REF_MAP.items()}
    assert rows == expected, {'missing': expected - rows, 'stale': rows - expected}
