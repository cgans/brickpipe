# Testing brickpipe end to end on Windows

Everything here runs in **Windows PowerShell**. No WSL shell is needed, though
Docker Desktop uses WSL 2 behind the scenes.

Use `py -3` wherever a command below says so. If your machine has no `py`
launcher, use `python` instead; the setup check tells you which one works.

## What you need

| Software | Needed for | Install |
|---|---|---|
| Git | getting the code | `winget install --id Git.Git -e` |
| Python 3.10 or newer | running brickpipe | `winget install --id Python.Python.3.13 -e` |
| Docker Desktop | running LDraw Nova | `winget install --id Docker.DockerDesktop -e` |
| LDraw part library | the check that pieces clip together | LPub3D downloads it on first start; the checker finds it |
| LPub3D | viewing the build instructions | portable zip from [its releases page](https://github.com/trevorsandy/lpub3d/releases/latest); see step 8 |
| An AI provider key (OpenAI, Anthropic or OpenRouter) | Nova's agent | from the provider |
| A Rebrickable key | `--availability` | free: rebrickable.com > Account > Settings > API |
| A BrickLink account | uploading the wanted list | free |

Close and reopen PowerShell after installing anything, so it is found.

## 1. Get brickpipe and check the machine

```powershell
git clone https://github.com/cgans/brickpipe.git
cd brickpipe
powershell -ExecutionPolicy Bypass -File scripts\check-setup.ps1
```

The check changes nothing. It prints `[ OK ]`, `[WARN]` or `[FAIL]` for each
item, with the fix underneath. At this point `Nova folders` is expected to
fail; step 4 fixes it. Run the check again whenever you change something.

## 2. Run brickpipe on its own

```powershell
py -3 -m unittest discover -s tests
py -3 -m brickpipe examples\garden-shed.mpd -o out
notepad out\report.md
```

All tests should pass, and `out` should contain `parts.csv`,
`bricklink_wanted.xml`, `flat.ldr`, `instructions.mpd` and `report.md`.

## 3. Live-test the availability check

```powershell
$env:REBRICKABLE_API_KEY = "your-key"
py -3 -m brickpipe examples\garden-shed.mpd -o out --availability
Import-Csv out\availability.csv | Format-Table
```

It takes under ten seconds. Every row should have a status. The key lasts
until you close the window; nothing is written to disk.

## 4. Install LDraw Nova

From the folder that **contains** `brickpipe`, so the three folders sit side by
side:

```powershell
cd ..
git clone -c core.autocrlf=false --branch v0.6.0 https://github.com/anteloc/ldraw-nova.git
git clone -c core.autocrlf=false --branch v0.6.0 https://github.com/anteloc/ldraw-nova-docker.git
```

`-c core.autocrlf=false` matters on Windows. Without it Git rewrites the line
endings of Nova's shell scripts and the container fails to start.

Start Docker Desktop and wait for it to say the engine is running, then:

```powershell
cd ldraw-nova-docker
docker compose build
docker compose up -d
```

The first build downloads about 5 GB and can take a while. Open
<http://localhost:8765> and add your AI provider key in the app. The app has
no login, so only run it on a network you trust.

Go back and rerun the check; everything except optional items should pass:

```powershell
cd ..\brickpipe
powershell -ExecutionPolicy Bypass -File scripts\check-setup.ps1
```

## 5. The page (the easy way)

With Nova running, double-click `brickpipe-ui.cmd` in the `brickpipe` folder,
or run:

```powershell
py -3 -m brickpipe.web
```

Your browser opens <http://localhost:8766>. Open Settings on the page, paste
your Rebrickable key (it stays in the browser), check that the Nova database
path points at `..\ldraw-nova\data\ldraw-info.db`, then describe a model and
press Design and check. Steps 6 and 7 below are what the page does for you;
skip to step 8 when it finishes.

## 5b. Generate a small model (by hand)

In the Nova app, start a chat and ask for something simple, for example:

> A small garden shed, about 150 pieces, using only ordinary bricks, plates
> and tiles in common colours.

Small keeps the token cost and the eventual brick order low. Nova saves each
finished model on your disk as:

```
..\ldraw-nova-docker\data\generated\<model-name>.mpd
```

```powershell
dir ..\ldraw-nova-docker\data\generated\*.mpd
```

## 6. Run brickpipe on the model

```powershell
py -3 -m brickpipe ..\ldraw-nova-docker\data\generated\<model-name>.mpd -o out --nova-db ..\ldraw-nova\data\ldraw-info.db --availability
notepad out\report.md
```

## 7. Send the problems back to the agent

If `out\feedback.md` exists, attach it (or paste its contents) into the same
Nova chat and send it. When the agent finishes, repeat step 6 on the new file.
Stop when no `feedback.md` is written, or after three rounds if it is not
getting shorter.

### Or let brickpipe run the loop

```powershell
py -3 -m brickpipe ..\ldraw-nova-docker\data\generated\<model-name>.mpd -o out --nova-db ..\ldraw-nova\data\ldraw-info.db --availability --nova-chat latest
notepad out\loop.md
```

This sends `feedback.md` to the most recent Nova chat, waits for the agent,
fetches the repaired model and checks it again, up to three times. Watch the
Nova tab if you like; the agent's replies appear there as usual.

## 8. Check the results in the real tools

- **Instructions:** LPub3D for Windows has no installer. From its
  [latest release](https://github.com/trevorsandy/lpub3d/releases/latest),
  download the 64-bit zip, named like `LPub3D_x86_64-2.4.9...zip` (not the
  `renderers-win` zip and not the 32-bit `LPub3D_x86-` one). Right-click >
  Extract All to a folder such as `Desktop\LPub3D`, run `LPub3D.exe` from the
  extracted folder, and accept the LDraw library download on first start. If
  SmartScreen objects: More info > Run anyway. Then File > Open
  `out\instructions.mpd` and page through the steps. Export a PDF from its
  File menu.
- **Shopping list:** on BrickLink, open
  <https://www.bricklink.com/v3/wanted/upload.page> (logged in) and choose
  "Upload BrickLink XML format". That option takes pasted text, not a file:
  open `out\bricklink_wanted.xml` in Notepad, select all, copy, and paste it
  into the box. Note any item it rejects. The "Buy All" view on the new
  wanted list gives a price estimate.
- **Second opinion:** upload `out\flat.ldr` to Rebrickable as a custom parts
  list to see its own part matching and price estimate.

## 9. Stop Nova when you are done

```powershell
cd ..\ldraw-nova-docker
docker compose down
```

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `python` opens the Microsoft Store or prints nothing | Windows' placeholder, not real Python | Install Python (table above), reopen PowerShell, use `py -3` |
| `running scripts is disabled on this system` | PowerShell's default policy | Run the check exactly as written, with `-ExecutionPolicy Bypass` |
| `docker: command not found` or `error during connect` | Docker Desktop is not installed or not running | Start Docker Desktop; wait for "Engine running" |
| Docker Desktop will not start, or asks for WSL | WSL 2 is missing or out of date | In an administrator PowerShell: `wsl --install`, then `wsl --update`, then restart Windows. Virtualization must be enabled in the BIOS |
| Container exits at once; logs show `no such file or directory` or `\r` | Nova was cloned with Windows line endings | Delete both Nova folders and clone again as in step 4 |
| `port is already allocated` | Something else uses 8765 or 8443 | Create `ldraw-nova-docker\.env` containing `LDRAW_NOVA_WEB_PORT=8766`, then `docker compose up -d` and use that port |
| Build fails with `no space left on device` | Docker's disk is full | `docker system prune`, or raise the disk limit in Docker Desktop > Settings > Resources |
| Build is very slow or the machine crawls | Docker has too little memory | Close other apps; raise memory in Docker Desktop > Settings > Resources |
| `Nova database not found` | Wrong `--nova-db` path | The three folders must sit side by side; check with `dir ..\ldraw-nova\data\ldraw-info.db` |
| `Availability check did not run: No API key` | The key is not set in this window | Set `$env:REBRICKABLE_API_KEY` again; it does not survive closing PowerShell |
| `Rebrickable rejected the API key` | Wrong or regenerated key | Copy the key again from your Rebrickable settings |
| LPub3D cannot find parts | It has not downloaded the LDraw library | Accept its offer to download the library on first start |
| LPub3D will not start from the zip | It was run from inside the zip | Extract the whole zip first, then run `LPub3D.exe` from the extracted folder |

To see why the Nova container failed:

```powershell
cd ..\ldraw-nova-docker
docker compose logs --tail 50
```

## What to report back

- `out\report.md`, `out\availability.csv`, and each round's `out\feedback.md`
- Whether the agent's repairs fixed what was asked
- Anything LPub3D or BrickLink rejected
- The output of the setup check if any step failed
