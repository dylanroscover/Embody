-- Local development seed for the additive API routes.
-- Intended for a fresh local D1 database after migrations have been applied.

INSERT OR REPLACE INTO users_profile (id, handle, avatar_url, bio, trust_level)
VALUES ('dev-user', 'dev', NULL, 'Local development user for the auth stub.', 'verified');

INSERT OR REPLACE INTO tags (id, name, slug) VALUES
  ('tag-noise', 'noise', 'noise'),
  ('tag-texture', 'texture', 'texture'),
  ('tag-ambient', 'ambient', 'ambient'),
  ('tag-stock-td', 'stock-td', 'stock-td'),
  ('tag-feedback', 'feedback', 'feedback'),
  ('tag-tunnel', 'tunnel', 'tunnel'),
  ('tag-loop', 'loop', 'loop'),
  ('tag-motion', 'motion', 'motion'),
  ('tag-particles', 'particles', 'particles'),
  ('tag-curl-noise', 'curl-noise', 'curl-noise'),
  ('tag-trails', 'trails', 'trails'),
  ('tag-audio', 'audio', 'audio'),
  ('tag-spectrum', 'spectrum', 'spectrum'),
  ('tag-bars', 'bars', 'bars'),
  ('tag-performance', 'performance', 'performance'),
  ('tag-raymarching', 'raymarching', 'raymarching'),
  ('tag-sdf', 'sdf', 'sdf'),
  ('tag-glsl', 'glsl', 'glsl'),
  ('tag-lighting', 'lighting', 'lighting'),
  ('tag-post', 'post', 'post'),
  ('tag-bloom', 'bloom', 'bloom'),
  ('tag-grade', 'grade', 'grade'),
  ('tag-utility', 'utility', 'utility');

INSERT OR REPLACE INTO specimens (
  id, slug, author_id, title, description, category, difficulty, requires, op_count,
  family_summary, current_version_id, thumbnail_key, license, visibility, tier, scan_status,
  capability_json, likes_count, views_count
) VALUES
  (
    'sp-layered-noise-field',
    'layered-noise-field',
    'dev-user',
    'Layered Noise Field',
    'Multi-octave noise that drifts like pigment suspended in glass.',
    'generative-abstract',
    'starter',
    'none',
    8,
    'TOP',
    'ver-layered-noise-field',
    '',
    'CC-BY-4.0',
    'public',
    'community',
    'clean',
    '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}',
    0,
    0
  ),
  (
    'sp-infinite-zoom-tunnel',
    'infinite-zoom-tunnel',
    'dev-user',
    'Infinite Zoom Tunnel',
    'A scale and rotate feedback loop that folds itself into a tunnel.',
    'feedback',
    'starter',
    'none',
    6,
    'TOP',
    'ver-infinite-zoom-tunnel',
    '',
    'CC-BY-4.0',
    'public',
    'community',
    'clean',
    '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}',
    0,
    0
  ),
  (
    'sp-curl-noise-swarm',
    'curl-noise-swarm',
    'dev-user',
    'Curl Noise Swarm',
    'A particle field pushed by curl noise with soft additive trails.',
    'particles',
    'intermediate',
    'none',
    18,
    'SOP,CHOP,TOP',
    'ver-curl-noise-swarm',
    '',
    'CC-BY-4.0',
    'public',
    'community',
    'clean',
    '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}',
    0,
    0
  ),
  (
    'sp-spectrum-reactor',
    'spectrum-reactor',
    'dev-user',
    'Spectrum Reactor',
    'Audio bands drive mirrored bars, bloom, and reactive color pressure.',
    'audio-reactive',
    'intermediate',
    'none',
    14,
    'CHOP,COMP,TOP',
    'ver-spectrum-reactor',
    '',
    'CC-BY-4.0',
    'public',
    'community',
    'clean',
    '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}',
    0,
    0
  ),
  (
    'sp-signed-distance-lantern',
    'signed-distance-lantern',
    'dev-user',
    'Signed Distance Lantern',
    'A compact SDF raymarch with glassy edges and a slow internal glow.',
    'raymarching-sdf',
    'advanced',
    'none',
    9,
    'TOP',
    'ver-signed-distance-lantern',
    '',
    'CC-BY-4.0',
    'public',
    'community',
    'clean',
    '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}',
    0,
    0
  ),
  (
    'sp-bloom-grade-stack',
    'bloom-grade-stack',
    'dev-user',
    'Bloom Grade Stack',
    'A reusable post chain for glow, lift, contrast, and final output polish.',
    'compositing-post',
    'starter',
    'none',
    11,
    'TOP',
    'ver-bloom-grade-stack',
    '',
    'CC-BY-4.0',
    'public',
    'community',
    'clean',
    '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}',
    0,
    0
  );

