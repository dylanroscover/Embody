# Text Encoding and ASCII Punctuation

Every file in this repo must be valid UTF-8, and must never be round-tripped
through a legacy codepage (Windows-1252 / Latin-1). That round-trip is exactly
what turns a clean em-dash into mojibake -- a UTF-8 byte sequence decoded as
CP1252 and then re-saved as UTF-8. It happened once in `execute.py`; never
reintroduce it.

## Always write UTF-8

- Never re-encode a file as CP1252 / Latin-1.
- When editing files byte-for-byte, preserve any existing BOM. Several
  TD-exported sources (`EmbodyExt.py`, `EnvoyExt.py`, `TDNExt.py`,
  `CatalogManagerExt.py`, the template DATs) carry a UTF-8 BOM (`EF BB BF`) at
  offset 0 -- keep it.

## Use ASCII punctuation in code, data, and generated text

Applies to `.py`, `.json`, `.css`, `.js`, `.txt`, and anything an agent
generates (`llms.txt`, `llms-full.txt`, `for-ai.*`, etc.):

| Do not use (glyph) | Use instead |
|---|---|
| em dash | `-`  (or ` - `) |
| en dash | `-` |
| ellipsis | `...` |
| left / right / bi arrows | `->`  `<-`  `<->` |
| multiplication sign | `x` |
| curly single / double quotes | `'`  `"` |
| bullet | `-`  or  `*` |
| box-drawing (tree art) | `-`  `\|`  `+`  `\` |
| greater/less-or-equal | `>=`  `<=` |
| non-breaking space | regular space |

## HTML and CSS use escapes, never raw glyphs

- **HTML**: entities -- `&mdash;` `&ndash;` `&hellip;` `&rarr;` `&rsquo;`
  `&ldquo;` `&rdquo;` `&times;` `&nbsp;`. Source stays pure ASCII; the page
  still renders real typography. The whole `web/` landing site already does this.
- **CSS**: unicode escapes in `content` properties -- `content: '\203A '`, not a
  raw glyph.

## Hand-written Markdown prose is exempt

`docs/` and `manifesto.md` may keep deliberate em-dashes -- they render
correctly through MkDocs (UTF-8). When in doubt, still prefer ASCII.

## Why

A raw em-dash is correct UTF-8, but viewed in a Windows-1252 context -- a TD
textport, a misconfigured editor, or a `.txt` / `.json` served without a charset
-- it shows as mojibake. ASCII punctuation cannot mojibake anywhere. Machine
files that other tools fetch (`llms.txt`, `for-ai.json`) especially must be ASCII.
