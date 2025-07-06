#!python3
import argparse
import base64
import dotenv
import git
from github import Github
import glob
import json
import os
import psutil
import requests
import shutil
import subprocess
import sys
import tempfile
import time
import vdf
import yaml
import zipfile

dotenv.load_dotenv()

FACTORIO_ID = "427520" # static, does not change

def load_info_json(path):
    """
    Given a directory or a zipfile, returns the info.json dict if found and valid,
    or None if info.json is missing at the correct location.
    Raises on invalid/corrupt json or missing required keys.
    """
    if os.path.isdir(path):
        info_path = os.path.join(path, "info.json")
        if not os.path.isfile(info_path):
            return None
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse info.json in {os.path.abspath(path)}: {e}")
    elif zipfile.is_zipfile(path):
        zip_base = os.path.splitext(os.path.basename(path))[0]
        info_json_path = f"{zip_base}/info.json"
        with zipfile.ZipFile(path, "r") as z:
            try:
                with z.open(info_json_path) as f:
                    info = json.load(f)
            except KeyError:
                # info.json not found at expected path
                return None
            except Exception as e:
                raise ValueError(f"Failed to parse info.json in {path}: {e}")
    else:
        raise ValueError(f"Not a directory or zipfile: {path}")

    # Check for minimal required keys
    if not isinstance(info, dict):
        raise ValueError(f"info.json in {path} is not a valid object")
    if "name" not in info or "version" not in info:
        raise ValueError(f"info.json in {path} missing required 'name' or 'version' key")
    return info

def validate_mod(mod_name, mod_path, info=None, verbose=False):
    try:
        if info is None:
            info = load_info_json(mod_path)
    except Exception as e:
        print(f"[validate_mod_path] {e}")
        return False
    if info is None:
        raise FileNotFoundError(f"Missing info.json in {os.path.abspath(mod_path)}")
    if info.get("name") != mod_name:
        print(f"[validate_mod_directory] info.json 'name' key mismatch: expected '{mod_name}', found '{info.get('name')}'")
        return False
    return True

def get_mod_version_or_raise(mod_path, info=None):
    abs_mod_path = os.path.abspath(mod_path)
    if info is None:
        info = load_info_json(mod_path)
    if info is None:
        raise FileNotFoundError(f"Missing info.json in {abs_mod_path}")
    if "version" not in info:
        raise KeyError(f"info.json missing required 'version' key in {abs_mod_path}")
    return info["version"]

def resolve_mod_dest(mod, global_dest=None):
    """
    Returns absolute path for the module's destination directory.
    Priority:
        1. CLI/global_dest (from --dest)
        2. config['default_dest']
        3. local dir './<modname>'
    - Per-mod dest is always joined relative to the active base dest.
    - If per-mod dest ends with '/', append modname to the path.
    - Per-mod dest can use '../', or be a bare directory, but cannot be absolute (for safety).
    """
    name = mod.get("name")
    mod_dest = mod.get("dest")  # might be None

    if global_dest:
        base = os.path.abspath(global_dest)
    elif "__global_default_dest__" in mod:
        base = os.path.abspath(mod["__global_default_dest__"])
    else:
        base = os.getcwd()

    if mod_dest:
        # Forbid absolute paths for per-mod dest, to ensure safety & portability
        if os.path.isabs(mod_dest):
            raise ValueError(f"Per-module dest should not be absolute: {mod_dest}")
        if mod_dest.endswith("/") or mod_dest.endswith(os.sep):
            # Join to base, then append the mod name
            dest = os.path.normpath(os.path.join(base, mod_dest, name))
        else:
            # Join to base, use as is
            dest = os.path.normpath(os.path.join(base, mod_dest))
    elif global_dest or "__global_default_dest__" in mod:
        dest = os.path.normpath(os.path.join(base, name))
    else:
        dest = os.path.abspath(name)

    return dest

def repo_matches_expected_url(repo, expected_url):
    """
    Return True if any remote URL matches the expected repo URL (normalized).
    """
    # Normalize both URLs to ignore .git and https vs ssh, etc
    def normalize(url):
        url = url.lower().rstrip('/')
        if url.endswith('.git'):
            url = url[:-4]
        return url
    expected_norm = normalize(expected_url)
    for remote in repo.remotes:
        for remote_url in remote.urls:
            if normalize(remote_url) == expected_norm:
                return True
    return False

def should_update_mod(mod, global_update):
    """
    Returns True/False: should we update this mod?
    Per-mod update overrides global; both default to True if omitted.
    """
    if "update" in mod:
        return bool(mod["update"])
    if global_update is not None:
        return bool(global_update)
    return True  # default if nothing set

def load_config_or_exit(config_path):
    """Load YAML config, handle errors, return (modules, global_config dict)."""
    if not os.path.exists(config_path):
        print(f"ERROR: Config file '{config_path}' not found.")
        sys.exit(1)
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    modules = config.get("modules", [])
    global_config = config.get("config", {}) or {}
    return modules, global_config

def find_mod(modules, name):
    """Find a mod by name (case-insensitive). Returns dict or None."""
    for mod in modules:
        if mod.get("name", "").lower() == name.lower():
            return mod
    return None

def get_mods_to_process(modules, modname=None):
    """Return a list of modules: just one if modname is set, all otherwise."""
    if modname:
        m = find_mod(modules, modname)
        if not m:
            print(f"ERROR: Mod '{modname}' not found in config.")
            sys.exit(1)
        return [m]
    return modules

def print_module_intro(name, repo, dest):
    print(f"Module: {name}")
    print(f"  Repo: {repo}")
    print(f"  Target directory: {dest}")

