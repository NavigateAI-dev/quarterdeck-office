# Running Quarterdeck

How to install, upgrade, back up, restore and roll back Quarterdeck, and
exactly what leaves your Mac. For what each part does, see the
[README](../README.md); for the trust model, [SECURITY.md](../SECURITY.md).

## Install

1. Docker Desktop, and a copy of this repository.
2. `python3 -m scripts.init_local` — writes `.env`, generates the credential
   root key into `.secrets/root.key`, and creates `backups/`.
3. `docker compose up -d --build` — builds and starts every service: `api`,
   `runner`, `sources`, `fetcher`, `browser`, `installer`, `remote` and
   PostgreSQL.
4. `python3 -m scripts.launch` — checks the machine, then opens the office with
   a one-time sign-in code.
5. Follow the setup steps in the browser: your name, a model connection, a first
   project. Keys are entered there and nowhere else.

## Upgrade

```
python3 -u -m scripts.upgrade
```

It stops taking new work, waits for work in flight to reach a step boundary,
takes a backup, rebuilds and restarts `api`, `runner`, `sources`, `fetcher`,
`remote` and `installer` (the database migrates as `api` starts), smoke tests,
and only then takes work again; then it restarts the calendar helper on the
Mac. `-u` prints each step as it happens. The `browser` service is not in that
list; when its code has changed, rebuild it too with
`docker compose up -d --build browser`.
It refuses to continue if the backup did not land, and a failed smoke test fails
the upgrade rather than reporting success — the office is left paused with the
rollback printed.

## Back up and restore

```
docker compose exec -T api python -m scripts.backup        # writes backups/<archive>
python3 -m scripts.restore_drill                           # proves the newest one restores
python3 -m scripts.restore backups/<archive>               # puts it back for real
```

`scripts.restore_drill` is the one to run regularly. It restores the archive
into a throwaway stack with its own volumes and ports, compares every table's
rows (each value, in any order) and every file's digest against the archive,
asks the restored office for its status, and destroys the stack. It builds
this folder's version, so it restores an archive taken at this version's
schema; an archive an upgrade took before migrating belongs to the previous
version (see Rolling back). It never touches the
office you are using. A backup you have never restored is a hope, not a backup.

The archive holds the database and the workspace, not the credential root key.
Keep `.secrets/root.key` somewhere safe and separate; without it a restored
office still runs, but the stored provider keys cannot be read and have to be
entered again.

## Rolling back

An upgrade takes a backup before it migrates. Rolling back restores that
archive into the previous version **beside** the upgraded office, never over
it: a restore refuses a database that holds data, and the upgraded office's
data stays where it is until you remove it yourself.

1. Stop the upgraded office, keeping its data: `docker compose stop` (never
   `down -v`).
2. Put the previous version in a new folder: unpack its release archive, or
   copy its checkout. Copy `.env` and `.secrets/` into it from the current
   folder, and add two lines to its `.env`:

   ```
   COMPOSE_PROJECT_NAME=quarterdeck-rollback
   BACKUP_HOST_DIR=/full/path/to/the/current/folder/backups
   ```

   The project name gives the rolled-back office volumes of its own, so it
   starts empty. From that folder every command below, and later
   `scripts.launch` and `scripts.upgrade`, act on it and not on the upgraded
   office. Keep `.secrets/root.key` the same, or stored provider keys cannot
   be read.
3. In the new folder, create its database at the previous version's schema:

   ```
   docker compose up -d --build postgres
   docker compose run --rm migrate
   ```

4. Restore the archive the upgrade took (the upgrade printed its name,
   `backups/quarterdeck-backup-<time>.tar.gz`), then compare the result with
   the archive:

   ```
   docker compose run --rm --no-deps api python -m scripts.restore /backups/<archive>
   docker compose run --rm --no-deps api python -m scripts.verify_restore /backups/<archive>
   ```

   Every line must say ok: the schema revision, every table's rows (each
   value, not only the count) and every file. `schema_revision_mismatch`
   means this folder is not the version the archive came from.
