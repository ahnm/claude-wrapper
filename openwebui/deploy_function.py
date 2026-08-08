"""Push a pipe file into OpenWebUI's Functions, so editing it here is enough.

  python deploy_function.py venture_council.py
  python deploy_function.py *.py --url http://127.0.0.1:3000
  python deploy_function.py venture_council.py --dry-run

Creates the function if it is missing, updates it in place if it exists, and
leaves its enabled state and configured valves alone. The id defaults to the
filename stem (venture_council.py -> venture_council); pass --id to override.

The API key is read, in order, from --key, $OPENWEBUI_API_KEY, --key-file, or
~/.openwebui_api_key. Get one in OpenWebUI: avatar -> Settings -> Account ->
API keys -> Create new secret key. Treat it like a password - it carries your
full account access. Do not commit it.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:3000"
DEFAULT_KEY_FILE = Path.home() / ".openwebui_api_key"


def read_key(args) -> str:
    if args.key:
        return args.key.strip()
    env = os.environ.get("OPENWEBUI_API_KEY", "").strip()
    if env:
        return env
    for path in (Path(args.key_file) if args.key_file else None, DEFAULT_KEY_FILE):
        if path and path.is_file():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
    sys.exit(
        "No API key. Pass --key, set OPENWEBUI_API_KEY, or write the key to "
        f"{DEFAULT_KEY_FILE}.\nOpenWebUI: avatar -> Settings -> Account -> API keys."
    )


def frontmatter(source: str) -> dict:
    """Pull title/description out of the pipe's leading docstring."""
    m = re.match(r'\s*"""(.*?)"""', source, re.DOTALL)
    if not m:
        return {}
    block, out, key = m.group(1), {}, None
    for line in block.splitlines():
        field = re.match(r"^(\w+):\s*(.*)$", line)
        if field:
            key = field.group(1).lower()
            value = field.group(2).strip()
            out[key] = "" if value in (">-", ">", "|") else value
        elif key and line.strip():
            out[key] = (out.get(key, "") + " " + line.strip()).strip()
    return out


def call(url: str, key: str, path: str, payload=None):
    req = urllib.request.Request(
        url.rstrip("/") + path,
        method="POST" if payload is not None else "GET",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach OpenWebUI at {url}: {e.reason}")


def norm(s) -> str:
    """Loose key for matching ids/names across case and punctuation."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def installed(args, key: str) -> list:
    """Existing functions, so we update in place instead of duplicating."""
    status, body = call(args.url, key, "/api/v1/functions/")
    if status in (401, 403):
        sys.exit(f"OpenWebUI rejected the API key (HTTP {status}). Check the key, and that "
                 "API keys are enabled in Admin Settings -> General.")
    if status != 200 or not isinstance(body, list):
        sys.exit(f"Could not list functions (HTTP {status}): {body}")
    return body


def deploy(path: Path, args, key: str, existing: list) -> bool:
    source = path.read_text(encoding="utf-8")
    meta = frontmatter(source)
    fid = args.id or re.sub(r"\W", "_", path.stem).lower()
    name = args.name or meta.get("title") or path.stem

    by_id = {f.get("id") for f in existing}
    # An explicit --id is a deliberate target (a side-by-side test copy, say);
    # never loosely remap it onto the live function.
    if not args.id and fid not in by_id:
        # Installed by hand? OpenWebUI derives its own id from the title and
        # title-cases the name, so match loosely rather than duplicating the
        # function under a second id.
        match = next((f for f in existing if norm(f.get("name")) == norm(name)), None)
        match = match or next(
            (f for f in existing if norm(f.get("id")).startswith(norm(fid))), None)
        if match:
            fid = match["id"]
            # keep the installed display name; renaming it would move the entry
            # in the model picker for no reason
            name = match.get("name") or name
            print(f"  note: matched installed {name!r}, updating id {fid!r}")
    exists = fid in {f.get("id") for f in existing}

    payload = {
        "id": fid,
        "name": name,
        "content": source,
        "meta": {"description": meta.get("description", ""), "manifest": meta},
    }
    verb = "update" if exists else "create"
    print(f"{path.name}: id={fid!r} name={name!r} -> {verb} ({len(source):,} chars)")
    if args.dry_run:
        print("  DRY RUN - nothing sent")
        return True

    endpoint = (f"/api/v1/functions/id/{fid}/update" if exists
                else "/api/v1/functions/create")
    status, body = call(args.url, key, endpoint, payload)
    if status != 200:
        print(f"  FAILED - HTTP {status}: {body}")
        return False
    print(f"  OK - {verb}d")

    active = isinstance(body, dict) and body.get("is_active")
    if not active and args.activate:
        status, _ = call(args.url, key, f"/api/v1/functions/id/{fid}/toggle", {})
        print("  enabled" if status == 200 else f"  could not enable (HTTP {status})")
    elif not active:
        print("  note: function is disabled — pass --activate, or enable it in "
              "Workspace -> Functions")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="pipe .py file(s) to deploy")
    ap.add_argument("--url", default=os.environ.get("OPENWEBUI_URL", DEFAULT_URL))
    ap.add_argument("--key", help="API key (prefer OPENWEBUI_API_KEY)")
    ap.add_argument("--key-file", help=f"file holding the key (default {DEFAULT_KEY_FILE})")
    ap.add_argument("--id", help="function id; defaults to the filename stem. An explicit "
                                 "id is targeted exactly — use it for side-by-side test copies")
    ap.add_argument("--name", help="display name; defaults to the docstring title")
    ap.add_argument("--activate", action="store_true",
                    help="enable the function after creating it (new ones start disabled)")
    ap.add_argument("--delete", action="store_true",
                    help="remove the function instead of deploying (needs --id)")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen")
    args = ap.parse_args()

    paths = [Path(f) for f in args.files]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        sys.exit("Not a file: " + ", ".join(str(p) for p in missing))
    if args.id and len(paths) > 1:
        sys.exit("--id cannot be used with multiple files")

    key = read_key(args)
    print(f"OpenWebUI: {args.url}")
    existing = installed(args, key)

    if args.delete:
        if not args.id:
            sys.exit("--delete needs an explicit --id, so it cannot remove the wrong one")
        target = next((f for f in existing if f.get("id") == args.id), None)
        if not target:
            sys.exit(f"No function with id {args.id!r} — nothing to delete")
        print(f"delete {args.id!r} ({target.get('name')!r})")
        if args.dry_run:
            sys.exit("DRY RUN - nothing sent")
        req = urllib.request.Request(
            args.url.rstrip("/") + f"/api/v1/functions/id/{args.id}/delete",
            method="DELETE", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            print("  OK - deleted" if r.status == 200 else f"  HTTP {r.status}")
        sys.exit(0)
    print(f"{len(existing)} function(s) already installed")
    ok = all([deploy(p, args, key, existing) for p in paths])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
