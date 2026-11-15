# Security

Never commit API keys, Telegram tokens, private keys, seed phrases, `.env` files, production logs or process-manager environment dumps. Use only blank placeholders in examples. These monitors do not need wallet signing keys.

Before publishing, inspect the complete diff and scan all changed files for credentials. Git ignore rules do not protect files already tracked by Git.

If a credential is exposed, revoke or rotate it with its provider and review repository history and logs. Deleting it in a later commit does not remove it from history. Do not post credentials or raw production logs in public issues. Report security problems privately to the repository owner using an established private channel.