def get_fmtk_command():
    if sys.platform.startswith("win"):
        return "fmtk.cmd"
    else:
        return "fmtk"

def run_fmtk_command(args, cwd, verbose=False, echo=False):
    """
    Runs fmtk as a subprocess with given args list (not a string).
    - args: List[str], e.g. ['fmtk', 'package', '--outdir', '/some/path']
    - cwd: Directory to run fmtk from.
    Returns (exit_code, stdout, stderr)
    """
    fmtk  = [get_fmtk_command()]
    if echo:
        fmtk.insert(0, "echo")
    try:
        completed = subprocess.run(
            fmtk + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False
        )
        if verbose:
            print(f"  Ran: {' '.join(args)} (cwd={cwd})")
            print(f"  Return code: {completed.returncode}")
        if completed.stdout:
            print(f"  STDOUT:\n{completed.stdout.strip()}" if verbose else completed.stdout.strip())
        if completed.stderr:
            print(f"  STDERR:\n{completed.stderr.strip()}" if verbose else completed.stderr.strip())
        return completed.returncode, completed.stdout, completed.stderr
    except Exception as e:
        print(f"  ERROR running fmtk: {e}")
        return -1, "", str(e)

def find_mod_assets_or_sources(modules, source_path):
    """
    Given a list of module dicts and a source path, returns a dict:
    {modname: mod_path}
    - If bundle_zip is False: mod_path is the validated source directory for the mod.
    - If bundle_zip is True or missing: mod_path is the validated mod zip file.
    Raises on any missing or invalid mod assets.
    """
    results = {}
    for mod in modules:
        modname = mod.get("name")
        bundle_zip = mod.get("bundle_zip", True)
        if not bundle_zip:
            # Look for directory
            mod_dir = os.path.join(source_path, modname)
            if not os.path.isdir(mod_dir):
                raise FileNotFoundError(f"Expected source dir for '{modname}' not found: {mod_dir}")
            if not validate_mod(modname, mod_dir):
                raise ValueError(f"Directory for mod '{modname}' does not contain a valid info.json or name mismatch")
            results[modname] = mod_dir
        else:
            # Look for zip(s)
            pattern = os.path.join(source_path, f"{modname}_*.zip")
            zips = glob.glob(pattern)
            # Filter only those that are valid mod zips
            valid_zips = []
            for zipfile_path in zips:
                try:
                    info = load_info_json(zipfile_path)
                    if validate_mod(modname, zipfile_path, info):
                        valid_zips.append(zipfile_path)
                except Exception as e:
                    print(f"Skipping invalid zip {zipfile_path}: {e}")
                    continue
            if len(valid_zips) == 0:
                raise FileNotFoundError(f"No valid zip found for mod '{modname}' in {source_path}")
            if len(valid_zips) > 1:
                pretty = ', '.join(os.path.basename(f) for f in valid_zips)
                raise ValueError(f"Multiple valid zips found for mod '{modname}': {pretty}")
            results[modname] = valid_zips[0]
    print(f"found assets: {results}")
    return results

def get_github_repo(repo_url, token=None):
    """
    Given a GitHub repo URL, returns a PyGithub Repo object.
    """
    url = repo_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = url.split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub repo URL: {repo_url}")
    owner, repo = parts[-2], parts[-1]
    gh = Github(token) if token else Github()
    return gh.get_repo(f"{owner}/{repo}")

def download_file_from_github_api(repo_url, file_path, dest_path, branch="main", token=None, repo=None):
    """
    Download a single file from a GitHub repo using PyGithub.
    """
    # Parse owner/repo from repo_url
    if repo is None:
        repo = get_github_repo(repo_url, token=token)
    try:
        file_content = repo.get_contents(file_path, ref=branch)
    except Exception as e:
        raise FileNotFoundError(f"Could not find {file_path} in {repo.full_name}@{branch}: {e}")

    # PyGithub returns content base64 encoded
    content = base64.b64decode(file_content.content)
    with open(dest_path, "wb") as f:
        f.write(content)
    print(f"Downloaded {file_path} from {repo.full_name}@{branch} to {dest_path}")

def download_latest_release_asset(repo_url, asset_name, dest_path, token=None, repo=None):
    if repo is None:
        repo = get_github_repo(repo_url, token)
    try:
        release = repo.get_latest_release()
    except Exception as e:
        raise RuntimeError(f"Could not get latest release: {e}")
    for asset in release.get_assets():
        if asset.name == asset_name:
            asset_url = asset.browser_download_url
            headers = {"Authorization": f"token {token}"} if token else {}
            with requests.get(asset_url, headers=headers, stream=True) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            print(f"Downloaded {asset_name} from latest release of {repo.full_name} to {dest_path}")
            return
    raise FileNotFoundError(f"Asset '{asset_name}' not found in latest release of {repo.full_name}")