5. `python3 -m scripts.launch` from the new folder, then unpause the office
   under Settings → Switches.

To go forward again: `docker compose stop` in the new folder, `docker compose
start` in the original. Remove the upgraded office's data (`docker compose
down -v` in the original folder) only once you are sure you will not.

## Where data goes

Everything is local except the calls you ask for.

- **Stays on the machine**: tasks, artifacts, chat history, agents, projects,
  knowledge and its extracted text, settings, the policy log, backups.
- **Leaves the machine**, and only then:
  - model calls, straight to the provider you connected, with its key from the
    office's encrypted store; the agents never hold it.
  - web search, to the search provider whose key you configured.
  - page fetches, to hosts on the allow-list, from a process with no credentials.
  - **a reader service**, when a source refuses the office, carrying the address
    of the page it could not read. Only after a refusal, never for a page it can
    read itself, and `READER_BASE_URL` empty turns it off.
  - Slack messages, to your workspace, for the channels you paired, with the
    answer's file and chart pictures; files you share with the bot are
    downloaded from `files.slack.com` only.
  - **calendars**: .ics and CalDAV addresses you connected, fetched straight
    from the office (never through the page reader); Google's Calendar API and
    Microsoft Graph for calendars you signed in to, including the edits you
    make. The Mac's calendars are read by a helper on the Mac through macOS.
  - **accounts**: Gmail, Google Drive, Outlook and OneDrive, read only, when a
    granted agent calls them.
  - **the office's own browser**, when it is on: the pages a browse step, a
    quick lookup or a hard-to-read page opens, from a container with no
    logins and no route to the office.
  - **builds**: the coding CLI on your Mac (Claude Code, Codex, opencode or
    DeepSeek Harness) calls its own provider with your own sign-in; when you
    allow packages, the npm and PyPI registries, from the `installer` service;
    when you put a website online, Netlify or GitHub with your token.
  - **tool calls**, to the MCP servers you added and granted to a project,
    carrying what the agent chose to send; a server's sign-in goes to the
    vendor's own pages, in your browser.
  - **skill previews**: reading a skill from skills.sh or GitHub asks
    `api.github.com`, `raw.githubusercontent.com` and the skill's skills.sh
    page, with no credential.
  - **balance reads**, to DeepSeek's, OpenRouter's, Moonshot's and
    SiliconFlow's own endpoints, with that connection's key, at most every five
    minutes.
  - engineering work, to the repository and forge you named.
  - **judgments, to TypeSafe**, when a `TYPESAFE_API_KEY` is set. The office asks
    small typed questions while it works — what a chat message is asking for,
    whether a critique still raises the same concerns, whether a claim is
    supported by its evidence, whether a link is worth following, which
    project a Slack question belongs to, and which finished answers bear on a
    new task. These carry the text being judged.
  - **trace projections, to TypeSafe**, and only when you ask for a finished
    task to be evaluated into project memory. One line per lesson and the trace
    fact behind it, capped and stripped of anything shaped like a credential.
    Never automatic: no hook and no completion trigger, and a dry run shows
    exactly what would be sent while sending nothing.
- **Never requested from a third party by the pages themselves**: the interface
  fonts (Archivo, JetBrains Mono, Geist) are bundled with the office, and the office loads no remote
  scripts or stylesheets.
- **Never leaves**: provider keys and other secrets (encrypted at rest, with the
  root key in `.secrets/root.key`, outside every data volume and outside every
  backup), and the contents of `.env`.

## Reporting a security problem

Please do not open a public issue. Report it privately through GitHub's
**Report a vulnerability** button on Navigate AI's Quarterdeck repository
(Security tab). Quarterdeck holds provider keys, so treat a suspected leak as a
key rotation first: revoke the key at the provider, then run
`python3 -m scripts.rotate_secret_key`.
