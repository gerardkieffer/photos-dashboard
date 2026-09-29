/* Explanations for the technical terms on the dashboard.
 *
 * Attached declaratively: any element carrying data-help="key" gets an ⓘ that
 * reveals the matching entry. Keeping the prose here rather than in index.html
 * keeps the markup readable and makes it obvious what is documented.
 *
 * Ground rule, inherited from APPLE-PHOTOS-LIBRARY-INTERNALS.md: say what was
 * measured, and say plainly when something is not established. Several of the
 * numbers Photos stores have no published meaning, and a confident invented
 * label would be worse than an honest "unknown".
 */

const GLOSSARY = {
  queue: {
    title: 'The background job queue',
    body: `<p><code>ZBACKGROUNDJOBWORKITEM</code> is Photos' to-do list. Every row is one
      unit of deferred work: index this photo for search, render a thumbnail, look for
      duplicates, upload a resource. Photos adds rows as things change and removes them
      as they are done.</p>
      <p>A large number is not by itself a problem. A freshly imported or newly promoted
      library can legitimately queue hundreds of thousands of items. What matters is
      whether the number is <em>falling</em>, and whether anything is allowed to run —
      see the scheduler panel.</p>`,
  },
  jobtype: {
    title: 'ZJOBTYPE and ZJOBFLAGS',
    body: `<p>Two integers Photos uses to route each work item to the right worker.
      Apple publishes no mapping for them, and this dashboard will not invent one.</p>
      <p><b>What is established here, from your own data:</b></p>
      <ul>
        <li><b>Type 11</b> is cascade donation — every row of this type carries the
          literal identifier <code>PLCascadeDonationRequired</code>, which names
          <code>PLBackgroundJobCascadeDonationWorker</code>. Donation is how Photos
          hands content to Spotlight.</li>
        <li><b>Type 2</b> covers both per-asset work and collection-scoped work: some of
          its identifiers are JSON objects like
          <code>{"type":1700,"label":"Schueberfouer"}</code>, naming a place, event or
          category rather than a photo. Those inner <code>type</code> numbers are
          <em>not</em> album kinds — only two of the eight seen exist in
          <code>ZGENERICALBUM.ZKIND</code>, and those albums have no title — so they are
          a separate taxonomy whose meaning is not established.</li>
      </ul>
      <p><b>ZJOBFLAGS is a bitmask.</b> Every value observed decomposes cleanly into
      powers of two (3 = 1|2, 14 = 2|4|8, 1026 = 2|1024), so the dashboard shows the
      individual bits. What each bit means is not established.</p>`,
  },
  workers: {
    title: 'What the background workers actually do',
    body: `<p>The queue is drained by a set of named workers. These names are read from
      the Objective-C runtime of Apple's own <code>PhotoLibraryServices</code> framework,
      so they are real — but which worker a given <code>ZJOBTYPE</code> number routes to
      is <em>not</em> established, so the list below is a catalogue of the work Photos
      does, not a decoder for the numbers in the table.</p>`,
  },
  attributable: {
    title: 'Attributable to an asset',
    body: `<p>The share of rows in this bucket whose <code>ZIDENTIFIER</code> is an asset
      UUID, so the job can be traced to a specific photo.</p>
      <p>This is worth flagging because the reference documentation states that this
      table carries "no asset reference". That is true of its <em>columns</em> — there is
      no foreign key to <code>ZASSET</code> — but in practice most identifiers are asset
      UUIDs that join straight to <code>ZASSET.ZUUID</code>. The rest are literal job
      names or JSON blobs.</p>`,
  },
  ageprofile: {
    title: 'Age profile',
    body: `<p>How long the items in this bucket have been waiting, from
      <code>ZTIMESTAMP</code>. A queue full of items older than a week means work is
      being enqueued but never run — usually because the scheduler is refusing it, or
      because nothing holds the library.</p>`,
  },
  derived: {
    title: 'Why "completed" is derived, not counted',
    body: `<p>Photos keeps no record of finished jobs: rows are deleted when done, and
      there is no completion log, no timestamp, no tombstone. So a completion rate has
      to be inferred.</p>
      <p>Between two samples, <code>pending_now = pending_prev + arrivals − departures</code>.
      Arrivals are countable (rows whose <code>ZTIMESTAMP</code> falls in the interval),
      so departures follow. It is a good estimate, not a counter Photos maintains, and it
      is labelled that way wherever it appears.</p>
      <p>For work that is genuinely, verifiably finished, use the per-worker analysis
      coverage panel instead.</p>`,
  },
  eta: {
    title: 'Drain ETA',
    body: `<p>Pending items divided by the current net drain rate. It assumes the present
      pace continues, which it very often will not: analysis is throttled while the Mac is
      in use, and finishing one job frequently enqueues others.</p>
      <p>Treat it as "is this hours or weeks", not as a delivery date.</p>`,
  },
  scheduler: {
    title: 'The scheduler verdicts',
    body: `<p>Whether Photos' heavy analysis is <em>allowed</em> to run is decided by the
      Duet Activity Scheduler, and that decision is recorded nowhere on disk — only in the
      unified log. This panel reads it with <code>log show</code>.</p>
      <ul>
        <li><b>CP</b> — Can Proceed. Work is permitted.</li>
        <li><b>MNP</b> — May Not Proceed. Refused for now.</li>
        <li><b>AMNP</b> — Absolutely May Not Proceed.</li>
      </ul>
      <p>The policy that usually blocks it is <b>Device Activity</b>: it will not run the
      work while somebody is at the keyboard. To let a large library finish, leave the Mac
      idle, on mains power, and prevent sleep. Measured throughput while the machine was in
      use was roughly 2–7 items per minute.</p>
      <p>No verdicts logged is normal when there is no analysis work in flight.</p>`,
  },
  holders: {
    title: 'Processes holding the library',
    body: `<p>A Photos library only gets background processing while a daemon has it open.
      A <em>secondary</em> library gets none at all unless Photos.app is open on it; the
      System Photo Library is held permanently.</p>
      <p>Checking only <code>photolibraryd</code> is misleading — most of the work is done
      by <code>photoanalysisd</code>, <code>mediaanalysisd</code> and
      <code>managedcomputekitservice</code>.</p>
      <p>Note that work started by Photos.app is <em>cancelled</em> when Photos quits, so
      repeatedly opening and closing the app makes no net progress.</p>`,
  },
  searchindex: {
    title: 'The search index (psi.sqlite)',
    body: `<p>Photos keeps its own search index beside the main database. It answers a
      question the database cannot: has Photos actually <em>noticed</em> this metadata yet?</p>
      <p>Importantly, keyword indexing runs <b>independently of the analysis queue</b>. A
      backlog of hundreds of thousands of pending jobs does not mean your keywords are
      unsearchable — measured on a real library, keywords were essentially fully indexed
      while 48,330 analysis jobs were still queued.</p>`,
  },
  keywordsearch: {
    title: 'Keywords searchable',
    body: `<p>How many of your keyword titles have an entry in the search index at
      category 1200 (user keywords), as opposed to Apple's own scene labels at 1500/1501,
      which often use the same words.</p>
      <p>Two traps are handled here: strings in the index are NUL-terminated, and text
      occurs in both Unicode normalisation forms. Comparing raw bytes reports over a
      thousand phantom "missing" keywords.</p>`,
  },
  categories: {
    title: 'Search term categories',
    body: `<p>The index groups search terms by source. Only three category numbers have
      established meanings: <b>1200</b> is your own keywords, <b>1500</b> and <b>1501</b>
      are Apple's scene recognition in singular and plural form. The rest are unlabelled
      here because their meanings are not established.</p>
      <p>This distinction matters when testing whether an import worked: searching Photos
      for a word can hit Apple's scene classifier rather than your keyword.</p>`,
  },
  coverage: {
    title: 'Per-worker analysis coverage',
    body: `<p>This is the closest thing to a genuine "work finished" record the library
      holds.</p>
      <p><code>ZASSETANALYSISSTATE</code>, the table that sounds like it should answer
      this, is empty in every library measured. But
      <code>ZMEDIAANALYSISASSETATTRIBUTES</code> has one row per asset and one version
      column per analysis worker, left at zero until that worker has processed the asset.
      A version greater than zero is direct evidence of completed work.</p>
      <p>Video workers show counts without percentages because their eligible asset
      population is not established. "No recorded analysis" means no positive versions
      were found; it does not establish whether the worker is applicable or stalled.</p>`,
  },
  pkhealth: {
    title: 'Save integrity — the check integrity_check cannot do',
    body: `<p>Core Data does not use SQLite's AUTOINCREMENT. Every primary key is
      allocated from <code>Z_PRIMARYKEY.Z_MAX + 1</code>, and that counter must be updated
      to match.</p>
      <p>If a table's highest row ever exceeds its counter, the next save Photos attempts
      collides — and because <em>every</em> save also writes a persistent-history
      transaction, this breaks all editing in the entire library, not just the rows that
      were written. Symptoms are silent: typed text vanishes on Enter, new keywords render
      greyed out and are discarded.</p>
      <p>Critically, <code>PRAGMA integrity_check</code> returns <code>ok</code> in this
      state, so it cannot rule the problem out. This comparison is the only check that
      catches it. It is almost always caused by an external tool writing to the library
      without maintaining the counters.</p>`,
  },
  persistenthistory: {
    title: 'Persistent history — every save lands here',
    body: `<p><code>ATRANSACTION</code> gets one row per save, <code>ACHANGE</code> one
      row per changed object. Together they are a live record of everything writing to the
      library, which makes them the best available "what is Photos doing right now" signal.</p>
      <p>For scale: adding a single keyword to a single photo through the UI produced 8
      transaction rows and 21 change rows.</p>`,
  },
  authors: {
    title: 'Which subsystem is writing',
    body: `<p>Each transaction references a string pool entry naming the code path that
      made it — things like <code>PLCloudBatchDownloader handleIncomingBatch:</code> or
      <code>Album-countCoalescer</code>. These names say far more about what Photos is
      actually doing than any counter does.</p>`,
  },
  zent: {
    title: 'Why entity numbers are resolved every time',
    body: `<p><code>Z_ENT</code> maps a row to its Core Data entity. These numbers are
      <b>not stable</b> — measured on one library before and after a Repair, Keyword moved
      from 47 to 52 and Person from 54 to 59, while the reported schema version did not
      change at all.</p>
      <p>So every entity number and join-table name on this dashboard is resolved at read
      time, on the same connection that uses it. Nothing is cached between runs.</p>`,
  },
  assets: {
    title: 'Counting assets',
    body: `<p>The correct filter for "real photos I own" is
      <code>ZBUNDLESCOPE = 0 AND ZTRASHEDSTATE = 0</code>.</p>
      <ul>
        <li><b>Shared-album</b> assets (scope 2) have a directory recorded but no file on
          disk.</li>
        <li><b>Hidden</b> and <b>excluded from grid</b> are independent mechanisms, not two
          names for one thing.</li>
        <li><b>With GPS</b> excludes the <code>-180.0</code> sentinel, which is what Photos
          stores for "no location" instead of NULL. Filtering on NOT NULL alone gives
          thousands of false positives.</li>
      </ul>`,
  },
  faces: {
    title: 'Faces and people',
    body: `<p>Most person rows are unnamed automatic clusters, so a large "person clusters"
      figure next to a small "named people" figure is normal.</p>
      <p>Worth knowing: Photos never reads face regions out of your files. Face naming
      comes exclusively from its own detection pipeline, at import and afterwards — not
      from XMP metadata, and not at repair. A library whose files carried 12,348 face
      regions had 264 named faces in the database.</p>`,
  },
  resources: {
    title: 'Resources and local availability',
    body: `<p><code>ZINTERNALRESOURCE</code> inventories every file backing an asset —
      originals, renders, thumbnails.</p>
      <p><b>Not local</b> means the file is in iCloud but not on this Mac. A populated
      database next to an empty-looking originals folder is the "Optimise Mac Storage"
      case, not a corrupt library.</p>`,
  },
  albums: {
    title: 'Albums, folders and collections',
    body: `<p>Albums, folders, import sessions, shared albums and smart albums all share a
      single table and are told apart by entity number plus a kind code. Album titles are
      <b>not unique</b>, so they are only meaningful together with their key.</p>
      <p><b>Moments</b> are automatic time-and-place groupings; <b>memories</b> are the
      curated slideshows Photos generates.</p>`,
  },
  wal: {
    title: 'WAL and -shm',
    body: `<p>The database uses write-ahead logging. Recent changes live in the
      <code>-wal</code> file until they are folded back into the main database, which is
      why copying <code>Photos.sqlite</code> alone silently loses recent edits.</p>
      <p>The <code>-shm</code> is the WAL index: a derived scratch file with no library
      data in it, which every reader updates. A growing WAL usually just means Photos is
      busy.</p>`,
  },
  access: {
    title: 'How this dashboard reads your library',
    body: `<p>Read-only, enforced three independent ways: the file handle is opened
      <code>mode=ro</code>, the connection runs under <code>PRAGMA query_only</code>, and a
      SQLite authorizer rejects every non-read operation before a statement is even
      compiled.</p>
      <p><b>Direct mode</b> opens the database and its WAL read-only.
      <code>test_readonly.py</code> checks database/WAL contents byte-for-byte on
      disposable WAL-mode copies. SQLite may still update the
      <code>-shm</code> WAL index, which every reader of a WAL database does, Photos
      included.</p>
      <p><b>Snapshot mode</b> (<code>--snapshot-mode</code>) uses SQLite to create consistent database backups, then queries those copies.
      Backup readers can also update source shared-memory files. Full copies require
      disk space; they are not APFS clones. The read transaction can grow the source
      WAL while writers continue. Use an offline library copy for isolation.</p>`,
  },
  tiers: {
    title: 'Why different data is sampled at different rates',
    body: `<p>Queue depth changes every few seconds; the number of photos in your library
      changes over days. Sampling both at one interval would either miss the fast signal or
      waste space on the slow one.</p>
      <p>So each metric belongs to a tier with its own interval, and old samples are
      compacted into coarser buckets that keep minimum and maximum as well as the average —
      so a 30-day chart still shows spikes rather than a flat line.</p>
      <p>History is kept in a SQLite file per library and survives restarts.</p>`,
  },
  appleepoch: {
    title: 'Timestamps',
    body: `<p>Core Data stores time as seconds since 1 January 2001 UTC, not the usual 1970
      epoch, and stores it in UTC rather than local time. Everything shown here is already
      converted to your local time zone.</p>`,
  },
  history: {
    title: 'Stored history',
    body: `<p>Samples are written to a SQLite database outside your photo library, one file
      per library, keyed by the database's inode — two libraries can have very similar
      paths, and mixing their series would draw a cliff from one library's numbers to
      another's.</p>
      <p>The file persists across restarts, so charts keep their history when you stop and
      start the server.</p>`,
  },
};

