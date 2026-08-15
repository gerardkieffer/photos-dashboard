# Apple Photos library — technical reference

> **Note on this copy.** This is the technical reference the dashboard's
> collectors are built from, included here because the code and its error
> messages cite its section numbers. It is identical to the original except that
> the person names and the one place name used as worked examples have been
> replaced with invented ones, so the document can be published. The replacements
> preserve each example's point: the variant-spelling names still show an accent
> dropped, a surname respelled, and a first name against a full name; the
> NFC/NFD name still begins with the same two characters as the byte sequences
> quoted beside it; and the time zone is still one at +02:00, matching the
> `ZTIMEZONEOFFSET` value shown. All measurements, counts and queries are
> unchanged.


Everything below was **measured directly**, against real libraries. Nothing here is recalled from
documentation or inferred from naming. Where something was observed but not explained, it says so.

Numbers in tables are counts from the measured libraries and will differ on yours — they are given
so you can judge sample size, not so you can expect the same figures. The **rules** are meant to be
general; every version-specific fact is paired with the query that re-derives it.

| | |
|---|---|
| Reference library | 21,738 own assets, 3,381 shared-album assets, 77 GB of originals, 22,499 files |
| `LibrarySchemaVersion` | **5001** (`database/DataModelVersion.plist`) |
| `MetaSchemaVersion` | 3 |
| macOS | Darwin 24.6.0 / 25.6.0 (Sequoia / Tahoe) |
| Verified with | ExifTool 13.55, sqlite3 3.51, Python 3.14 |

---

## 1. Bundle layout

`X.photoslibrary` is a directory, not a file:

```
X.photoslibrary/
├── database/          Photos.sqlite (+ -wal, -shm, .lock), metaSchema.db, photos.db,
│                      DataModelVersion.plist, search/, rebuildHistory
├── originals/         the untouched imported files          ← the only irreplaceable data
├── resources/
│   ├── renders/       edited versions + adjustment recipes
│   ├── derivatives/   thumbnails and display-size JPEGs (regenerable)
│   ├── cpl/           cloud photo library staging (cloudsync.noindex, derivatives)
│   ├── journals/      *.plj / *.plist change journals (Asset, Album, …)
│   ├── caches/  streams/  smartsharing/
├── scopes/            cloudsharing/, momentshared/, syndication/
├── private/           per-client state, one dir per bundle id (photolibraryd, photoanalysisd, …)
├── external/          same bundle-id layout as private/
└── internal/          incl. csutaxonomy/CSUTaxonomy.sqlite
```

`originals/` and `database/` are the two directories that matter. `derivatives/` (45,149 files in the
reference library, 1.3 GB of Spotlight index alongside) is a cache and is rebuilt on demand.

**Third-party tools may add directories inside the bundle.** digiKam, pointed at `originals/`, creates
`originals/.dtrash/` and moves "deleted" files there rather than unlinking them — so they still occupy
space inside the library and still appear to a naive directory walk. Any tool that inventories
`originals/` should prune dot-directories.

---

## 2. The database is Core Data

`Photos.sqlite` is a Core Data store, which dictates every naming convention in it.

### 2.1 Entities and the `Z_ENT` number

`Z_PRIMARYKEY` maps entity names to numbers:

```sql
SELECT Z_ENT, Z_NAME, Z_SUPER, Z_MAX FROM Z_PRIMARYKEY ORDER BY Z_ENT;
```

`Z_SUPER` gives inheritance: `Album`, `Folder`, `FetchingAlbum`, `ImportSession` and `Conversation`
are all subclasses of `GenericAlbum`, so they share the single `ZGENERICALBUM` table and are told
apart by `Z_ENT`.

`Z_MAX` is the highest primary key issued for that entity. It is both a cheap row-count upper bound
and, when writing, a value you **must** maintain (§3.2).

### 2.2 `Z_ENT` numbers are not stable — not even within one `LibrarySchemaVersion`

This is the single most important portability rule in this document, and it is stronger than
previously thought.

Measured on **the same library**, before and after a Photos "Repair Library" pass:

| entity | before | after |
|---|---:|---:|
| `AdditionalAssetAttributes` | 1 | 1 |
| `Asset` | 3 | 3 |
| `DetectedFace` | 20 | **23** |
| `GenericAlbum` | 29 | **32** |
| `Album` | 30 | **33** |
| `Folder` | 38 | **41** |
| `InternalResource` | 46 | **51** |
| `Keyword` | 47 | **52** |
| `Moment` | 53 | **58** |
| `Person` | 54 | **59** |

`LibrarySchemaVersion` read **5001 both times**, and `MetaSchemaVersion` **3 both times**. The
persistent-history string pool (§3.4) recorded

```
-[PLModelMigrator _recordCurrentVersionMetadataInPersistentStore:migrationType:forceRebuildReason:
  sourceModelVersion:updateLegacyMigrationState:journalRebuildRequred:]
-[PLModelMigrator _loadFileSystemDataIntoDatabaseIfNeededWithReason:…] (inode=…) .Rebuild UT
```

so a model migration ran during the repair. New entities were inserted into the managed object
model, shifting everything after them by 3 to 5. The version string did not move with it.

**Consequences:**

- `LibrarySchemaVersion` is **not** a safe cache key for entity numbers. Re-resolve on every run,
  in the same process that does the work.
- Any hard-coded `Z_ENT` — in a query, a script, or a lookup table — is a latent bug that will fire
  the next time the user repairs their library or updates macOS.
- Numbers you resolved before an operation that can trigger migration (repair, rebuild, OS upgrade)
  are stale afterwards.

### 2.3 Join tables are named after `Z_ENT` — never hard-code them

Many-to-many relations live in tables called `Z_<ENT><RELATIONSHIP>`, and *both* column names embed
an entity number:

| Table | Columns | Meaning |
|---|---|---|
| `Z_1KEYWORDS` | `Z_1ASSETATTRIBUTES`, `Z_52KEYWORDS` | 1 = AdditionalAssetAttributes, 52 = Keyword |
| `Z_33ASSETS` | `Z_33ALBUMS`, `Z_3ASSETS` | 33 = Album, 3 = Asset |

The same library previously had `Z_47KEYWORDS` and `Z_30ASSETS` (§2.2). Resolve at runtime:

```sql
-- find the table
SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Z\_%KEYWORD%' ESCAPE '\';
-- then find its columns
PRAGMA table_info(Z_1KEYWORDS);
```

A robust resolver looks for the table by pattern, then picks the column whose name contains the
target entity word rather than assuming a number:

```python
cols = [r[1] for r in con.execute("PRAGMA table_info(Z_1KEYWORDS)")]
kw_col   = next(c for c in cols if "KEYWORD" in c.upper())
attr_col = next(c for c in cols if c != kw_col)
```

Note the ordering convention: the column for entity *E*'s side is `Z_<E><INVERSE-RELATIONSHIP-NAME>`.
In `Z_1KEYWORDS`, the AdditionalAssetAttributes side is `Z_1ASSETATTRIBUTES` because the inverse
relationship on `Keyword` is called `assetAttributes`. Do not assume the number in a column name
matches the entity that column points at — match on the word.

### 2.4 Reading the database safely

The live store uses WAL. To get a consistent read-only snapshot without touching the library:

```sh
cp Photos.sqlite Photos.sqlite-wal Photos.sqlite-shm /somewhere/
sqlite3 /somewhere/Photos.sqlite "PRAGMA journal_mode=delete;"   # folds the WAL in
sqlite3 /somewhere/Photos.sqlite "PRAGMA integrity_check;"       # expect: ok
```

Copying `Photos.sqlite` alone loses everything still in the WAL — which can be megabytes of recent
edits.

Check nothing has the library open before writing to it:

```sh
lsof -p "$(pgrep -x photolibraryd)" | grep photoslibrary
pgrep -x Photos
```

### 2.5 System vs secondary libraries — which processes touch what

`photolibraryd`, `photoanalysisd` and `cloudphotod` run permanently but each holds only **one**
library: the System Photo Library (or, on a machine with no system library set, something like
`~/Library/Photos/Libraries/Syndication.photoslibrary`). Verify rather than assume:

```sh
for p in photolibraryd photoanalysisd cloudphotod; do
  pid=$(pgrep -x $p) && lsof -p "$pid" | grep -o '/[^ ]*photoslibrary' | sort -u
done
defaults read com.apple.photolibraryd SystemLibraryPath
```

**A secondary library gets no background processing at all.** Face analysis, scene classification and
search indexing only advance while Photos.app is open in the foreground on that library. In the
reference library, `database/search/psi.sqlite` was not written once in the hours between a repair
and the next inspection, because Photos was not left open. If you are waiting for a secondary library
to finish indexing, the app must stay open — and on external storage this can take hours.

