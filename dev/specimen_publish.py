# specimen_publish -- on Ctrl+S, refresh each website Specimen .tdn from the live
# /specimen_lab gallery, self-contained (DAT scripts embedded) so they are
# copy-paste ("embody it") ready. specimens/manifest.json is the curation source
# of truth (which specimens, their slug/category/path); the live TD network is
# the content source. Unchanged files are skipped so unrelated saves never churn
# git. Project-specific (not an Embody feature): reads the embody.tools manifest
# and writes the site's specimens/ folder.
import json
from pathlib import Path


def _publish():
    emb = op.Embody
    if not emb:
        return {'error': 'Embody not found'}
    TDN = emb.ext.TDN
    repo = Path(project.folder).resolve().parent          # dev/ -> repo root
    spec_dir = repo / 'specimens'
    manifest_path = spec_dir / 'manifest.json'
    if not manifest_path.exists():
        return {'error': f'manifest not found: {manifest_path}'}

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    written, skipped, missing = [], [], []
    for spec in manifest.get('specimens', []):
        slug = spec.get('slug', '')
        comp = op('/specimen_lab/' + slug.replace('-', '_'))
        if not comp:
            missing.append(slug)
            continue
        out = spec_dir / spec['tdn_path']
        res = TDN.ExportNetwork(root_path=comp.path,
                                include_dat_content=True, embed_all=True)
        if not res.get('success'):
            missing.append(slug + ' (export failed)')
            continue
        new = res['tdn']
        old = TDN._read_existing_tdn(str(out)) if out.exists() else None
        if old and TDN._tdn_content_equal(new, old):
            skipped.append(slug)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(TDN._compact_json_dumps(new), encoding='utf-8')
        written.append(slug)

    if written or missing:
        emb.Log(
            f'Specimen publish: {len(written)} written, '
            f'{len(skipped)} unchanged, {len(missing)} missing', 'INFO')
        if missing:
            emb.Log(f'  missing/failed: {", ".join(missing)}', 'WARNING')
    return {'written': written, 'skipped': skipped, 'missing': missing}


def onProjectPostSave():
    try:
        _publish()
    except Exception as e:
        try:
            op.Embody.Log(f'Specimen publish failed: {e}', 'ERROR')
        except Exception:
            print(f'Specimen publish failed: {e}')
    return