def build_release_zip(modules, mod_assets, jkm_path, launcher_path, modlist_path, default_dest, bundle_name, bundle_version):
    """
    Assembles the full Factorio Access bundle zip with mixed zips and folders for mods.
    mod_assets: {modname: path} where path is either zipfile or dir.
    """
    content_folder = f"{bundle_name}_content"
    bundle_folder = f"{bundle_name}_{bundle_version.replace('.', '_')}"
    zip_name = f"{bundle_name}_{bundle_version}.zip"
    mods_folder = "mods"
    with tempfile.TemporaryDirectory() as tmpdir:
        # Make the mods dir inside the tempdir
        mods_dir = os.path.join(tmpdir, bundle_folder, content_folder, mods_folder)
        os.makedirs(mods_dir, exist_ok=True)
        # Copy each mod asset (zip or dir)
        for mod in modules:
            name = mod["name"]
            modpath = mod_assets[name]
            if os.path.isdir(modpath):
                dest_dir = os.path.join(mods_dir, name)
                shutil.copytree(modpath, dest_dir, ignore=shutil.ignore_patterns('.git'))
            else:
                shutil.copy2(modpath, mods_dir)
        # Copy mod-list.json
        shutil.copy2(modlist_path, mods_dir)
        # Copy Factorio.jkm and launcher.exe
        content_dir = os.path.join(tmpdir, bundle_folder, content_folder)
        shutil.copy2(jkm_path, content_dir)
        shutil.copy2(launcher_path, content_dir)
        # Build the release zip at the right location
        zip_base = os.path.join(default_dest, bundle_folder)  # no .zip!
        archive_path = shutil.make_archive(
            base_name=zip_base,
            format="zip",
            root_dir=tmpdir,
            base_dir=bundle_folder
        )
        print(f"Release zip created: {archive_path}")
        return archive_path

def is_valid_steam_install(path):
    """
    Attempts to validate path is an actual steam install by checking for main executable and config file.
    """
    if not os.path.isdir(path): return False
    exe = "steam.exe" if sys.platform.startswith("win") else "steam"
    exe_path = os.path.join(path, exe)
    config_path = os.path.join(path, "config", "config.vdf")
    return os.path.isfile(exe_path) and os.path.isfile(config_path)

def find_steam_base():
    """
    Attempts to find the base Steam install directory for Windows, Linux, or macOS.
    Returns the path as a string if found, else None.
    """
    if sys.platform.startswith("win"):
        default_path=r"C:\Program Files (x86)\Steam"
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                value, regtype = winreg.QueryValueEx(key, "SteamPath")
                if value and is_valid_steam_install(value):
                    return value
                else:
                    path = default_path
        except Exception:
            # Could not read registry
            path = default_path
        # Try to fall back to default path if registry lookup failed
        if path and is_valid_steam_install(path):
            return path

    elif sys.platform.startswith("linux"):
        candidates = [
            os.path.expanduser("~/.steam/steam"),
            os.path.expanduser("~/.local/share/Steam")
        ]
        for path in candidates:
            if is_valid_steam_install(path):
                return path
    elif sys.platform == "darwin":
        # macOS
        path = os.path.expanduser("~/Library/Application Support/Steam")
        if is_valid_steam_install(path):
            return path
    return None

def find_factorio_steam_path():
    """
    Attempts to locate the steam install path for factorio
    """
    steam_path = find_steam_base()
    if steam_path is None: return None
    library_folders_path = os.path.join(steam_path, "SteamApps", "libraryfolders.vdf")
    with open(library_folders_path, encoding="utf-8") as f:
        library_folders = vdf.parse(f)
    for library in library_folders.get("libraryfolders", {}).values():
        if FACTORIO_ID in library.get("apps", {}):
            library_path = library["path"]
            break
    else:
        library_path = None
    if library_path is None: return None
    if sys.platform.startswith("win"):
        factorio_path = os.path.join(library_path, "SteamApps", "common", "Factorio", "bin", "x64", "factorio.exe")
    elif sys.platform.startswith("linux"):
        factorio_path = os.path.join(library_path, "SteamApps", "common", "Factorio", "bin", "x64", "factorio")
    elif sys.platform == "darwin":
        factorio_path = os.path.join(library_path, "SteamApps", "common", "Factorio", "factorio.app", "Contents", "MacOS", "factorio")
    if os.path.isfile(factorio_path):
        return factorio_path
    else:
        return None

def find_factorio_installer_path():
    """
    Attempts to locate the standalone/installer Factorio executable (non-Steam).
    Returns full path to the executable if found, else None.
    """
    possible_paths = []

    if sys.platform.startswith("win"):
        # Windows installer locations
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        possible_paths.extend([
            os.path.join(program_files, "Factorio", "bin", "x64", "factorio.exe"),
            os.path.join(program_files_x86, "Factorio", "bin", "x64", "factorio.exe"),
            # Common "manual unzip" location:
            os.path.expanduser(r"~\factorio\bin\x64\factorio.exe"),
        ])
    elif sys.platform.startswith("linux"):
        # Linux installer/portable locations
        possible_paths.extend([
            "/opt/factorio/bin/x64/factorio",
            os.path.expanduser("~/factorio/bin/x64/factorio"),
        ])
    elif sys.platform == "darwin":
        # macOS .app or user folder
        possible_paths.extend([
            "/Applications/factorio.app/Contents/MacOS/factorio",
            os.path.expanduser("~/factorio/factorio.app/Contents/MacOS/factorio"),
        ])

    # Return first existing file
    for path in possible_paths:
        if os.path.isfile(path):
            return path
    return None

def prompt_for_custom_factorio_path():
    """
    Prompts the user for a custom Factorio base folder, checks for the executable inside.
    Returns full path to the executable if valid, else None.
    """
    if sys.platform.startswith("win"):
        rel_path = os.path.join("bin", "x64", "factorio.exe")
    elif sys.platform.startswith("linux"):
        rel_path = os.path.join("bin", "x64", "factorio")
    elif sys.platform == "darwin":
        rel_path = os.path.join("factorio.app", "Contents", "MacOS", "factorio")
    else:
        print("Unsupported OS.")
        return None

    base = input("Enter your Factorio folder location (the base, not the bin/x64): ").strip()
    if not os.path.isdir(base):
        print(f"Directory '{base}' does not exist or is not a folder.")
        return None

    exe_path = os.path.join(base, rel_path)
    if os.path.isfile(exe_path):
        print(f"Found Factorio executable at: {exe_path}")
        return exe_path
    else:
        print(f"Could not find Factorio executable at: {exe_path}")
        return None

