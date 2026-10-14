# Tundra

A fork of [Permifrost](https://gitlab.com/gitlab-data/permifrost) for managing Snowflake permissions from a
declarative YAML spec. It's faster on big accounts and understands the object types Snowflake has shipped since
Permifrost stopped keeping up.

Anything not covered here (spec structure, `read`/`write` semantics, wildcards, ownership, `--diff`, `--role`,
`--user`, `!include`, `require-owner`) works exactly as it does upstream. See the
[Permifrost README](https://gitlab.com/gitlab-data/permifrost/-/blob/master/README.md).

```bash
pip install tundra-cli
# or the latest main
pip install git+https://github.com/henryupton/tundra.git

tundra run spec.yml --dry
tundra spec-test spec.yml
```

## Performance

Permifrost runs every Snowflake round trip serially, and on a large account most of them are wasted.

- **Parallel fetch.** Role grants, user grants and table-existence checks are fetched concurrently and merged
  back in a deterministic order, so output is identical run to run. Concurrency is `--max-workers` (or
  `PERMISSION_BOT_MAX_WORKERS`, default 8), and the connection pool is sized to match.
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
  - sysadmin:
      account_privileges: [execute task, execute managed task]
  - transformer:
      account_privileges: [apply tag, manage grants]
```

Declaring the key makes the list authoritative, like `warehouses`: undeclared account grants on that role are
revoked, and a role without the key is left alone. Names are case- and whitespace-insensitive. Two kinds of grant
are never revoked: Snowflake's own privileges on its system roles (no grantor in `SHOW GRANTS`), and the
ACCOUNTADMIN-only `create share`, `import share` and `manage share target`, which `SECURITYADMIN` can't touch. The
latter are also rejected at spec load, so grant them outside Tundra.

**Tasks** get their own `privileges.tasks` family beside `tables`, with the same pattern shapes and `ON ALL` plus
`ON FUTURE` grants:

```yaml
roles:
  - orchestrator:
      privileges:
        tasks:
          write: [raw.*.*]
```

| read      | write              |
| --------- | ------------------ |
| `monitor` | `monitor, operate` |

They're kept out of `tables` on purpose, so a `tables` wildcard never hands a reader `MONITOR` on a schema's tasks.
Task grants inside the spec's databases are fully managed. No `create task` schema privilege is derived: ownership
stays with whichever role creates the task, and `EXECUTE TASK` comes from `account_privileges`.

## Database and application roles

A `member_of` entry containing a period is a `DB.ROLE` reference. Tundra checks it against
`SHOW DATABASE ROLES`, falls back to `SHOW APPLICATION ROLES` for an application of the same name, and grants or
revokes accordingly. Membership is fully managed: anything granted but not listed is revoked.

```yaml
roles:
  - monitor:
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
```

- `has_password: true` rewrites `type: service` to `legacy_service`, since Snowflake won't let a `SERVICE` user hold
  a password. `has_password: false` sets `PASSWORD = NULL`. Omit it for `SERVICE` users.
- `parameters` sets user-level session defaults. Allowed keys are `timezone`, `week_start`, `week_of_year_policy`,
  the `*_output_format` family, `timestamp_type_mapping`, `query_tag`, `statement_timeout_in_seconds`,
  `statement_queued_timeout_in_seconds` and `client_session_keep_alive`. Removing one leaves the value in place.
- User identifiers and property values are quoted and uppercased properly.

## Spec directories

`run` and `spec-test` accept a directory. Every `.yml`/`.yaml` under it (recursive, sorted, dotfiles skipped) is a
fragment holding any top-level keys, merged before validation:

```
spec/
  settings.yml
  databases.yml
  roles/custom.yml
  users/service.yml
```

List sections concatenate. An entity defined twice is an error naming both files. Scalar settings must agree.

## Running it

**Objects dropped mid-run.** Grants are planned from a snapshot, so a schema that CI drops and recreates while
Tundra is applying would fail every queued statement. Tundra re-looks up the object instead: gone prints
`[VANISHED]` (not a failure), recreated is retried once. Every `[ERROR]` prints Snowflake's message beneath it.

**Running role.** The current role must be `SECURITYADMIN` or a role that has been granted it (checked with
`IS_ROLE_IN_SESSION`), so a purpose-built role works.

**Workload identity federation.** Short-lived OIDC tokens (GitHub Actions, say) with no stored secret:

```bash
PERMISSION_BOT_AUTHENTICATOR=WORKLOAD_IDENTITY
PERMISSION_BOT_WORKLOAD_IDENTITY_PROVIDER=OIDC
PERMISSION_BOT_TOKEN_COMMAND='python mint_token.py --audience my-account'
```

The command's stdout is the token, and it's rerun for every pooled connection so a long run never reuses an expired
one. It takes precedence over the other auth methods, which are otherwise
[as upstream](https://gitlab.com/gitlab-data/permifrost/-/blob/master/README.md#connection-parameters).

**Warehouses.** `size` is optional.

## Contributing

Issues and PRs welcome on [GitHub](https://github.com/henryupton/tundra/issues). Tests run on every PR. Versions are
bumped by the release workflow on merge to `main`, not by hand.

```bash
PYTHONPATH=src python -m pytest tests -q
```