**Promoting a library to System Photo Library changes this completely, and is independent of
iCloud.** Photos → Settings → General → *Use as System Photo Library* is a separate switch from
Settings → iCloud → *iCloud Photos*; a library can be the system library with iCloud off, so none of
the §13 sync hazards apply. Measured after promoting a library on an external volume, with Photos
**closed**, these processes held it:

```
67 handles  managedcomputekitservice   (Apple Intelligence compute)
45          photoanalysisd
20          photolibraryd
17          mediaanalysisd
 8          cloudphotod
 4 each     Spotlight, Wallpaper extension
```

and `ZBACKGROUNDJOBWORKITEM` held 48,726 queued jobs. The search index went from 110 KB to 28.8 MB
in one session.

Two ways to confirm the promotion took effect — the obvious one is unreliable:

```sh
defaults read com.apple.photolibraryd SystemLibraryPath      # was empty even after promotion
defaults read com.apple.photolibraryd | grep -A2 ShortCodeD  # lists the promoted bundle path
lsof | grep "<library path>" | awk '{print $1}' | sort | uniq -c   # the real evidence
```

`SystemLibraryPath` was **not** set in the measured case; the library appeared instead under
`PLBackgroundJobServiceBundleRecordsKeyWithShortCode*`. Checking only `photolibraryd`'s open files
also misses it — the bulk of the work is done by `photoanalysisd`, `mediaanalysisd` and
`managedcomputekitservice`.

Note that promotion starts Apple's own scene and object recognition over the whole library. Those
labels live in the media-analysis tables, not in `ZKEYWORD`, so they do not collide with imported
keywords — but the library will then carry two independent sets of "what is in this photo".

**Even after promotion, the heavy analysis is throttled while the Mac is in use.** This surprised us,
and it is invisible from the database: the only evidence lives in the unified log (§20). The Duet
Activity Scheduler evaluates each analysis job against a set of weighted policies, and the heaviest
of them refuses to run the work while somebody is at the keyboard:

```
com.apple.mediaanalysisd.photos.visualsearch:
  {name: Device Activity Policy, policyWeight: 20.000, response: {33, 0.00, [{deviceActivity == 1}]}}
  Decision: MNP
```

`deviceActivity == 1` means the machine is active, the policy scores `0.00`, and `MNP` means *May Not
Proceed*. Over a two-hour window of normal use we counted 4,008 `MNP` decisions against 1,384 `CP`
(*Can Proceed*) and 258 `AMNP` (*Absolutely May Not Proceed*).

Two further constraints, both measured, make a long analysis run harder to complete than it looks:

- Work started by Photos.app is **cancelled when Photos quits**. The log fills with
  `userCanceledErr` and messages such as `Scene asset processing cancelled` and
  `Face asset processing cancelled`. Every task is logged with `origin: On demand` and tied to the
  Photos.app process id, so opening and closing the app repeatedly makes no net progress.
- Throughput while the machine was in use was roughly 2–7 queue items per minute against a queue of
  48,000. At that rate a full first-time analysis of a 21,000-asset library is days of idle time, not
  an overnight job.

So to let a large library finish analysing: promote it, open Photos, put the machine on mains power,
prevent sleep, and then genuinely leave it alone. Note that `caffeinate -s` was not sufficient in one
test; the Energy Saver settings were needed instead.

Importantly, **this queue is not a prerequisite for keyword search** — see §19, where keyword
indexing turned out to be essentially complete while 48,330 analysis jobs were still pending. Do not
assume a stalled analysis queue explains a keyword problem.

Distinguish libraries by inode, not path — two paths can look alike:

```sh
stat -f "%i %N" "A/database/Photos.sqlite" "B/database/Photos.sqlite"
```

---

## 3. Writing to the store

Reading a Core Data store is ordinary SQL. Writing to it is not — there are four mechanisms that
have nothing to do with your data and everything to do with whether Photos accepts and displays it.

### 3.1 Core Data triggers call functions that only exist inside Core Data

Tables such as `ZPERSON` and `ZDETECTEDFACE` carry triggers like:

```sql
CREATE TRIGGER ZT_ZPERSON_ZFACECOUNT_ZDETECTEDFACE_ZPERSON_INSERT_INCREMENT
AFTER INSERT ON ZPERSON FOR EACH ROW BEGIN
  UPDATE ZPERSON SET ZFACECOUNT = (SELECT COUNT(Z_PK) FROM ZDETECTEDFACE
    WHERE (ZPERSONFORFACE = NEW.Z_PK)
      AND (ZHIDDEN == 0 AND ZASSETVISIBLE == 1 AND ZCENTERX != 0))
   WHERE Z_PK = NEW.Z_PK;
  SELECT NSCoreDataTriggerUpdateAffectedObjectValue('ZPERSON', Z_ENT, Z_PK, 'ZFACECOUNT', ZFACECOUNT)
    FROM ZPERSON WHERE Z_PK = NEW.Z_PK;
END
```

The functions seen in these triggers:

- `NSCoreDataTriggerUpdateAffectedObjectValue`
- `NSCoreDataDATriggerUpdatedAffectedObjectValue`
- `NSCoreDataDATriggerInsertUpdatedAffectedObjectValue`

They are registered by Core Data's own SQLite runtime, so a plain `sqlite3` connection aborts the
statement with **`no such function`**. Enumerate them for any store before writing:

```sql
SELECT sql FROM sqlite_master WHERE type='trigger';
```

**Register them as no-ops rather than dropping the triggers:**

```python
for fn in ("NSCoreDataTriggerUpdateAffectedObjectValue",
           "NSCoreDataDATriggerUpdatedAffectedObjectValue",
           "NSCoreDataDATriggerInsertUpdatedAffectedObjectValue"):
    con.create_function(fn, -1, lambda *a: None)
```

These calls are cache-invalidation hooks aimed at a *live* Core Data context; writing offline there
is nothing to notify, and returning NULL is inert. The triggers' real work — the `UPDATE` that
maintains `ZPERSON.ZFACECOUNT` — is ordinary SQL and still runs. Dropping the triggers instead would
leave denormalised counters stale and risks not restoring them.

Corollary: **do not maintain trigger-managed columns yourself.** Setting `ZFACECOUNT` by hand
overwrites the value the trigger computed under Apple's own predicate
(`ZHIDDEN=0 AND ZASSETVISIBLE=1 AND ZCENTERX<>0`), which is not the predicate you would guess.

### 3.2 Primary keys come from `Z_PRIMARYKEY`

Core Data does not use SQLite `AUTOINCREMENT`. For each entity, the next primary key is
`Z_PRIMARYKEY.Z_MAX + 1`, and `Z_MAX` must be updated to match:

```python
maxpk = con.execute("SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME='Keyword'").fetchone()[0]
maxpk += 1
con.execute("INSERT INTO ZKEYWORD (Z_PK, Z_ENT, Z_OPT, ZTITLE, ZUUID) VALUES (?,?,1,?,?)",
            (maxpk, ent_keyword, title, str(uuid.uuid4()).upper()))
con.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_NAME='Keyword'", (maxpk,))
```

Every row also needs:

- `Z_ENT` — the entity number, resolved at runtime (§2.2)
- `Z_OPT` — the optimistic-locking counter; **1** for a freshly inserted row
- `ZUUID` / `ZPERSONUUID` etc. where the entity has one — uppercase UUID string

**This applies to Core Data's own internal tables too, and getting it wrong breaks the whole
library.** `Z_PRIMARYKEY` contains entries named `CHANGE`, `TRANSACTION` and `TRANSACTIONSTRING`
for the persistent-history tables (§3.4), with `Z_ENT` values in a high range such as 16001–16003:

```sql
SELECT Z_ENT, Z_NAME, Z_MAX FROM Z_PRIMARYKEY
 WHERE Z_NAME IN ('CHANGE','TRANSACTION','TRANSACTIONSTRING');
```

A tool that allocated `ATRANSACTION.Z_PK` as `MAX(Z_PK)+1` and did not advance `Z_MAX` left the
counter one behind the highest row. Core Data then tried to insert a transaction at that same id and
failed:

```
CoreData: error: … Persitent History - Transaction constraint primary key violation
NSCocoaErrorDomain = 134030, NSValidationErrorKey = Transaction
→ PhotoKit save failed → PHPhotosErrorDomain Code=-1
```

Because **every** save writes a history transaction, this broke *all* editing in the library — new
titles, captions and keywords silently refused to save, in the UI appearing as text that vanished on
Enter and keywords rendered greyed-out and then discarded. The damage was library-wide even though
only 30 assets had been touched, because the counter is global rather than per-asset. Removing the
offending rows so that `MAX(Z_PK)` matched `Z_MAX` again restored normal operation immediately.

Two lessons worth carrying:

- **Never use `MAX(Z_PK)+1`.** Always allocate from `Z_PRIMARYKEY.Z_MAX + 1` and write the new value
  back, for every table including the internal ones.
