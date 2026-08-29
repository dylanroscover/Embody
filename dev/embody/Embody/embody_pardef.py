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


def ensureCustomPage(comp, name):
    """Return the COMP's custom page called `name`, creating it if absent."""
    for page in comp.customPages:
        if page.name == name:
            return page
    return comp.appendCustomPage(name)


def ensureCustomPar(comp, page, name, style, **attrs):
    """Get-or-create one custom parameter. Returns the Par, never a ParGroup.

    Probes the WHOLE COMP, not the page: custom parameter names are a flat
    per-COMP namespace and append*() defaults to replace=True, so a
    page-scoped probe would silently destroy a same-named par living on
    another page.

    `is None` throughout, never truthiness: evaluating a Par is what
    truthiness does, so a user par holding 0 -- or carrying a broken
    expression -- would read as absent and get appended twice (or raise).

    Raises ValueError on a style mismatch. The caller decides; this never
    destroys a parameter to force the declared shape.
    """
    existing = next((p for p in comp.customPars if p.name == name), None)
    if existing is not None:
        if existing.style != style:
            raise ValueError(
                'par %s on %s is style %s, declared %s -- refusing to '
                'replace it (destroy() would take the value, expressions '
                'and exports with it)' % (name, comp.path, existing.style, style))
        _applyAttrs(existing, attrs)
        return existing

    adder = getattr(page, 'append' + style, None)
    if adder is None:
        raise ValueError('no append%s on a custom page (par %s)' % (style, name))
    adder(name)
    # append*() returns a ParGroup; re-read to get the Par itself.
    created = next((p for p in comp.customPars if p.name == name), None)
    if created is None:
        raise ValueError('append%s did not produce par %s on %s'
                         % (style, name, comp.path))
    _applyAttrs(created, attrs)
    return created


def _applyAttrs(par, attrs):
    """Apply declared attributes SYMMETRICALLY -- on create and on re-find.

    Applying them only at creation means a schema change (a new default, a
    corrected label) never reaches an install that already has the par.
    `val` is excluded on purpose: that is the user's, not the schema's.
    """
    for key, value in attrs.items():
        if key == 'val':
            continue
        try:
            setattr(par, key, value)
        except Exception:
            pass
