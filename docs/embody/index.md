# Embody

**Embody** is the lateral velocity layer of the project. It solves the oldest problem in TouchDesigner: your work lives inside a binary `.toe` file that nothing outside TD can read, diff, or understand. Embody pulls your operators out of that file and into text and structured files on disk — files that mirror your network hierarchy, files your AI assistant can read, files git can diff, files that rebuild your network automatically the next time you open the project. The `.toe` stops being the source of truth. The files become the source of truth.

## Why Embody?

A `.toe` file is a black box. You can open it in TD and change things. You can save it. But you can't:

- **Diff two versions** — binary files produce no meaningful output in `git diff`
- **Branch safely** — reverting means opening an older `.toe` and hoping it has everything you need
- **Let an AI read it** — without Embody, your AI assistant has no way to inspect what's inside your network. It can describe what *might* be there, but it cannot see what *is*
- **Review it in a pull request** — a changed `.toe` shows up as a binary blob. There's nothing to review.

Embody solves this by externalizing your operators to files — `.py`, `.tox`, `.tdn`, `.json` — that live in a folder structure mirroring your network hierarchy. Edit those files and the change is live the next time the project opens. Put them under version control and you have branching, diffing, and history for your TouchDesigner network.

## Key Design Principles

### Bidirectional Sync

Embody maintains a two-way relationship between your `.toe` and the files on disk. Neither direction is lossy:

| Direction | When | What happens |
|---|---|---|
| **TD → disk** | ++ctrl+shift+u++ (Update) | Dirty COMPs and DATs write their current state to external files |
| **Disk → TD** | Project open | All externalized operators rebuild from disk automatically |

The files are the persistent record. The `.toe` is a working state. On every project open, Embody restores everything that was externalized — TOX-strategy COMPs from `.tox` files, TDN-strategy COMPs from `.tdn` JSON files, and DATs via TouchDesigner's native file parameter.

### Dirty Tracking

Embody watches every externalized operator for parameter changes. When something changes, the operator is marked dirty. When you press ++ctrl+shift+u++, only dirty operators are written — no unnecessary file writes, no spurious git changes. The `externalizations.tsv` tracking table records each operator's path, type, externalized file path, dirty state, and build number.

### Non-Destructive File Management

Embody only manages files it created. It tracks every externalized file it owns and will never delete or overwrite a file it didn't create. If you remove an externalization tag, Embody untracks the file but leaves it on disk. Untracked files are never touched.

### Build Metadata

Every externalized COMP gets three parameters injected automatically:

| Parameter | Value |
|---|---|
| `Buildnumber` | The Embody build number active at externalization time |
| `Touchbuild` | The TouchDesigner build number |
| `Builddate` | UTC timestamp of the last externalization |

This gives you a permanent record of which version of Embody and TD produced each externalized file — useful when debugging across machine setups or after upgrades.

## Externalization Strategies

Each operator you tag gets an externalization strategy that determines the file format:

| Strategy | File | Best for |
|---|---|---|
| **TOX** | `.tox` | COMPs where you prioritize restore speed and don't need to diff the contents — complex UI components, third-party COMPs, anything with heavy internal state |
| **TDN** | `.tdn` | COMPs you want to read, diff, and review in git — signal processing chains, custom logic, anything you actively edit |
| **DAT** (auto-detected) | `.py`, `.json`, `.xml`, `.csv`, etc. | Scripts and data — extension code, configuration files, lookup tables |

For DATs, the format is determined by the DAT's content type — a Python DAT externalizes to `.py`, a JSON DAT to `.json`, and so on. Embody detects this automatically.

## Usage

=== "Keyboard Shortcuts"

    - ++lctrl++ ++lctrl++ — Tag/untag an operator for externalization
    - ++ctrl+shift+u++ — Update (write all dirty operators to disk)
    - ++ctrl+shift+e++ — Open the Embody manager window

=== "Python API"

    ```python
    # Tag an operator for externalization
    op.Embody.ext.Embody.tagOp(op('/project1/myComp'))

    # Write all dirty operators to disk
    op.Embody.Update()

    # Query all externalized operators and their status
    ops = op.Embody.ext.Embody.getExternalizedOps()

    # Reinitialize MCP + AI config files
    op.Embody.InitEnvoy()

    # Reinitialize git config (.gitignore, .gitattributes)
    op.Embody.InitGit()
    ```

## Embody + Git

Once your operators are externalized, every `git commit` is a snapshot of your network in a form any developer tool can inspect. What this makes possible:

- **Readable diffs** — `git diff` shows exactly which parameters changed on which operators between two commits
- **Branching by idea** — each experimental direction is a branch. Merging brings working parts back together without manual copy-paste in the TD network editor.
- **Pull request reviews** — reviewers can comment on specific parameter changes, script edits, and network structure without opening TD
- **Bisect** — `git bisect` can find the commit that introduced a visual or performance regression, because every change is a discrete, readable commit
- **AI context** — your AI assistant can run `git diff` against your externalized files and understand exactly what changed between sessions, without you summarizing it

A git repository is optional — Embody works in any project folder. But if you use git, every externalized save is a clean, reviewable diff for free.