- **Verify counters after any write**, because the failure is silent at write time and only surfaces
  later, inside Photos:

  ```sql
  SELECT p.Z_NAME, p.Z_MAX FROM Z_PRIMARYKEY p;   -- compare against MAX(Z_PK) of each table
  ```

  A `MAX(Z_PK)` greater than its `Z_MAX` is a live fault, not a cosmetic one.

### 3.3 `Z_OPT` is incremented on every save

`Z_OPT` is Core Data's optimistic-locking version. Photos increments it on the objects it saves —
measured on a single keyword edit: `ZADDITIONALASSETATTRIBUTES.Z_OPT` 4→5 and `ZASSET.Z_OPT` 5→6;
on a photo edited several times, 3→9. Direct inserts that leave `Z_OPT` untouched produce rows that
are structurally valid but were never "saved" in Core Data's accounting.

### 3.4 Persistent history — why correct data can be invisible in the UI

This is the mechanism most likely to make a direct write appear to have failed when it has not.

Three tables implement Core Data **persistent history tracking**:

| table | contents |
|---|---|
| `ATRANSACTION` | one row per save: `ZTIMESTAMP` (Apple epoch float), `ZQUERYGEN` blob, and `ZAUTHORTS` / `ZBUNDLEIDTS` / `ZCONTEXTNAMETS` / `ZPROCESSIDTS` indexes into the string pool |
| `ATRANSACTIONSTRING` | the string pool — e.g. `com.apple.photolibraryd`, `photolibraryd`, `PhotoKit changes: com.apple.Photos (Photos) (inode=83139) .Generic IN` |
| `ACHANGE` | one row per changed object: `ZCHANGETYPE` (0 = insert, 1 = update), `ZENTITY` (the `Z_ENT`), `ZENTITYPK` (the object's `Z_PK`), `ZTRANSACTIONID`, and a `ZCOLUMNS` blob bitmap of which properties changed, plus `ZTOMBSTONE0…5` |

Measured: adding **one** keyword to **one** photo through the Photos UI produced

```
ZKEYWORD            +1     the keyword itself
Z_1KEYWORDS         +2     the join rows
ATRANSACTION        +8
ACHANGE            +21     against AdditionalAssetAttributes (1), Asset (3), Keyword (52)
ATRANSACTIONSTRING  +1
```

**The failure mode.** Keywords inserted directly into `ZKEYWORD` + the join table are read correctly
by SQL, survive Photos restarts, and are structurally indistinguishable from Photos' own rows — and
yet the Info inspector (⌘I) shows an empty Keywords field, across repeated relaunches. Typing any
keyword onto that photo by hand makes **all** of its previously-invisible keywords appear at once,
because the edit forces the relationship to be re-faulted.

Observed asymmetry: in the same library and the same write, **faces appeared in the People UI
immediately** while keywords did not. So not every part of the UI depends on this path, and the
absence of one is not evidence that the write failed.

**The fault is in the Photos.app UI layer, not in the store or the daemons.** A unified-log capture
(§20) taken while the Info panel showed an empty Keywords field for an asset that had one keyword in
the database settles this. At the moment the panel was displaying nothing, `photolibraryd` logged

```
keyword for asset: <private>          ← one line: it read the keyword correctly
Donating 1 items to Spotlight for bundleID: com.apple.Photos
```

and after a keyword was typed into the same photo it logged two such lines, one per keyword. So the
row, the relationship, the daemon's read path and the search-index donation were all working while
the UI showed nothing.

That evidence also rules out the plausible-sounding fixes:

- **Not persistent history.** Photos wrote its own `ATRANSACTION` rows around the edit, and
  `photolibraryd` needed no history entry to read the keyword.
- **Not the search index.** Editing an asset enqueues a per-asset job for
  `PLBackgroundJobHighPrioritySearchIndexingWorker` (flags `asset-core`), and the directly-inserted
  keyword had already been ingested.
- **Not `Z_OPT`.** The asset carried an incremented `Z_OPT` and still did not display.

Attempted and ineffective:

1. Incrementing `Z_OPT` on the touched objects and their owning objects — no effect.
2. Synthesising `ATRANSACTION` + `ACHANGE` rows — no effect, and **actively destructive if done
   carelessly**: allocating those rows' primary keys without advancing `Z_PRIMARYKEY.Z_MAX` breaks
   every subsequent save in the library (§3.2). That failure also invalidated the experiment, since
   Photos could not process history at all afterwards.

The only reliable trigger found is **touching the asset in the UI**: adding any keyword makes all of
that asset's keywords appear at once. The single suggestive clue in the log is
`Missing prefetched properties … Fetching on demand on the main queue`, logged repeatedly as the
panel opens, which hints at a UI-side property prefetch that does not include the keyword
relationship for already-cached assets. That was **not** verified — the Info panel's own fetch is an
in-process Core Data operation and is not logged.

Practical guidance: treat this as cosmetic. Directly-inserted keywords are correct, readable by
`photolibraryd`, indexed, and searchable. Do not escalate to writing Core Data internals for a
display refresh; the one attempt to do so broke all editing in the library.

### 3.5 A safe write procedure

1. Confirm `pgrep -x Photos` is empty and no daemon holds this library (§2.5). Note that this second
   condition **cannot be met for the System Photo Library**: `photolibraryd` and the analysis daemons
   hold it permanently, even with Photos closed. Writing to a promoted library therefore always races
   a live Core Data client that may hold cached copies of the rows you touch. Prefer doing such work
   while the library is still a secondary one, or demote it first.
2. Copy `Photos.sqlite` + `-wal` + `-shm` aside as a backup.
3. Open with plain `sqlite3`, register the `NSCoreData*` no-ops (§3.1).
4. Resolve `Z_ENT` numbers and join-table columns (§2.2, §2.3).
5. `BEGIN IMMEDIATE`, do all inserts, update `Z_PRIMARYKEY.Z_MAX`, `COMMIT` — roll back as one unit
   on any error.
6. `PRAGMA integrity_check` — expect `ok`.
7. Compare `MAX(Z_PK)` against `Z_PRIMARYKEY.Z_MAX` for **every** table you touched, internal tables
   included. `MAX(Z_PK) > Z_MAX` means the next write from Photos will collide (§3.2).
8. Open Photos and make one trivial edit — add a keyword to any photo, then remove it. If it saves,
   the store still accepts writes. This catches counter damage that no `integrity_check` will.

---

## 4. Assets

### 4.1 Identity and file path

`ZASSET` is one row per asset. The original's path is built from two columns:

```
originals/<ZDIRECTORY>/<ZFILENAME>
```

- **Own assets** (`ZBUNDLESCOPE = 0`): `ZDIRECTORY` is a single hex character `0`–`F`, the first
  character of the UUID, and `ZFILENAME` is `<ZUUID>.<ext>` — e.g.
  `originals/7/724E6B25-ACC3-4A76-A94E-B16668CF0408.heic`.
- **Shared-album assets** (`ZBUNDLESCOPE = 2`): `ZDIRECTORY` is `<numeric-scope>/<album-UUID>` and
  the file is **not on disk**. There is no local original to write to.

`ZASSET.ZUUID` is the identity to key on everywhere.

Photos **renames on import**: originals are stored as `UUID.ext`, with the extension normalised —
every JPEG becomes `.jpeg`, never `.jpg`. The user-facing name is kept in
`ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME`.

**Companion files use a `_N` suffix before the extension**, and they are *not* separate assets:

| pattern | what it is |
|---|---|
| `<UUID>_3.mov` | the video component of a Live Photo |
| `<UUID>_5.xmp` | an XMP sidecar Photos itself keeps for some assets |
| `<UUID>_5.aae` | an Apple adjustment sidecar |

A UUID parser must therefore anchor on `<UUID>` followed immediately by `.` — a regex like
`^([0-9A-F]{8}-…-[0-9A-F]{12})\.` — so companions do not collide with their parent asset. In the
reference library this correctly separated 22,499 files into 21,738 assets plus companions.

### 4.2 The columns that scope a query

| Column | Values seen | Meaning |
|---|---|---|
| `ZBUNDLESCOPE` | 0, 2 | 0 = own library, 2 = iCloud Shared Album |
| `ZKIND` | 0, 1 | 0 = photo, 1 = video |
| `ZTRASHEDSTATE` | 0, 1 | 1 = in Recently Deleted |
| `ZHIDDEN` | 0, 1 | 1 = in the Hidden album |
| `ZVISIBILITYSTATE` | 0, 2 | 2 = excluded from the main grid; **not** the same as `ZHIDDEN` |
| `ZFAVORITE` | 0, 1 | |

The near-universal correct filter for "real photos I own":

```sql
WHERE ZBUNDLESCOPE = 0 AND ZTRASHEDSTATE = 0
```

`ZVISIBILITYSTATE = 2` and `ZHIDDEN = 1` are independent mechanisms with no overlap in the reference
library (426 and 106 assets). Photos does **not** export either through the normal Export command.

### 4.3 Dimensions and orientation

`ZASSET.ZWIDTH` / `ZHEIGHT`, and `ZADDITIONALASSETATTRIBUTES.ZORIGINALWIDTH` / `ZORIGINALHEIGHT`.

**`ZASSET.ZORIENTATION` is not reliably the file's EXIF orientation.** Measured across 16,442
candidate photos:

| | count |
|---|---|
| DB and EXIF agree | 13,774 |
| **DB and EXIF disagree** | **644** (572 are DB=1 while EXIF=6) |
| file carries no EXIF orientation | 2,024 |

The reason was not established. **For any geometry that must line up with the file, read the
orientation from the file, not the database.**

### 4.4 Missing originals are not pruned

Deleting files from `originals/` outside Photos does **not** cause a repair to remove the
corresponding assets. Measured: 4 assets whose originals were deleted (2 of them Live Photos, so 6
files including `_3.mov` companions) still had complete `ZASSET` rows after a full Repair Library,
pointing at paths that no longer exist. A repair reconciles the database with itself, not with the
filesystem.

---

## 5. Dates and time zones

All Core Data timestamps are **seconds since 2001-01-01 00:00:00 UTC** (Apple epoch). Add
`978307200` for Unix time:

```sql
SELECT datetime(ZDATECREATED + 978307200, 'unixepoch') FROM ZASSET;
```

Verified end to end: `ZDATECREATED = 781885472.253` → `2025-10-11 14:24:32` UTC, while the file's
`DateTimeOriginal` is `2025:10:11 16:24:32` with `OffsetTimeOriginal +02:00`. **The stored value is
UTC**; local wall-clock time needs the offset.

The offset lives in `ZEXTENDEDATTRIBUTES`:

- `ZTIMEZONEOFFSET` — seconds (e.g. 7200)
- `ZTIMEZONENAME` — either an Olson name (`Europe/Zurich`) or a fixed offset (`GMT+0200`),
  depending on what the capturing device recorded

Other dates on `ZASSET`: `ZADDEDDATE`, `ZMODIFICATIONDATE`, `ZTRASHEDDATE`, `ZCLOUDSERVERPUBLISHDATE`.

`ZEXTENDEDATTRIBUTES` also mirrors much of the shot data: `ZCAMERAMAKE`, `ZCAMERAMODEL`,
`ZLENSMODEL`, `ZISO`, `ZAPERTURE`, `ZSHUTTERSPEED`, `ZFOCALLENGTH`, `ZFLASHFIRED`, `ZCODEC`,
`ZFPS`, `ZDURATION`, `ZCREDIT`, plus its own `ZLATITUDE`/`ZLONGITUDE`.

---

## 6. Keywords (tags)

```
ZASSET ─1:1─ ZADDITIONALASSETATTRIBUTES ─M:N─ Z_1KEYWORDS ─→ ZKEYWORD.ZTITLE
```

```sql
SELECT a.ZUUID, w.ZTITLE
  FROM ZASSET a
  JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
  JOIN Z_1KEYWORDS k  ON k.Z_1ASSETATTRIBUTES = aa.Z_PK
  JOIN ZKEYWORD  w    ON w.Z_PK = k.Z_52KEYWORDS   -- resolve 52 from Z_PRIMARYKEY
 WHERE a.ZBUNDLESCOPE = 0 AND a.ZTRASHEDSTATE = 0;
```

The same keyword can be linked twice through different rows, so de-duplicate.

`ZKEYWORD` is small: `Z_PK`, `Z_ENT`, `Z_OPT`, `ZSHORTCUT` (NULL throughout the reference library),
`ZTITLE`, `ZUUID`. To attach a keyword to an asset you need **three** things, not one: a `ZKEYWORD`
row (reuse if the title already exists), a join row, and the join must hang off
`ZADDITIONALASSETATTRIBUTES.Z_PK` — **not** `ZASSET.Z_PK`.

An empty `ZEDITEDIPTCATTRIBUTES` table exists with a `ZKEYWORDS` column. In the reference library it
has **0 rows** even for assets with many keywords, so it is not the keyword store.

---

## 7. People and faces

The most intricate part of the schema, and the part with the most non-obvious geometry.

### 7.1 Tables

- **`ZPERSON`** — one row per person cluster. `ZFULLNAME` is the name the user typed;
  `ZDISPLAYNAME`, `ZPERSONUUID`, `ZFACECOUNT` (trigger-maintained, §3.1), `ZKEYFACE` →
  `ZDETECTEDFACE.Z_PK`. Most rows are unnamed auto-clusters (`ZTYPE=0, ZDETECTIONTYPE=1,
  ZVERIFIEDTYPE=0`). `ZVERIFIEDTYPE = 1` and `-2` both occur; semantics not established.
  Also carries `ZCONTACTMATCHINGDICTIONARY`, a keyed-archiver binary plist.
- **`ZDETECTEDFACE`** — one row per detected face instance, ~74 columns.
  `ZASSETFORFACE` → `ZASSET.Z_PK`, `ZPERSONFORFACE` → `ZPERSON.Z_PK`.
  Filter `ZHIDDEN = 0 AND ZISINTRASH = 0`.
  Rich attribute set: `ZQUALITY`, `ZBLURSCORE`, `ZROLL`, `ZPOSEYAW`, `ZHASSMILE`, `ZEYESSTATE`,
  `ZGLASSESTYPE`, `ZAGETYPE`, `ZGENDERTYPE`, `ZMANUAL`, body/torso boxes
  (`ZBODYCENTERX/Y/WIDTH/HEIGHT`), gaze (`ZGAZECENTERX/Y`, `ZGAZEANGLE`), and
  `ZSOURCEWIDTH`/`ZSOURCEHEIGHT` (the pixel dimensions the detection ran against).
- **`ZDETECTEDFACEPRINT`**, **`ZFACECROP`**, **`ZDETECTEDFACEGROUP`** — the recognition machinery.

A named face is the join of three tables:

```sql
SELECT a.ZUUID, p.ZFULLNAME, f.ZCENTERX, f.ZCENTERY, f.ZSIZE
  FROM ZDETECTEDFACE f
  JOIN ZPERSON p ON p.Z_PK = f.ZPERSONFORFACE
  JOIN ZASSET  a ON a.Z_PK = f.ZASSETFORFACE
 WHERE f.ZHIDDEN = 0 AND f.ZISINTRASH = 0
   AND TRIM(COALESCE(p.ZFULLNAME,'')) <> '';
```

### 7.2 Face geometry — reading

`ZDETECTEDFACE` stores `ZCENTERX`, `ZCENTERY`, `ZSIZE` as normalised floats. Their relationship to
the image is **not** the obvious one. Determined empirically against 2,707 face regions already
written by Apple or Lightroom (matched by person name, plus unambiguous single-face files).

**Axis mapping — depends on the file's EXIF orientation:**

| EXIF orientation | (ZCENTERX, ZCENTERY) → (X, Y) | samples | median centre error |
|---|---|---|---|
| 1 | `(x, 1-y)` | 2,444 | 0.021 |
| 3 | `(1-x, y)` | 34 | 0.020 |
| 6 | `(1-y, 1-x)` | 195 | 0.011 |
| 8 | `(y, x)` | 33 | 0.023 |

Each winner beat the runner-up by **10–20×**, so there is no ambiguity. Orientation 2 (mirrored) had
too few samples to fit and remains unknown.

1. **The Y axis is flipped** relative to image coordinates for the common case (orientation 1).
2. **The mapping is independent of `AppliedToDimensions`.** Files declare that field inconsistently
   for rotated photos; fitting separately per declared frame gives the same winning mapping, so the
   normalised coordinates describe the displayed image regardless.
3. Use the **file's** EXIF orientation, not `ZASSET.ZORIENTATION` (§4.3).

**Box size, reading direction:**

- The box is **square in pixels** — measured aspect ratio (w·W)/(h·H) has median **1.000**.
- `ZSIZE` normalises against **`max(W, H)`**, not W or H individually (stdev 0.263 vs 0.392 / 0.497).
- side in pixels = `ZSIZE × 1.318 × max(W, H)` (median factor; stdev 0.26, so ±20% on drawn size —
  positions are far more accurate than sizes).

Reconstructed boxes reach a **median IoU of 0.633, 99% above 0.3** against 1,751 ground-truth pairs.

### 7.3 Face geometry — writing

Inverting §7.2 to go from an MWG region (`XMP-mwg-rs:RegionInfo`, whose `Area` X/Y are the **centre**
and W/H the extent, all normalised) to `ZDETECTEDFACE`:

| EXIF orientation | (X, Y) → (ZCENTERX, ZCENTERY) |
|---|---|
| 1 | `x = X`,  `y = 1 - Y` |
| 3 | `x = 1 - X`,  `y = Y` |
| 6 | `x = 1 - Y`,  `y = 1 - X` |
| 8 | `x = Y`,  `y = X` |

**The 1.318 factor does not invert.** It describes `ZSIZE` → pixels for *drawing*; using it in reverse
made every written box 1.35× too large. Calibrated against 601 (region, existing face) pairs where
Photos already stored a named face for the same person on the same asset:

```
max(area_w, area_h) / ZSIZE   →   median 1.7728, mean 1.7806, stdev 0.338
```

so

```
ZSIZE = max(area_w, area_h) / 1.7728
```

`max(W, H)` is invariant under rotation, which makes this orientation-independent — no width/height
swap is needed for rotated files.

Validating the *position* inversion the same way gave a median centre error of **0.0075**, with
94.2% under 0.05 — better than the forward fit's own 0.021, and correct for all four orientations.

**Always calibrate against the target library.** The ratio depends on what the region source
considers a "face box" (digiKam, Lightroom and Apple draw different margins). The method: find
assets where a named region in the file and a named `ZDETECTEDFACE` for the same person coexist,
take the median ratio, use that as the divisor.

**Inserting a face** needs, at minimum: `Z_PK` from `Z_PRIMARYKEY` (§3.2), `Z_ENT`, `Z_OPT=1`,
`ZUUID`, `ZASSETFORFACE`, `ZPERSONFORFACE`, `ZCENTERX/Y`, `ZSIZE`, `ZSOURCEWIDTH/HEIGHT`, and the
several dozen non-null integer attribute columns — copy their values from an existing row rather
than inventing them. Values observed on Apple's own rows include `ZGAZEANGLE=1000.0`,
`ZQUALITY=-1.0`, `ZGAZECONFIDENCE=-1.0`, `ZFACEALGORITHMVERSION=15`, `ZASSETVISIBLE=1`,
`ZDETECTIONTYPE=1`. Set `ZMANUAL=1` to mark a face as user-supplied.

**Deduplicate faces by position, not by name.** The same physical face routinely carries variant
spellings across sources and over time — `Amélie Rousseau` / `Amelie Rousseau`,
`Thomas Meier` / `Thomas Meyer`, `Priya` / `Priya Raghunathan`. Keying on the name
inserts one `ZDETECTEDFACE` per spelling on the same face. A tolerance of **0.02** in normalised
units worked well; of 702 unwritten person entries in one run, **510** were variant spellings
correctly suppressed this way.

### 7.4 Photos never reads face data out of files

Face naming comes exclusively from Photos' own detection pipeline. It does not import
`XMP-mwg-rs:RegionInfo` or `XMP-iptcExt:PersonInImage`, at import **or** at repair. Measured at
scale: a library whose originals carried 12,348 face regions and 9,406 `PersonInImage` entries had
**264** named faces in the database — 2.8%, all from Apple's own detection.

Writing face regions into files is therefore for portability to other software, not a route into
Photos. Faces must be inserted directly (§7.3).

---

## 8. Locations

- `ZASSET.ZLATITUDE` / `ZLONGITUDE` — decimal degrees, signed.
  **`-180.0` is the "no location" sentinel**, not NULL. Filtering on `IS NOT NULL` alone gives
  thousands of false positives (8,446 of 21,694 rows in the reference library).
- `ZADDITIONALASSETATTRIBUTES.ZPLACEANNOTATIONDATA` — a **binary plist**, `{'name': str,
  'level': int}`, for a place the user picked by name rather than by coordinate.
  Parse with `plistlib.loads()`.
- `ZASSET.ZLOCATIONDATA` / reverse-geocode caches, and `ZMOMENT.ZAPPROXIMATELATITUDE` /
  `ZLONGITUDE` / `ZLOCALIZEDLOCATIONNAMES` for the moment a photo belongs to.

The database mostly mirrors what the camera recorded: of 13,248 assets with real coordinates, 12,937
already had GPS in the file.

---

## 9. Titles and captions

Two different tables, easily confused:

- **Title** — `ZADDITIONALASSETATTRIBUTES.ZTITLE`. There is **no** `ZASSET.ZTITLE`.
- **Caption / description** — `ZASSETDESCRIPTION.ZLONGDESCRIPTION`, linked via
  `ZASSETDESCRIPTION.ZASSETATTRIBUTES` → `ZADDITIONALASSETATTRIBUTES.Z_PK`.

```sql
SELECT a.ZUUID, aa.ZTITLE, d.ZLONGDESCRIPTION
  FROM ZASSET a
  JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
  LEFT JOIN ZASSETDESCRIPTION d ON d.ZASSETATTRIBUTES = aa.Z_PK;
```

An asset with no caption has **no `ZASSETDESCRIPTION` row at all**, so setting one is an insert, not
an update.

---

## 10. Albums and folders

All album-like objects share `ZGENERICALBUM`, separated by `Z_ENT` and `ZKIND`:

| entity | `ZKIND` | What it is |
|---|---|---|
| Album | 2 | **user-created album** |
| Folder | 4000 | user-created folder |
| Folder | 3999 / 3998 | root containers |
| ImportSession | 1506 | one per import |
| DuplicateAlbum | 1510 | duplicate groups |
| CloudSharedAlbum | 1505 | iCloud Shared Albums |
| FetchingAlbum | 1507, 1552, 1600–1641 | smart/system collections |
| Album | 3571–3573 | `progress-sync`, `progress-ota-restore`, `progress-fs-import` |

So user albums are `Z_ENT = <Album> AND ZKIND = 2`, folders `Z_ENT = <Folder> AND ZKIND = 4000` —
with both entity numbers resolved at runtime (§2.2). Hierarchy via `ZPARENTFOLDER`; ordering via
`ZCUSTOMSORTKEY` / `ZCUSTOMSORTASCENDING`.

```sql
SELECT g.ZTITLE, a.ZUUID
  FROM Z_33ASSETS j                      -- resolve the table name first!
  JOIN ZGENERICALBUM g ON g.Z_PK = j.Z_33ALBUMS
  JOIN ZASSET a        ON a.Z_PK = j.Z_3ASSETS
 WHERE g.Z_ENT = 33 AND g.ZKIND = 2;
```

`Z_FOK_3ASSETS` is Core Data's ordering key within the album. Album titles are **not unique** —
disambiguate by `Z_PK`.

---

## 11. Edits (non-destructive adjustments)

Photos never modifies the original. An edit is a recipe plus a rendered result.

**Database side:**

- `ZASSET.ZADJUSTMENTSSTATE` — `0` = unedited, `2` = edited, `3` = edited (all
  `com.apple.video.slomo` in the reference library). Both 2 and 3 mean "has adjustments".
- `ZASSET.ZADJUSTMENTTIMESTAMP`
- **`ZUNMANAGEDADJUSTMENT`** — linked by `ZASSETATTRIBUTES` → `ZADDITIONALASSETATTRIBUTES.Z_PK`
  (**not** directly to `ZASSET`). Columns: `ZADJUSTMENTFORMATIDENTIFIER`,
  `ZADJUSTMENTFORMATVERSION`, `ZEDITORLOCALIZEDNAME`, `ZADJUSTMENTBASEIMAGEFORMAT`,
  `ZADJUSTMENTRENDERTYPES`, `ZOTHERADJUSTMENTSFINGERPRINT`,
  `ZSIMILARTOORIGINALADJUSTMENTSFINGERPRINT`, `ZUUID`.

Which app made the edit (reference library):

| `ZADJUSTMENTFORMATIDENTIFIER` | `ZEDITORLOCALIZEDNAME` | n |
|---|---|---|
| `com.apple.photo` | `com.apple.mobileslideshow` | 604 |
| `com.apple.photo` | `com.apple.camera` | 328 |
| `com.apple.photo` | `com.apple.Photos` | 219 |
| `com.apple.photo` | `com.apple.photos` | 188 |
| `com.niksoftware.snapseedforipad` | (same) | 176 |
| `com.apple.video.slomo` | — | 22 |
| various `com.pixelmatorteam.*` | Pixelmator / Photomator | ~91 |

The editor field is a bundle id, and Apple's own edits appear under four different capitalisations —
normalise before grouping.

**Disk side** — `resources/renders/<first-hex-char-of-UUID>/`:

```
0537ED8F-BDCA-4873-B6E3-AC1B207FC859.plist          ← the adjustment recipe
0537ED8F-BDCA-4873-B6E3-AC1B207FC859_1_201_a.heic   ← the rendered edited image
```

The `.plist` name is exactly the asset UUID. The `_1_201_a` suffix was **not** determined; the same
style appears in `resources/derivatives/masters/` as `_4_5005_c.jpeg`. Treat as opaque.

**This is why a re-import loses edits**: the crop and colour work lives in these plists and in
`ZUNMANAGEDADJUSTMENT`, not in the original file and not in the rendered JPEG's metadata.

---

## 12. Resources: what exists for each asset

`ZINTERNALRESOURCE` is the inventory of every file backing an asset (154,696 rows in the reference
library).

- `ZASSET` → the asset
- `ZRESOURCETYPE` — 0 dominates (originals and derivatives); 1, 3, 4, 13, 14 also occur
- `ZDATASTORESUBTYPE`, `ZRECIPEID`, `ZDATASTORECLASSID` — variant discriminators; numeric meanings
  **not** established
- `ZLOCALAVAILABILITY` — `1` = the file is on this Mac, `-1` = it is not
- `ZREMOTEAVAILABILITY` — `1` = present in iCloud
- `ZFINGERPRINT` — content hash (§13)
- `ZDATALENGTH`, `ZUNORIENTEDWIDTH/HEIGHT`, `ZCOMPACTUTI`, `ZCODECFOURCHARCODENAME`

To find out whether an asset's original is actually downloaded, use `ZLOCALAVAILABILITY` on the
resource rows rather than trusting a path in `ZASSET`. A library with a populated database and an
empty-looking `originals/` is the "Optimise Mac Storage" case, not a corrupt library.

---

## 13. iCloud sync model

This determines what you may and may not safely do to a library's files.

- Every asset carries `ZCLOUDASSETGUID`; `ZASSET.ZCLOUDLOCALSTATE` is `1` for own assets that are
  synced and `0` otherwise. A library detached from iCloud reads `0` across the board — check this
  before assuming the hazard below applies.
- **The original is immutable.** Edits sync as adjustment data plus a render; a changed original is
  never re-uploaded.
- Sync is **content-addressed**, by:
  - `ZINTERNALRESOURCE.ZFINGERPRINT` — e.g. `AcAS5z1AlkX3SFGyOEVHT/Qn9Eu0`, a base64 21-byte value
    with a leading `0x01` version byte. Apple-proprietary; **not** MD5/SHA, and not reproducible.
  - `ZADDITIONALASSETATTRIBUTES.ZORIGINALSTABLEHASH` and `ZORIGINALHASH`.
- There is **no "please re-upload this file" flag**. No table matches `%upload%`, `%queue%`,
  `%pending%`, `%cloudsync%`; `ZCLOUDRESOURCE` has 0 rows; `ZBACKGROUNDJOBWORKITEM` carries analysis
  jobs only.

**Practical rule:** if you modify files in `originals/` of a library whose database says they are
synced, do not then enable iCloud on it. The stored fingerprints no longer match the bytes, and
Photos' recovery path is to fetch the cloud copy — silently overwriting your changes.

---

## 14. What Photos reads from files, and when

Established by writing metadata into originals and then measuring the database before and after a
full **Repair Library** (Option-Command at launch), on a library where the files carried 33,715
keyword values and 9,206 person names the database did not have:

| what the repair changed | |
|---|---|
| keywords | **+0 / −0** |
| named faces | **+0 / −0** |
| album memberships | **+0 / −0** |
| titles, captions, coordinates | **0 assets** |
| assets removed for missing originals | **0** |

**A repair does not re-read embedded metadata. Photos reads it at import only.**

The corollary is visible in a library's steady state. Comparing file metadata against the database
for the same assets:

| field | in file | also in DB |
|---|---:|---:|
| title | 254 | 253 (99.6%) |
| description | 694 | 653 (94.1%) |
| GPS | 13,213 | 13,151 (99.5%) |
| keyword | 8,378 | 43.1% |
| person | 4,723 | 2.8% |

Title, caption and GPS were present *at import* and were absorbed. Keywords and face regions added to
the files afterwards — by Lightroom, digiKam or ExifTool — were never picked up, because Photos never
looked again.

So the write-back path for anything added after import is direct database insertion (§3), and a
repair is not a substitute.

---

## 15. Writing metadata into originals

### 15.1 Preserving file dates

- `exiftool -overwrite_original_in_place -P` — keeps the **inode**.
- Plain `-overwrite_original` writes a temp file and renames it over the original: **new inode, new
  birth time**.
- Birth-time behaviour is **tool- and path-dependent, so measure rather than assume.** In a
  controlled ExifTool test, `-P -overwrite_original_in_place` truncated *both* mtime and birth time
  to a whole second (`1786345249.977339` → `1786345249.000000`). In a 22,499-file digiKam run over
  the same library, birth time survived intact on all 20,235 files written in place, and was reset
  only on the 2,258 that were replaced (new inode).
- Restore both exactly:
  - mtime: `os.utime(path, ns=(atime_ns, mtime_ns))`
  - birth time: `setattrlist(2)` with `ATTR_CMN_CRTIME` (`0x200`) and a `timespec`. There is no
    stdlib call for this, and `SetFile -d` is only second-accurate.
- **Record `st_birthtime_ns`, not `st_birthtime`.** The float cannot round-trip to the nanosecond, so
  a float-based restore never converges — it reports the same files as needing repair on every run.
  With the integer, a second pass correctly reports zero.
- Guard restores on the **inode**: if it changed, the file was replaced, and a date could be restored
  onto content you did not verify. Offer an explicit override rather than doing it silently.

### 15.2 APFS clones make backups free — but only until you write

```sh
cp -Rc "X.photoslibrary/originals" "originals-clone-backup"
```

77 GB / 22,500 files in **10–22 seconds**, with no measurable change in free space. Blocks are shared
until a file is modified, and an in-place write correctly breaks the sharing.

**The catch:** every write breaks sharing for that file, so during a full metadata pass the clone
grows toward the entire size of `originals/`. On a volume with 26 GiB free and 77 GiB of originals,
the clone would exhaust the disk mid-run. A same-volume clone is an excellent *pre-flight* snapshot
and a poor *during-the-run* backup; for the latter, use another disk.

### 15.3 Format capabilities

| Format | EXIF | IPTC IIM | XMP | Notes |
|---|---|---|---|---|
| JPEG, TIFF, DNG | yes | yes | yes | |
| HEIC | yes | **no** | yes | IPTC IIM is a JPEG/TIFF construct |
| PNG, GIF, WebP | no | no | yes | |
| MOV, MP4, M4V | no | no | yes | XMP goes into a QuickTime atom |

Two container changes are unavoidable and benign: GIF must become **89a** to carry XMP, and WebP must
become **Extended WEBP** (VP8X).

### 15.4 Gotchas that cost real time

- **Never use `-fast` / `-fast2` for "does this file already have X?" checks.** It stops before
  trailing metadata. Measured on 291 sampled files: it hid `Keys:GPSCoordinates` on **57** of them.
- **ExifTool prints its summary (`N image files updated`) on stderr, not stdout.** Parsing only
  stdout makes every write look like a failure.
- **`RegionInfo` is replaced wholesale, never appended.** Writing with `+=` grafts fields onto an
  unrelated existing box. Read the current struct, merge, write the complete value. Nested JSON
  structs work with `-json=file.json`; Apple's `XMP-apple-fi` extensions (FaceID, ConfidenceLevel,
  AngleInfoRoll/Yaw) round-trip byte-exact.