def choose_factorio_executable():
    """
    Prompts the user to select from detected Steam, installer, or custom Factorio installs.
    Returns the chosen executable path, or None if the user cancels.
    """
    while True:
        # Auto-detect options
        steam_path = find_factorio_steam_path()
        installer_path = find_factorio_installer_path()

        options = []
        option_texts = []

        if steam_path:
            options.append(steam_path)
            option_texts.append(f"Steam install: {steam_path}")
        if installer_path:
            options.append(installer_path)
            option_texts.append(f"Standalone installer: {installer_path}")
        # Always add custom path option last
        option_texts.append("Enter a custom Factorio path")
        options.append(None)  # Placeholder for custom path

        # Build and display menu
        print("\nSelect the Factorio executable to use:")
        for idx, desc in enumerate(option_texts, 1):
            print(f" {idx}. {desc}")
        print(" 0. Exit/cancel")

        try:
            choice = input("Enter your selection: ").strip()
        except EOFError:
            print("\nInput cancelled.")
            return None, False

        if not choice.isdigit():
            print("Invalid input. Please enter a number.")
            continue
        choice_num = int(choice)
        if choice_num == 0:
            print("Exiting.")
            return None, False
        if 1 <= choice_num <= len(options):
            selected = options[choice_num-1]
            if selected is not None:
                # Found auto-detected (steam or installer)
                return selected, selected == steam_path
            else:
                # Custom path chosen
                custom = prompt_for_custom_factorio_path()
                if custom:
                    return custom, False
                else:
                    print("\nInvalid path or Factorio executable not found. Please try again.")
                    continue
        else:
            print("Invalid selection. Please try again.")
            continue

def find_factorio_datadir(exe_path):
    """
    Given a full path to the Factorio executable, attempts to return the data dir
    """
    possible_paths = []
    exe_dir = os.path.dirname(exe_path)
    bin_token = f"{os.sep}bin"
    idx = exe_dir.lower().rfind(bin_token)
    if idx != -1:
        portable_factorio_root = exe_dir[:idx]
        if not portable_factorio_root:
            # Handle (rare) root paths
            portable_factorio_root = os.sep
    else:
        # Fallback: parent of exe_dir
        portable_factorio_root = os.path.dirname(os.path.dirname(exe_dir))
    if sys.platform.startswith("win"):
        platform_factorio_root = os.path.join(os.environ.get("appdata"), "factorio")
    elif sys.platform.startswith("linux"):
        platform_factorio_root= os.path.expanduser("~/.factorio")
    elif sys.platform == "darwin":
        platform_factorio_root = os.path.expanduser("~/Library/Application Support/factorio")
    for factorio_root in [portable_factorio_root, platform_factorio_root]:
        if all({os.path.isdir(os.path.join(factorio_root, subdir)) for subdir in {"config", "mods", "saves"}}):
            return factorio_root

    return None

def install_mod_assets_to_datadir(mod_assets, datadir):
    """
    Copies mod zips or folders from mod_assets into datadir/mods.
    Removes any existing zip/folder with the same name before copying.
    """
    mods_dir = os.path.join(datadir, "mods")
    os.makedirs(mods_dir, exist_ok=True)

    for modname, asset_path in mod_assets.items():
        if os.path.isdir(asset_path):
            # Mod is a folder. Target is mods/modname (copytree).
            dest = os.path.join(mods_dir, modname)
            # Remove existing folder if present
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            elif os.path.isfile(dest):
                os.remove(dest)
            shutil.copytree(asset_path, dest, ignore=shutil.ignore_patterns('.git'))
            print(f"Installed folder mod: {modname} → {dest}")
        else:
            # Mod is a zipfile. Target is mods/<basename>.zip
            zip_name = os.path.basename(asset_path)
            dest = os.path.join(mods_dir, zip_name)
            # Remove existing file or folder with the same name
            if os.path.isfile(dest):
                os.remove(dest)
            elif os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.copy2(asset_path, dest)
            print(f"Installed zip mod: {zip_name} → {dest}")

def get_steam_process():
    """
    Return the process whose name is exactly 'steam.exe' (case-insensitive), or None if not found.
    """
    for process in psutil.process_iter(['name']):
        try:
            name = process.info.get('name')
            if name and name.lower() == 'steam.exe':
                return process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

def is_steam_running():
    steam = get_steam_process()
    if steam is None: return False
    return steam.is_running()

def force_quit_steam(wait_seconds=5):
    """
    Attempts to terminate Steam gracefully first, then kills it if needed.
    Returns True if Steam is not running after this process, False otherwise.
    """
    steam = get_steam_process()
    if steam:
        print("Attempting to terminate Steam gracefully...")
        try:
            steam.terminate()
            steam.wait(timeout=wait_seconds)
        except psutil.TimeoutExpired:
            print("Steam did not close after terminate. Forcing kill...")
            try:
                steam.kill()
                steam.wait(timeout=wait_seconds)
            except Exception as e:
                print(f"Could not kill Steam: {e}")
                return False
        except Exception as e:
            print(f"Error terminating Steam: {e}")
            return False

        # Give a moment for process to exit
        time.sleep(2)
        if not is_steam_running():
            print("Steam has been closed.")
            return True
        else:
            print("Steam is still running after kill attempt.")
            return False
    else:
        print("Steam is not currently running.")
        return True

