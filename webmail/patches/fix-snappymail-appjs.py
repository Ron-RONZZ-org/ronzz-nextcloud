#!/usr/bin/env python3
"""Ronzz.org fix: add `activeScriptName` + `advancedMode` observables to the
SnappyMail Filters settings page view model (`static/js/min/app.min.js`).

Why a script and not `patch -p1`: the bundle is a single ~200 KB line, so a
unified diff would carry the whole line twice (~400 KB, unreviewable). This
does an exact-string replacement with drift detection instead.

Usage (from the SnappyMail version dir, i.e. `/var/www/snappymail/snappymail/v/2.38.2`,
same `cd` as the filters-ux patch):
    python3 /path/to/ronzz-nextcloud/webmail/patches/fix-snappymail-appjs.py

Re-run safe: prints "already applied" if the change is present.
Exit codes: 0 = ok/already applied, 1 = anchor drifted (upstream bundle
changed — re-derive the hunk), 2 = file not found.
"""
import sys
from pathlib import Path

OLD = (
    'this.hasActive=Ce((()=>this.scripts().filter((e=>e.active())).length)),'
    'this.scriptForDeletion=ko.observable(null).askDeleteHelper()}addScript(){'
)
NEW = (
    'this.hasActive=Ce((()=>this.scripts().filter((e=>e.active())).length)),'
    'this.activeScriptName=Ce((()=>{const t=this.scripts().find(e=>e.active());return t?t.name():""})),'
    'this.advancedMode=ko.observable(!!localStorage.getItem("snappymail_advanced")),'
    'this.advancedMode.subscribe(e=>localStorage.setItem("snappymail_advanced",e?"1":"0")),'
    'this.scriptForDeletion=ko.observable(null).askDeleteHelper()}addScript(){'
)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path('static/js/min/app.min.js')
    if not path.is_file():
        print(f'ERROR: {path} not found', file=sys.stderr)
        return 2
    data = path.read_text(encoding='utf-8')
    if NEW in data:
        print(f'OK: already applied to {path}')
        return 0
    count = data.count(OLD)
    if count != 1:
        print(
            f'ERROR: anchor found {count} times in {path} (expected exactly 1) '
            '- upstream bundle changed, this patch drifted; re-derive the hunk',
            file=sys.stderr,
        )
        return 1
    path.write_text(data.replace(OLD, NEW), encoding='utf-8')
    print(f'OK: patched {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
