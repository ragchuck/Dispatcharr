import os
import re
import threading

lock = threading.Lock()

# ---------------------------------------------------------------------------
# Logo auto-matching helpers
# ---------------------------------------------------------------------------

LOGO_DIR = "/data/logos"
LOGO_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
LOGO_MATCH_THRESHOLD = 75


def normalize_logo_name(name):
    """Normalize a name for logo matching: strip extension, collapse separators, lowercase."""
    name = re.sub(r"\.\w{2,5}$", "", name)
    name = name.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", name).strip().lower()


def build_logo_candidates():
    """
    Build the full set of logo candidates from the DB and the logo directory.

    Returns a 2-tuple:
      db_logos        - list of dicts {id, name, url, norm_name}
      file_candidates - list of (path, display_name, norm_name) for every
                        image file found under LOGO_DIR
    """
    from .models import Logo

    db_logos = [
        {**entry, "norm_name": normalize_logo_name(entry["name"])}
        for entry in Logo.objects.values("id", "name", "url")
    ]

    file_candidates = []
    if os.path.isdir(LOGO_DIR):
        for dirpath, _dirs, filenames in os.walk(LOGO_DIR):
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in LOGO_IMAGE_EXTS:
                    display_name = (
                        os.path.splitext(filename)[0]
                        .replace("-", " ")
                        .replace("_", " ")
                        .title()
                    )
                    file_candidates.append((
                        os.path.join(dirpath, filename),
                        display_name,
                        normalize_logo_name(filename),
                    ))

    return db_logos, file_candidates



# Dictionary to track usage: {account_id: current_usage}
active_streams_map = {}

def increment_stream_count(account):
    with lock:
        current_usage = active_streams_map.get(account.id, 0)
        current_usage += 1
        active_streams_map[account.id] = current_usage
        account.active_streams = current_usage
        account.save(update_fields=['active_streams'])

def decrement_stream_count(account):
    with lock:
        current_usage = active_streams_map.get(account.id, 0)
        if current_usage > 0:
            current_usage -= 1
            if current_usage == 0:
                del active_streams_map[account.id]
            else:
                active_streams_map[account.id] = current_usage
            account.active_streams = current_usage
            account.save(update_fields=['active_streams'])