- **ExifTool emits placeholder *text* for binary values**, literally
  `(Binary data 0 bytes, use -b option to extract)`. Ingested naively this becomes a real keyword or
  caption. Filter it, or the string ends up as a `ZKEYWORD` row.
- **Extension ≠ content.** In the reference library 82 files' real type differs from their extension
  (PNG data named `.jpeg`, MP4 named `.mov`, M4V named `.mp4`) — a consequence of the import renaming
  in §4.1. ExifTool refuses to write ("Not a valid JPEG (looks more like a PNG)"). Work around it by
  copying to a correctly-named temp file, writing there, then writing the bytes back with
  `open(path,'r+b')` + `truncate()` so the inode and dates survive.
- **EXIF vs XMP GPS sign.** EXIF stores magnitude plus `GPSLongitudeRef` = `W`; XMP-exif stores a
  signed value. Compare by fully qualified group, or use `Composite:GPSLatitude` /
  `Composite:GPSLongitude`, which ExifTool already resolves to signed decimals.
- **ExifTool's family-1 group for EXIF GPS is `GPS`, not `EXIF`.** You write `EXIF:GPSLatitude` and
  read it back as `GPS:GPSLatitude`.

---

## 16. Text encoding — NFC vs NFD

Person and keyword values occur in **both Unicode normalisation forms** in real libraries. macOS
filesystems hand out NFD; most XMP writers emit NFC. Measured in one library: `Géraldine Fontaine` stored
both as `47 65 CC 81 …` (e + combining acute) and `47 C3 A9 …` (precomposed); 13 logical values
appeared in both forms and 30 more were NFD-only.