def prompt_modify_steam_options():
    """
    Ask the user if they want to modify Steam launch options.
    Returns True to proceed, False to skip.
    """
    while True:
        print("\nModify Steam launch options for Factorio?")
        print("  1. Yes, modify launch options")
        print("  2. No, skip this step")
        choice = input("Enter your choice: ").strip()
        if choice == "1":
            return True
        elif choice == "2":
            print("Skipping Steam launch option modification.")
            return False
        else:
            print("Invalid choice. Please enter 1 or 2.")

def prompt_steam_exit_text_menu():
    """
    Prompts the user to exit Steam, with a retry and force quit option.
    Returns True if Steam was closed (or not running), False if user skips/cancels.
    """
    while True:
        if not is_steam_running():
            print("Steam is not running. Continuing.")
            return True
        print("\nSteam is currently running. Please close Steam before continuing.")
        print("Options:")
        print("  1. Retry (check if you've closed Steam manually)")
        print("  2. Force quit Steam")
        print("  0. Skip and do NOT modify launch options")
        choice = input("Enter your choice: ").strip()
        if choice == "1":
            if not is_steam_running():
                print("Steam is closed. Continuing.")
                return True
            else:
                print("Steam is still running.")
        elif choice == "2":
            success = force_quit_steam()
            if success:
                print("Steam has been closed. Continuing.")
                return True
            else:
                print("Failed to close Steam. Please try again or close it manually.")
        elif choice == "0":
            print("Skipping Steam launch option modification.")
            return False
        else:
            print("Invalid selection. Please enter 1, 2, or 0.")

def set_factorio_steam_launch_options(steam_path, launcher_path, factorio_id="427520"):
    """
    Sets custom launch options for Factorio in all Steam user accounts on this install.
    - steam_path: root path to the Steam install (the folder with 'userdata')
    - launcher_path: full path to launcher.exe to set as the launch option
    """
    userdata_path = os.path.join(steam_path, "userdata")
    if not os.path.isdir(userdata_path):
        print(f"Could not find Steam userdata directory at {userdata_path}")
        return

    accounts = [d for d in os.listdir(userdata_path) if os.path.isdir(os.path.join(userdata_path, d))]
    if not accounts:
        print(f"No Steam accounts found in {userdata_path}")
        return

    custom_options = f'"{launcher_path}" %command%'
    updated_accounts = 0

    for accountid in accounts:
        config_path = os.path.join(userdata_path, accountid, "config", "localconfig.vdf")
        if not os.path.exists(config_path):
            # Not all accounts have this file
            continue
        try:
            with open(config_path, encoding='utf-8') as f:
                lconfig = vdf.load(f)
            # Defensive: check full path to launch options key
            store = lconfig.get("UserLocalConfigStore", {})
            apps = store.get("Software", {}).get("Valve", {}).get("Steam", {}).get("apps", {})
            if factorio_id not in apps:
                continue  # This account hasn't run Factorio

            # Set the launch options
            apps[factorio_id]['LaunchOptions'] = custom_options
            # Back up original
            shutil.copy2(config_path, config_path + ".bak")
            # Write new config
            with open(config_path, 'w', encoding='utf-8', newline='') as f:
                vdf.dump(lconfig, f, pretty=True)
            print(f"Set launch options for Factorio (account {accountid}) in {config_path}")
            updated_accounts += 1
        except Exception as e:
            print(f"Failed to update launch options for account {accountid}: {e}")

    if updated_accounts == 0:
        print("No launch options set (Factorio not found in any localconfig.vdf)")
    else:
        print(f"Launch options set for Factorio in {updated_accounts} account(s).")

def modify_factorio_steam_launch_options(steam_path, launcher_path):
    """
    Optionally prompts the user and modifies Steam launch options for Factorio.
    Only proceeds if user consents and Steam is not running.
    """
    if not prompt_modify_steam_options():
        return  # User skipped
    if not prompt_steam_exit_text_menu():
        return  # User cancelled/skip from within steam menu
    set_factorio_steam_launch_options(steam_path, launcher_path)

def install_assets_to_data_dir(default_dest, data_dir, factorio_path):
    """
    Copies mod-list.json and launcher.exe into the appropriate locations
    in the Factorio data directory, and runs 'factorio --dump-data'.
    Prints status messages and returns True on success, False on error.
    """
    try:
        # Ensure mods folder exists
        mods_dir = os.path.join(data_dir, "mods")
        os.makedirs(mods_dir, exist_ok=True)

        # Copy mod-list.json and launcher.exe
        shutil.copy2(os.path.join(default_dest, "mod-list.json"), mods_dir)
        shutil.copy2(os.path.join(default_dest, "launcher.exe"), data_dir)
        print("Copied mod-list.json and launcher.exe.")

    except Exception as e:
        print(f"Error during asset installation: {e}")
        return False
    return True