INSERT OR REPLACE INTO specimen_versions (
  id, specimen_id, version_num, tdn_r2_key, tdn_sha256, size_bytes, op_count, scan_id,
  signature_ref, changelog
) VALUES
  ('ver-layered-noise-field', 'sp-layered-noise-field', 1, 'seed/layered-noise-field.tdn', '0000000000000000000000000000000000000000000000000000000000000001', 75, 8, 'scan-layered-noise-field', NULL, 'Local seed fixture.'),
  ('ver-infinite-zoom-tunnel', 'sp-infinite-zoom-tunnel', 1, 'seed/infinite-zoom-tunnel.tdn', '0000000000000000000000000000000000000000000000000000000000000002', 76, 6, 'scan-infinite-zoom-tunnel', NULL, 'Local seed fixture.'),
  ('ver-curl-noise-swarm', 'sp-curl-noise-swarm', 1, 'seed/curl-noise-swarm.tdn', '0000000000000000000000000000000000000000000000000000000000000003', 72, 18, 'scan-curl-noise-swarm', NULL, 'Local seed fixture.'),
  ('ver-spectrum-reactor', 'sp-spectrum-reactor', 1, 'seed/spectrum-reactor.tdn', '0000000000000000000000000000000000000000000000000000000000000004', 70, 14, 'scan-spectrum-reactor', NULL, 'Local seed fixture.'),
  ('ver-signed-distance-lantern', 'sp-signed-distance-lantern', 1, 'seed/signed-distance-lantern.tdn', '0000000000000000000000000000000000000000000000000000000000000005', 78, 9, 'scan-signed-distance-lantern', NULL, 'Local seed fixture.'),
  ('ver-bloom-grade-stack', 'sp-bloom-grade-stack', 1, 'seed/bloom-grade-stack.tdn', '0000000000000000000000000000000000000000000000000000000000000006', 72, 11, 'scan-bloom-grade-stack', NULL, 'Local seed fixture.');

INSERT OR REPLACE INTO scans (
  id, version_id, scanner_version, verdict, capability_json, findings_json
) VALUES
  ('scan-layered-noise-field', 'ver-layered-noise-field', 'seed', 'clean', '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}', '[]'),
  ('scan-infinite-zoom-tunnel', 'ver-infinite-zoom-tunnel', 'seed', 'clean', '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}', '[]'),
  ('scan-curl-noise-swarm', 'ver-curl-noise-swarm', 'seed', 'clean', '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}', '[]'),
  ('scan-spectrum-reactor', 'ver-spectrum-reactor', 'seed', 'clean', '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}', '[]'),
  ('scan-signed-distance-lantern', 'ver-signed-distance-lantern', 'seed', 'clean', '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}', '[]'),
  ('scan-bloom-grade-stack', 'ver-bloom-grade-stack', 'seed', 'clean', '{"scanner_version":"seed","verdict":"clean","counts":{"execute_dats":0,"file_read_exprs":0,"web_ops":0,"extensions":0,"storage_payloads":0,"denylisted_types":0,"traversal_paths":0,"external_refs":0},"findings":[]}', '[]');

