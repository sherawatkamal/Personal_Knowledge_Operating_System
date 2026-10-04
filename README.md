# pkos: personal knowledge operating system

Self-hosted, cited answers over your own email, calendar and meeting notes. Phase one is under construction; see `PLAN.md`.

## Privacy model

This runs on your machine. The database lives in a Docker volume on your disk and has no published port. Nothing leaves the machine except model calls you explicitly configure, and a fully local mode through Ollama is part of phase one.

### Encryption at rest

**Phase one relies on your operating system's full-disk encryption (FileVault on macOS). Application-level encryption is deferred to phase two.**

This is a decision, not an omission. Here's why:
- **Postgres has no built-in transparent encryption.** Column-level encryption would cover exactly the text that full-text and vector search must read. Search would either stop working, or the plaintext would leak back out through the indexes.
- **The workable option is an encrypted volume.** On Docker Desktop for Mac that's an encrypted disk image holding the database files: an OS-level setup step rather than application code.
- **FileVault already covers the main threat.** It gives full-disk encryption with a key you hold, which protects against a stolen disk.

The `./pkos` wrapper checks FileVault on every run and warns if it's off. The `data/` directory (OAuth tokens, eval results, extraction dumps) is plaintext on disk under the same protection.

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
