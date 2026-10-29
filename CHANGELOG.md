# Changelog

Versions follow [Semantic Versioning](https://semver.org/). The release workflow cuts a release each time a pull request merges to `main`.

## 2.0.0

tundra compares Snowflake names by identity. Every spelling that Snowflake resolves to one object, such as `raw.public`, `RAW.PUBLIC` and `raw."PUBLIC"`, is one name to tundra, in the spec, in what it reads from Snowflake and in the SQL it plans.

### Before you upgrade

- tundra needs Python 3.11 or later.
- tundra depends on `snowflake-core` 1.14.0 or later. It brings in pydantic 2, which needs typing-extensions 4.6.1 or later, and raises the minimum versions of certifi to 2026.10.19, urllib3 to 2 and python-dateutil to 2.8.2. An environment that pins any of these lower, or pins pydantic 1, won't resolve. The minimum `snowflake-connector-python` doesn't change.
- Some spellings that worked in 1.0.6 now name a different object:
  - An unquoted name for an object stored in lowercase, such as a warehouse created as `"lower_wh"` or a schema created as `raw."lower_case"`. 1.0.6 accepted only the unquoted spelling and then planned SQL against the uppercase name. 2.0 reports the unquoted spelling missing, so quote it in the spec: `'"lower_wh"'`.
  - A part that isn't a valid identifier, such as `my-db` or `gitlab-ci`, for an object stored in uppercase. 1.0.6 uppercased these parts in database roles, application roles, tags and user properties, and 2.0 keeps their case. The Snowflake check reports database and application roles spelled this way as missing. Tags and user properties aren't checked against Snowflake, so `default_role: gitlab-ci` quietly changes `DEFAULT_ROLE` to `"gitlab-ci"`, and a tag under `my-db` is set on `"my-db"` and unset from `"MY-DB"`. Spell these parts in uppercase, for example `snowflake.CORTEX-MODEL-ROLE-MISTRAL-LARGE2` for a Cortex application role.
- Dry-run each spec with `tundra run spec.yml --dry` before the first 2.0 run, and compare it with a 1.0.6 dry run if you can. Spellings and globs that 1.0.6 skipped or matched wrongly now match, so the first run can grant, revoke or transfer ownership that 1.0.6 never planned.

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
- Uppercase, quoted and mixed-case spellings of an existing object load and match the grants it holds. `RAW.PUBLIC`, `Raw.Public` and `raw."PUBLIC"` plan the same as `raw.public`, `raw.group` matches the schema `GROUP`, and `lab_results."AGGREGATE"` matches `AGGREGATE`. This applies to the existence checks, references between spec sections, role owners, `member_of` and its `exclude`, and the `--role` and `--user` filters. In 1.0.6 these spellings failed with Missing Entity, Reference, Privilege or owner errors, matched nothing (`exclude`, `--role` and `--user`), or planned a grant and a revoke of the same object. An uppercase `member_of: [ANALYST]` did that on a plain run, so each run revoked the membership and the next granted it back.
- A part that isn't a valid identifier, such as `my-db`, `gitlab-ci` or `1raw`, is the exact name with its case kept, and can be written without quotes. `member_of: [my-db.db-role-1]` plans `GRANT DATABASE ROLE "my-db"."db-role-1"` (1.0.6: `"MY-DB"."DB-ROLE-1"`), and `default_role: gitlab-ci` sends `DEFAULT_ROLE = "gitlab-ci"` (1.0.6: `"GITLAB-CI"`, which is a different role). Without `--skip-validation`, 1.0.6 couldn't manage a database named `my-db` under either spelling.
- Databases, warehouses, integrations, external volumes, `default_role` and `default_warehouse` are single names that never split on dots, so `etl.wh` is `"etl.wh"`. A quoted dot stays inside its part: `db.schema."table.v1"` is a three-part name and `"my.role"` in `member_of` is an account role.
- An unquoted `member_of` entry with two dots splits at the first one, so application roles with a dot in their name can be granted. `snowflake.CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH` plans `GRANT APPLICATION ROLE "SNOWFLAKE"."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"`, where 1.0.6 rejected every spelling with a Name error. A mistyped `mydb.myschema.role` is the database role `mydb."myschema.role"`, which the Snowflake check reports missing.
- Names that Snowflake stores in lowercase, or with quotes or dots in them, render correctly when tundra expands a wildcard or plans an entry. A schema created as `"lower_case"` renders as `raw."lower_case"`, where 1.0.6 wrote `raw.lower_case`, which is a different schema.
- The `Current user is:` and `Current role is:` lines and most error messages show names as tundra renders them, so the user `JANE.DOE@EXAMPLE.COM` prints as `"JANE.DOE@EXAMPLE.COM"` where 1.0.6 lowercased it.

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

### Python API

- New module `tundra.identifiers` with `ObjectName`, `NamePattern` and `InvalidNameError`. `ObjectName` replaces `SnowflakeConnector.snowflaky` and the other `snowflaky_*` helpers, which are removed along with `show_external_volumes`, `show_grants_to_database_role` and `FUTURE_PLACEHOLDER_PATTERN`.
- The `SnowflakeConnector` readers, `get_current_user`, `get_current_role`, and the state held by `EntityGenerator`, `SnowflakeSpecLoader` and `SnowflakeGrantsGenerator` use `ObjectName` and `NamePattern` values instead of strings. An `ObjectName` equals only the string of its rendering, so `ObjectName("RAW.PUBLIC") == "raw.public"` is true and `== "RAW.PUBLIC"` is false, and it has no string methods such as `.split()`. Formatting a `NamePattern` with an f-string raises `TypeError`.
- `show_grants_to_role`, `show_future_grants` and `show_roles_granted_to_user` return sets where they returned lists. The other readers and `SnowflakeSpecLoader.grants_to_role` still use lists.
- `show_grants_to_role`, `show_future_grants` and `show_grants_to_role_with_grant_option` skip grants on kinds tundra doesn't manage, such as functions, procedures and stages, so `SnowflakeRoleGrantChecker` no longer finds them. `show_grants_to_role` also leaves out account grants, which 1.0.6 returned under `account` (read them with `show_account_grants_to_role`), and files external volume grants under `external volume` whether Snowflake reports `EXTERNAL_VOLUME` or `EXTERNAL VOLUME`.

### Fixed

- `tundra --version` prints the installed version. Every 1.x release printed 0.15.6.
- On Python 3.12 and later, tundra no longer prints `SyntaxWarning: invalid escape sequence '\s'` when it runs without cached bytecode, for example on the first run after a `uv` install.
- The release workflow uploads the version it just tagged. Before, each run uploaded the new code under the previous version number, so each `tundra-cli` version on PyPI holds the code of the next git tag: 1.0.5 holds 1.0.6, and 0.15.24 holds 1.0.0. PyPI has no 1.0.6, and its files can't be replaced, so install a 1.x release from its git tag, for example `pip install "git+https://github.com/henryupton/tundra@1.0.6"`.