Comparing raw bytes produced roughly **1,200 phantom "missing" keywords, 1,100 phantom missing
people, and reported 73 missing titles when only 1 actually was**.

**Normalise to NFC for every comparison; store the raw value.** In SQLite, register a function so the
normalisation is available inside queries:

```python
con.create_function("nfc", 1, lambda s: unicodedata.normalize("NFC", s) if isinstance(s, str) else s,
                    deterministic=True)
```

This matters doubly when writing: without it, a sync creates duplicate `ZKEYWORD` rows and duplicate
`ZPERSON` clusters that differ only by byte encoding.

---

## 17. Access: what macOS blocks

Photos libraries **in the home directory** are unreadable without Full Disk Access —
`Operation not permitted`, from TCC rather than any sandbox. Measured probes:

| probe | result |
|---|---|
| the System Photo Library in `~/Pictures` | blocked |
| a *different*, non-system library in `~/Pictures` | blocked |
| a directory named `*.photoslibrary` created by hand in `~/Pictures` | readable |
| a genuine library bundle on an external volume | **readable** |

So neither the bundle extension alone nor the location alone is the trigger: it is a genuine library
bundle **in a protected location**. Being the System Photo Library is *not* the discriminator — any
real library under the home directory is protected.

Practical consequence: working on a copy on an external volume needs no permission grant at all,
which is also the safest place to experiment. Moving that copy back under `~/Pictures` re-arms the
restriction.

