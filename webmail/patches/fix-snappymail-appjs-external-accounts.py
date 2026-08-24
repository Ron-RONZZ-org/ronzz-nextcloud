#!/usr/bin/env python3
"""Ronzz.org fix: add `manual` + IMAP/SMTP server-settings observables to the
SnappyMail "Add account" popup view model (`static/js/min/app.min.js`).

Part of the external-secondary-accounts feature (policy: @ronzz.org restriction
applies to primary login only; secondary accounts may use any external domain,
auto-detected or via manual server settings). The template + i18n + PHP side is
`snappymail-external-accounts.patch`.

Why a script and not `patch -p1`: the bundle is a single ~400 KB line, so a
unified diff would carry the whole line twice (~800 KB, unreviewable). This
does exact-string replacements with drift detection instead.

Usage (from the SnappyMail version dir, i.e. `/var/www/snappymail/snappymail/v/2.38.2`,
same `cd` as the patch):
    python3 /path/to/ronzz-nextcloud/webmail/patches/fix-snappymail-appjs-external-accounts.py

Re-run safe: prints "already applied" if the changes are present.
Exit codes: 0 = ok/already applied, 1 = anchor drifted (upstream bundle
changed — re-derive the hunk), 2 = file not found.
"""
import sys
from pathlib import Path

EDITS = [
    # 1) observables for the manual server-settings section
    (
        'class AccountPopupView extends AbstractViewPopup{constructor(){super("Account"),Ae(this,{isNew:!0,name:"",email:"",password:"",submitRequest:!1,submitError:"",submitErrorAdditional:""})}hideError(){this.submitError("")}',
        'class AccountPopupView extends AbstractViewPopup{constructor(){super("Account"),Ae(this,{isNew:!0,name:"",email:"",password:"",submitRequest:!1,submitError:"",submitErrorAdditional:"",manual:!1,imapHost:"",imapPort:"993",imapType:"SSL",smtpHost:"",smtpPort:"587",smtpType:"STARTTLS"})}hideError(){this.submitError("")}',
    ),
    # 2) reset the manual toggle when the popup is hidden/opened
    (
        'onHide(){this.password(""),this.submitRequest(!1),this.submitError(""),this.submitErrorAdditional("")}onShow(e){let t=e?.isAdditional();this.isNew(!t),this.name(t?e.name:""),this.email(t?e.email:"")}}',
        'onHide(){this.password(""),this.submitRequest(!1),this.submitError(""),this.submitErrorAdditional(""),this.manual(!1)}onShow(e){let t=e?.isAdditional();this.isNew(!t),this.name(t?e.name:""),this.email(t?e.email:""),this.manual(!1)}}',
    ),
]


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path('static/js/min/app.min.js')
    if not path.is_file():
        print(f'ERROR: {path} not found', file=sys.stderr)
        return 2
    data = path.read_text(encoding='utf-8')

    all_applied = all(new in data for _, new in EDITS)
    if all_applied:
        print(f'OK: already applied to {path}')
        return 0

    for i, (old, new) in enumerate(EDITS, 1):
        if new in data:
            continue
        count = data.count(old)
        if count != 1:
            print(
                f'ERROR: edit #{i} anchor found {count} times in {path} '
                '(expected exactly 1) - upstream bundle changed, this patch '
                'drifted; re-derive the hunk',
                file=sys.stderr,
            )
            return 1
        data = data.replace(old, new)

    path.write_text(data, encoding='utf-8')
    print(f'OK: patched {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
