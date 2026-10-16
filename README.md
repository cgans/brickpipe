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
| `owned.csv` | With an inventory: each lot with how many you have and how many to buy |

With Nova's database and the LDraw part library present, the report also says
whether every piece is clipped to the model (see below).

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

### Check that the pieces clip together

```
python -m brickpipe model.mpd -o out/ --nova-db ../ldraw-nova/data/ldraw-info.db --ldraw-dir C:\LDraw
```

Needs two things, both found automatically when they are in the usual places:

- **The LDraw part library.** Get it once with `python -m brickpipe --get-ldraw`
  (about 150 MB, free, from ldraw.org; kept in brickpipe's own folder), or press
  "Download the library" in the page's settings. A folder with `parts/` and
  `p/`, or LPub3D's `complete.zip`, also works via `--ldraw-dir` or `LDRAW_DIR`.
  A part's studs are references to the `stud.dat` primitive in its official
  geometry, so the geometry has to be read.
- **Nova's shadow library** (`data/offLibShadow` next to its database): LDCad's
  connector records, which say where each part's sockets, pins and holes are.

A piece counts as attached when one of its studs, pins or axles sits exactly
in a matching socket or hole on another piece. Pieces that nothing attaches to
are errors and go into `feedback.md`. Pieces held only by clips, hinges or
bars, or with no connector data, are reported as unverified rather than
flagged. Overlapping geometry (a tyre on a rim) counts as a press fit.

This is still not a proof that the model stands up; it is a proof that it can
be assembled.

### Leave out the bricks you already have

```
python -m brickpipe model.mpd -o out/ --owned mine.csv
python -m brickpipe model.mpd -o out/ --availability --owned-rebrickable YOUR_USER_TOKEN
```

`mine.csv` is lines of `part,colour,quantity`: the part as an LDraw or
BrickLink id, the colour as an LDraw code or name, or blank for "any colour".
`--owned-rebrickable` adds everything in your Rebrickable sets and part lists;
get the token once with `python -m brickpipe --rebrickable-login` (your
password goes to Rebrickable once and is not kept), or set
`REBRICKABLE_USER_TOKEN`. The page has the same two inputs under "Bricks you
already have".

The wanted list then holds only what is left to buy. `owned.csv` lists every
lot with need, have and buy, and the report says how much is covered.
Matching a Rebrickable collection needs the availability check, since that
is where Rebrickable's part and colour ids come from.

### Estimate what it will cost (experimental, behind a flag)

```
BRICKPIPE_FEATURES=prices python -m brickpipe model.mpd -o out/ --availability --prices --price-region north_america --price-currency USD
```

This is built and tested against canned responses but not yet against the
live API, because BrickLink only issues API keys to **seller accounts**, and
becoming one needs a single positive feedback: buy something from any
BrickLink store first. Until that has been done the feature stays behind
`BRICKPIPE_FEATURES=prices`; without it the page hides the panel and
`--prices` says so.

`--prices` asks BrickLink's price guide what each lot still to buy costs and
writes `prices.csv` (average and lowest price each, line total, how many
sellers list it). The report and the page give the total: about X at
average prices, or Y at every seller's lowest, before shipping. Lots nobody
is selling, lots from three sellers or fewer, and pieces costing 1.50 or
more each go into the repair request as things the agent should swap for
commoner parts.

It needs the four BrickLink API values as environment variables
(`BRICKLINK_CONSUMER_KEY`, `BRICKLINK_CONSUMER_SECRET`, `BRICKLINK_TOKEN`,
`BRICKLINK_TOKEN_SECRET`), from
[bricklink.com/v2/api/register_consumer.page](https://www.bricklink.com/v2/api/register_consumer.page):
register a consumer, then add an access token for your computer's public IP
address. The token only works from that address, so a changed home IP means
adding a new token on that page. On the page they go under Settings, Prices
(BrickLink), and stay in the browser. `--price-used` prices used parts and
`--price-sold` uses the last six months of sales instead of current listings.
Prices are cached for a day. Best with `--availability`, which supplies
BrickLink's own part and colour ids.

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
- **Connectivity** (needs `--nova-db` and the LDraw library): pieces that no
  stud, pin or axle joins to the model.

- **Prices** (needs the BrickLink API values): what the lots still to buy
  cost today, and which ones nobody sells, few sell, or are pricey per piece.

Not covered: whether the model stands up under gravity, and connections made
by clips, hinges and bars.

## Known limits

- The price estimate (`--prices`) has not run against the live BrickLink API
  yet; see the feature flag above.

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

1. **Turn on the price estimate.** The plan: order the first model's bricks
   through the wanted list (any paid BrickLink order earns the one positive
   feedback a seller account needs), upgrade the account to seller, register
   an API consumer and a token for the home IP address, then start the page
   with `BRICKPIPE_FEATURES=prices` and run the model again. The first live
   run is the test; the `prices.csv` and any BrickLink error text are what to
   look at. Once it works, drop the flag and the feature is on for everyone.
2. **Revisit: the LDraw part library has to be on the user's machine.** The
   connectivity check needs the 146 MB `complete.zip` from ldraw.org. Today
   the user has to notice the hint and press "Download the library" (or run
   `--get-ldraw`); nothing fetches it on its own. Work out whether brickpipe
   should get it automatically on first run (licence allows redistribution;
   the download is one HTTPS GET with a known size), whether a first-run
   prompt is better than a silent 146 MB download, and why the current flow
   stops at a button. Likely answer: a one-time prompt the first time the
   page loads without a library ("brickpipe needs the LDraw part library,
   146 MB, for the clip-together check. Download now? / Not now"), using the
   download and progress code that already exists. Also note what else
   depends on it and what the user sees when it is missing.
3. Clip, hinge and bar connections in the connectivity check.
4. Recognise bricks you already have from a photo, confirmed by you before
   they are subtracted.

## Notes for Nova

Things found along the way that belong upstream in LDraw Nova rather than
here are collected in [docs/nova-upstream-notes.md](docs/nova-upstream-notes.md).

## Licence note

brickpipe contains no LDraw Nova code or data; it only reads Nova's output and,
optionally, its database file at run time. Nova itself is AGPL-3.0, with its
docs under CC BY-SA 4.0. No licence has been chosen for brickpipe yet.

LEGO is a trademark of the LEGO Group, which does not sponsor or endorse this
project.
