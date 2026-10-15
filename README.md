# brickpipe

Takes an LDraw model (for example one designed by an AI agent in
[LDraw Nova](https://github.com/anteloc/ldraw-nova)) and produces what you need
to actually build it:

| File | What it is |
|---|---|
| `parts.csv` | Every part and colour with quantities |
| `bricklink_wanted.xml` | Wanted list for BrickLink; paste its contents into the XML box at Want > Upload |
| `flat.ldr` | All pieces in one model with no submodels, for upload to Rebrickable or BrickLink |
| `instructions.mpd` | The model with build steps of at most 8 pieces, ready for LPub3D |
| `report.md` | Piece counts and every problem found |
| `availability.csv` | With `--availability`: whether each part was ever made in that colour |
| `feedback.md` | When there is something to fix: a repair request for the agent that designed the model |

Python 3.10 or newer. No third-party packages.

## The page

With LDraw Nova running, start brickpipe's own page and everything below
happens from the browser:

```
python -m brickpipe.web
```

It opens <http://localhost:8766>. Describe a model (or pick one Nova already
made), press Design and check, and watch each round: Nova designs, brickpipe
checks, the two go back and forth until every piece can be bought. The page
ends with the files for the final model and a button that copies the wanted
list for BrickLink. On Windows, `brickpipe-ui.cmd` does the same with a
double-click.

Runs are kept under `runs/` and listed on the page, so you can close it and
come back. The Rebrickable key is stored in your browser and sent with each
run; the server keeps it in memory for that run only. The page listens on
localhost only.

The command line below does the same things one step at a time.

## Run it

```
python -m brickpipe examples/garden-shed.mpd -o out/
```

With Nova's part database you also get part names, a check that every part
exists, better step ordering and the floating-piece check:

```
python -m brickpipe model.mpd -o out/ --nova-db ../ldraw-nova/data/ldraw-info.db
```

### Check that the parts exist in those colours

```
export REBRICKABLE_API_KEY=your-key
python -m brickpipe model.mpd -o out/ --availability
```

The key is free: rebrickable.com > Account > Settings > API. Keep it in the
environment, not in a file in this repository. It is sent only in the request
header and is never written to the cache, the report or an error message.

Each part and colour is looked up in Rebrickable's catalogue of official sets:

- **not made in this colour** is reported as an error, with the colours the
  part was made in.
- **rare** means it appears in three sets or fewer, so expect it to be scarce.
  The warning names the pieces and the part's commoner colours, and goes into
  `feedback.md` as a "should fix" item.
- The wanted list switches to BrickLink's own part and colour ids wherever
  Rebrickable supplies them, in place of the bundled table.

The first run on a model takes about one second per distinct part, because
Rebrickable allows roughly one request per second. Responses are cached for 30
days in `~/.cache/brickpipe/rebrickable` (change with `--cache-dir`), so later
runs are instant. If the check cannot run, the report says so and everything
else is still produced.

### Send the problems back to the agent

When a run finds problems the designing agent can fix, it writes `feedback.md`.
It lists only those problems, each with what to change and where: the
submodel, the part and colour, the position inside that submodel, and the
comment the agent wrote above the line.

1. Open the Nova chat that produced the model.
2. Attach `feedback.md` (or paste its contents) and send it.
3. Run brickpipe again on the regenerated `.mpd`.

Repeat until no `feedback.md` is written. Problems only you can act on, such
as ids to confirm, stay in `report.md` and are not sent.

### Or let brickpipe run the loop

With the Nova web app running, brickpipe can do the sending and fetching
itself:

```
python -m brickpipe model.mpd -o out/ --nova-db ../ldraw-nova/data/ldraw-info.db --availability --nova-chat latest
```

`--nova-chat` names the chat that designed the model (`latest` picks the most
recent one; a chat id or its first few characters also works). Each round
checks the current model, attaches `feedback.md` to a message in that chat,
waits for the agent to finish, downloads the model it delivered, and checks
again. It stops when a check comes back clean, after `--rounds` repairs
(default 3), or when the agent delivers nothing.

Outputs: `out/round-N/` holds each round's files, `out/models/` the delivered
models, and `out/loop.md` the summary. The exit status is 0 only when the
final model is clean.

The agent's turn is sent with Nova's "full" tool permissions so the loop never
waits for a click; pass `--ask-tools` to approve tools yourself in the Nova
UI instead. Nova's tools work inside that chat's own folder. Nova has no login,
so `--nova-url` should point at a machine you trust (default
`http://localhost:8765`).

Options: `--max-step-parts N` (default 8), `--keep-steps` to leave the model's
steps untouched, `--strict` to exit with status 1 when an error is found.

Tests: `python -m unittest discover -s tests`

## End-to-end test run

On Windows, follow [docs/testing-on-windows.md](docs/testing-on-windows.md),
which adds a setup check and the Windows-specific commands.

1. **Install LDraw Nova** (needs Git and Docker, about 5 GB of disk):

   ```
   git clone --branch v0.6.0 https://github.com/anteloc/ldraw-nova.git
   git clone --branch v0.6.0 https://github.com/anteloc/ldraw-nova-docker.git
   cd ldraw-nova-docker
   docker compose build
   docker compose up -d
   ```

   Open http://localhost:8765 and give the app a key for OpenAI, Anthropic or
   OpenRouter. The app has no login, so keep it on a network you trust. A
   TypeSafe key (set in Settings) is optional; without it part search falls
   back to plain text search.

2. **Generate a small model.** Ask for 100 to 300 pieces in ordinary bricks and
   plates for the first attempt. Save the model's LDraw source as an `.mpd` file.

3. **Run brickpipe** on it with `--nova-db`. Read `report.md` first. Fix or
   regenerate if it lists errors or floating pieces.

4. **Price it.** Upload `flat.ldr` to Rebrickable (as a custom list) or
   `bricklink_wanted.xml` to BrickLink. Anything they reject is a part or colour
   that does not exist as specified.

5. **Make instructions.** Open `instructions.mpd` in
   [LPub3D](https://trevorsandy.github.io/lpub3d/) and export a PDF.

6. **Order and build.** Note every place the model could not be assembled as
   drawn. That list decides what to build next here.

## What the checks do and do not cover

- **Structure:** submodels that are referenced but missing, and submodel loops.
- **Colour:** pieces left with no real colour, and colours missing from the table.
- **Duplicates:** two copies of a part in exactly the same place.
- **Embedded parts:** parts whose definition is carried inside the file, with
  the id they were listed under so you can confirm it.
- **Parts** (needs `--nova-db`): ids not in the LDraw library, old ids that have
  been renamed, and drawing geometry used as if it were a piece.
- **Floating** (needs `--nova-db`): groups of pieces whose bounding boxes touch
  nothing in the main structure.

Not covered: whether touching pieces really clip together, whether the model
stands up, and whether a part that exists is in stock or affordable today.

## Known limits

- The availability check has only been tested against canned responses that
  follow Rebrickable's API docs. Its first run against the live API is still
  to be done.
- Without `--availability`, the next two points apply.
- `data/colours.csv` maps LDraw colour codes to BrickLink colour ids for the
  common colours. Check the colours in a wanted list before ordering. Colours
  not in the table are left out of the XML and listed in the report.
- LDraw part ids are used as BrickLink item ids. They match for most parts but
  not all. Uploading `flat.ldr` lets the site do its own matching instead.
- Build order is by height only.

## Next steps

1. Stud-level connectivity for ordinary bricks and plates.
2. Price estimate per model.

## Notes for Nova

Things found along the way that belong upstream in LDraw Nova rather than
here are collected in [docs/nova-upstream-notes.md](docs/nova-upstream-notes.md).

## Licence note

brickpipe contains no LDraw Nova code or data; it only reads Nova's output and,
optionally, its database file at run time. Nova itself is AGPL-3.0, with its
docs under CC BY-SA 4.0. No licence has been chosen for brickpipe yet.

LEGO is a trademark of the LEGO Group, which does not sponsor or endorse this
project.
