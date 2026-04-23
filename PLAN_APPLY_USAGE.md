# Plan and Apply Commands

Tundra now supports Terraform-style workflow with `plan` and `apply` commands for safer permission management.

## Overview

- **`tundra plan`** - Generate an execution plan showing what changes would be made (like `--dry` mode)
- **`tundra apply`** - Apply permissions either from a spec file or from a saved plan

## Commands

### tundra plan

Generate an execution plan and optionally save it to a file.

```bash
# Generate and display a plan
tundra plan roles.yml

# Save plan to a file
tundra plan roles.yml --out plan.json

# Plan for specific roles
tundra plan roles.yml --role analyst --role developer --out plan.json

# Plan for specific users
tundra plan roles.yml --user john.doe@example.com --out plan.json

# Show full diff (including already granted permissions)
tundra plan roles.yml --diff --out plan.json
```

**Options:**
- `--out PATH` - Save plan to JSON file
- `--diff` - Show full diff including already granted permissions
- `--role ROLE` - Generate plan for specific roles (can specify multiple)
- `--user USER` - Generate plan for specific users (can specify multiple)
- `--ignore-memberships` - Don't include role membership grants/revokes
- `--skip-validation` - Skip entity existence validation
- `--ignore-missing-objects` - Ignore grants for non-existent objects

**Output:**
```
Execution plan:

+ [PENDING] GRANT USAGE ON WAREHOUSE analytics TO ROLE analyst;
+ [PENDING] GRANT SELECT ON DATABASE raw TO ROLE analyst;
  [SKIPPED] GRANT ROLE analyst TO ROLE sysadmin;

Plan: 2 to add, 1 already granted

Plan saved to: plan.json
```

### tundra apply

Apply permissions from a spec file or execute a saved plan.

```bash
# Apply directly from spec file (like current 'run' command)
tundra apply roles.yml

# Apply from a saved plan
tundra apply --plan plan.json

# Apply with options
tundra apply roles.yml --role analyst --diff
```

**Options:**
- `--plan PATH` - Execute from a saved plan file
- `--diff` - Show full diff including already granted permissions
- `--role ROLE` - Apply for specific roles (only when using spec file)
- `--user USER` - Apply for specific users (only when using spec file)
- `--ignore-memberships` - Don't handle role membership grants/revokes
- `--skip-validation` - Skip entity existence validation
- `--ignore-missing-objects` - Ignore grants for non-existent objects

**Note:** You must specify either a spec file OR `--plan`, not both.

## Workflow Examples

### Basic Plan and Apply

```bash
# 1. Generate and review plan
tundra plan roles.yml --out plan.json

# 2. Review the plan file if needed
cat plan.json

# 3. Apply the plan
tundra apply --plan plan.json
```

### CI/CD Pipeline

```bash
# In CI: Generate plan
tundra plan roles.yml --out plan.json

# Store plan.json as artifact

# In CD: Apply plan after approval
tundra apply --plan plan.json
```

### Role-specific Changes

```bash
# Plan changes for specific role
tundra plan roles.yml --role analyst --out analyst-plan.json

# Review and apply
tundra apply --plan analyst-plan.json
```

## Plan File Format

Plans are saved in JSON format with the following structure:

```json
{
  "version": "1.0",
  "timestamp": "2026-01-14T21:00:00Z",
  "spec_file": "roles.yml",
  "options": {
    "roles": ["analyst"],
    "users": [],
    "run_list": ["roles"],
    "ignore_memberships": false,
    "skip_validation": false,
    "ignore_missing_objects": false
  },
  "queries": [
    {
      "already_granted": false,
      "sql": "GRANT USAGE ON WAREHOUSE analytics TO ROLE analyst"
    }
  ],
  "summary": {
    "total": 3,
    "new": 2,
    "already_granted": 1
  }
}
```

## Comparison with Existing Commands

| Old Command | New Equivalent |
|-------------|----------------|
| `tundra run roles.yml --dry` | `tundra plan roles.yml` |
| `tundra run roles.yml` | `tundra apply roles.yml` |
| N/A | `tundra plan roles.yml --out plan.json && tundra apply --plan plan.json` |

The `run` command still exists and works as before for backward compatibility.

## Benefits

1. **Separation of Planning and Execution** - Review changes before applying
2. **Audit Trail** - Save plans for compliance and review
3. **Safe CI/CD** - Generate plans in CI, apply in CD after approval
4. **Version Control** - Commit plan files alongside spec files
5. **Reproducibility** - Execute exact same changes captured in plan

## Tips

- Always review the plan output before applying
- Save important plans to version control
- Use `--diff` to see full context including already-granted permissions
- Plans include timestamp and original spec file for audit purposes
- Plans can be inspected with standard JSON tools (`jq`, etc.)
