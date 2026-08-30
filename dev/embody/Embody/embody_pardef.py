"""Get-or-create for custom pages and parameters.

Code owns the schema; the user owns the value. Extensions reinitialize on every
source save, so every creation path here is get-or-create -- never
create-blindly. Four ad-hoc copies of the page lookup had already drifted
(one searched for a page named 'Build Info' while creating 'About', so it never
matched an existing page); this is the single place that logic lives now.

Deliberately does NOT destroy anything. Par.destroy() takes the user's value,
expressions and exports with it and is unrecoverable, so a style mismatch is
reported and refused rather than "fixed".
"""

# TD reports a tuplet's style by its widest family: an RGB group reads 'RGBA',
# XY/XYZ read 'XYZW' (TDXNExt's custom-par reconstruction carries the same
# workaround). Compare the declared style against every spelling TD may
# report for it, never the literal -- or every RGB par is a "mismatch".
_STYLE_FAMILY = {
    'RGB': ('RGB', 'RGBA'), 'RGBA': ('RGBA',),
    'XY': ('XY', 'XYZW'), 'XYZ': ('XYZ', 'XYZW'), 'XYZW': ('XYZW',),
    'UV': ('UV', 'UVW'), 'UVW': ('UVW',), 'WH': ('WH',),
}

# The user's, never the schema's. Re-applying any of these on a re-find would
# clobber an expression, binding or export the user set on the par.
_USER_STATE = frozenset((
    'val', 'expr', 'bindExpr', 'bindMaster', 'mode', 'export', 'exportOP',
    'exportSource',
))


def ensureCustomPage(comp, name):
    """Return the COMP's custom page called `name`, creating it if absent."""
    for page in comp.customPages:
        if page.name == name:
            return page
    return comp.appendCustomPage(name)


def ensureCustomPar(comp, page, name, style, **attrs):
    """Get-or-create one custom parameter.

    Returns the Par for a single-value style and the ParGroup for a
    multi-value one (RGB, XYZ, WH, ...). TD stores a tuplet as component
    pars named <name>r/<name>g/<name>b whose `tupletName` is <name>, so a
    probe by Par.name never finds it, and append*() -- replace=True by
    default -- would silently re-create it on every reinit.

    Probes the WHOLE COMP, not the page: custom parameter names are a flat
    per-COMP namespace, so a page-scoped probe would destroy a same-named par
    living on another page.

    `is None` / list-emptiness throughout, never truthiness on a Par:
    evaluating a Par is what truthiness does, so a user par holding 0 -- or
    carrying a broken expression -- would read as absent and get appended
    twice (or raise).

    Declared attributes (label, min, max, default, help, readOnly, ...) are
    applied SYMMETRICALLY, on create and on re-find, so a schema change
    reaches an install that already has the par. Anything in _USER_STATE is
    ignored. An attribute TD rejects raises -- a typo'd keyword must not
    become a silent no-op.

    Raises ValueError on a style mismatch. The caller decides; this never
    destroys a parameter to force the declared shape.
    """
    found = _find(comp, name)
    if found:
        actual = found[0].style
        if actual not in _STYLE_FAMILY.get(style, (style,)):
            raise ValueError(
                'par %s on %s is style %s, declared %s -- refusing to '
                'replace it (destroy() would take the value, expressions '
                'and exports with it)' % (name, comp.path, actual, style))
    else:
        adder = getattr(page, 'append' + style, None)
        if adder is None:
            raise ValueError('no append%s on a custom page (par %s)' % (style, name))
        adder(name)
        found = _find(comp, name)
        if not found:
            raise ValueError('append%s did not produce par %s on %s'
                             % (style, name, comp.path))
    for par in found:
        _applyAttrs(par, attrs)
    if len(found) == 1:
        return found[0]
    return found[0].parGroup


def _find(comp, name):
    """Every component par of tuplet `name` (one entry for a single par)."""
    return [p for p in comp.customPars if p.tupletName == name]


def _applyAttrs(par, attrs):
    """Apply declared attributes, raising on the first TD rejects."""
    failed = []
    for key, value in attrs.items():
        if key in _USER_STATE:
            continue
        try:
            setattr(par, key, value)
        except Exception as e:
            failed.append('%s=%r (%s)' % (key, value, e))
    if failed:
        raise ValueError('could not apply declared attributes on %s: %s'
                         % (par.name, '; '.join(failed)))
