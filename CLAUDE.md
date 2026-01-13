# Claude Instructions

## Project Overview
<!-- What this project does and its purpose -->

## Development Commands
All project commands are defined in `.claude/commands.env`

## Git Workflow
- Run tests before every commit/push
- Only push if all tests pass
- Use descriptive commit messages

## Implementation Rules
**No creative liberties** - Only implement what is explicitly discussed and decided
- Never invent UI elements, charts, or features without user approval
- Never create placeholder/demo content or hardcoded values
- Always ask "what should this show?" before creating any display elements

**Data-driven approach** - All content must come from files
- Create data files first, then build components that read from them
- For mockups: create realistic data files, never hardcode values
- Example: inventory dashboard needs `data/inventory.json`, not `const items = [...]`

**Decision-first workflow**
1. Discuss what to build (purpose, data, layout)
2. Create data structure/files
3. Build components that consume the data
4. Document everything as you go at each step

## Documentation Format
**Use loglog format** for all documentation and notes
- Create documentation as `*.log` files using loglog syntax
- Convert to markdown when needed: `loglog file.log > file.md`
- Follow loglog syntax from https://github.com/k1monfared/loglog
- Installation: `pip install loglog` or `cargo install loglog`

## Code Conventions
<!-- Coding style, naming conventions, frameworks used -->

## Testing Instructions
<!-- How to run tests, test patterns to follow -->

## Architecture Notes
<!-- Key directories, important files, design patterns -->

## Environment Setup
<!-- Dependencies, environment variables, setup steps -->