---

## 18. Behaviour of third-party taggers (measured with digiKam)

Useful as a model for what any external tagger does to a library.

- **Tags land in six places at once**: `XMP-digiKam:TagsList`, `XMP-mediapro:CatalogSets`,
  `XMP-microsoft:LastKeywordXMP`, `XMP-acdsee:Categories`, `XMP-dc:Subject` and `IPTC:Keywords`.
  Only the last two are candidates for Photos to read.
- **Hierarchical tags** (`Animals/dog`) go to `XMP-lr:HierarchicalSubject` and
  `XMP-digiKam:TagsList`. Photos has no tag hierarchy — only the leaf can be represented.
- **Face regions are replaced, not merged** (§15.4). Measured over 22,499 files: regions fell from
  12,348 to 10,075 while *named* regions rose from 9,506 to 9,979 — a net gain that concealed
  **391 named (person, file) pairs destroyed**, 89 files losing every named region they had. Always
  snapshot before, and diff by (person, file) pair rather than by totals.
- **Deletions go to `originals/.dtrash/`**, inside the library bundle (§1).
- **Timestamp settings are honoured**: with "update file timestamp when modified" enabled, exactly
  the modified files (11,120 of 22,499) had mtime pushed forward; the untouched 9,115 kept theirs.
- **Cosmetic-looking metadata churn is mostly harmless but must be classified, not assumed.**
  In one run, 8,105 files showed date changes — almost all `IPTC:TimeCreated` gaining a timezone
  suffix (`19:12:36` → `19:12:36+00:00`), with actual capture time altered on only 7 files. GPS
  changed on 1,368 files, but latitude/longitude deltas were **exactly 0.0** and only altitude moved,
  by ~1e-6 (rational re-encoding). Compute the deltas; do not judge by the count of changed files.
  Note the timezone suffix asserts UTC, which is wrong for a local-time value — harmless for Photos,
  which keys on EXIF `DateTimeOriginal` + `OffsetTimeOriginal`, but not for every consumer.

