# pkos: personal knowledge operating system

Self-hosted, cited answers over your own email, calendar and meeting notes. Phase one is under construction; see `PLAN.md`.

## Privacy model

This runs on your machine. The database lives in a Docker volume on your disk and has no published port. Nothing leaves the machine except model calls you explicitly configure, and a fully local mode through Ollama is part of phase one.

### Encryption at rest

**Phase one relies on your operating system's full-disk encryption (FileVault on macOS). Application-level encryption is deferred to phase two.**

This is a decision, not an omission:
- **FileVault covers the stolen-disk threat.** That is the threat the privacy model names, and full-disk encryption with a key you hold addresses it.
- **An encrypted volume adds nothing beyond FileVault on a single-user machine.** It would protect the same data against the same threat, with the same person holding the key.
- **Application-level encryption would break search.** Full-text and vector search need to read the text they index, so encrypting it inside the application would break both.

So encryption at rest is phase two.

The `./pkos` wrapper checks FileVault on `./pkos health` and on its first run, and warns if it's off. The `data/` directory (OAuth tokens, eval results, extraction dumps) is plaintext on disk under the same protection.

## Install

Requires Docker Desktop.

```sh
cp .env.example .env    # optional: every setting has a working default
docker compose up       # builds, starts Postgres, runs migrations, prints a health check
./pkos health
./pkos test
```

## Results

No eval results yet. The baseline is recorded here in step 4.