def install_jaws_jkm_file(source_dir):
    """
    Optionally copy the Factorio.jkm file to detected JAWS settings folders.
    Only runs on Windows, and only if JAWS settings folders are found.
    """
    if not sys.platform.startswith("win"):
        return

    appdata = os.environ.get("APPDATA")
    if not appdata:
        print("Cannot locate APPDATA; skipping JAWS install.")
        return

    jaws_root = os.path.join(appdata, "Freedom Scientific", "JAWS")
    if not os.path.isdir(jaws_root):
        print("No JAWS install found at", jaws_root)
        return

    jaws_versions = [v for v in os.listdir(jaws_root) if os.path.isdir(os.path.join(jaws_root, v))]
    install_targets = []
    for version in jaws_versions:
        version_dir = os.path.join(jaws_root, version, "Settings")
        if not os.path.isdir(version_dir):
            continue
        for lang in os.listdir(version_dir):
            lang_dir = os.path.join(version_dir, lang)
            if os.path.isdir(lang_dir):
                install_targets.append({"version": version, "lang": lang, "path": lang_dir})

    if not install_targets:
        print("No JAWS settings folders found for any version/language.")
        return

    filename = "Factorio.jkm"
    source_jkm = os.path.join(source_dir, filename)
    if not os.path.isfile(source_jkm):
        print(f"Cannot find {filename} in {source_dir}.")
        return

    # Interactive choice if multiple installs, otherwise install to the only one
    print("\nDetected JAWS settings folders:")
    for i, entry in enumerate(install_targets, 1):
        print(f"  {i}. JAWS {entry['version']} ({entry['lang']}) at {entry['path']}")
    if len(install_targets) == 1:
        print(f"Installing {filename} to {install_targets[0]['path']}")
        shutil.copy2(source_jkm, install_targets[0]['path'])
        print("Copy complete.")
        return

    print(f"  {len(install_targets)+1}. Install to ALL above")
    print("  0. Skip JAWS install/exit")
    while True:
        choice = input("Select which JAWS install to copy to: ").strip()
        if choice.isdigit():
            choice = int(choice)
            if choice == 0:
                print("Skipping JAWS install.")
                return
            elif 1 <= choice <= len(install_targets):
                target = install_targets[choice-1]
                print(f"Copying {filename} to {target['path']}")
                shutil.copy2(source_jkm, target['path'])
                print("Copy complete.")
                return
            elif choice == len(install_targets)+1:
                print(f"Copying {filename} to ALL detected JAWS settings folders.")
                for entry in install_targets:
                    shutil.copy2(source_jkm, entry['path'])
                print("Copy complete.")
                return
        print("Invalid choice. Please enter a valid number.")

def main():
    parser = argparse.ArgumentParser(
        prog="fa_release_tool",
        description="Factorio Access Release Tool: automate mod packaging, dependencies, and more."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch_parser = subparsers.add_parser("fetch", help="Fetch dependencies and prepare them for packaging.")
    fetch_parser.add_argument(
        "modname",
        nargs="?",  # Optional positional argument; if not given, zip all
        help="Name of the mod to fetch (if omitted, fetches all mods)"
    )
    fetch_parser.add_argument(
        "-d", "--dest",
        help="Directory to clone all mods into (overrides config default_dest)"
    )

    package_parser = subparsers.add_parser("package", help="Package mods using fmtk.")
    package_parser.add_argument(
        "modname",
        nargs="?",  # Optional positional argument; if not given, package all mods
        help="Name of the mod to package (if omitted, packages all mods)"
    )
    package_parser.add_argument(
        "-o", "--outdir",
        help="Directory to output packaged mod zips (defaults to default_dest)",
        default=None
    )
    package_parser.add_argument(
        "-s", "--source",
        help="Directory where cloned mods are found (overrides config default_dest)"
    )

    upload_parser = subparsers.add_parser(
        "upload",
        help="Upload the main FactorioAccess zip using fmtk upload"
    )

    bundle_parser = subparsers.add_parser(
        "bundle",
        help="Bundle all built mod zips, dependencies, and assets into a release archive."
    )
    bundle_parser.add_argument(
        "-s", "--source",
        help="Directory where packaged mod zips are found (overrides config default_dest)",
        default=None
    )

    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish the built release zip to a GitHub release"
    )
    publish_parser.add_argument(
        "--zip", required=True, help="Path to the zip file to upload"
    )
    publish_parser.add_argument(
        "--tag", help="Tag for the release (default: v<version from zip name>)"
    )
    publish_parser.add_argument(
        "--prerelease", action="store_true", help="Mark the release as a prerelease"
    )

    install_parser = subparsers.add_parser(
        "install",
        help="Install Factorio Access bundle (WIP)"
    )

    args = parser.parse_args()

    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "package":
        cmd_package(args)
    elif args.command == "upload":
        cmd_upload(args)
    elif args.command == "bundle":
        cmd_bundle(args)
    elif args.command == "publish":
        cmd_publish(args)
    elif args.command == "install":
        cmd_install(args)
    else:
        parser.error("Unknown subcommand (should never happen!)")