---

## 19. The search index (`database/search/`)

Photos keeps its own search index beside the Core Data store, and it is worth knowing about because
it answers a question the main database cannot: *has Photos actually noticed this metadata yet?*

```
database/search/
├── psi.sqlite (+ -wal, -shm)   the searchable term index — a plain SQLite file
├── Spotlight/                  a Core Spotlight index (over 1 GB in the reference library)
└── initialSuggestions.bplist
```

`psi.sqlite` is readable with ordinary `sqlite3`. Its useful tables:

| table | contents |
|---|---|
| `assets` | one row per indexed asset: `uuid_0`, `uuid_1` (the asset UUID split into two 64-bit integers) and `creationDate` |
| `groups` | the searchable terms: `category`, `content_string`, `normalized_string`, `lookup_identifier`, `score` |
| `prefix`, `prefix_data`, `prefix_idx`, … | prefix-matching support tables |

**Every string in this database is NUL-terminated.** `content_string` for the keyword *chaise* is
stored as `'chaise\x00'`. Exact-match comparisons therefore fail silently while `LIKE '%chaise%'`
appears to work, which makes for a very convincing wrong answer — in our case a script initially
reported all 1,060 keywords as unindexed when in fact 1,053 were present. Always `rstrip('\x00')`
before comparing.

The `category` column separates sources of search terms. Two matter for anyone importing metadata:

| category | meaning | example |
|---|---|---|
| 1200 | **user keywords** — the contents of `ZKEYWORD` | `chaise`, empty `lookup_identifier` |
| 1500 / 1501 | **Apple's own scene recognition**, singular and plural forms | `Chaise` / `Chaises`, `lookup_identifier = scene/334` |

The distinction matters when you test whether an import worked: searching Photos for a word can hit
Apple's scene classifier rather than your keyword, and the two are easy to confuse because they often
use the same word. Query `psi.sqlite` directly to tell them apart.

**The Photos search box is cross-source, so raw hit counts prove very little.** A single query is
matched against user keywords, Apple's scene and object labels, text recognised in images (OCR),
filenames, place names, album titles and person names all at once. Searching a library for two
translations of the same tag returned 3 results for one and 77 for the other — a difference explained
by the other sources, not by the tags.

The UI can narrow this: alongside the general search, Photos offers to scope a query to a single kind
of metadata (Keywords, Places, People and so on). Two caveats make it unreliable as a verification
tool for a library that is still catching up:

- the scoped choices are **not all offered until indexing is far enough along**, so the option you
  want may simply be absent
- results are **incomplete while indexing is in progress**, so a low count can mean "not indexed yet"
  rather than "not present"

For an unambiguous answer, query `psi.sqlite` directly and count rows in `groups` at
`category = 1200` (§19 above), or read `ZKEYWORD` and its join table for ground truth independent of
the index altogether.

**Indexing is triggered per asset by an edit.** Editing any asset — adding a keyword, for instance —
enqueues a job picked up by `PLBackgroundJobHighPrioritySearchIndexingWorker` with flags `asset-core`.
The log shows the whole cycle for a single asset in about 50 ms:

```
PLBackgroundJobHighPrioritySearchIndexingWorker found search jobs needing processing with flags asset-core
Coalesced batch of 1 Asset searchable objects (EFCE5FE1-…)
[MADEmbeddingStoreService] Performing in-process embedding fetching for 1 assets
keyword for asset: <private>            ← one line per keyword on the asset
Donating 1 items to Spotlight for bundleID: com.apple.Photos
```

Counting those `keyword for asset` lines is a practical way to see exactly what Photos believes an
asset's keywords are, independently of what the UI displays.

**Keyword indexing runs independently of the analysis queue.** Measured on the reference library
while 48,330 `ZBACKGROUNDJOBWORKITEM` rows were still pending: 21,162 of 21,694 untrashed assets were
already present in `assets`, and 1,053 of 1,060 `ZKEYWORD` titles already had a category-1200 entry —
including 460 of the 465 keywords that had been inserted directly into the database a few hours
earlier. Keywords written by direct insertion (§3) do get indexed, and quickly.

To check coverage for your own library:

```python
nfc = lambda s: unicodedata.normalize("NFC", (s or "").rstrip("\x00"))
titles = {nfc(t) for (t,) in db.execute("SELECT ZTITLE FROM ZKEYWORD WHERE ZTITLE IS NOT NULL")}
indexed = {nfc(s) for (s,) in psi.execute("SELECT content_string FROM groups WHERE category=1200")}
print(sorted(titles - indexed))          # keywords Photos cannot yet search for
```

A related dead end worth documenting so nobody repeats it: there is **no way to tell which assets a
batch of background jobs covered**. `ZASSETANALYSISSTATE` exists but was empty (0 rows) in the
reference library, and `ZBACKGROUNDJOBWORKITEM` holds only `ZJOBTYPE`, `ZJOBFLAGS`, `ZIDENTIFIER` and
timestamps — no asset reference. Progress can be measured in aggregate (queue depth, index size, row
counts in `assets`) but not attributed to particular photos.

---

## 20. Diagnosing with the unified log

Some behaviour of the Photos daemons is visible nowhere else — not in the database, not in `lsof`,
not in file timestamps. The scheduler decision in §2.5 is the clearest example: nothing on disk
records *why* analysis is not progressing.

The Console app and the `log` command-line tool read the same unified log; the CLI is easier to
filter and aggregate. One trap on the way in: **`log` alone hits a zsh builtin** and fails with
`too many arguments`. Use the absolute path.

