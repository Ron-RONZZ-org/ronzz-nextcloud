# Contacts — patched frontend (dashboard.ronzz.org)

The deployed Contacts app (8.8.0) runs a **locally rebuilt frontend**. The
release tarball ships only built `js/`/`css/`, so these patches cannot be
applied to the deployed files with `patch` — they are applied to the **source**,
the bundle is rebuilt with vite, then swapped into `custom_apps/contacts/`.

Two patches stack on `nextcloud/contacts` **v8.8.0**:

1. **`patches/pr-5626-sorted-insertion.patch`** — upstream
   [PR #5626](https://github.com/nextcloud/contacts/pull/5626) (fix for
   [#5681](https://github.com/nextcloud/contacts/issues/5681)):
   `src/store/contacts.js` `addContact` silently dropped a new contact when its
   name sorted before every existing entry ("Contact introuvable"). **Still open
   upstream**, so it must be re-applied on every rebuild.
2. **`patches/duplicate-contact-and-fresh-copy-name.patch`** — local change
   (upstream [PR #5734](https://github.com/nextcloud/contacts/pull/5734)):
   - `src/store/addressbooks.js`: `copyContactToAddressbook` creates the copy
     with `addressbook.dav.createVCard(newContact.toStringStripQuotes())`
     instead of a WebDAV `COPY`. A `COPY` keeps the source file name, so the
     clone shared the original's URI in the other address book and a later move
     collided (`412 Precondition Failed`).
   - `src/components/ContactDetails.vue`: new **Duplicate contact** action
     (copies into the same address book).
   - `l10n/fr.{json,js}`: "Clone contact" → "Cloner le contact", new
     "Dupliquer le contact".

> **Status: LIVE (2026-09-19).** Deployed bundle `contacts-main.mjs` md5
> `075d68dce2f133c17a1b808e1d09cc01`; previous patched app backed up at
> `/var/backups/nextcloud/contacts-app-8.8.0-p5626-20260919.tgz`. Users need
> one hard refresh (Ctrl+Shift+R) after a swap — the `?v=` cache buster for
> `contacts-main.mjs` is tied to the app version (8.8.0) and does not change.
> Server-side source of truth: `RonzzIT:Deployment/Nextcloud` (gated wiki);
> full history: `logs/nextcloud.md` (local NC).

## Contents

```
contacts/
├── README.md                                         ← this runbook
└── patches/
    ├── pr-5626-sorted-insertion.patch                ← upstream #5626 (store/contacts.js + test)
    └── duplicate-contact-and-fresh-copy-name.patch   ← local (store + ContactDetails + l10n/fr)
```

## Rebuild & deploy

After `occ app:update contacts`, or on a fresh checkout:

```bash
# 1. Source at the exact release, in a scratch dir
git clone --depth 1 --branch v8.8.0 https://github.com/nextcloud/contacts.git contacts-8.8.0
cd contacts-8.8.0

# 2. Apply both patches, in order (context-based; both apply cleanly to v8.8.0)
git apply /path/to/contacts/patches/pr-5626-sorted-insertion.patch
git apply /path/to/contacts/patches/duplicate-contact-and-fresh-copy-name.patch

# 3. Build (Node ^24 — see engines in package.json)
npm ci
npm run build            # vite build --mode production

# 4. Backup the current deployed app, then swap js/ + css/ + the two l10n files
TS=$(date +%Y%m%d-%H%M%S)
ssh ronzz-linux-server-2 "docker exec nextcloud sh -c 'cd /var/www/html/custom_apps && tar czf /tmp/contacts-app-backup.tgz contacts' \
  && docker cp nextcloud:/tmp/contacts-app-backup.tgz /tmp/contacts-app-backup.tgz \
  && sudo mv /tmp/contacts-app-backup.tgz /var/backups/nextcloud/contacts-app-8.8.0-p5626-$TS.tgz \
  && docker exec nextcloud rm /tmp/contacts-app-backup.tgz"

tar czf /tmp/contacts-build.tgz js css l10n/fr.json l10n/fr.js
scp /tmp/contacts-build.tgz ronzz-linux-server-2:/tmp/
ssh ronzz-linux-server-2 "docker cp /tmp/contacts-build.tgz nextcloud:/tmp/contacts-build.tgz \
  && docker exec nextcloud sh -c 'cd /var/www/html/custom_apps/contacts && rm -rf js css \
     && tar xzf /tmp/contacts-build.tgz \
     && chown -R www-data:www-data js css l10n/fr.json l10n/fr.js \
     && rm /tmp/contacts-build.tgz'"
```

`js/` and `css/` are deleted before extracting, so hashed chunk names from the
old build do not linger. A static-file swap does **not** touch opcache, so the
`docker restart nextcloud` rule for GUI app-store operations (README §13) does
not apply here.

## Verification

Validated end-to-end (throwaway user, deleted afterwards) on the deployed
instance:

- "Dupliquer le contact" issues a `PUT` to a **fresh** random URI in the same
  address book (two cards, distinct URIs/UIDs, same fields).
- "Cloner le contact" to another book issues a `PUT` to a fresh URI (was a
  `COPY` keeping the source name).
- The move that used to fail now succeeds: `MOVE ab1/test-uid-0001.vcf →
  ab2/test-uid-0001.vcf` (`Overwrite: F`) returns **201** instead of 412.