def cmd_fetch(args):
    modules, global_config = load_config_or_exit(args.config)
    global_default_dest = global_config.get("default_dest")
    global_update = global_config.get("update")
    global_dest = getattr(args, 'dest', None)
    modname = getattr(args, 'modname', None)

    # Stash global_default_dest in each module for use in dest resolver
    for mod in modules:
        if global_default_dest:
            mod["__global_default_dest__"] = global_default_dest

    for mod in get_mods_to_process(modules, modname):
        name = mod.get("name")
        repo = mod.get("repo")
        branch = mod.get("branch")
        commit = mod.get("commit")

        try:
            dest = resolve_mod_dest(mod, global_dest=global_dest)
        except Exception as e:
            print(f"  ERROR: Could not resolve destination for {name}: {e}\n")
            continue

        print_module_intro(name, repo, dest)

        try:
            if os.path.exists(dest):
                try:
                    r = git.Repo(dest)
                    if not repo_matches_expected_url(r, repo):
                        print(f"  ERROR: Directory '{dest}' contains a git repo, but its remotes do not match the expected repo URL.")
                        print(f"         Expected: {repo}")
                        print(f"         Found remotes: {[url for remote in r.remotes for url in remote.urls]}")
                        print("         Please move or delete this directory and try again.\n")
                        continue

                    update = should_update_mod(mod, global_update)

                    if not update:
                        print("  Repo exists and matches, skipping update (update is disabled for this mod).")
                        if validate_mod(name, dest):
                            version = get_mod_version_or_raise(dest)
                            print(f"  Found {name}; version {version}.\n")
                        else:
                            print(f"  Found {name}, but cannot validate info.json.\n")
                        continue

                    print("  Existing git repo found. Fetching updates...")
                    r.remotes.origin.fetch()
                    if branch:
                        print(f"  Checking out branch '{branch}'...")
                        r.git.checkout(branch)
                        r.remotes.origin.pull()
                    if commit:
                        print(f"  Checking out commit '{commit}'...")
                        r.git.checkout(commit)
                    else:
                        print(f"  Checked out latest on branch '{branch or 'default'}'.")
                except git.exc.InvalidGitRepositoryError:
                    print(f"  WARNING: Directory '{dest}' exists but is not a git repo. Please clean up manually.")
                    continue
            else:
                print(f"  Cloning repo to '{dest}'...")
                if branch:
                    r = git.Repo.clone_from(repo, dest, branch=branch)
                else:
                    r = git.Repo.clone_from(repo, dest)
                if commit:
                    print(f"  Checking out commit '{commit}'...")
                    r.git.checkout(commit)
                else:
                    print(f"  Checked out latest on branch '{branch or 'default'}'.")

            if validate_mod(name, dest):
                version = get_mod_version_or_raise(dest)
                print(f"  Done fetching {name}; version {version}.\n")
            else:
                print(f"Failed to fetch {name}. Cannot validate info.json.\n")
        except Exception as e:
            print(f"  ERROR: Problem fetching {name}: {e}\n")
            continue

def cmd_package(args):
    modules, global_config = load_config_or_exit(args.config)
    modname = getattr(args, 'modname', None)
    global_default_dest = args.source  or global_config.get("default_dest")
    outdir = args.outdir or global_default_dest
    mods_to_package = get_mods_to_process(modules, modname)

    # Make sure outdir exists and is absolute
    outdir = os.path.abspath(outdir)
    os.makedirs(outdir, exist_ok=True)

    for mod in mods_to_package:
        name = mod.get("name")
        source = resolve_mod_dest(mod, global_default_dest)  # Where the mod source is checked out
        if not os.path.isdir(source):
            print(f"  ERROR: Mod directory '{source}' does not exist. Skipping {name}.\n")
            continue
        print(f"Packaging mod '{name}' using `fmtk`...")
        exit_code, stdout, stderr = run_fmtk_command(
            ['package', '--outdir', os.path.relpath(outdir, source)],
            cwd=source,
            verbose=False
        )
        if exit_code == 0:
            print(f"Successfully packaged '{name}'.\n")
        else:
            print(f"Packaging failed for '{name}'. See above for details.\n")

def cmd_upload(args):
    modules, global_config = load_config_or_exit(args.config)
    default_dest = global_config.get("default_dest")
    # Find main mod entry (FactorioAccess)
    main_mod = next((m for m in modules if m.get("name") == "FactorioAccess"), None)
    if not main_mod:
        print("ERROR: FactorioAccess mod not found in config.")
        sys.exit(1)
    # Ensure bundle_zip True (for this upload we want the zip)
    main_mod = main_mod.copy()
    main_mod["bundle_zip"] = True

    mod_assets = find_mod_assets_or_sources([main_mod], default_dest)
    mod_zip = mod_assets["FactorioAccess"]

    # Prepare fmtk upload command
    modname = main_mod["name"]
    args_list = ["upload", mod_zip, modname]
    cwd = default_dest
    print(f"Uploading {mod_zip} as mod '{modname}' using fmtk...")
    exit_code, stdout, stderr = run_fmtk_command(args_list, cwd, verbose=True, echo=True)

    if exit_code == 0:
        print(f"Upload complete.")
    else:
        print(f"Upload failed with exit code {exit_code}.")