```sh
# what a daemon has been doing
/usr/bin/log show --last 15m --predicate 'process == "photoanalysisd"' --style compact

# scheduler verdicts for photo analysis
/usr/bin/log show --last 2h --predicate 'eventMessage CONTAINS "mediaanalysisd.photos"' --style compact \
  | grep -oE "Decision: [A-Z]+" | sort | uniq -c

# which policy is blocking, and how heavily
/usr/bin/log show --last 2h --predicate 'eventMessage CONTAINS "mediaanalysisd.photos"' --style compact \
  | grep -oE "name: [A-Za-z ]+Policy, policyWeight: [0-9.]+"

# live tail
/usr/bin/log stream --predicate 'process == "mediaanalysisd"' --style compact
```

Processes worth watching: `photoanalysisd`, `mediaanalysisd`, `photolibraryd`, `cloudphotod`,
`managedcomputekitservice`, and `dasd` / `duetexpertd` for scheduling decisions.

Two cautions learned the hard way:

- **Do not count every "cancel" as a failure.** Grepping for `cancel` catches
  `xpc_connection_cancel()`, which is routine XPC teardown. Match on the specific text — `Scene asset
  processing cancelled`, `Face asset processing cancelled`, `userCanceledErr` — before concluding
  anything is wrong.
- **A few `success: NO` lines are normal.** In one two-hour window PhotoKit writes succeeded 216
  times and failed 6; the failures were transient. Compare against the success count rather than
  reacting to the first error.

Sandbox denials naming unrelated paths (for instance `duetexpertd` being denied an xattr read
somewhere in the home directory) are background noise from other subsystems and are not evidence of a
Photos problem.

### 20.1 Error-level messages that are actually harmless

The unified log labels a great deal of routine chatter as `error` (`erreur` in a localised Console).
The following were all present in a library that was working perfectly, and chasing them wastes time:

| message | what it is |
|---|---|
| `Missing prefetched properties for … Fetching on demand on the main queue, which may degrade performance` | a Photos performance hint, not a failure (49 occurrences in one short session) |
| `Info Panel Syndication: Unexpected UI case where there's nothing to show but the section is still visible` | a cosmetic Apple UI bug in the Info panel |
| `On demand analysis failed … Code=-128` | `userCanceledErr` — normal cancellation (§2.5) |
| `Warning: -[NSWindow makeKeyWindow] … returned NO from -[NSWindow canBecomeKeyWindow]` | AppKit noise |
| `RBSStateCapture remove item called for untracked item …` from `runningboardd` | third-party Finder extensions being torn down; the message often names them (cloud-sync clients, antivirus) |
| `errors encountered while discovering extensions: … query cancelled` | plug-in discovery being cancelled |

### 20.2 Diagnosing a library that will not save

If edits made in the Photos UI silently fail — typed text disappearing when you press Enter, a new
keyword rendering greyed-out and then being discarded — the log will name the cause, and the database
will not. Search for the save path:

```sh
/usr/bin/log show --last 10m --predicate 'process IN {"Photos","photolibraryd"}' --style compact \
  | grep -iE "save failed|constraint|PHPhotosErrorDomain|dispatchRequest"
```

A `Persitent History - Transaction constraint primary key violation` (Apple's own spelling) means a
primary-key counter is behind its table's highest row — almost always the result of an external write
that did not maintain `Z_PRIMARYKEY.Z_MAX`. See §3.2 for the cause and the repair. Note that
`PRAGMA integrity_check` returns `ok` in this state, so it cannot be used to rule the problem out.

---

## 21. Things that are easy to assume wrongly

1. `Z_ENT` numbers are **not** stable across a repair, even with an unchanged `LibrarySchemaVersion`.
2. `ZASSET.ZORIENTATION` is not always the file's EXIF orientation.
3. `ZLATITUDE` uses `-180` rather than NULL for "no location".
4. There is no `ZASSET.ZTITLE`; the title is on `ZADDITIONALASSETATTRIBUTES`.
5. Join-table column numbers are schema- and migration-specific.
6. Album titles are not unique.
7. Face coordinates need a per-orientation axis flip and a `max(W,H)` size base — and the read-side
   size factor does not invert.
8. Shared-album assets have a `ZDIRECTORY` value but no file on disk.
9. `ZUNMANAGEDADJUSTMENT` links through `ZADDITIONALASSETATTRIBUTES`, not `ZASSET`.
10. Photos exports neither hidden nor trashed assets.
11. A repair does not re-read file metadata, and does not remove assets whose originals are gone.
12. Correct rows are not enough for the UI to show them — persistent history is part of the contract
    (§3.4).
13. Plain `sqlite3` cannot execute the triggers in `Photos.sqlite` without the `NSCoreData*`
    functions.
14. A secondary library gets no background indexing; nothing happens unless Photos is open on it —
    unless it is promoted to System Photo Library, which is a separate switch from iCloud.
15. Even a promoted library will not finish analysing while the Mac is in use; the scheduler says so
    only in the unified log (§2.5, §20).
16. Keyword search indexing is *not* gated on the analysis queue — a large pending queue does not
    mean keywords are unsearchable (§19).
17. Strings in `psi.sqlite` are NUL-terminated, so exact comparisons against them fail silently.
18. `Z_PRIMARYKEY` tracks Core Data's internal history tables (`CHANGE`, `TRANSACTION`,
    `TRANSACTIONSTRING`) as well as the model entities — and a primary key allocated past `Z_MAX`
    breaks every save in the library, not just the rows you wrote (§3.2).
19. `PRAGMA integrity_check` returning `ok` does **not** mean Photos can still write to the store
    (§20.2).
20. Photos' search box spans keywords, scene labels, OCR text, filenames, places and people at once,
    so the number of search hits says nothing about whether a specific keyword was indexed. Scoping a
    search to one metadata type helps, but the scoped options are incomplete — and their results
    partial — until indexing has progressed far enough (§19).
21. Most `error`-level lines in the unified log are harmless (§20.1).

---

## 22. Explicitly not established

Stated so nothing here is mistaken for verified fact:

- The `ZCOLUMNS` bitmap layout in `ACHANGE`, and therefore whether persistent-history rows can be
  synthesised safely (§3.4).
- What makes the Info panel show directly-inserted keywords. The fault is now localised to the
  Photos.app UI layer (§3.4): `photolibraryd` reads such keywords correctly at the same moment the
  panel shows none. `Z_OPT` increments and synthesised persistent history were both tried and had no
  effect. The `Missing prefetched properties` message is the only lead, and the panel's own fetch is
  not observable from outside the process.
- The meaning of `ATRANSACTION.ZQUERYGEN`. It was left NULL in the synthesised rows rather than
  forged.
- Why faces inserted directly appear in the People UI immediately while keywords do not. Both are
  read correctly from the store; only the keyword relationship fails to reach the Info panel.
- Valid non-zero values of `ZASSET.ZSEARCHINDEXREBUILDSTATE`, and whether setting it triggers a
  reindex. Every row read `0` in the reference library; setting it to `1` on a sample of 20 assets
  produced no observable effect.
- The full schema of `database/search/psi.sqlite`. Its `assets` and `groups` tables and the
  category numbers 1200 / 1500 / 1501 are now documented (§19), but the remaining categories, the
  `score` and `token_ranges_*` columns, and the format of the Core Spotlight index beside it are not.
- How `uuid_0` / `uuid_1` in `psi.sqlite.assets` map back to an asset UUID string. They are evidently
  the UUID split into two 64-bit integers, but the byte order was not verified, so joining the search
  index back to `ZASSET` by UUID was not attempted.
- The meaning of the `_1_201_a` / `_4_5005_c` suffixes on render and derivative filenames.
- The numeric semantics of `ZINTERNALRESOURCE.ZDATASTORESUBTYPE`, `ZRECIPEID`, `ZDATASTORECLASSID`.
- The meaning of `ZPERSON.ZVERIFIEDTYPE` values `1` vs `-2`.
- Why `ZASSET.ZORIENTATION` disagrees with EXIF on 644 photos.
- The internal structure of the adjustment `.plist` recipes in `resources/renders/`.
- The face-region axis mapping for EXIF orientation 2 (mirrored) — too few samples to fit.
- Whether populating `ZDETECTEDFACEPRINT` / `ZFACECROP` / `ZDETECTEDFACEGROUP` is required for
  inserted faces to behave fully like Apple's own. What *is* established is that they survive
  Apple's own analysis: after promoting a library to System Photo Library and letting
  `photoanalysisd` run over it, 9,440 directly-inserted faces changed by a net -1 (two duplicate
  rows for one person removed), with no person re-clustered and no keyword lost.
- `ZASSETDESCRIPTION` vs `ZADDITIONALASSETATTRIBUTES.ZACCESSIBILITYDESCRIPTION` — only the former was
  used and verified.
