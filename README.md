# Tundra

A fork of [Permifrost](https://gitlab.com/gitlab-data/permifrost) for managing Snowflake permissions from a
declarative YAML spec. It's faster on big accounts and understands the object types Snowflake has shipped since
Permifrost stopped keeping up.

Anything not covered here (spec structure, `read`/`write` semantics, `!include`, `require-owner`) works as it does
in Permifrost. See the [Permifrost README](https://gitlab.com/gitlab-data/permifrost/-/blob/master/README.md).
Names, wildcards, ownership globs and the `--role` and `--user` filters follow the rules under [Names](#names). The
[CHANGELOG](CHANGELOG.md) lists what changed from 1.x.

```bash
# Python 3.11 or later
pip install tundra-cli
# or the latest main
pip install git+https://github.com/henryupton/tundra.git

tundra run spec.yml --dry
tundra spec-test spec.yml
```

On an older Python, pip installs a 1.x release instead. Tundra depends on `snowflake-core` 1.14, which brings in
pydantic 2 and newer certifi, urllib3 and python-dateutil than some environments pin. The
[CHANGELOG](CHANGELOG.md#before-you-upgrade) lists the minimum versions.

## Performance

Permifrost runs every Snowflake round trip serially, and on a large account most of them are wasted.

- **Parallel fetch.** Grants and future grants for each role, account privileges for each role that declares them,
  roles and tags for each user, and the table and task listings behind the existence checks are fetched
  concurrently and merged back in a deterministic order, so output is identical run to run. Concurrency is
  `--max-workers` on `run`, or `PERMISSION_BOT_MAX_WORKERS` for `run` and `spec-test`, default 8. The connection
  pool keeps that many connections and opens as many again under load.
- **No enumeration under wildcards.** A `db.schema.*` grant is issued as `ON ALL` plus `ON FUTURE`, yet
  Permifrost still ran `SHOW <objects> IN SCHEMA` per schema, per object type, per role just to build its revoke
  allowlist. Tundra tracks wildcard coverage by schema scope instead, so those calls are gone. On one production
  spec that was 1,658s of a 4,938s apply. Emitted SQL is unchanged.
- **Skip what you don't need.** `--skip-validation` skips the entity-existence checks, and
  `--ignore-missing-objects` drops grants on missing objects rather than failing the run.

## New object types

All table-like types ride the existing `tables` read/write lists, and a wildcard covers every type.

| Type          | read     | write                                          | Schema write adds      |
| ------------- | -------- | ---------------------------------------------- | ---------------------- |
| Iceberg table | `select` | `insert, update, delete, truncate, references` | `create iceberg table` |
| Dynamic table | `select` | `monitor, operate`                             |                        |
| Streamlit     | `usage`  | `usage`                                        | `create streamlit`     |
| Semantic view | `select` | `select`                                       | `create semantic view` |

These matter because Snowflake's `ON ALL TABLES` / `ON ALL VIEWS` don't reach any of them, so under Permifrost they
go silently ungranted. Schema write also grants `create cortex search service` and `create mcp server`. New types
are one entry in [`src/tundra/table_object_types.py`](src/tundra/table_object_types.py).

**External volumes** are a top-level spec section, granted `USAGE` from a role's `external_volumes:` list (needed to
read or write Iceberg tables):

```yaml
external_volumes:
  - s3_iceberg:
      storage_provider: s3

roles:
  - data_engineer:
      external_volumes: [s3_iceberg]
```

## Account privileges and tasks

**Account privileges** (`GRANT ... ON ACCOUNT`) are a flat list on the role:

```yaml
roles:
  - loader:
      account_privileges: [execute task, execute managed task]
  - tag_admin:
      account_privileges: [apply tag]
```

Declaring the key makes the list authoritative, like `warehouses`: undeclared account grants on that role are
revoked, and a role without the key is left alone. Names are case- and whitespace-insensitive. Two kinds of grant
are never revoked: Snowflake's own privileges on its system roles (no grantor on the grant), and the
ACCOUNTADMIN-only `create share`, `import share` and `manage share target`, which `SECURITYADMIN` can't touch. The
latter are also rejected at spec load, so grant them outside Tundra.

**Tasks** get their own `privileges.tasks` family beside `tables`, with the same pattern shapes and `ON ALL` plus
`ON FUTURE` grants:

```yaml
roles:
  - scheduler:
      privileges:
        tasks:
          write: [demo.jobs.*]
```

| read      | write              |
| --------- | ------------------ |
| `monitor` | `monitor, operate` |

They're kept out of `tables` on purpose, so a `tables` wildcard never hands a reader `MONITOR` on a schema's tasks.
Task grants inside the spec's databases are fully managed. No `create task` schema privilege is derived: ownership
stays with whichever role creates the task, and `EXECUTE TASK` comes from `account_privileges`.

## Database and application roles

A `member_of` entry with an unquoted period is a `DB.ROLE` reference. Tundra splits it at the first unquoted period,
so an application role with a dot in its name works: `snowflake.CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH`. A part with a
character an unquoted identifier can't have, such as `-`, keeps its case under [Names](#names), so write Cortex model
roles in uppercase. Tundra checks the reference against the database's roles, falls back to `SHOW APPLICATION ROLES`
for an application of the same name, and grants or revokes accordingly. Membership is fully managed: anything granted
but not listed is revoked.

```yaml
roles:
  - auditor:
      member_of:
        - snowflake.usage_viewer                    # database role
        - snowflake.cortex_analyst_requests_viewer  # application role
```

Database and application roles are referenced, never defined, and must already exist. Spec roles can't contain a
period, and users can't be granted either kind. Grants an application made itself are never revoked. `MANAGE GRANTS`
doesn't reach the `SNOWFLAKE` application's roles, so the role Tundra runs as must hold every application role the
spec hands out.

## Users

On top of Permifrost's user properties:

```yaml
users:
  - etl_service:
      can_login: true
      type: service                 # person (default), service, legacy_service
      default_secondary_roles: [all]
      parameters:
        timezone: Pacific/Auckland  # validated IANA name
        week_start: 1
      tags:
        cost_management.tags.team: data  # fully qualified tag: value
```

- `has_password: true` rewrites `type: service` to `legacy_service`, since Snowflake won't let a `SERVICE` user hold
  a password. `has_password: false` sets `PASSWORD = NULL`. Omit it for `SERVICE` users.
- `parameters` sets user-level session defaults. Allowed keys are `timezone`, `week_start`, `week_of_year_policy`,
  the `*_output_format` family, `timestamp_type_mapping`, `query_tag`, `statement_timeout_in_seconds`,
  `statement_queued_timeout_in_seconds` and `client_session_keep_alive`. Removing one leaves the value in place.
- `tags` is fully managed, unlike `parameters`: every tag set directly on a listed user is read from
  `TAG_REFERENCES`, the ones the spec declares are set, and any other is unset, including on a user with no `tags`
  key. Users not in the spec are left alone. Values are case-insensitive and set upper case, like `type`, so a
  tag with allowed values needs them upper case. The tags must already exist, and the role Tundra runs as needs
  `APPLY TAG` on the account and a warehouse, which `TAG_REFERENCES` needs to run.
- `default_role`, `default_warehouse` and `default_namespace` are sent quoted, in the case their names resolve to
  under [Names](#names): `default_role: analyst` sets `"ANALYST"`, and `default_role: gitlab-ci` sets `"gitlab-ci"`.
  `display_name`, `first_name`, `middle_name`, `last_name`, `email` and `comment` are sent as written, in single
  quotes. Tundra doesn't escape or check them, so a value with a `'` breaks the `ALTER USER` statement.

## Spec directories

`run` and `spec-test` accept a directory. Every `.yml`/`.yaml` under it (recursive, sorted, dotfiles skipped) is a
fragment holding any top-level keys, merged before validation:

```
spec/
  settings.yml
  databases.yml
  roles/common.yml
  users/systems.yml
```

List sections concatenate. An entity defined twice is an error: under one spelling in two files, the error names both
files, and under two spellings of one name (`loader` and `LOADER`), it names both spellings. Scalar settings must
agree.

## Names

Tundra compares names the way Snowflake resolves them, so `raw.public`, `RAW.PUBLIC` and `raw."PUBLIC"` are one
schema in the spec, in what Tundra reads and in the SQL it plans.

| Spelling                                                                      | Refers to                        |
| ----------------------------------------------------------------------------- | -------------------------------- |
| Unquoted identifier characters: `raw.events`                                  | The uppercase name, `RAW.EVENTS` |
| Quoted: `raw."events"`                                                        | The name in the case written     |
| Anything else, no quotes needed: `my-db`, `gitlab-ci`, `jane.doe@example.com` | The exact name, case kept        |

- A user Snowflake stores as `JANE.DOE@EXAMPLE.COM` has to be written in uppercase.
- Users, databases, warehouses, integrations, external volumes, `default_role` and `default_warehouse` are single
  names and never split on dots: `etl.wh` is `"etl.wh"`.
- Unquoted glob text resolves to uppercase too: `raw.events_*` matches `EVENTS_2024`. Quote a fragment to match a
  lowercase name: `raw."events_"*`.
- In a schema part and in `owns`, `*` matches anywhere in the name, so `owns: schemas: [raw.pre_*]` transfers
  ownership of each matching schema. No schema glob matches `INFORMATION_SCHEMA`. The table part of a
  `privileges.tables` entry takes only a lone `*`.
- A `*` in `member_of` has to be the only role included: `['*']`, or `include: ['*']` with an `exclude` list of
  named roles.
- A malformed name in the spec, `--role` or `--user` fails before Tundra connects.

## Running it

**Already-granted statements.** `run` prints the statements it applies, or with `--dry` the ones it would apply.
`--print-skipped` also prints the ones already granted, as `[SKIPPED]`, or as `[PENDING]` with `--dry`. With
`--diff`, a new statement starts with `+ ` and an already-granted one with two spaces. `-v` shows INFO logs and `-vv`
DEBUG logs, and neither prints more statements.

**Objects dropped mid-run.** Grants are planned from a snapshot, so a schema that CI drops and recreates while
Tundra is applying would fail every queued statement. Tundra re-looks up the object instead: gone prints
`[VANISHED]` (not a failure), recreated is retried once, on the transport its first try ended on. Every `[ERROR]`
and `[VANISHED]` line has the error on one indented line beneath it: `errno (sqlstate): message` from SQL, or
`error code (HTTP status): message` from the [REST API](#snowflakes-rest-api), whose 404 messages lowercase the
name. A request `snowflake.core` refuses to build prints its validation error instead.

**Running role.** The current role must be `SECURITYADMIN` or a role that has been granted it (checked with
`IS_ROLE_IN_SESSION`), so a purpose-built role works.

**Workload identity federation.** Short-lived OIDC tokens (GitHub Actions, say) with no stored secret:

```bash
PERMISSION_BOT_AUTHENTICATOR=WORKLOAD_IDENTITY
PERMISSION_BOT_WORKLOAD_IDENTITY_PROVIDER=OIDC
PERMISSION_BOT_TOKEN_COMMAND='python mint_token.py --audience my-account'
```

The command's stdout is the token. Tundra reruns it for each connection the pool opens, so no connection logs in
with an expired token. REST calls use the session of one pooled connection that Tundra holds for the run, and
`snowflake.core` renews that session's token when it expires. The REST path, that renewal included, hasn't been run
with `PERMISSION_BOT_TOKEN_COMMAND` yet, so dry-run in CI first and check the `API-capable listings:` line for a
`transport error`. `PERMISSION_BOT_TOKEN_COMMAND` takes precedence over the other auth methods, which are otherwise
[as in Permifrost](https://gitlab.com/gitlab-data/permifrost/-/blob/master/README.md#connection-parameters).

**Warehouses.** `size` is optional.

## Snowflake's REST API

Tundra reads and writes through Snowflake's REST API, with `snowflake.core`, where the API covers the call. These
reads go through it: grants and future grants to a role, grants to a user, and the database, warehouse, user,
database role and per-database schema listings. Roles, integrations, application roles, tables and other schema
objects, user tags, the account-wide schema listing and the session checks are read through SQL.

These writes go through it:

| Statement                                                                       | Example                                                              |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| One privilege on a database, `IMPORTED PRIVILEGES` included, granted or revoked | `GRANT usage ON database raw TO ROLE analyst`                        |
| One privilege on future tables in a schema, granted or revoked                  | `GRANT select ON FUTURE tables IN schema raw.public TO ROLE analyst` |
| One privilege on all tables in a schema, granted                                | `GRANT select ON ALL tables IN schema raw.public TO ROLE analyst`    |

- A statement in the table runs as SQL when the request would quote a name: one stored in lowercase or mixed case
  (written quoted in the spec, such as `raw."events"`), a reserved word other than `SNOWFLAKE`, such as `raw.group`,
  or a name such as `my-db`.
- Every other statement runs as SQL. The routing table, [`ROUTES`](src/tundra/operations.py), lists each kind of
  statement and the test against Snowflake behind its transport.

**Fallback.** A listing is read again through SQL, and a statement runs once more as SQL, for these reasons:

| Reason             | For        | Cause                                                                                                  |
| ------------------ | ---------- | ------------------------------------------------------------------------------------------------------ |
| `at the row limit` | Listings   | The listing came back with 10,000 rows, so it may have left rows out                                   |
| `invalid model`    | Listings   | `snowflake.core` or Tundra can't parse a row in the listing                                            |
| `API error`        | Listings   | An API error other than a 404 for a missing object                                                     |
| `syntax error`     | Statements | Snowflake can't parse the SQL it built from the request                                                |
| `server error`     | Statements | A 5xx                                                                                                  |
| `unrecognised 404` | Statements | A 404 for a missing object that isn't the statement's own database, schema or object                   |
| `non-JSON error`   | Both       | An error body `snowflake.core` can't read                                                              |
| `transport error`  | Both       | A connection failure, a thread that can't be started, or a `snowflake.core` client that can't be built |
| `retry timeout`    | Both       | `snowflake.core` resent a request answered 429, 503 or 504 until 600 s after the first send            |
| `missed deadline`  | Both       | No answer within `PERMISSION_BOT_API_TIMEOUT` seconds                                                  |

- After a transport error, retry timeout or missed deadline, on a read or a write, the rest of the run reads and
  writes through SQL.
- A listing's 404 for a missing object is raised, as SQL's `does not exist` is. A statement's other API errors, such
  as a 403 or a 404 with an error code other than Snowflake's does-not-exist `002003`, print as `[ERROR]` without
  running as SQL, and so does a request `snowflake.core` refuses to build.
- The first statement in a run to fall back for each reason, and the failure that switches a run to SQL, log at
  WARNING to stderr, shown as `[WARNING ]` with the level padded to eight characters. Other fallbacks log at INFO,
  which `tundra -v run` shows.

**Summary lines.**

```text
  API-capable listings: 5 through the API, 3 through SQL (fallbacks: 2 at the row limit, 1 non-JSON error).
  API-capable statements: 1 through the API, 3 through SQL (fallbacks: 1 syntax error, 1 server error).
```

- `run` and `run --dry` print the listings line after planning, and `spec-test` prints it before
  `Snowflake specs successfully loaded`. `run` prints the statements line after applying any statement the API can
  take.
- Green means everything the API was meant to take went through it, and yellow means something went to SQL instead.
- In brackets, a line counts the fallbacks by reason. It adds `then SQL for the rest of the run` after a transport
  error, retry timeout or missed deadline, and the statements line says
  `SQL for the rest of the run after a listing's` and the reason when a read's failure sent its statements to SQL.
  Under `--sql-only` the lines still print, noted `SQL only`.
- A statement whose kind runs as SQL counts as through SQL, so a green line with a large SQL count is normal. Reads
  that are always SQL, and ownership, `ALTER USER` property and application role statements, aren't counted. User tag
  statements (`ALTER USER ... SET TAG`) are counted, as through SQL.

**Settings.**

| Setting                                                                  | Default | Effect                                                                                                                                                                                                                                                                 |
| ------------------------------------------------------------------------ | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--sql-only` on `run` and `spec-test`, or `PERMISSION_BOT_SQL_ONLY=true` | off     | Every read and write goes through SQL, and Tundra never builds the `snowflake.core` client.                                                                                                                                                                            |
| `PERMISSION_BOT_API_STRICT=true`                                         | off     | Tundra exits 1 with one red line, before applying anything, when a read would fall back to SQL. Writes and lookups during apply ignore it. `--sql-only` overrides it.                                                                                                  |
| `PERMISSION_BOT_API_TIMEOUT`                                             | `120`   | Seconds a REST listing or statement may go unanswered before Tundra uses SQL for it and the rest of the run. Tundra exits 1 before connecting if the value isn't a positive number, `--sql-only` included. Missed deadlines add less than twice the deadline to a run. |

- `PERMISSION_BOT_SQL_ONLY` and `PERMISSION_BOT_API_STRICT` are on for `1`, `true`, `yes` or `on`, in any case. Any
  other value, a typo included, is off.
- Turn strict mode on for CI dry runs once a dry run prints a green `API-capable listings:` line. Its error names the
  reader and the reason, without Snowflake's message.

**Late requests.** Tundra can't cancel a REST call that misses its deadline, and `snowflake.core` sends requests with
no socket timeout, so Snowflake can still apply an abandoned statement later, even after Tundra exits. Within a run
that's harmless, because `GRANT` and `REVOKE` are idempotent and no plan holds a statement a late one would undo.
Across runs, a late `GRANT` can put back a privilege the next run revoked, so after a `missed deadline` warning, don't
start another run with a changed spec straight away.

**Connections.** The first REST call checks a connection out of the pool and holds it for the run, so a run that uses
the API has one more Snowflake session than a SQL-only run. REST calls go over that session through `snowflake.core`'s
own HTTP pool. SQL reads and every statement that runs as SQL take connections from Tundra's pool, which holds up to
twice `--max-workers`, so the held connection doesn't keep a worker waiting.

**Limits.** `snowflake.core` 1.14 refuses a database whose kind isn't `PERMANENT` or `TRANSIENT`, and a user whose
`default_secondary_roles` isn't `ALL` or `NONE`. `SHOW DATABASES` lists the `SNOWFLAKE` database, which every account
has, as `APPLICATION` and a shared database as `IMPORTED DATABASE`, and `SHOW USERS` lists the `()` or `('all')` that
Tundra sets on each spec user as `[]` or `["ALL"]`. If the REST API returns the same values, that listing reads
through SQL on every run and strict mode fails. This hasn't been checked against a real account. The warehouse listing
and a user's grants take no row limit, so Tundra spots a truncated one only if Snowflake's default limit is 10,000.

## Docs site

`tundra docs generate` writes a static site for a spec, in the style of `dbt docs`: a page for each role, user,
warehouse, database, integration and external volume, and a graph of who inherits what. `tundra docs serve` serves it
locally and opens it in a browser.

```bash
tundra docs generate --spec spec/ --target-dir target
tundra docs serve --target-dir target
```

| Option                             | Does                                                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `--spec`                           | Spec file or spec directory, as `run` takes it. Required.                                                     |
| `--target-dir`                     | Where the site goes, and where `serve` looks for it. Default `target`.                                        |
| `--live`                           | Also reads the account's grants and marks each membership and usage grant as applied, not applied or drift.   |
| `--family 'LABEL=PATTERN'`         | Gives roles whose names match the glob their own colour and row in the key. Repeatable; the first match wins. |
| `--sql-only`                       | With `--live`, reads through SQL only.                                                                        |
| `--host`, `--port`, `--no-browser` | For `serve`. It listens on `127.0.0.1:8080` and opens a browser by default.                                   |

- The site is `index.html` with the catalogue inside it, `catalogue.json` with the same data, and `assets/`. It opens
  from disk with no network and nothing to install beyond tundra, because the browser lays out the graphs with
  Graphviz compiled to WebAssembly ([Viz.js](https://github.com/mdaines/viz-js)), which ships in the package.
- Without `--live`, tundra reads the spec alone and never connects. Memberships come from the planner `run` uses, so
  `member_of: ['*']`, `include` and `exclude`, and the skipped grants between Snowflake's default roles resolve as
  `run` applies them to an account that has every role the spec defines. Wildcard privileges are listed as written.
  A spec that `run` refuses fails here with the same error.
- `--live` reads the account as `run` does, with the same `PERMISSION_BOT_*` settings and [running role](#running-it),
  through a connector that refuses to grant or revoke. A grant `run` would leave alone is applied, one it would make is
  not applied, and one it would revoke is drift. The comparison covers role memberships and usage of warehouses,
  databases, integrations and external volumes. Schema, table and task privileges come from the spec only.
- People (users without `type: SERVICE`) are grouped by the exact set of roles they hold, and a toggle in the graph
  draws them one by one. Pages leave out users' names and email addresses.
- In the graph, hover a node to trace everything that reaches it and everything it reaches, click to pin it, click a
  row of the key to highlight a family, scroll to zoom and drag to pan. Each page has a lineage graph of its own, and
  **Open in the graph** focuses the whole graph on it.

## Contributing

Issues and PRs welcome on [GitHub](https://github.com/henryupton/tundra/issues). Tests run on every PR. Versions are
bumped by the release workflow on merge to `main`, not by hand.

```bash
pip install -r requirements.txt   # Python 3.11 or later
PYTHONPATH=src python -m pytest tests -q
```

Tests never reach Snowflake. `tests/conftest.py` gives each test dummy `PERMISSION_BOT_*` credentials, clears any
other login or transport setting your shell has, and fails a test that logs in, builds a `snowflake.core` client or
sends a REST request.
