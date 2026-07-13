'''
Embody v{{VERSION}}
===============

Embody keeps TouchDesigner projects in version control. It
externalizes your tagged COMPs and DATs to diffable files on
disk (.tox, .tdn, .py, ...), so a binary .toe stops being the
thing you have to trust -- the files are.

Bundled with Embody is Envoy, an embedded MCP server that lets
AI coding assistants (Claude Code and others) create, modify,
connect, and query operators in your live TD session, and
manage your externalizations -- all over a local connection.

The core loop
-------------
1. Tag an operator for externalization -- hover it and press
   {{TAGGERTAP}}.
2. Update -- press {{SC:Shortcutupdateall}}. Embody walks your
   project and writes every tagged operator to disk.
3. Work as normal. The externalized files on disk are the
   source of truth; the .toe is just a container. Everything
   tagged is recoverable from the files, even without saving.

To un-externalize, pulse Disable -- it removes only the files
Embody tracks and leaves anything else in the folder alone.

Turning on Envoy (the MCP server)
---------------------------------
Toggle the Envoyenable parameter ON. Envoy starts on a local
port and writes a .mcp.json into your project root so AI
clients can discover it. Regenerate config at any time with
op.Embody.InitEnvoy(); set up git with op.Embody.InitGit().

Getting around
--------------
{{SHORTCUTS}}
Every shortcut is editable on the Embody COMP's Shortcuts
parameter page -- type a combo, or pulse Record and press
the keys. Leave a binding empty to disable it.

This panel is a quick orientation, not the manual. For the
full reference -- supported formats, folder configuration,
duplicate handling, the Manager UI, the TDN format, every
Envoy tool, and troubleshooting -- see the docs:

  https://embody.tools
  https://dylanroscover.github.io/Embody/

The Github button on the Embody page opens the source repo.
'''
