# Changelog

Versions follow [Semantic Versioning](https://semver.org/). The release workflow cuts a release each time a pull request merges to `main`.

## 2.0.1

### Grants

- Schema write grants `create masking policy` with the other create privileges, so a role can create masking policies in any schema it writes, as it already can functions and Cortex Search services. A role that wrote a schema it didn't own couldn't create a masking policy there. Every role with schema write gains the privilege on its next run.

## 2.0.0

tundra compares Snowflake names by identity. Every spelling that Snowflake resolves to one object, such as `raw.public`, `RAW.PUBLIC` and `raw."PUBLIC"`, is one name to tundra, in the spec, in what it reads from Snowflake and in the SQL it plans. tundra also reads grants and listings, and sends some grants and revokes, through Snowflake's REST API with `snowflake.core`, and uses SQL for everything else and for any call the API gives no usable answer to.

### Before you upgrade

- tundra needs Python 3.11 or later.
- tundra depends on `snowflake-core` 1.14 (`~=1.14.0`, so 1.14.0 or a later 1.14 patch release), because the model validators in `snowflake-core` decide which REST listings tundra can read. It brings in pydantic 2, which needs typing-extensions 4.6.1 or later, and raises the minimum versions of certifi to 2026.10.19, urllib3 to 2 and python-dateutil to 2.8.2. An environment that pins any of these lower, pins pydantic 1, or needs `snowflake-core` 1.15 or later won't resolve. The minimum `snowflake-connector-python` doesn't change.
- tundra reads through [Snowflake's REST API](#snowflakes-rest-api) and sends some grants and revokes through it by default. `--sql-only` on `run` and `spec-test`, or `PERMISSION_BOT_SQL_ONLY=true`, keeps every read and write on SQL, as 1.0.6 did, though the `API-capable` summary lines still print, noted `SQL only`.
- Some spellings that worked in 1.0.6 now name a different object:
  - An unquoted name for an object stored in lowercase, such as a warehouse created as `"lower_wh"` or a schema created as `raw."lower_case"`. 1.0.6 accepted only the unquoted spelling and then planned SQL against the uppercase name. 2.0 reports the unquoted spelling missing, so quote it in the spec: `'"lower_wh"'`.
  - A part that isn't a valid identifier, such as `my-db` or `gitlab-ci`, for an object stored in uppercase. 1.0.6 uppercased these parts in database roles, application roles, tags and user properties, and 2.0 keeps their case. The Snowflake check reports a database or application role spelled this way as missing when its database part names a database that exists. When it doesn't, as for `my-db.db-role-1` where only `MY-DB` exists, the run stops with Snowflake's error that the database doesn't exist. Tags and user properties aren't checked against Snowflake, so `default_role: gitlab-ci` quietly changes `DEFAULT_ROLE` to `"gitlab-ci"`, and a tag under `my-db` is set on `"my-db"` and unset from `"MY-DB"`. Spell these parts in uppercase, for example `snowflake.CORTEX-MODEL-ROLE-MISTRAL-LARGE2` for a Cortex application role.
- A pipeline that reads tundra's output will see these changes:
  - On stdout, a new `API-capable listings:` line comes before `SQL Commands generated for given spec file:`, and a run that applies a statement the API can take ends with an `API-capable statements:` line. When something fell back to SQL, these lines are yellow and name the reasons, which can include `API error` and `server error`, so a script that fails a run on the word `error` needs to skip them.
  - The line under an `[ERROR]` or `[VANISHED]` statement that ran through the API reads `error code (HTTP status): message`, where SQL prints `errno (sqlstate): message`, and the API's 404 messages lowercase the object's name. A request that `snowflake.core` refuses to build prints pydantic's validation message instead. A script that greps these lines for a sqlstate or for SQL's wording has to match the API form as well, which looks like this:

    ```text
    [ERROR] GRANT select ON FUTURE tables IN schema raw.public TO ROLE locked;
        003001 (403): Insufficient privileges to operate on role 'LOCKED'.
    ```

  - `-v` and `-vv` only set the log level. With `-v`, 1.0.6 also printed every statement that was already granted, as `[SKIPPED]` when applying and as `[PENDING]` with `--dry`. 2.0 prints them only with the new `--print-skipped` flag on `run`, so a dry run that used `-v --diff` for the full diff needs `--print-skipped` as well. The header that ends `(Full diff with both new and already granted commands):` prints only when `--diff` and `--print-skipped` are both given, and `--diff` alone prints the plain `SQL Commands generated for given spec file:` header.
  - On stderr, lines logged at WARNING (shown as `[WARNING ]`, with the level padded to eight characters) say when a statement or the rest of the run went through SQL.
  - Nothing on a `[SUCCESS]`, `[ERROR]` or `[VANISHED]` line says which transport ran the statement.
  - With `--skip-validation`, a spec role, user or database that's missing from Snowflake still ends the run with a traceback, but through the API it shows `snowflake.core`'s `NotFoundError` (404) text where SQL showed `does not exist or not authorized`.
- A spec, or a `--role` or `--user` value, that 1.0.6 ran can now fail with a [Spec or Name error](#spec-checks) and exit 1 when it loads, for example a spec that defines a role twice.
- tundra exits 1 with one red line, before it applies anything, when a read would fall back to SQL while `PERMISSION_BOT_API_STRICT` is set, or when `PERMISSION_BOT_API_TIMEOUT` isn't a positive number of seconds. A bad timeout fails before tundra connects, `--sql-only` included:

  ```text
  PERMISSION_BOT_API_TIMEOUT must be a positive number of seconds, not 'soon'
  ```

- After a `missed deadline` warning for a statement, don't start another run with a changed spec straight away. Snowflake can still apply the abandoned request after tundra exits, and a late `GRANT` that lands after the next run revokes the privilege puts it back.
- tundra imports `snowflake.core` when it starts, `--sql-only` included, so each start takes longer. On an Apple M4 Max, `import tundra.cli` takes about 0.7 s on Python 3.13 and 1.6 s on Python 3.11, against about 0.25 s and 0.6 s for the libraries 1.0.6 imported.
- Dry-run each spec with `tundra run spec.yml --dry` before the first 2.0 run, and compare it with a 1.0.6 dry run if you can, leaving out the new `API-capable listings:` line. Spellings and globs that 1.0.6 skipped or matched wrongly now match, so the first run can grant, revoke or transfer ownership that 1.0.6 never planned. A second dry run with `--sql-only` should plan the same statements. If it doesn't, run with `--sql-only` and [open an issue](https://github.com/henryupton/tundra/issues).

### Snowflake's REST API

- These listings go through the REST API: grants to a role, future grants to a role, grants to a user, databases, warehouses, users, the schemas in a database, and the database roles in a database. Roles, integrations, application roles, tables and other schema objects, user tags, the account-wide schema listing, and the `CURRENT_USER`, `CURRENT_ROLE` and `IS_ROLE_IN_SESSION` checks are read through SQL.
- A listing is read again through SQL when it comes back at the 10,000-row limit, when `snowflake.core` or tundra can't read a row in it (`invalid model`), on an API error other than a 404 for a missing object, on an error body `snowflake.core` can't read (`non-JSON error`), or on a transport error, retry timeout or missed deadline. A 404 for a missing object is raised, as SQL's `does not exist` is.
- These statements go through the API, and the routing table, `ROUTES` in `tundra.operations`, records the test against Snowflake behind each one:
  - one privilege on a database, granted or revoked, `IMPORTED PRIVILEGES` included: `GRANT usage ON database raw TO ROLE analyst`
  - one privilege on future tables in a schema, granted or revoked: `GRANT select ON FUTURE tables IN schema raw.public TO ROLE analyst`
  - one privilege on all tables in a schema, granted: `GRANT select ON ALL tables IN schema raw.public TO ROLE analyst`. tundra never revokes `ON ALL`.

  A statement of one of these kinds runs as SQL when it has more than one privilege, or when the request would quote a name: one stored in lowercase or mixed case (written quoted in the spec, such as `raw."events"`), a reserved word such as `raw.group`, or a name such as `my-db`. `SNOWFLAKE` is sent unquoted, so `IMPORTED PRIVILEGES` on it goes through the API. In practice, the `USAGE` on a database and the `SELECT` on a schema's tables that `read` grants go through the API, and `write` bundles run as SQL. Every other statement runs as SQL. Ownership, `ALTER USER` properties and application role grants and revokes can only run as SQL.
- A statement the API gives no usable answer for runs once more as SQL. That covers a syntax error, a 5xx, an error body `snowflake.core` can't read, a transport error, a retry timeout, a missed deadline, and a 404 that doesn't name the statement's own database, schema or object (`unrecognised 404`). A role missing from Snowflake gets that 404, so its `[ERROR]` shows SQL's message. Any other API error, such as a 401, 403 or 409, prints as `[ERROR]`, because its SQL would be refused too. Only a 404 with Snowflake's does-not-exist code, `002003`, can be an `unrecognised 404`, and a 404 with any other code prints as `[ERROR]`. A request `snowflake.core` refuses to build prints its validation error as `[ERROR]` too, and doesn't run as SQL. A 404 that names the statement's target is a dropped object, which apply looks up as before, and a statement whose object is back is retried once on the transport its first try ended on.
- After a transport error, a retry timeout or a missed deadline, on a read or a write, the rest of the run reads and writes through SQL. A thread that can't be started for a REST call counts as a transport error.
- `PERMISSION_BOT_API_TIMEOUT` is the deadline, in seconds, for each REST listing and each statement sent through the API. It defaults to 120 and takes fractions such as `0.5`. Building the `snowflake.core` client, which runs `SELECT SYSTEM$CLIENT_VERSION_INFO()`, isn't timed, and neither are SQL reads. If the client can't be built, that counts as a transport error: the rest of the run goes through SQL, and with `PERMISSION_BOT_API_STRICT` set the run fails. tundra can't cancel a call that misses its deadline, so it stops waiting and leaves the call running until it ends or tundra exits. `snowflake.core` resends a request answered 429, 503 or 504 until 600 s after the first send, so with the default deadline a sustained 429, 503 or 504 ends as a missed deadline. Missed deadlines add less than twice the deadline to a run, because nothing starts on the API after the first one.
- A statement abandoned at its deadline can still be applied after its SQL has run, after later statements, and after tundra exits. `snowflake.core` sends the first request with no socket timeout, resends one answered 429, 503 or 504 for up to 600 s, and Snowflake finishes a request it answered with a 202 whatever the client does. Within a run this is harmless, because `GRANT` and `REVOKE` are idempotent and no plan holds a statement that a late one would undo. Across runs it isn't: a late `GRANT` that lands after the next run has revoked the privilege puts it back until the run after. A statement's missed-deadline warning says `though the API request may still be applied` before the error detail, and a listing's doesn't:

  ```text
  2026-11-02 07:48:40: [WARNING ] [snowflake_connector] GRANT usage ON database raw TO ROLE analyst through snowflake.core failed (missed deadline), so it and the rest of the run go through SQL, though the API request may still be applied: still running after 120 s (PERMISSION_BOT_API_TIMEOUT sets the deadline)
  ```

- `PERMISSION_BOT_API_STRICT=true` makes a read that would fall back to SQL fail the run with one red line and exit 1, before anything is applied. Turn it on for CI dry runs once a dry run prints a green `API-capable listings:` line. After a transport error, retry timeout or missed deadline, every later API-capable read fails at once. The line names the reader and the reason, without Snowflake's error:

  ```text
  show_schemas would read through SQL instead of the API (at the row limit), and PERMISSION_BOT_API_STRICT is set
  ```

  Writes, and the lookups apply makes for dropped objects, ignore strict mode, so it can't stop a run halfway. `--sql-only` overrides it.
- `--sql-only` and `PERMISSION_BOT_SQL_ONLY` send every read and write through SQL, and tundra never builds the `snowflake.core` client, so the run opens no extra session and starts no threads for deadlines. `PERMISSION_BOT_SQL_ONLY` and `PERMISSION_BOT_API_STRICT` are on for `1`, `true`, `yes` or `on` in any case, and off for any other value, a typo included.
- After planning, `run` and `run --dry` print a line counting the API-capable listings by transport, and `spec-test` prints it before `Snowflake specs successfully loaded`. After applying, `run` prints a line counting the API-capable statements it applied, if there were any. Each line is green when everything the API was meant to take went through it and yellow when something went to SQL instead. In brackets it counts the fallbacks by reason, says when the rest of the run went to SQL and why, or says `SQL only` under `--sql-only`:

  ```text
    API-capable listings: 5 through the API, 3 through SQL (fallbacks: 2 at the row limit, 1 non-JSON error).
    API-capable statements: 0 through the API, 2 through SQL (SQL for the rest of the run after a listing's missed deadline).
  ```

  Reads that are always SQL, and ownership, `ALTER USER` property and application role statements, aren't counted. Other statements whose kind the routing table sends to SQL, user tags and memberships among them, count as through SQL, so a green line with a large SQL count is normal.
- Fallbacks log to stderr. The first statement in a run to fall back for each reason logs at WARNING, and later ones with the same reason log at INFO:

  ```text
  2026-11-02 07:48:40: [WARNING ] [snowflake_connector] <sql> through snowflake.core failed (<reason>), so it runs as SQL: <detail>
  ```

  On reads, only the failure that switches the run to SQL logs at WARNING. Other read fallbacks log at INFO, so by default only the yellow summary shows them. `tundra -v run` shows INFO lines.
- `snowflake.core`'s own log records, such as the ERROR it logs for an error body it can't read, don't print.
- The first REST call checks a connection out of the run's pool, builds the `snowflake.core` client over it, and holds it for the run. A run that uses the API therefore has one more Snowflake session than a SQL-only run, and with `PERMISSION_BOT_TOKEN_COMMAND` it runs the command once more. The pool holds up to twice `--max-workers` connections, so the held connection doesn't keep a worker waiting. REST calls go through `snowflake.core`'s own HTTP connection pool.
- `snowflake.core` 1.14's validators refuse a database whose kind isn't `PERMANENT` or `TRANSIENT`, such as `APPLICATION` or `IMPORTED DATABASE`, and a user whose `default_secondary_roles` isn't `ALL` or `NONE`. `SHOW DATABASES` lists the `SNOWFLAKE` database, which every account has, as `APPLICATION` and a shared database as `IMPORTED DATABASE`, and `SHOW USERS` lists the `()` or `('all')` that tundra sets on each spec user as `[]` or `["ALL"]`. If the REST API returns the same values, then on every run the whole database or user listing reads through SQL, the listings line is yellow and strict mode fails. Whether a real account's REST API returns these values hasn't been checked.
- `snowflake.core` 1.14 can't pass a row limit to the warehouse listing or to a user's grants, so tundra catches a truncated one only if Snowflake's default limit is 10,000.

### Spec checks

- A role, user, database, warehouse, integration or external volume defined more than once is a Spec error, whichever spelling each definition uses:

  ```text
  Spec error: role LOADER is defined more than once (first as loader)
  ```

  1.0.6 accepted a role repeated under one spelling and planned each definition in turn, so when the definitions differed, the role's grants and revokes swapped on every run. A repeated identical definition is an error too. The same spelling in two files of a spec directory was already an error and keeps its `is defined in both` message.
- A `*` in `member_of` has to be the only role included: `member_of: ['*']`, or `include: ['*']` with an `exclude` list of named roles. A `*` next to other roles, a `*` in `exclude`, and patterns such as `role_*` or `mydb.*` fail when the spec loads:

  ```text
  Name error: Not a valid role name: role_* (A * in member_of has to be the only role included)
  ```

  1.0.6 planned `GRANT ROLE "*"` for `['*', role_b]`, which fails when applied, ignored `exclude: ['*']`, and failed at the Snowflake check or while fetching grants for patterns. A quoted `'"*"'` is now a role named `*`, where 1.0.6 treated it as every role.
- Malformed names in definitions, privileges, `member_of`, tags and user properties fail when the spec loads, before tundra connects to Snowflake. That covers stray or unterminated quotes (`raw.ab"c`), empty parts (`raw..x`), a `<table>`-style placeholder in a schema or table entry, and parts over 255 characters. 1.0.6 reported most of these as missing objects, dropped them, or planned broken SQL such as `DEFAULT_ROLE = "MY"ROLE"`. `owner` values aren't checked when the spec loads.
- A malformed `--role` or `--user` value is a Name error that says what's wrong with it, before tundra loads the spec or connects, in both `run` and `spec-test`:

  ```text
  Name error: Not a valid role name for --role: '"loader': '"loader' isn't a quoted identifier; quote the whole part
  ```

  1.0.6 selected nothing for it and exited 0.

### Names

- Unquoted parts that are valid identifiers resolve to uppercase whatever case they're typed in, the way Snowflake resolves them. `raw.MixedCase` is the schema `RAW.MIXEDCASE` and renders as `raw.mixedcase`, where 1.0.6 treated it as case-sensitive and reported it missing. Quote a name that really is case-sensitive: `raw."MixedCase"`.
- Uppercase, quoted and mixed-case spellings of an existing object load and match the grants it holds. `RAW.PUBLIC`, `Raw.Public` and `raw."PUBLIC"` plan the same as `raw.public`, and `raw.group` matches the schema `GROUP`. This applies to the existence checks, references between spec sections, role owners, `member_of` and its `exclude`, and the `--role` and `--user` filters. In 1.0.6 these spellings failed with Missing Entity, Reference, Privilege or owner errors, matched nothing (`exclude`, `--role` and `--user`), or planned a grant and a revoke of the same object. An uppercase `member_of: [ANALYST]` did that on a plain run, so each run revoked the membership and the next granted it back.
- A part that isn't a valid identifier, such as `my-db`, `gitlab-ci` or `1raw`, is the exact name with its case kept, and can be written without quotes. `member_of: [my-db.db-role-1]` plans `GRANT DATABASE ROLE "my-db"."db-role-1"` (1.0.6: `"MY-DB"."DB-ROLE-1"`), and `default_role: gitlab-ci` sends `DEFAULT_ROLE = "gitlab-ci"` (1.0.6: `"GITLAB-CI"`, which is a different role). Without `--skip-validation`, 1.0.6 couldn't manage a database named `my-db` under either spelling.
- Databases, warehouses, integrations, external volumes, `default_role` and `default_warehouse` are single names that never split on dots, so `etl.wh` is `"etl.wh"`. A quoted dot stays inside its part: `db.schema."table.v1"` is a three-part name and `"my.role"` in `member_of` is an account role.
- An unquoted `member_of` entry with two dots splits at the first one, so application roles with a dot in their name can be granted. `snowflake.CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH` plans `GRANT APPLICATION ROLE "SNOWFLAKE"."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"`, where 1.0.6 rejected every spelling with a Name error. A mistyped `mydb.myschema.role` is the database role `mydb."myschema.role"`, which the Snowflake check reports missing.
- Names that Snowflake stores in lowercase, or with quotes or dots in them, render correctly when tundra expands a wildcard or plans an entry. A schema created as `"lower_case"` renders as `raw."lower_case"`, where 1.0.6 wrote `raw.lower_case`, which is a different schema.
- The `Current user is:` and `Current role is:` lines and most of tundra's own error messages show names as tundra renders them, so the user `JANE.DOE@EXAMPLE.COM` prints as `"JANE.DOE@EXAMPLE.COM"` where 1.0.6 lowercased it.

### Wildcards

- Unquoted glob text resolves to uppercase: `raw.Loading_*` and `RAW.LOADING_*` match `LOADING_ORDERS`. In 1.0.6, `raw.Loading_*` matched nothing and `RAW.LOADING_*` failed the spec check on its uppercase database. Unquoted glob text no longer matches names stored in lowercase, so quote the fragment, `raw."loading_"*`, to match `"loading_lower"`. A lone `*` still matches every name.
- A glob that isn't an identifier keeps its case: `raw.Mixed-*` matches `"Mixed-Case"`.
- In a schema part and in `owns` entries, `*` matches anywhere and `**` is the same as `*`. `raw.pre*suf` works, and `raw.*mid*` matches names containing `MID` (1.0.6 matched every schema). The table part of a `privileges.tables` entry still takes only a lone `*`, as in 1.0.6: `raw.public.ev*` plans no grants, so tundra revokes the grants the role holds on the tables it would match.
- No glob in the schema position matches `INFORMATION_SCHEMA`, however the database is spelled. 1.0.6 matched it under some spellings, such as `RAW$X.*`.
- Ownership of a partial glob covers the matching schemas and tables. `owns: schemas: [raw.pre_*]` transfers ownership of each matching schema with `COPY CURRENT GRANTS`, where 1.0.6 planned ownership of the literal `raw."pre_*"`, which Snowflake rejects.

### Plans

- Revokes are sorted by name within each section, so a plan comes out in the same order under any `PYTHONHASHSEED`.
- tundra recognises grants a role already holds, and revokes stale ones, in cases 1.0.6 missed:
  - external volumes that SHOW GRANTS reports as `EXTERNAL_VOLUME`
  - external volumes, warehouses and integrations whose names need quoting (`my-vol`, `etl-wh`). Where SHOW GRANTS reported `EXTERNAL VOLUME`, 1.0.6 revoked a held grant on `my-vol` in the same run that skipped granting it, so access dropped on every other run.
  - future grants held by roles whose names need quoting (`gitlab-ci`)
  - memberships of account roles with a dot in their name. A role that holds `"etl.loader"` without listing it gets `REVOKE ROLE "etl.loader" FROM role loader`, where 1.0.6 never revoked these from a role.
- Grants between Snowflake's default roles are skipped however they're spelled. 1.0.6 planned `GRANT ROLE PUBLIC TO role sysadmin` for `member_of: [PUBLIC]`.
- Future grants are read once for each spec role, through the REST API or with `SHOW FUTURE GRANTS TO ROLE`, `--ignore-memberships` included. 1.0.6 read them once for each database and once for each schema. A role that doesn't exist holds none, and tundra logs `Role <name> doesn't exist, so it holds no future grants` at INFO.
- A spec plans the same statements whichever transport reads the grants.

### Python API

- New module `tundra.identifiers` with `ObjectName`, `NamePattern` and `InvalidNameError`. `ObjectName` replaces `SnowflakeConnector.snowflaky` and the other `snowflaky_*` helpers, which are removed along with `show_external_volumes`, `show_grants_to_database_role` and `FUTURE_PLACEHOLDER_PATTERN`. For names in REST models it has `ObjectName.from_api`, and the module has `canonical_from_api` and `api_granted_name`.
- New modules `tundra.operations` and `tundra.snowflake_api`. `tundra.operations` holds each planned statement as a frozen operation (`Privileges`, `BulkPrivileges`, `RoleMembership`, `UserTag` or `SqlStatement`), the SQL templates, which `tundra.snowflake_grants` still exports, and `ROUTES`, `routes_to_api` and `api_capable`, which decide each statement's transport. `tundra.snowflake_api.SnowflakeApi` wraps `snowflake.core`'s `Root`. `tundra.concurrency` adds `call_with_deadline`, `DeadlineExceeded` and `ThreadStartError` beside `parallel_map`.
- The `SnowflakeConnector` readers, `get_current_user`, `get_current_role`, and the state held by `EntityGenerator`, `SnowflakeSpecLoader` and `SnowflakeGrantsGenerator` use `ObjectName` and `NamePattern` values instead of strings. An `ObjectName` equals only the string of its rendering, so `ObjectName("RAW.PUBLIC") == "raw.public"` is true and `== "RAW.PUBLIC"` is false, and it has no string methods such as `.split()`. Formatting a `NamePattern` with an f-string raises `TypeError`.
- These readers go through `snowflake.core` and fall back to SQL: `show_databases`, `show_warehouses`, `show_users`, `show_schemas(database)`, `show_database_roles`, `show_grants_to_role`, `show_account_grants_to_role`, `show_grants_to_role_with_grant_option`, `show_roles_granted_to_user` and the new `show_future_grants_to_role`. Through the API, a missing role, user or database raises `snowflake.core.exceptions.NotFoundError` where SQL raises SQLAlchemy's `ProgrammingError`, and with `PERMISSION_BOT_API_STRICT` set a reader raises `ApiFallbackError` in place of a fallback. Patching `run_query` doesn't intercept these reads, so pass `sql_only=True` to keep them on SQL.
- `show_grants_to_role`, `show_future_grants` and `show_roles_granted_to_user` return sets where they returned lists, and the new `show_future_grants_to_role` returns sets too. The other readers still use lists. `SnowflakeSpecLoader` reads future grants with `show_future_grants_to_role` for each spec role, where it called `show_future_grants` for each database and schema, so `SnowflakeSpecLoader.grants_to_role` holds the spec's roles only, still in lists.
- `show_grants_to_role`, `show_future_grants` and `show_grants_to_role_with_grant_option` skip grants on kinds tundra doesn't manage, such as functions, procedures and stages, so `SnowflakeRoleGrantChecker` no longer finds them. `show_grants_to_role` also leaves out account grants, which 1.0.6 returned under `account` (read them with `show_account_grants_to_role`), and files external volume grants under `external volume` whether Snowflake reports `EXTERNAL_VOLUME` or `EXTERNAL VOLUME`.
- `generate_permission_queries` returns `PlannedCommand`s: dicts with the same `already_granted` and `sql` keys, which carry the statement as an `operation` attribute and dump to YAML as plain mappings. `tundra.cli.permissions.apply_query` runs each statement with `conn.execute(operation)`, so a connector that overrides `run_query` doesn't see statements sent through the API, and a stand-in connector needs `execute`, `object_exists` and `write_stats`, plus `sql_only` and `api_failure` if its `write_stats` is a `WriteStats`. A plain dict, or a `PlannedCommand` whose `sql` you've changed, runs its `sql` as SQL.
- `SnowflakeConnector(config=None, sql_only=False)` raises `InvalidSettingError`, a `ValueError`, for a bad `PERMISSION_BOT_API_TIMEOUT`. The connector adds `api`, `execute`, `writes_through_api`, `read_stats`, `write_stats`, `api_failure`, `api_strict`, `api_timeout` and a settable `sql_only`. `tundra.snowflake_connector` also adds `ApiFallbackError` (with `.reader` and `.reason`), `ReadStats`, `WriteStats`, `Executed`, `api_timeout()` and `api_error_detail()`.
- `SnowflakeConnector.missing_object(error, operation=None)` takes the statement's operation, and for a 404 from the API returns the operation's own name, because the API's message lowercases it.
- `SnowflakeSpecLoader(sql_only=True)` sets `sql_only` on the connector it's given, and `SnowflakeSpecLoader` raises `InvalidSettingError` for a bad `PERMISSION_BOT_API_TIMEOUT` even when it's given a connector. `report_reads()` prints the `API-capable listings:` line to stdout, and the loader under `spec_test=True` and `generate_permission_queries` call it. A `SnowflakeGrantsGenerator` given a connector reads through it for `member_of: ['*']`, schema expansion and table listings, where 1.0.6 built its own connectors from the `PERMISSION_BOT_*` settings.
- tundra adds a `NullHandler` to the `snowflake.core` logger. Records still propagate, so a program that configures the root logger gets `snowflake.core`'s records.

### Fixed

- `tundra --version` prints the installed version. Every 1.x release printed 0.15.6.
- On Python 3.12 and later, tundra no longer prints `SyntaxWarning: invalid escape sequence '\s'` when it runs without cached bytecode, for example on the first run after a `uv` install.
- The release workflow uploads the version it just tagged. Before, each run uploaded the new code under the previous version number, so each `tundra-cli` version on PyPI holds the code of the next git tag: 1.0.5 holds 1.0.6, and 0.15.24 holds 1.0.0. PyPI has no 1.0.6, and its files can't be replaced, so install a 1.x release from its git tag, for example `pip install "git+https://github.com/henryupton/tundra@1.0.6"`.
- A run builds one connector, with one engine and connection pool, and uses it to load, plan and apply. 1.0.6 built a new one, with its own logins, for the grants generator, several spec checks and apply.
