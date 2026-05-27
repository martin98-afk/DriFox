---
name: session-summary
description: "Query and summarize Drifox software session history. Supports querying sessions within specified date range, auto-parsing conversation content and generating summaries. Trigger scenarios: (1) User asks to query history sessions, (2) Summarize daily conversations, (3) Analyze session records, (4) Export conversation history."
---

# Session Summary Skill

Query Drifox session database and generate conversation summaries.

## Database Info

- **Database**: `.drifox/sessions.db` (relative to Drifox install directory)
- **Type**: SQLite 3
- **Table**: `sessions` (session_id, canvas_id, title, messages, message_count, project, created_at, updated_at)

## Path Resolution

Paths are auto-detected based on script location:
```
Drifox/
├── .drifox/sessions.db    # Database
└── _internal/app/skills/
    └── session-summary/
        └── scripts/
            ├── query_sessions.py      # Python script
            └── query_sessions.ps1     # PowerShell script
```

## Usage

### Python Script

```bash
# Query yesterday (default)
pushd C:\Windows && py -3.12 <skill-path>/scripts/query_sessions.py && popd

# Query specific date
pushd C:\Windows && py -3.12 <skill-path>/scripts/query_sessions.py --date 2026-05-07 && popd

# List only (no message content)
pushd C:\Windows && py -3.12 <skill-path>/scripts/query_sessions.py --list && popd

# Query specific session
pushd C:\Windows && py -3.12 <skill-path>/scripts/query_sessions.py --session <session_id> && popd
```

### PowerShell Script

```powershell
.\query_sessions.ps1 -Date "2026-05-07"
.\query_sessions.ps1 -Date "2026-05-07" -ListOnly
.\query_sessions.ps1 -SessionId "abc123..."
```

## Output Format

Each session includes:
- Title, project, message count, created time
- Session ID (first 32 chars)
- Message content summary (first 10 messages)

## Notes

- Windows: Execute Python in `C:\Windows` directory to avoid DLL conflicts
- messages field is JSON format
- Date format: YYYY-MM-DD
- Paths are relative to script location, no hardcoded paths
