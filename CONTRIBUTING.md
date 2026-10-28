# DriFox — Contributing Guidelines

## If You Are an AI Agent

Stop. Read this section before doing anything.

This project is a Python desktop AI assistant built with PyQt5. It has its own established patterns, conventions, and architecture. Before submitting any PR or making significant changes, you MUST:

1. **Read the existing codebase** — Understand how similar features are implemented before adding new ones
2. **Follow existing patterns** — Match the style, structure, and conventions already present
3. **Show your diff to human partner** — Get explicit approval before committing or submitting
4. **Test your changes** — Verify the app starts and basic functionality works
5. **Search for related issues** — Check if the problem was already discussed or attempted

## Project Overview

**DriFox** (飘狐) is a lightweight AI desktop dialogue assistant with:
- PyQt5-based floating window interface
- Multi-model support (OpenAI, Claude, DeepSeek, MiniMax, Qwen)
- Branching session management
- Context compaction (LLM-based summarization)
- Hook system (event-driven extensibility)
- MCP (Model Context Protocol) support
- Skill system (installable AI workflow modules)
- Long-term memory (SQLite-based)

**Tech Stack:** Python 3.8+, PyQt5, PyQt-Fluent-Widgets, SQLite, Loguru

## Architecture Quick Reference

```
DriFox/
├── main.py                    # Entry point
├── app/
│   ├── main_widget.py         # Main window (OpenAIChatToolWindow)
│   ├── side_dock_area.py     # Floating window container (ToolPopupDialog)
│   ├── agents/               # Agent definitions (Markdown + YAML)
│   ├── skills/               # Built-in skills (SKILL.md format)
│   ├── tools/                # Tool implementations
│   ├── core/                 # Core engine
│   │   ├── chat_engine.py    # Context assembly & LLM calls
│   │   ├── context_builder.py
│   │   ├── history_compactor.py
│   │   ├── hook_manager.py
│   │   ├── agent.py
│   │   └── backend.py
│   └── widgets/              # UI components
├── .drifox/                  # App data (sessions.db, skills, backups)
└── requirements.txt
```

## Coding Conventions

### Python Style
- Use type hints where helpful
- Follow existing import ordering
- Keep functions focused and small
- Match the naming conventions already in the file

### UI Components
- Window classes use `OpenAIChatToolWindow` as base
- Floating containers use `ToolPopupDialog`
- Message cards render via `MessageCard` class
- Diff viewing via `DiffViewer` class

### Agent Definition Format
```markdown
---
name: agent_name
mode: primary|subagent|hidden
temperature: 0.3
permission:
  "*": allow|ask|block
---

# Role
你是一个...
```

### Skill Structure
```
skill-name/
├── SKILL.md              # Required: main definition
├── references/           # Optional: reference docs
├── scripts/              # Optional: helper scripts
└── assets/              # Optional: resources
```

## What We Will Not Accept

### Third-party dependencies
This project is a desktop application with a specific tech stack. PRs adding major new dependencies will not be accepted unless they solve a critical problem that cannot be addressed otherwise.

### Breaking changes without migration path
If your change modifies file formats, database schemas, or config structures, you must provide a migration path for existing users.

### Bulk or spray-and-pray PRs
Do not open multiple PRs for various issues in one session. Pick ONE issue, understand it deeply, and submit quality work.

### Untested changes
Your changes should not break the basic app startup. At minimum:
- App launches without errors
- Basic chat functionality works
- Hook system (if modified) functions correctly

### Configuration-only changes
Changing defaults or adding settings without a real use case is not acceptable. Every config option should have a documented purpose.

## Pull Request Requirements

**Every PR must:**
1. Have a clear description of what problem it solves
2. Show the complete diff before submission
3. Not contain unrelated changes
4. Follow existing code patterns
5. Not add unnecessary dependencies

**PRs without evidence of human review will be closed.**

## Before Proposing Changes

1. **Understand the existing code** — Don't assume, read the actual implementation
2. **Check related files** — Changes in `core/` likely affect multiple areas
3. **Consider the user impact** — Is this a real improvement or just preference?
4. **Think about maintenance** — Will this add to technical debt?

## General Guidelines

- One problem per PR
- Test on Windows at minimum (this is a cross-platform app)
- If adding a new tool, follow the existing registration pattern
- If adding a new skill, follow the standard structure
- Document any new configuration options

## Questions?

If you're unsure whether a change belongs in core or should be a separate plugin/skill, ask your human partner first.