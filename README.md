# FAReleaseTool

**Automated mod packager, bundler, and installer for Factorio Access.**  
Handles pulling sources, building zips, bundling assets, and even (optionally) configuring accessibility helpers for blind gamers.

---

## Features

- **Fetches mod sources** from repos defined in a YAML config file.
- **Packages mods** into Factorio-compatible zips using `fmtk`.
- **Bundles** all zips and required assets (launcher, JKM, mod-list.json) into a single distributable archive.
- **Configurable**: Each mod can be bundled as a zip or as raw source (see YAML).
- **Untested upload**: (Echoes upload commands; flip the `echo=True` argument to actually upload zips to the Factorio mod portal.)
- **Installs mods** into detected Factorio user data folders, including:
  - Steam (tested on Windows)
  - Standalone installer
  - Custom/portable installs
- **Configures Steam launch options** for accessibility.
- **Optionally copies JAWS JKM file** to all detected JAWS user config folders (Windows only; see caveats).
- **Can publish release bundles** to GitHub.

---

## Subcommands

All subcommands are accessible from the main CLI entry point.

- **fetch**:  
  Pulls/updates all mod source repositories defined in the YAML config.

- **package**:  
  Packages one or more mods into Factorio zip format using `fmtk`.

- **bundle**:  
  Combines all zips and assets (launcher, mod-list.json, JKM) into a single distributable archive.

- **upload**:  
  (Experimental/untested) Uploads built zips to the Factorio mod portal using `fmtk upload`. Currently, the tool *echoes* the command for safety. To actually run, set `echo=False` in your config/code.

- **publish**:  
  (Optional) Publishes the final bundle to a GitHub release.

- **install**:  
  Installs mods and assets into the detected Factorio data directory (Steam, installer, or custom location). Optionally sets Steam launch options and installs JAWS script on Windows.

---

## Environment Variables & `.env` Support

- The tool loads environment variables from a `.env` file in your working directory.
- To supply your GitHub token for API calls, add this line to your `.env`:
    ```
    GITHUB_TOKEN=yourtokenhere
    ```
- **Any environment variables used by `fmtk`** can also be set in `.env` and will be inherited by all `fmtk` subprocesses (untested, but should Just Work™).

---

## Configuration

- All mod info, repo URLs, and packaging options are defined in a YAML file (default: `config.yaml`).
- Each mod can be set to bundle as a zip (`bundle_zip: true`, default) or as source folder (`bundle_zip: false`).

---

## JAWS (Windows) Note

- **JAWS script copying is untested with real JAWS installs.**  
  The code works with simulated directory structures, but may need refinement for production/real-world use.
- You must be running on Windows and have a valid JAWS config directory for script copying to occur.

---

## Installation & Usage

Clone the repo, install dependencies (`pip install -r requirements.txt`), and run with:
`python fa_release_tool.py <subcommand> [options]`


See `-h` or `--help` for details on arguments for each subcommand.

---

## Caveats / To-Do

- **Upload and JAWS install** are not fully tested in the wild.
- Only tested with windows steam and portable installs

