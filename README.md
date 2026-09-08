# MIA Assistant

MIA is a Windows desktop AI assistant built with Python and PySide6. The current
version combines an OpenRouter chat, web search, Git utilities, code review,
technical-specification drafting, development methodologies, and project rules
in one interface.

## Current state

This first public version preserves the project as it existed before the next
major improvement pass. Local secrets, downloaded models, logs, memory, the
virtual environment, and personal documents are intentionally not published.

## Entry point

```powershell
python mia_dev_agent.py
```

Configuration is read from `.env`. Start by copying `.env.example` to `.env`
and add your own OpenRouter key. Never commit `.env`.

Windows setup and launch helpers are provided in `setup.ps1` and `autorun.bat`.

## GitHub help

A short Russian-language guide for working with this repository is available in
[GITHUB_GUIDE.md](GITHUB_GUIDE.md).