INSERT OR IGNORE INTO specimen_tags (specimen_id, tag_id) VALUES
  ('sp-layered-noise-field', 'tag-noise'),
  ('sp-layered-noise-field', 'tag-texture'),
  ('sp-layered-noise-field', 'tag-ambient'),
  ('sp-layered-noise-field', 'tag-stock-td'),
  ('sp-infinite-zoom-tunnel', 'tag-feedback'),
  ('sp-infinite-zoom-tunnel', 'tag-tunnel'),
  ('sp-infinite-zoom-tunnel', 'tag-loop'),
  ('sp-infinite-zoom-tunnel', 'tag-motion'),
  ('sp-curl-noise-swarm', 'tag-particles'),
  ('sp-curl-noise-swarm', 'tag-curl-noise'),
  ('sp-curl-noise-swarm', 'tag-trails'),
  ('sp-curl-noise-swarm', 'tag-motion'),
  ('sp-spectrum-reactor', 'tag-audio'),
  ('sp-spectrum-reactor', 'tag-spectrum'),
  ('sp-spectrum-reactor', 'tag-bars'),
  ('sp-spectrum-reactor', 'tag-performance'),
  ('sp-signed-distance-lantern', 'tag-raymarching'),
  ('sp-signed-distance-lantern', 'tag-sdf'),
  ('sp-signed-distance-lantern', 'tag-glsl'),
  ('sp-signed-distance-lantern', 'tag-lighting'),
  ('sp-bloom-grade-stack', 'tag-post'),
  ('sp-bloom-grade-stack', 'tag-bloom'),
  ('sp-bloom-grade-stack', 'tag-grade'),
  ('sp-bloom-grade-stack', 'tag-utility');

INSERT INTO specimens_fts (rowid, slug, title, description, tags, author_handle, dat_text)
SELECT rowid, 'layered-noise-field', 'Layered Noise Field', 'Multi-octave noise that drifts like pigment suspended in glass.', 'noise texture ambient stock-td', 'dev', 'Noise TOP Level TOP Composite TOP Null TOP'
FROM specimens WHERE id = 'sp-layered-noise-field';

INSERT INTO specimens_fts (rowid, slug, title, description, tags, author_handle, dat_text)
SELECT rowid, 'infinite-zoom-tunnel', 'Infinite Zoom Tunnel', 'A scale and rotate feedback loop that folds itself into a tunnel.', 'feedback tunnel loop motion', 'dev', 'Feedback TOP Transform TOP Composite TOP Level TOP'
FROM specimens WHERE id = 'sp-infinite-zoom-tunnel';

INSERT INTO specimens_fts (rowid, slug, title, description, tags, author_handle, dat_text)
SELECT rowid, 'curl-noise-swarm', 'Curl Noise Swarm', 'A particle field pushed by curl noise with soft additive trails.', 'particles curl-noise trails motion', 'dev', 'Particle SOP Noise CHOP Render TOP Cache TOP'
FROM specimens WHERE id = 'sp-curl-noise-swarm';

INSERT INTO specimens_fts (rowid, slug, title, description, tags, author_handle, dat_text)
SELECT rowid, 'spectrum-reactor', 'Spectrum Reactor', 'Audio bands drive mirrored bars, bloom, and reactive color pressure.', 'audio spectrum bars performance', 'dev', 'Audio Device In CHOP Audio Spectrum CHOP Math CHOP Geometry COMP Render TOP Level TOP'
FROM specimens WHERE id = 'sp-spectrum-reactor';

INSERT INTO specimens_fts (rowid, slug, title, description, tags, author_handle, dat_text)
SELECT rowid, 'signed-distance-lantern', 'Signed Distance Lantern', 'A compact SDF raymarch with glassy edges and a slow internal glow.', 'raymarching sdf glsl lighting', 'dev', 'GLSL TOP Constant TOP Level TOP Null TOP'
FROM specimens WHERE id = 'sp-signed-distance-lantern';

INSERT INTO specimens_fts (rowid, slug, title, description, tags, author_handle, dat_text)
SELECT rowid, 'bloom-grade-stack', 'Bloom Grade Stack', 'A reusable post chain for glow, lift, contrast, and final output polish.', 'post bloom grade utility', 'dev', 'Blur TOP Level TOP Composite TOP HSV Adjust TOP'
FROM specimens WHERE id = 'sp-bloom-grade-stack';
