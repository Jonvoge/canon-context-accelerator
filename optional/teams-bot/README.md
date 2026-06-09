# Canon — Teams Bot (Optional)

This directory contains the optional Microsoft Teams bot integration, parked here because it is
**not required** for the core Canon workflow (git repo + scan + MCP server).

## What's here

| File/folder | Description |
|---|---|
| `bot/` | Bot Framework application (aiohttp + BotBuilder) |
| `teams-app/` | Teams app manifest and assets |
| `teams-app.zip` | Packaged Teams app for sideloading |
| `Dockerfile.bot` | Container image for the bot service |
| `deploy.ps1` | Azure Container Apps deployment script |

## When to revive this

Only install this when a client explicitly requires in-Teams interaction:
- Proactive digest notifications pushed into a Teams channel
- Conversational ad-hoc queries via a Teams bot
- Teams interview handler for new domain definitions

## What it depends on

Beyond the core repo secrets, the Teams bot requires:
- Azure subscription with Container Apps environment
- Azure Container Registry
- Teams app registration (Azure AD app with `TeamsAppInstallation.ReadWriteForUser` permissions)
- `MICROSOFT_APP_ID` and `MICROSOFT_APP_PASSWORD` (Bot Framework credentials)
- `CANON_GITHUB_TOKEN` (PAT for opening issues/PRs from the bot)

Approximately 11 additional environment variables and 3 Azure resource registrations.

## How to deploy

1. Restore these files to the repo root (reverse the move)
2. `cd optional/teams-bot`
3. Review and update `deploy.ps1` with your Azure resource names
4. Run `./deploy.ps1`
5. Sideload `teams-app.zip` into your Teams tenant

## What the bot does (for reference)

The bot handled three workflows in v4:
1. **Digest**: Proactive cards pushed to Teams with scan findings
2. **Ad-hoc update**: In-chat commands to flag issues or trigger re-scans
3. **Interview**: Conversational Q&A to draft definitions for undocumented measures

In v5, these are replaced by:
- Digest → GitHub issue notifications (GitHub emails @mentioned users natively)
- Ad-hoc update → direct PR or issue edits
- Interview → `canon bootstrap` → review draft PR