/* The workers Photos runs to drain the queue. Names come from the Objective-C
 * class list of Apple's PhotoLibraryServices framework — real, but deliberately
 * NOT presented as a ZJOBTYPE decoder, because that mapping is not established. */
const WORKERS = [
  ['Search indexing', 'Makes assets findable in the search box. Runs at three priorities — high, and two low-priority variants for battery and mains power.'],
  ['Cascade donation', 'Hands content to Spotlight so photos surface in system-wide search.'],
  ['Resource availability', 'Works out which files backing an asset are present locally and which are only in iCloud.'],
  ['Deferred render derivatives', 'Generates thumbnails and display-size versions, at high and low priority.'],
  ['Edit rendering', 'Renders the visible result of edits, separately for images and video.'],
  ['Duplicate detector', 'Finds duplicate photos for the Duplicates album.'],
  ['Person sync', 'Keeps people and face clusters consistent.'],
  ['Stable hash', 'Computes content hashes used to match assets across devices.'],
  ['Asset resource upload', 'Uploads originals and renders to iCloud, plus the extension runner that performs it.'],
  ['Shared asset container update', 'Maintains shared albums and their membership.'],
  ['Syndication', 'Handles photos shared into the library from Messages and similar — sync, download, sanitisation and cleanup.'],
  ['Guest asset sync', 'Syncs assets belonging to a shared library.'],
  ['Asset stack', 'Groups related shots, such as bursts, into stacks.'],
  ['Feature availability', 'Determines which Photos features apply to this library.'],
];