def cmd_bundle(args):
    modules, global_config = load_config_or_exit(args.config)
    default_dest = args.source  or global_config.get("default_dest")
    launcher_repo = global_config.get("launcher_repo")
    launcher_branch = global_config.get("launcher_branch", "main")
    print("Checking for mod zip assets:")
    try:
        mod_assets = find_mod_assets_or_sources(modules, default_dest)
    except ValueError as e:
        print(f"Wrong number of mod zip assets. Please clean the source folder and re-run the fetch and package steps. {e}")
        sys.exit(1)
    else:
        print(f"Found {len(mod_assets)} mod zip files. OK.")
    jkm_filename = "Factorio.jkm"
    jkm_path = os.path.join(default_dest, jkm_filename)
    gh_token = os.getenv("GITHUB_TOKEN")  # Optional: set in env for private repo
    gh_repo = None
    print("Checking for jkm file:")
    if not os.path.isfile(jkm_path):
        try:
            gh_repo = get_github_repo(launcher_repo, token=gh_token)
            download_file_from_github_api(
                launcher_repo,
                jkm_filename,
                jkm_path,
                branch=launcher_branch,
                token=gh_token,
                repo=gh_repo
            )
        except Exception as e:
            print(f"ERROR: Could not download {jkm_filename} from {launcher_repo}: {e}")
            sys.exit(1)
    else:
        print(f"{jkm_filename} already exists, skipping download.")
    launcher_filename = "launcher.exe"
    launcher_path = os.path.join(default_dest, launcher_filename)
    print("Checking for launcher executable:")
    if not os.path.isfile(launcher_path):
        try:
            if gh_repo is None:
                gh_repo = get_github_repo(launcher_repo, token=gh_token)
            download_latest_release_asset(
                launcher_repo,
                launcher_filename,
                launcher_path,
                token=gh_token,
                repo=gh_repo
            )
        except Exception as e:
            print(f"ERROR: Could not download {launcher_filename} from {launcher_repo}: {e}")
            sys.exit(1)
    else:
        print(f"{launcher_filename} already exists, skipping download.")
    modlist_filename = "mod-list.json"
    modlist_path = os.path.join(default_dest, modlist_filename)
    print(f"Generating {modlist_filename}:")
    modlist = [{"name": "base", "enabled": True}]
    for mod in modules:
        modlist.append({"name": mod["name"], "enabled": True})
    modlist = {"mods": modlist}
    try:
        with open(modlist_path, "w", encoding="utf-8") as f:
            json.dump(modlist, f, indent=2, separators=(",", ": "))
    except Exception as e:
        print(f"Error creating modlist at {modlist_path}: {e}")
    else:
        print(f"Created modlist at {modlist_path}.")
    # create the final output zip
    main_mod = None
    for mod in modules:
        if mod.get("name") == "FactorioAccess":
            main_mod = mod
            break
    if main_mod is None:
        print("ERROR: Could not find FactorioAccess mod in modules.")
        sys.exit(1)
    is_beta = bool(main_mod.get("beta", False))
    main_mod_path = resolve_mod_dest(main_mod, default_dest)
    fa_version = get_mod_version_or_raise(main_mod_path)
    bundle_name = f"{main_mod.get("name")}_{"beta" if is_beta else ""}"
    zip_path = build_release_zip(modules, mod_assets, jkm_path, launcher_path, modlist_path, default_dest, bundle_name, fa_version)
    print(f"zip_path: {zip_path}")

def cmd_publish(args):
    zip_path = args.zip
    if not os.path.isfile(zip_path):
        print(f"ERROR: Release zip file not found: {zip_path}")
        sys.exit(1)
    # Extract version from zip filename if no tag is given
    base = os.path.basename(zip_path)
    # Expected: FactorioAccess_beta_1_2_3.zip or FactorioAccess_1_2_3.zip
    tag = args.tag
    if not tag:
        parts = os.path.splitext(base)[0].split('_')
        if len(parts) > 1:
            tag = parts[-1]
        if not tag:
            tag = "vlatest"
    modules, global_config = load_config_or_exit(getattr(args, 'config', "modules.yaml"))
    fa_mod = find_mod(modules, "FactorioAccess")
    if fa_mod is None:
        print("Error: unable to load FactorioAccess entry from mod list")
        sys.exit(1)
    fa_repo_url = fa_mod.get("repo")
    if not fa_repo_url:
        print("ERROR: No repo configured in YAML.")
        sys.exit(1)
    gh_token = os.getenv("GITHUB_TOKEN")  # Optional: set in env for private repo
    gh_repo = get_github_repo(fa_repo_url, token=gh_token)

    # Check for existing release
    release = None
    try:
        release = gh_repo.get_release(tag)
        print(f"Found existing release for tag {tag}. Will update assets.")
    except Exception:
        # Create new release if not found
        print(f"No release found for tag {tag}. Creating new release.")
        release = gh_repo.create_git_release(
            tag=tag,
            name=f"Release {tag}",
            message=f"Automated release for {tag}",
            draft=False,
            prerelease=getattr(args, "prerelease", False)
        )
    # Remove asset with the same name if exists
    asset_name = os.path.basename(zip_path)
    for asset in release.get_assets():
        if asset.name == asset_name:
            print(f"Removing existing asset '{asset_name}' from release.")
            asset.delete_asset()
    # Upload the new asset
    print(f"Uploading {asset_name} to release {tag}...")
    with open(zip_path, "rb") as f:
        release.upload_asset(
            path=zip_path,
            name=asset_name,
            label=asset_name,
            content_type="application/zip"
        )
    print(f"Upload complete: {asset_name} for release {tag}.")

def cmd_install(args):
    """
    Installs all necessary assets, mods, and helper files into the user's Factorio data directory.

    - Loads config and module definitions.
    - Prompts user to select Factorio executable (Steam or standalone).
    - Finds the appropriate Factorio data directory.
    - Installs mods and supporting assets (mod-list.json, launcher.exe).
    - If running Steam, optionally configures Steam launch options for Factorio.
    - Optionally installs the Factorio JAWS script (Windows only).

    Exits quietly if any required step is skipped or not confirmed by the user.
    """
    modules, global_config = load_config_or_exit(args.config)
    default_dest = global_config.get("default_dest")

    # Prompt user to select Factorio (Steam/installer/custom)
    factorio_path, is_steam = choose_factorio_executable()
    print(f"Chosen Factorio executable: {factorio_path}")

    # Find Factorio data directory
    data_dir = find_factorio_datadir(factorio_path)
    print(f"Detected Factorio data directory: {data_dir}")

    # Locate mods/assets to install and install them
    assets = find_mod_assets_or_sources(modules, default_dest)
    install_mod_assets_to_datadir(assets, data_dir)
    install_assets_to_data_dir(default_dest, data_dir, factorio_path)

    # If Steam install, optionally set launch options
    if is_steam:
        launcher_path = os.path.join(data_dir, "launcher.exe")
        modify_factorio_steam_launch_options(find_steam_base(), launcher_path)

    # Optionally install JAWS script (on Windows)
    install_jaws_jkm_file(default_dest)
if __name__ == "__main__":
    main()
