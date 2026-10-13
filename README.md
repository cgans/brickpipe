# brickpipe

Takes an LDraw model (for example one designed by an AI agent in
[LDraw Nova](https://github.com/anteloc/ldraw-nova)) and produces what you need
to actually build it:

| File | What it is |
|---|---|
| `parts.csv` | Every part and colour with quantities |
| `bricklink_wanted.xml` | Wanted list for BrickLink (Want > Upload) |
| `flat.ldr` | All pieces in one model with no submodels, for upload to Rebrickable or BrickLink |
| `instructions.mpd` | The model with build steps of at most 8 pieces, ready for LPub3D |
| `report.md` | Piece counts and every problem found |

Python 3.10 or newer. No third-party packages.

## Run it

```
python -m brickpipe examples/garden-shed.mpd -o out/
```

With Nova's part database you also get part names, a check that every part
exists, better step ordering and the floating-piece check:

```
python -m brickpipe model.mpd -o out/ --nova-db ../ldraw-nova/data/ldraw-info.db
```

Options: `--max-step-parts N` (default 8), `--keep-steps` to leave the model's
steps untouched, `--strict` to exit with status 1 when an error is found.

Tests: `python -m unittest discover -s tests`

## End-to-end test run

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
- **Parts** (needs `--nova-db`): ids not in the LDraw library, old ids that have
  been renamed, and drawing geometry used as if it were a piece.
- **Floating** (needs `--nova-db`): groups of pieces whose bounding boxes touch
  nothing in the main structure.

Not covered: whether touching pieces really clip together, whether the model
stands up, and whether each part is sold in the chosen colour.

## Known limits

- `data/colours.csv` maps LDraw colour codes to BrickLink colour ids for the
  common colours. Check the colours in a wanted list before ordering. Colours
  not in the table are left out of the XML and listed in the report.
- LDraw part ids are used as BrickLink item ids. They match for most parts but
  not all. Uploading `flat.ldr` lets the site do its own matching instead.
- Build order is by height only.

## Next steps

1. Colour availability: look each part and colour up in the Rebrickable API and
   flag combinations that were never produced.
2. Feed `report.md` back to the agent so it repairs its own model.
3. Stud-level connectivity for ordinary bricks and plates.
4. Price estimate per model.

## Licence note

brickpipe contains no LDraw Nova code or data; it only reads Nova's output and,
optionally, its database file at run time. Nova itself is AGPL-3.0, with its
docs under CC BY-SA 4.0. No licence has been chosen for brickpipe yet.

LEGO is a trademark of the LEGO Group, which does not sponsor or endorse this
project.