/* ---------- attachment ---------- */
function attachHelp(root = document) {
  root.querySelectorAll('[data-help]').forEach(host => {
    if (host.querySelector(':scope > .hint')) return;
    const entry = GLOSSARY[host.dataset.help];
    if (!entry) return;
    const btn = document.createElement('button');
    btn.className = 'hint';
    btn.type = 'button';
    btn.setAttribute('aria-label', `Explain: ${entry.title}`);
    btn.setAttribute('aria-expanded', 'false');
    btn.textContent = 'i';
    const tip = document.createElement('span');
    tip.className = 'tip';
    tip.setAttribute('role', 'tooltip');
    tip.innerHTML = `<b>${entry.title}</b>${entry.body}`;
    btn.appendChild(tip);
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const open = btn.classList.toggle('open');
      btn.setAttribute('aria-expanded', String(open));
      document.querySelectorAll('.hint.open').forEach(o => {
        if (o !== btn) { o.classList.remove('open'); o.setAttribute('aria-expanded', 'false'); }
      });
    });
    host.appendChild(btn);
  });
  if (!attachHelp._bound) {
    attachHelp._bound = true;
    document.addEventListener('click', () => {
      document.querySelectorAll('.hint.open').forEach(o => {
        o.classList.remove('open');
        o.setAttribute('aria-expanded', 'false');
      });
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        document.querySelectorAll('.hint.open').forEach(o => {
          o.classList.remove('open');
          o.setAttribute('aria-expanded', 'false');
        });
      }
    });
  }
}
